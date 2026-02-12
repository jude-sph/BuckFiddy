import logging
import time
from datetime import datetime, timezone

import anthropic

from buckfiddy.agent.prompts import build_system_prompt
from buckfiddy.agent.tools import TOOLS, WEB_SEARCH_TOOL, ToolDispatcher
from buckfiddy.config import Settings
from buckfiddy.markets.scanner import MarketScanner
from buckfiddy.risk.stoploss import check_stop_losses
from buckfiddy.state.store import StateStore
from buckfiddy.trading.base import TradingBackend

logger = logging.getLogger(__name__)


# Cost per million tokens by model (input, output)
MODEL_COSTS = {
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
WEB_SEARCH_COST_PER = 0.01  # $10 per 1000 searches


class CycleUsage:
    """Accumulates API usage stats for a single cycle."""

    def __init__(self):
        self.api_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_tokens = 0
        self.cache_read_tokens = 0
        self.web_searches = 0

    def record(self, response):
        self.api_calls += 1
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_creation_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    def record_web_search(self):
        self.web_searches += 1

    def calculate_cost(self, model: str) -> float:
        input_rate, output_rate = MODEL_COSTS.get(model, (3.0, 15.0))
        input_cost = (self.input_tokens / 1_000_000) * input_rate
        output_cost = (self.output_tokens / 1_000_000) * output_rate
        cache_write_cost = (self.cache_creation_tokens / 1_000_000) * (input_rate * 1.25)
        cache_read_cost = (self.cache_read_tokens / 1_000_000) * (input_rate * 0.1)
        search_cost = self.web_searches * WEB_SEARCH_COST_PER
        return input_cost + output_cost + cache_write_cost + cache_read_cost + search_cost


class AgentLoop:
    def __init__(
        self,
        backend: TradingBackend,
        scanner: MarketScanner,
        settings: Settings,
    ):
        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.backend = backend
        self.scanner = scanner
        self.settings = settings
        self.dispatcher = ToolDispatcher(backend, scanner, settings)
        self.system_prompt = build_system_prompt(settings)
        self.store = StateStore(settings.DB_PATH)
        self.cycle_count = 0

    def run(self):
        """Main loop — runs indefinitely."""
        logger.info("BuckFiddy agent loop starting")
        while True:
            try:
                self._run_cycle()
            except KeyboardInterrupt:
                logger.info("Shutting down gracefully")
                break
            except Exception as e:
                logger.error(f"Cycle {self.cycle_count} failed: {e}", exc_info=True)

            logger.info(
                f"Sleeping {self.settings.SCAN_INTERVAL_SECONDS}s until next cycle..."
            )
            try:
                time.sleep(self.settings.SCAN_INTERVAL_SECONDS)
            except KeyboardInterrupt:
                logger.info("Shutting down gracefully")
                break

    def run_single_cycle(self):
        """Run exactly one cycle — useful for testing."""
        self._run_cycle()

    def _run_cycle(self):
        self.cycle_count += 1
        logger.info(f"=== Cycle {self.cycle_count} starting ===")

        # Phase 1: Hard stop loss check (outside Claude's control)
        stop_loss_results = check_stop_losses(
            self.backend, self.settings.STOP_LOSS_PCT
        )
        for result in stop_loss_results:
            logger.warning(f"STOP LOSS: {result.message}")

        # Phase 2: Check and fill any open limit orders (mock backend only)
        if hasattr(self.backend, "check_open_orders"):
            self.backend.check_open_orders()

        # Phase 3: Run Claude's agent turn
        user_message = self._build_cycle_message()
        messages = [{"role": "user", "content": user_message}]
        usage = CycleUsage()

        summary = self._run_agent_turn(messages, usage)

        # Phase 4: Log cycle results and API costs
        self._log_cycle(summary, len(stop_loss_results), usage)

    def _run_agent_turn(self, messages: list, usage: CycleUsage) -> str:
        """Run Claude with tool use until it stops calling tools."""
        all_tools = TOOLS + [WEB_SEARCH_TOOL]
        summary = ""
        max_turns = 30  # Safety limit

        for turn in range(max_turns):
            logger.debug(f"Agent turn {turn + 1}")

            try:
                response = self.client.messages.create(
                    model=self.settings.CLAUDE_MODEL,
                    max_tokens=self.settings.CLAUDE_MAX_TOKENS,
                    system=self.system_prompt,
                    tools=all_tools,
                    messages=messages,
                )
            except anthropic.APIError as e:
                logger.error(f"Claude API error: {e}")
                break

            # Track API usage
            usage.record(response)

            # Append assistant response to messages
            messages.append({"role": "assistant", "content": response.content})

            # Extract any text content for logging, count web searches
            for block in response.content:
                if hasattr(block, "text"):
                    summary += block.text + "\n"
                    logger.info(f"Claude: {block.text[:200]}")
                # Web search results appear as server tool use blocks
                if getattr(block, "type", None) == "server_tool_use" and getattr(block, "name", None) == "web_search":
                    usage.record_web_search()

            if response.stop_reason == "end_turn":
                logger.debug("Claude ended turn")
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info(
                            f"Tool call: {block.name}({_truncate(str(block.input), 100)})"
                        )
                        result = self.dispatcher.dispatch(block.name, block.input)
                        logger.info(f"Tool result: {_truncate(result, 200)}")
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            }
                        )
                messages.append({"role": "user", "content": tool_results})
            else:
                # Other stop reasons (max_tokens, etc.)
                logger.warning(f"Unexpected stop reason: {response.stop_reason}")
                break

        return summary

    def _build_cycle_message(self) -> str:
        parts = [
            "Begin your trading cycle. First, check your wallet state to understand "
            "your current financial position."
        ]

        # Review positions every other cycle
        if self.cycle_count % 2 == 0:
            parts.append(
                "\nAfter checking your wallet, review each of your open positions. "
                "For each position, use review_position to get market details, then "
                "research the topic with web_search, then submit a fresh probability "
                "estimate. If your new estimate suggests the position no longer has "
                "edge, close it."
            )

        parts.append(
            "\nThen scan for new market opportunities. For each interesting market, "
            "use web_search to research the topic, then submit your probability "
            "estimate. If there is a tradeable edge, decide on an appropriate position "
            "size (considering your balance and existing positions) and place a trade."
        )

        parts.append(
            "\nEnd with a brief summary of actions taken and your overall assessment."
        )

        return "\n".join(parts)

    def _log_cycle(self, summary: str, stop_losses: int, usage: CycleUsage):
        try:
            wallet = self.backend.get_wallet_state()
            now = datetime.now(timezone.utc).isoformat()
            cost = usage.calculate_cost(self.settings.CLAUDE_MODEL)

            # Cumulative API cost
            row = self.store.fetchone(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM api_usage"
            )
            cumulative_cost = (row["total"] if row else 0) + cost

            # Log cycle
            self.store.execute(
                "INSERT INTO cycle_log (cycle_number, stop_losses_triggered, "
                "balance_after, equity_after, claude_summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.cycle_count,
                    stop_losses,
                    wallet.balance,
                    wallet.total_equity,
                    summary[:2000] if summary else None,
                    now,
                ),
            )

            # Log API usage
            self.store.execute(
                "INSERT INTO api_usage (cycle_number, api_calls, input_tokens, "
                "output_tokens, cache_creation_tokens, cache_read_tokens, "
                "web_searches, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.cycle_count,
                    usage.api_calls,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_creation_tokens,
                    usage.cache_read_tokens,
                    usage.web_searches,
                    round(cost, 6),
                    now,
                ),
            )

            # Log equity snapshot
            self.store.execute(
                "INSERT INTO equity_snapshots (cycle_number, balance, "
                "position_value, equity, num_positions, cumulative_api_cost, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.cycle_count,
                    wallet.balance,
                    wallet.total_position_value,
                    wallet.total_equity,
                    len(wallet.positions),
                    round(cumulative_cost, 6),
                    now,
                ),
            )

            self.store.commit()
            logger.info(
                f"Cycle {self.cycle_count} complete — "
                f"Balance: ${wallet.balance:.2f}, "
                f"Equity: ${wallet.total_equity:.2f}, "
                f"Positions: {len(wallet.positions)} | "
                f"API: {usage.input_tokens}in/{usage.output_tokens}out "
                f"${cost:.4f} (total: ${cumulative_cost:.4f})"
            )
        except Exception as e:
            logger.error(f"Failed to log cycle: {e}")


def _truncate(s: str, max_len: int) -> str:
    return s[:max_len] + "..." if len(s) > max_len else s
