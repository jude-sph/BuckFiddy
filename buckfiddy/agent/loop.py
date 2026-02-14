import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import anthropic

from buckfiddy.agent.prompts import (
    build_close_review_prompt,
    build_market_selection_prompt,
    build_position_review_prompt,
    build_research_prompt,
)
from buckfiddy.agent.tools import (
    CLOSE_REVIEW_TOOLS,
    POSITION_REVIEW_TOOLS,
    RESEARCH_TRADE_TOOLS,
    WEB_SEARCH_TOOL,
    ToolDispatcher,
)
from buckfiddy.config import Settings
from buckfiddy.markets.scanner import MarketScanner
from buckfiddy.risk.stoploss import check_risk_guards
from buckfiddy.state.store import StateStore
from buckfiddy.trading.base import TradingBackend

logger = logging.getLogger(__name__)


# Cost per million tokens by model (input, output)
MODEL_COSTS = {
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
WEB_SEARCH_COST_PER = 0.01  # $10 per 1000 searches


class CycleUsage:
    """Accumulates API usage stats for a single cycle across multiple models."""

    def __init__(self):
        self.api_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_tokens = 0
        self.cache_read_tokens = 0
        self.web_searches = 0
        self.total_cost = 0.0

    def record(self, response, model: str):
        self.api_calls += 1
        usage = response.usage
        in_tok = usage.input_tokens
        out_tok = usage.output_tokens
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.cache_creation_tokens += cache_create
        self.cache_read_tokens += cache_read

        # Calculate incremental cost for this API call
        input_rate, output_rate = MODEL_COSTS.get(model, (3.0, 15.0))
        self.total_cost += (in_tok / 1_000_000) * input_rate
        self.total_cost += (out_tok / 1_000_000) * output_rate
        self.total_cost += (cache_create / 1_000_000) * (input_rate * 1.25)
        self.total_cost += (cache_read / 1_000_000) * (input_rate * 0.1)

    def record_web_search(self):
        self.web_searches += 1
        self.total_cost += WEB_SEARCH_COST_PER


class AgentLoop:
    def __init__(
        self,
        backend: TradingBackend,
        scanner: MarketScanner,
        settings: Settings,
    ):
        client_kwargs = {"api_key": settings.ANTHROPIC_API_KEY}
        if settings.ANTHROPIC_BASE_URL:
            client_kwargs["base_url"] = settings.ANTHROPIC_BASE_URL
        self.client = anthropic.Anthropic(**client_kwargs)
        self.backend = backend
        self.scanner = scanner
        self.settings = settings
        self.dispatcher = ToolDispatcher(backend, scanner, settings)
        self.store = StateStore(settings.DB_PATH)
        # Resume cycle numbering from the database
        row = self.store.fetchone(
            "SELECT COALESCE(MAX(cycle_number), 0) AS max_cycle FROM cycle_log"
        )
        self.cycle_count = row["max_cycle"] if row else 0
        self._stop_event = threading.Event()
        self.running = False
        self.current_cycle_type = ""   # "research" | "check" | ""
        self.current_phase = ""        # e.g. "Position Review", "Market Selection", "Research"
        self._next_check_at: float = 0  # monotonic timestamp
        self._next_research_at: float = 0  # monotonic timestamp
        self._recently_researched: dict[str, float] = {}  # market_id → monotonic timestamp

    @property
    def next_check_secs(self) -> int:
        """Seconds until next check cycle, or 0 if not running."""
        if not self.running:
            return 0
        return max(0, int(self._next_check_at - time.monotonic()))

    @property
    def next_research_secs(self) -> int:
        """Seconds until next research cycle, or 0 if not running."""
        if not self.running:
            return 0
        return max(0, int(self._next_research_at - time.monotonic()))

    def request_stop(self):
        """Signal the agent loop to stop after the current cycle."""
        self._stop_event.set()

    def run(self):
        """Main loop — runs check and research cycles on independent schedules."""
        logger.info("BuckFiddy agent loop starting")
        self.running = True
        self._stop_event.clear()

        now = time.monotonic()
        self._next_check_at = now  # Run a check immediately on start
        self._next_research_at = now  # Run research immediately on start

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                ran_something = False

                # Check cycle is due
                if now >= self._next_check_at:
                    try:
                        self._run_cycle(force_check=True)
                        ran_something = True
                    except KeyboardInterrupt:
                        logger.info("Shutting down gracefully")
                        break
                    except Exception as e:
                        logger.error(f"Check cycle failed: {e}", exc_info=True)
                    self._next_check_at = time.monotonic() + self.settings.POSITION_CHECK_INTERVAL_SECONDS

                if self._stop_event.is_set():
                    break

                # Research cycle is due
                if now >= self._next_research_at:
                    try:
                        self._run_cycle(force_full=True)
                        ran_something = True
                    except KeyboardInterrupt:
                        logger.info("Shutting down gracefully")
                        break
                    except Exception as e:
                        logger.error(f"Research cycle failed: {e}", exc_info=True)
                    self._next_research_at = time.monotonic() + self.settings.FULL_CYCLE_INTERVAL_SECONDS

                if not ran_something or self._stop_event.is_set():
                    # Sleep until next event
                    self.current_phase = "Sleeping"
                    wake_at = min(self._next_check_at, self._next_research_at)
                    sleep_for = max(0, wake_at - time.monotonic())
                    if sleep_for > 0:
                        logger.info(f"Sleeping {sleep_for:.0f}s until next cycle...")
                    while time.monotonic() < wake_at:
                        if self._stop_event.is_set():
                            break
                        time.sleep(1)
        finally:
            self.running = False
            self.current_cycle_type = ""
            self.current_phase = ""
            logger.info("Agent loop stopped")

    def run_single_cycle(self):
        """Run exactly one research cycle (all phases) — useful for testing."""
        self.running = True
        try:
            self._run_cycle(force_full=True)
        finally:
            self.current_cycle_type = ""
            self.current_phase = ""
            self.running = False

    def run_single_check(self):
        """Run exactly one check cycle (position review only)."""
        self.running = True
        try:
            self._run_cycle(force_full=False, force_check=True)
        finally:
            self.current_cycle_type = ""
            self.current_phase = ""
            self.running = False

    # ── Cycle orchestration ──────────────────────────────────────────

    def _run_cycle(self, force_full: bool = False, force_check: bool = False):
        self.cycle_count += 1
        self.dispatcher.reset_cycle_counters()
        is_full = force_full and not force_check
        self.current_cycle_type = "research" if is_full else "check"
        self.current_phase = "Starting"
        logger.info(f"=== Cycle {self.cycle_count} ({self.current_cycle_type}) starting ===")

        # Hard risk guards (outside Claude's control) — only on check cycles
        guard_results = []
        if not is_full:
            guard_results = check_risk_guards(
                self.backend,
                self.settings.STOP_LOSS_PCT,
                self.settings.TAKE_PROFIT_PCT,
            )
            for gr in guard_results:
                logger.warning(f"{gr.guard_type.upper()}: {gr.message}")

        # Check and fill any open limit orders (mock backend only)
        if hasattr(self.backend, "check_open_orders"):
            self.backend.check_open_orders()

        usage = CycleUsage()
        summary = ""
        try:
            if is_full:
                summary = self._run_research_cycle(usage)
            else:
                summary = self._run_check_cycle(usage)
        finally:
            self._log_cycle(summary, len(guard_results), usage)

        # HARD STOP: if this cycle exceeded the cost cap, shut down the agent
        if usage.total_cost >= self.settings.MAX_CYCLE_COST_USD:
            logger.error(
                f"COST CAP EXCEEDED: cycle spent ${usage.total_cost:.4f} "
                f"(cap: ${self.settings.MAX_CYCLE_COST_USD:.2f}). "
                f"Stopping agent to prevent further spending."
            )
            self.request_stop()

    def _run_research_cycle(self, usage: CycleUsage) -> str:
        summary_parts = []

        # Phase 1: Market scan + selection (Haiku, fresh conversation)
        self.current_phase = "Market Selection"
        logger.info("Scanning and selecting markets")
        wallet = self.backend.get_wallet_state()
        markets = self.scanner.scan(
            max_results=self.settings.MAX_MARKETS_PER_SCAN
        )
        if not markets:
            summary_parts.append("[Market Scan] No markets found.")
            return "\n\n".join(summary_parts)

        # Register token metadata for all scanned markets
        for m in markets:
            for i, token_id in enumerate(m.clob_token_ids):
                outcome = m.outcomes[i] if i < len(m.outcomes) else f"Outcome {i}"
                self.backend.register_token_meta(
                    token_id, m.market_id, outcome, m.question
                )
                self.dispatcher._market_end_dates[m.market_id] = m.end_date

        # Filter out markets we already hold or recently researched
        held_market_ids = {p.market_id for p in wallet.positions}
        now = time.monotonic()
        cooldown = self.settings.MARKET_COOLDOWN_SECONDS
        # Purge expired cooldowns
        self._recently_researched = {
            mid: ts for mid, ts in self._recently_researched.items()
            if now - ts < cooldown
        }
        cooled_market_ids = set(self._recently_researched.keys())

        excluded = held_market_ids | cooled_market_ids
        eligible_markets = [m for m in markets if m.market_id not in excluded]

        if excluded:
            logger.info(
                f"Filtered {len(markets) - len(eligible_markets)} markets "
                f"({len(held_market_ids)} held, "
                f"{len(cooled_market_ids & {m.market_id for m in markets})} on cooldown)"
            )

        if not eligible_markets:
            summary_parts.append(
                "[Market Scan] All scanned markets are held or on cooldown."
            )
            return "\n\n".join(summary_parts)

        selected = self._phase_market_selection(eligible_markets, wallet, usage)
        summary_parts.append(
            f"[Market Selection] Selected {len(selected)} market(s) to research."
        )

        # Phase 2: Research + trade per market (Sonnet/Opus, fresh conversation each)
        max_research = self.settings.MAX_NEW_ESTIMATES_PER_CYCLE
        for i, market_info in enumerate(selected[:max_research]):
            # Cost cap check between phases
            if usage.total_cost >= self.settings.MAX_CYCLE_COST_USD:
                logger.warning(
                    f"Cost cap reached (${usage.total_cost:.4f}), "
                    f"skipping remaining research phases"
                )
                break
            self.current_phase = f"Research ({i+1}/{len(selected[:max_research])})"
            logger.info(
                f"Phase 3.{i+1}: Researching '{market_info.get('question', '?')[:60]}'"
            )
            phase_summary = self._phase_research_trade(market_info, wallet, usage)
            summary_parts.append(f"[Research] {phase_summary}")

            # Record this market as recently researched
            mid = market_info.get("market_id")
            if mid:
                self._recently_researched[mid] = time.monotonic()

            # Refresh wallet after each trade
            wallet = self.backend.get_wallet_state()

        return "\n\n".join(summary_parts)

    def _run_check_cycle(self, usage: CycleUsage) -> str:
        self.current_phase = "Position Review"
        wallet = self.backend.get_wallet_state()
        if not wallet.positions:
            return "Check cycle — no open positions to review."
        logger.info(
            f"Check cycle: reviewing {len(wallet.positions)} positions"
        )
        summary = self._phase_position_review(wallet, usage)

        # Escalation: if Haiku flagged any positions for closing, Opus reviews
        pending = self.dispatcher._pending_close_reviews
        if pending:
            logger.info(
                f"Escalating {len(pending)} position(s) to senior model for close review"
            )
            for i, review in enumerate(pending):
                # Cost cap check between close reviews
                if usage.total_cost >= self.settings.MAX_CYCLE_COST_USD:
                    logger.warning(
                        f"Cost cap reached (${usage.total_cost:.4f}), "
                        f"skipping remaining close reviews"
                    )
                    break
                self.current_phase = f"Close Review ({i+1}/{len(pending)})"
                review_summary = self._phase_close_review(review, usage)
                summary += f"\n\n[Close Review] {review_summary}"

        return summary

    # ── Phase implementations ────────────────────────────────────────

    def _lookup_original_reasoning(self, token_id: str) -> dict | None:
        """Find the original research estimate that led to a position."""
        row = self.store.fetchone(
            "SELECT e.claude_estimate, e.market_midpoint, e.edge, e.reasoning, e.id "
            "FROM trades t JOIN estimates e ON t.estimate_id = e.id "
            "WHERE t.token_id = ? AND t.side = 'BUY' "
            "ORDER BY t.executed_at DESC LIMIT 1",
            (token_id,),
        )
        if row:
            return {
                "claude_estimate": row["claude_estimate"],
                "market_midpoint": row["market_midpoint"],
                "edge": row["edge"],
                "reasoning": row["reasoning"],
                "estimate_id": row["id"],
            }
        return None

    def _phase_position_review(self, wallet, usage: CycleUsage) -> str:
        """Review all open positions in a single fresh micro-conversation (Haiku).

        Haiku evaluates whether the original thesis still holds, rather than
        re-estimating probabilities from scratch.
        """
        model = self.settings.CLAUDE_MODEL_FAST
        system = build_position_review_prompt(self.settings)

        # Build context for each position: original reasoning + current price
        lines = [
            f"Your current balance: ${wallet.balance:.2f}",
            f"You have {len(wallet.positions)} open position(s) to review.\n",
        ]
        for i, pos in enumerate(wallet.positions, 1):
            end_date = self.dispatcher._market_end_dates.get(
                pos.market_id, "unknown"
            )

            # Look up the original research reasoning
            original = self._lookup_original_reasoning(pos.token_id)

            # Get current market price
            try:
                current_price = self.backend.get_market_price(pos.token_id)
                current_mid = current_price.midpoint
            except Exception:
                current_mid = None

            # Compute remaining edge vs original estimate
            remaining_edge = None
            if original and current_mid is not None:
                remaining_edge = original["claude_estimate"] - current_mid

            # Store context for the flag handler and close review
            self.dispatcher._position_context[pos.position_id] = {
                "market_id": pos.market_id,
                "token_id": pos.token_id,
                "outcome": pos.outcome,
                "market_question": pos.market_question,
                "shares": pos.size,
                "entry_price": pos.avg_entry_price,
                "current_price": current_mid,
                "end_date": end_date,
                "original_estimate": original["claude_estimate"] if original else None,
                "original_reasoning": original["reasoning"] if original else None,
                "original_edge": original["edge"] if original else None,
                "estimate_id": original["estimate_id"] if original else None,
                "remaining_edge": remaining_edge,
            }

            # Build the position summary for Haiku
            pos_lines = [
                f"Position {i}:",
                f"  - Position ID: {pos.position_id}",
                f"  - Market: \"{pos.market_question}\"",
                f"  - Your bet: {pos.outcome}",
                f"  - Shares: {pos.size:.1f}",
                f"  - Entry price: ${pos.avg_entry_price:.3f}",
                f"  - Market end date: {end_date}",
            ]
            if current_mid is not None:
                pos_lines.append(f"  - Current market price: ${current_mid:.3f}")
            if original:
                pos_lines.append(
                    f"  - Original estimate: {original['claude_estimate']:.1%} "
                    f"(original edge: {original['edge']:+.1%})"
                )
                if remaining_edge is not None:
                    pos_lines.append(f"  - Remaining edge: {remaining_edge:+.1%}")
                pos_lines.append(
                    f"  - Original reasoning: {original['reasoning']}"
                )
            else:
                pos_lines.append("  - Original reasoning: (not available)")
            lines.append("\n".join(pos_lines) + "\n")

        lines.append(
            "Review each position above. If everything looks fine, say HOLD for "
            "each and explain briefly why. If you have a genuine concern about any "
            "position, call `flag_position_for_review` with the position_id and "
            "your specific concern."
        )

        messages = [{"role": "user", "content": "\n".join(lines)}]
        return self._run_conversation(
            model=model,
            system_prompt=system,
            tools=POSITION_REVIEW_TOOLS,
            messages=messages,
            usage=usage,
            max_turns=8,
            phase_label="position_review",
        )

    def _phase_close_review(self, review: dict, usage: CycleUsage) -> str:
        """Opus reviews a position flagged for closure by Haiku."""
        model = self.settings.CLAUDE_MODEL_RESEARCH
        system = build_close_review_prompt(self.settings)

        # Build context with original reasoning + reviewer's concern
        sections = [
            "A routine position check flagged this position for potential closure.\n",
            "**Your position:**",
            f"- Position ID: {review['position_id']}",
            f"- Market: \"{review['market_question']}\"",
            f"- Outcome held: {review['outcome']}",
            f"- Shares: {review['shares']:.1f}",
            f"- Entry price: ${review.get('entry_price', 0):.3f}",
            f"- Market end date: {review.get('end_date', 'unknown')}",
        ]

        current_price = review.get("current_price")
        if current_price is not None:
            sections.append(f"- Current market price: ${current_price:.3f}")

        remaining_edge = review.get("remaining_edge")
        if remaining_edge is not None:
            sections.append(f"- Remaining edge: {remaining_edge:+.1%}")

        # Original research reasoning (the key context for this decision)
        original_reasoning = review.get("original_reasoning")
        original_estimate = review.get("original_estimate")
        if original_reasoning:
            sections.append(
                f"\n**Original research reasoning (from when this trade was placed):**\n"
                f"- Original probability estimate: {original_estimate:.1%}\n"
                f"- Research: {original_reasoning}"
            )
        else:
            sections.append(
                "\n**Original research reasoning:** (not available — "
                "this trade may predate reasoning tracking)"
            )

        # Reviewer's concern
        concern = review.get("concern", "No specific concern provided")
        sections.append(
            f"\n**Reviewer's concern:**\n{concern}\n"
        )

        # Decision instructions
        estimate_id = review.get("estimate_id")
        close_args = f"position_id={review['position_id']}"
        if estimate_id:
            close_args += f" and estimate_id={estimate_id}"
        sections.append(
            f"**Your decision:**\n"
            f"Use `web_search` if you need to check for recent developments. "
            f"If you agree the position should be closed, call `close_position` "
            f"with {close_args}. "
            f"If the original thesis still holds, explain why and take no action."
        )

        user_msg = "\n".join(sections)
        messages = [{"role": "user", "content": user_msg}]
        tools = CLOSE_REVIEW_TOOLS + [WEB_SEARCH_TOOL]

        logger.info(
            f"Close review: evaluating '{review['market_question'][:50]}' "
            f"(concern: {concern[:80]})"
        )
        return self._run_conversation(
            model=model,
            system_prompt=system,
            tools=tools,
            messages=messages,
            usage=usage,
            max_turns=5,
            phase_label="close_review",
        )

    def _phase_market_selection(
        self, markets, wallet, usage: CycleUsage
    ) -> list[dict]:
        """Pick 1-2 markets to research using Haiku (no tools, structured output)."""
        model = self.settings.CLAUDE_MODEL_FAST
        system = build_market_selection_prompt(self.settings)

        # Build market list for Claude (pre-filtered: no held or cooldown markets)
        lines = [
            f"Your balance: ${wallet.balance:.2f} | "
            f"Open positions: {len(wallet.positions)}\n",
            "Available markets:\n",
        ]

        for m in markets:
            lines.append(
                f"- Market ID: {m.market_id}\n"
                f"  Question: \"{m.question}\"\n"
                f"  Description: {m.description[:300]}\n"
                f"  Outcomes: {m.outcomes}\n"
                f"  Token IDs: {m.clob_token_ids}\n"
                f"  End date: {m.end_date}\n"
                f"  Volume: ${m.volume:,.0f} | Liquidity: ${m.liquidity:,.0f}\n"
            )

        lines.append(
            "\nPick 1-2 markets where you have the strongest knowledge or "
            "intuition. Skip markets you know nothing about. "
            "Return your selection as a JSON array."
        )

        messages = [{"role": "user", "content": "\n".join(lines)}]
        summary = self._run_conversation(
            model=model,
            system_prompt=system,
            tools=[],
            messages=messages,
            usage=usage,
            max_turns=1,
            phase_label="market_selection",
        )

        # Parse selected markets from Claude's response
        selected = _extract_json(summary)
        if not isinstance(selected, list):
            logger.warning(
                f"Market selection returned non-list: {type(selected)}. "
                f"Raw: {summary[:200]}"
            )
            return []

        # Validate and enrich selected markets
        result = []
        market_lookup = {m.market_id: m for m in markets}
        for item in selected[:2]:
            if not isinstance(item, dict):
                continue
            mid = item.get("market_id", "")
            m = market_lookup.get(mid)
            if m:
                result.append({
                    "market_id": m.market_id,
                    "question": m.question,
                    "description": m.description,  # Full text — contains resolution criteria
                    "outcomes": m.outcomes,
                    "clob_token_ids": m.clob_token_ids,
                    "end_date": m.end_date,
                    "token_id": item.get("token_id", m.clob_token_ids[0]),
                    "outcome": item.get("outcome", m.outcomes[0]),
                    "reason": item.get("reason", ""),
                })
            else:
                logger.warning(f"Selected market_id '{mid}' not found in scan")

        logger.info(
            f"Market selection: {len(result)} market(s) chosen"
            + (f" — {[r['question'][:50] for r in result]}" if result else "")
        )
        return result

    def _phase_research_trade(
        self, market_info: dict, wallet, usage: CycleUsage
    ) -> str:
        """Research a single market and trade if edge found (Sonnet/Opus)."""
        model = self.settings.CLAUDE_MODEL_RESEARCH
        system = build_research_prompt(self.settings)

        user_msg = (
            f"Your balance: ${wallet.balance:.2f}\n"
            f"Open positions: {len(wallet.positions)}\n\n"
            f"Research this market and decide whether to trade:\n\n"
            f"Market ID: {market_info['market_id']}\n"
            f"Question: \"{market_info['question']}\"\n"
            f"Resolution criteria: {market_info['description']}\n"
            f"Outcomes: {market_info['outcomes']}\n"
            f"Token IDs: {market_info['clob_token_ids']}\n"
            f"End date: {market_info['end_date']}\n\n"
            f"Selection reason: {market_info.get('reason', 'N/A')}\n\n"
            f"Steps:\n"
            f"1. Use web_search to research facts about this topic\n"
            f"2. Form your probability estimate\n"
            f"3. Call submit_probability_estimate with market_id="
            f"{market_info['market_id']}, token_id="
            f"{market_info['token_id']}, outcome="
            f"{market_info['outcome']}\n"
            f"4. If tradeable edge exists, place the trade immediately"
        )

        messages = [{"role": "user", "content": user_msg}]
        tools = RESEARCH_TRADE_TOOLS + [WEB_SEARCH_TOOL]
        return self._run_conversation(
            model=model,
            system_prompt=system,
            tools=tools,
            messages=messages,
            usage=usage,
            max_turns=10,
            phase_label="research_trade",
        )

    # ── Generic conversation runner ──────────────────────────────────

    def _run_conversation(
        self,
        model: str,
        system_prompt: str,
        tools: list,
        messages: list,
        usage: CycleUsage,
        max_turns: int = 10,
        phase_label: str = "",
    ) -> str:
        """Run a micro-conversation with Claude. Returns text summary."""
        summary = ""
        model_short = model.split("-")[1] if "-" in model else model

        for turn in range(max_turns):
            # Cost ceiling: abort if this cycle has already exceeded the limit
            if usage.total_cost >= self.settings.MAX_CYCLE_COST_USD:
                logger.warning(
                    f"[{phase_label}] Cost ceiling reached "
                    f"(${usage.total_cost:.4f} >= ${self.settings.MAX_CYCLE_COST_USD:.2f}). "
                    f"Aborting phase."
                )
                break

            logger.debug(f"[{phase_label}] turn {turn + 1}/{max_turns}")

            response = self._api_call_with_retry(
                model, system_prompt, tools, messages
            )
            if response is None:
                break

            usage.record(response, model)
            self._flush_usage(usage)

            messages.append({"role": "assistant", "content": response.content})

            # Extract text and count web searches
            for block in response.content:
                if hasattr(block, "text"):
                    summary += block.text + "\n"
                    logger.info(
                        f"[{phase_label}] {model_short}: "
                        f"{block.text[:200]}"
                    )
                if (
                    getattr(block, "type", None) == "server_tool_use"
                    and getattr(block, "name", None) == "web_search"
                ):
                    usage.record_web_search()

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info(
                            f"[{phase_label}] Tool: "
                            f"{block.name}({_truncate(str(block.input), 100)})"
                        )
                        result = self.dispatcher.dispatch(
                            block.name, block.input
                        )
                        logger.info(
                            f"[{phase_label}] Result: {_truncate(result, 200)}"
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                else:
                    # Only server tools were called — no client results needed
                    break
            else:
                logger.warning(
                    f"[{phase_label}] Unexpected stop reason: "
                    f"{response.stop_reason}"
                )
                break

        return summary

    # ── API call with prompt caching ─────────────────────────────────

    def _api_call_with_retry(
        self,
        model: str,
        system_prompt: str,
        tools: list,
        messages: list,
        max_retries: int = 5,
    ):
        """Make an API call with exponential backoff and prompt caching."""
        # Only use cache_control with the native Anthropic API (not custom proxies)
        use_caching = not self.settings.ANTHROPIC_BASE_URL

        if use_caching:
            system = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system = system_prompt

        # Prepare tools
        call_tools = tools if tools else None
        if call_tools:
            call_tools = list(call_tools)  # Copy to avoid mutation
            if use_caching:
                # Add cache_control to last user-defined tool
                for i in range(len(call_tools) - 1, -1, -1):
                    if "input_schema" in call_tools[i]:
                        call_tools[i] = {
                            **call_tools[i],
                            "cache_control": {"type": "ephemeral"},
                        }
                        break

        for attempt in range(max_retries):
            try:
                kwargs = {
                    "model": model,
                    "max_tokens": self.settings.CLAUDE_MAX_TOKENS,
                    "system": system,
                    "messages": messages,
                }
                if call_tools:
                    kwargs["tools"] = call_tools
                return self.client.messages.create(**kwargs)
            except anthropic.RateLimitError:
                wait = min(2**attempt * 15, 120)
                logger.warning(
                    f"Rate limited (attempt {attempt + 1}/{max_retries}), "
                    f"waiting {wait}s..."
                )
                time.sleep(wait)
            except anthropic.APIError as e:
                logger.error(f"Claude API error: {e}")
                return None
            except Exception as e:
                logger.error(f"Unexpected API error: {type(e).__name__}: {e}")
                return None

        logger.error("Rate limit retries exhausted")
        return None

    # ── Usage tracking and logging ───────────────────────────────────

    def _flush_usage(self, usage: CycleUsage):
        """Write current API usage to DB mid-cycle so the dashboard updates live."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            cost = round(usage.total_cost, 6)

            existing = self.store.fetchone(
                "SELECT id FROM api_usage WHERE cycle_number = ?",
                (self.cycle_count,),
            )
            if existing:
                self.store.execute(
                    "UPDATE api_usage SET api_calls=?, input_tokens=?, "
                    "output_tokens=?, cache_creation_tokens=?, cache_read_tokens=?, "
                    "web_searches=?, cost_usd=?, created_at=? WHERE cycle_number=?",
                    (
                        usage.api_calls,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cache_creation_tokens,
                        usage.cache_read_tokens,
                        usage.web_searches,
                        cost,
                        now,
                        self.cycle_count,
                    ),
                )
            else:
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
                        cost,
                        now,
                    ),
                )
            self.store.commit()
        except Exception as e:
            logger.error(f"Failed to flush usage: {e}")

    def _log_cycle(self, summary: str, stop_losses: int, usage: CycleUsage):
        try:
            now = datetime.now(timezone.utc).isoformat()
            cost = usage.total_cost

            # Deduct API cost from mock balance (real costs reduce real money)
            if cost > 0 and hasattr(self.backend, "_set_balance"):
                current_bal = self.backend._get_balance()
                self.backend._set_balance(round(current_bal - cost, 6))
                logger.info(f"Deducted ${cost:.4f} API cost from balance")

            wallet = self.backend.get_wallet_state()

            # Final flush of API usage
            self._flush_usage(usage)

            # Cumulative API cost
            row = self.store.fetchone(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM api_usage"
            )
            cumulative_cost = row["total"] if row else 0

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


# ── Helpers ──────────────────────────────────────────────────────────


def _truncate(s: str, max_len: int) -> str:
    return s[: max_len] + "..." if len(s) > max_len else s


def _extract_json(text: str) -> list | dict | None:
    """Extract JSON from Claude's response text."""
    # Try to find JSON in code blocks first
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try to parse the whole text as JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Try to find a JSON array or object
    for pattern in [r"\[.*\]", r"\{.*\}"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
    return None
