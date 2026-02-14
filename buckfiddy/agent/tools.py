import json
import logging
from datetime import datetime, timezone

from buckfiddy.config import Settings
from buckfiddy.markets.scanner import MarketScanner
from buckfiddy.state.store import StateStore
from buckfiddy.trading.models import MarketPrice

logger = logging.getLogger(__name__)

# Prediction market domains to block from web search
BLOCKED_DOMAINS = [
    "polymarket.com",
    "metaculus.com",
    "manifold.markets",
    "predictit.org",
    "kalshi.com",
    "electionbettingodds.com",
    "oddschecker.com",
    "smarkets.com",
    "betfair.com",
    "insight-prediction.com",
    "futuur.com",
]


TOOLS = [
    {
        "name": "get_wallet_state",
        "description": (
            "Get your current wallet state: available balance, total position value, "
            "total equity, all open positions (with unrealized P&L), and all open orders. "
            "Call this at the start of each trading cycle to understand your financial state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "scan_markets",
        "description": (
            "Scan Polymarket for active, liquid binary prediction markets. "
            "Returns a list of markets with their questions, descriptions, outcomes, "
            "and end dates. Does NOT include current prices — you must estimate "
            "probabilities yourself before seeing prices."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of markets to return (default 10)",
                },
            },
        },
    },
    {
        "name": "submit_probability_estimate",
        "description": (
            "Submit your estimated probability for a market outcome BEFORE seeing "
            "the actual market price. After you submit, the system will compare "
            "your estimate to the real market price and tell you whether there is "
            "a tradeable edge. You must provide your reasoning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id": {
                    "type": "string",
                    "description": "The market condition ID",
                },
                "token_id": {
                    "type": "string",
                    "description": "The CLOB token ID for the outcome you're estimating",
                },
                "outcome": {
                    "type": "string",
                    "description": "The outcome name (e.g., 'Yes' or 'No')",
                },
                "estimated_probability": {
                    "type": "number",
                    "description": "Your estimated probability (0.0 to 1.0)",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Your reasoning for this probability estimate",
                },
            },
            "required": [
                "market_id",
                "token_id",
                "outcome",
                "estimated_probability",
                "reasoning",
            ],
        },
    },
    {
        "name": "place_limit_order",
        "description": (
            "Place a limit order (GTC - Good Til Cancelled). The order will sit "
            "on the book until filled or cancelled. Use this when you want a "
            "specific entry price."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token_id": {
                    "type": "string",
                    "description": "The CLOB token ID for the outcome",
                },
                "side": {
                    "type": "string",
                    "enum": ["BUY", "SELL"],
                    "description": "BUY to go long, SELL to exit",
                },
                "price": {
                    "type": "number",
                    "description": "Limit price (0.01 to 0.99)",
                },
                "size": {
                    "type": "number",
                    "description": "Number of shares",
                },
                "estimate_id": {
                    "type": "integer",
                    "description": "The estimate_id from submit_probability_estimate that led to this trade",
                },
            },
            "required": ["token_id", "side", "price", "size"],
        },
    },
    {
        "name": "place_market_order",
        "description": (
            "Place a market order (Fill or Kill). Executes immediately at "
            "the current best price. Use this when you want immediate execution. "
            "For BUY orders, amount is in dollars. For SELL orders, amount is in shares."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token_id": {
                    "type": "string",
                    "description": "The CLOB token ID for the outcome",
                },
                "side": {
                    "type": "string",
                    "enum": ["BUY", "SELL"],
                },
                "amount": {
                    "type": "number",
                    "description": "Dollar amount (BUY) or number of shares (SELL)",
                },
                "estimate_id": {
                    "type": "integer",
                    "description": "The estimate_id from submit_probability_estimate that led to this trade",
                },
            },
            "required": ["token_id", "side", "amount"],
        },
    },
    {
        "name": "cancel_order",
        "description": "Cancel a specific open order by its order ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID to cancel",
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "cancel_all_orders",
        "description": "Cancel all your open orders.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "close_position",
        "description": (
            "Close an entire position by selling all shares at market price. "
            "Use this when you want to exit a position entirely."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position_id": {
                    "type": "string",
                    "description": "The position ID to close",
                },
                "estimate_id": {
                    "type": "integer",
                    "description": "The estimate_id from submit_probability_estimate that triggered this close decision",
                },
            },
            "required": ["position_id"],
        },
    },
    {
        "name": "review_position",
        "description": (
            "Get a position's market details for re-evaluation. Returns the market "
            "question and your holding size, but NOT the current market price or "
            "your entry price. You should re-estimate the probability fresh, then "
            "use submit_probability_estimate to see if edge remains."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position_id": {
                    "type": "string",
                    "description": "The position ID to review",
                },
            },
            "required": ["position_id"],
        },
    },
    {
        "name": "flag_position_for_review",
        "description": (
            "Flag a position for senior review if the original thesis may no longer "
            "hold or the edge has clearly disappeared. Only call this if you have a "
            "genuine concern — the default is HOLD."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position_id": {
                    "type": "string",
                    "description": "The position ID to flag",
                },
                "concern": {
                    "type": "string",
                    "description": (
                        "Why this position should be reviewed — what looks wrong "
                        "with the original thesis, or why the edge has disappeared"
                    ),
                },
            },
            "required": ["position_id", "concern"],
        },
    },
]

# Anthropic server-side web search tool
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,
    "blocked_domains": BLOCKED_DOMAINS,
}

# --- Tool subsets per phase ---

# Tool name lookups
_TOOL_BY_NAME = {t["name"]: t for t in TOOLS}

POSITION_REVIEW_TOOLS = [
    _TOOL_BY_NAME["flag_position_for_review"],
]

CLOSE_REVIEW_TOOLS = [
    _TOOL_BY_NAME["close_position"],
]

RESEARCH_TRADE_TOOLS = [
    _TOOL_BY_NAME["submit_probability_estimate"],
    _TOOL_BY_NAME["place_market_order"],
    _TOOL_BY_NAME["place_limit_order"],
]

FULL_CYCLE_TOOLS = TOOLS  # All tools for the monolithic fallback


class ToolDispatcher:
    def __init__(self, backend, scanner: MarketScanner, settings: Settings):
        self.backend = backend
        self.scanner = scanner
        self.settings = settings
        self.store = StateStore(settings.DB_PATH)
        self._market_end_dates: dict[str, str] = {}  # market_id -> end_date
        self._new_estimates_this_cycle: int = 0  # Counter for new market estimates
        self._last_estimate: dict[str, dict] = {}  # market_id -> last estimate info
        self._pending_close_reviews: list[dict] = []  # Positions flagged for Opus review
        self._position_context: dict[str, dict] = {}  # position_id -> context for reviews

    def _find_opposite_token(self, token_id: str, market_id: str) -> tuple[str, str] | None:
        """Find the opposite token for a binary market.
        Returns (opposite_token_id, opposite_outcome) or None."""
        if not hasattr(self.backend, '_token_meta'):
            return None
        for tid, meta in self.backend._token_meta.items():
            if meta.get("market_id") == market_id and tid != token_id:
                return (tid, meta.get("outcome", "unknown"))
        return None

    def reset_cycle_counters(self):
        """Reset per-cycle counters. Call at the start of each cycle."""
        self._new_estimates_this_cycle = 0
        self._pending_close_reviews = []
        self._position_context = {}

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool and return the JSON result string."""
        handlers = {
            "get_wallet_state": self._handle_get_wallet_state,
            "scan_markets": self._handle_scan_markets,
            "submit_probability_estimate": self._handle_submit_estimate,
            "place_limit_order": self._handle_place_limit_order,
            "place_market_order": self._handle_place_market_order,
            "cancel_order": self._handle_cancel_order,
            "cancel_all_orders": self._handle_cancel_all_orders,
            "close_position": self._handle_close_position,
            "review_position": self._handle_review_position,
            "flag_position_for_review": self._handle_flag_position,
        }

        handler = handlers.get(tool_name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        try:
            result = handler(tool_input)
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}", exc_info=True)
            return json.dumps({"error": str(e)})

    def _handle_get_wallet_state(self, _input: dict) -> dict:
        wallet = self.backend.get_wallet_state()
        # Strip price-derived fields to prevent Claude from reverse-engineering
        # current market prices (current_value / size = midpoint)
        data = wallet.model_dump()
        for pos in data.get("positions", []):
            pos.pop("current_value", None)
            pos.pop("unrealized_pnl", None)
            pos.pop("unrealized_pnl_pct", None)
        # Also strip total_position_value (sum of current_values)
        data.pop("total_position_value", None)
        return data

    def _handle_scan_markets(self, input: dict) -> dict:
        max_results = min(
            input.get("max_results", self.settings.MAX_MARKETS_PER_SCAN),
            self.settings.MAX_MARKETS_PER_SCAN,
        )
        markets = self.scanner.scan(max_results=max_results)

        # Register token metadata so the mock backend can track positions
        for m in markets:
            for i, token_id in enumerate(m.clob_token_ids):
                outcome = m.outcomes[i] if i < len(m.outcomes) else f"Outcome {i}"
                self.backend.register_token_meta(
                    token_id, m.market_id, outcome, m.question, slug=m.slug
                )
                # Store end_date in our own metadata cache for position reviews
                self._market_end_dates[m.market_id] = m.end_date

        # Backfill slugs for existing positions/estimates from this scan
        for m in markets:
            if m.slug:
                self.store.execute(
                    "UPDATE positions SET slug = ? WHERE market_id = ? AND (slug IS NULL OR slug = '')",
                    (m.slug, m.market_id),
                )
                self.store.execute(
                    "UPDATE estimates SET slug = ? WHERE market_id = ? AND (slug IS NULL OR slug = '')",
                    (m.slug, m.market_id),
                )
        self.store.commit()

        # Return markets WITHOUT prices
        return {
            "markets": [
                {
                    "market_id": m.market_id,
                    "question": m.question,
                    "description": m.description[:500],  # Truncate long descriptions
                    "outcomes": m.outcomes,
                    "clob_token_ids": m.clob_token_ids,
                    "end_date": m.end_date,
                    "volume": m.volume,
                    "liquidity": m.liquidity,
                }
                for m in markets
            ],
            "count": len(markets),
        }

    def _handle_submit_estimate(self, input: dict) -> dict:
        """The critical tool: compare Claude's blind estimate to real market price."""
        token_id = input["token_id"]
        market_id = input["market_id"]
        outcome = input["outcome"]
        estimate = input["estimated_probability"]
        reasoning = input["reasoning"]

        # Check if this is a new-market estimate (not a position review)
        wallet = self.backend.get_wallet_state()
        is_position_review = any(
            p.token_id == token_id or p.market_id == market_id
            for p in wallet.positions
        )

        if not is_position_review:
            if self._new_estimates_this_cycle >= self.settings.MAX_NEW_ESTIMATES_PER_CYCLE:
                return {
                    "error": (
                        f"Estimate limit reached: you have already submitted "
                        f"{self._new_estimates_this_cycle} new market estimates this cycle "
                        f"(max {self.settings.MAX_NEW_ESTIMATES_PER_CYCLE}). "
                        f"Move on to your summary."
                    )
                }
            self._new_estimates_this_cycle += 1

        # Fetch the real price AFTER Claude has committed its estimate
        real_price: MarketPrice = self.backend.get_market_price(token_id)
        midpoint = real_price.midpoint

        # Get the market question from token metadata
        meta = self.backend._get_token_meta(token_id) if hasattr(self.backend, '_get_token_meta') else {}
        market_question = meta.get("market_question", "")

        # Calculate edge
        edge = estimate - midpoint
        abs_edge = abs(edge)
        tradeable = abs_edge >= self.settings.EDGE_THRESHOLD

        # Log the estimate and get its ID
        now = datetime.now(timezone.utc).isoformat()
        slug = meta.get("slug", "")
        cursor = self.store.execute(
            "INSERT INTO estimates (market_id, token_id, outcome, market_question, slug, "
            "claude_estimate, market_midpoint, edge, reasoning, tradeable, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                market_id,
                token_id,
                outcome,
                market_question,
                slug,
                estimate,
                midpoint,
                edge,
                reasoning,
                1 if tradeable else 0,
                now,
            ),
        )
        estimate_id = cursor.lastrowid
        self.store.commit()

        # Cache estimate for soft validation in order handlers
        self._last_estimate[market_id] = {
            "token_id": token_id,
            "outcome": outcome,
            "estimate": estimate,
            "edge": edge,
        }

        logger.info(
            f"Estimate: {outcome} @ {estimate:.3f} vs market {midpoint:.3f} "
            f"(edge: {edge:+.3f}, tradeable: {tradeable})"
        )

        # Check if Claude already holds a position in this market
        # (wallet already fetched above for estimate counter check)
        balance = wallet.balance
        held_position = next(
            (p for p in wallet.positions if p.token_id == token_id),
            None,
        )
        # Also check if holding the opposite side of this market
        held_opposite = next(
            (p for p in wallet.positions if p.market_id == market_id and p.token_id != token_id),
            None,
        )

        # Calculate suggested position size
        suggested_pct = min(
            0.05 + (abs_edge - self.settings.EDGE_THRESHOLD) * 0.5,
            self.settings.MAX_POSITION_PCT,
        ) if tradeable else 0
        suggested_amount = round(balance * suggested_pct, 2)

        if held_position and tradeable and edge < 0:
            # Claude holds this outcome but now thinks it's OVERVALUED
            # Flag for Opus review instead of closing directly
            end_date = self._market_end_dates.get(market_id, "unknown")
            self._pending_close_reviews.append({
                "position_id": held_position.position_id,
                "market_id": market_id,
                "token_id": token_id,
                "outcome": outcome,
                "market_question": market_question,
                "estimate": estimate,
                "midpoint": midpoint,
                "edge": edge,
                "reasoning": reasoning,
                "estimate_id": estimate_id,
                "shares": held_position.size,
                "end_date": end_date,
            })
            recommendation = (
                f"FLAGGED FOR SENIOR REVIEW — You hold "
                f"{held_position.size:.1f} shares of {outcome} but your new "
                f"estimate ({estimate:.3f}) is BELOW market ({midpoint:.3f}). "
                f"The edge has FLIPPED against you ({edge:+.1%}). "
                f"A senior model will review this position and decide whether "
                f"to close. No action needed from you."
            )
        elif held_position and not tradeable:
            # Edge has narrowed but not flipped — hold for long-horizon bets
            recommendation = (
                f"HOLD — edge has narrowed but position is still directionally "
                f"correct. You hold {held_position.size:.1f} shares of {outcome}. "
                f"Your estimate {estimate:.3f} vs market {midpoint:.3f} "
                f"(edge: {edge:+.1%}, below {self.settings.EDGE_THRESHOLD:.0%} threshold). "
                f"The market is moving towards your estimate, which is good. "
                f"Only close if you believe the fundamental thesis has changed or "
                f"if you want to take profit. No action required."
            )
        elif held_opposite and tradeable and edge > 0:
            # Claude holds the opposite side but now thinks THIS side is undervalued
            recommendation = (
                f"WARNING: You hold the OPPOSITE outcome ({held_opposite.outcome}) "
                f"but now estimate {outcome} at {estimate:.3f} vs market "
                f"{midpoint:.3f}. Consider closing position "
                f"{held_opposite.position_id} if edge has flipped."
            )
        elif tradeable and edge > 0:
            recommendation = (
                f"TRADE OPPORTUNITY — BUY \"{outcome}\" shares. "
                f"Market question: \"{market_question}\"\n"
                f"You estimate {estimate:.3f}, market is at {midpoint:.3f}. "
                f"Edge: +{abs_edge:.1%}. "
                f"Suggested: place_market_order with token_id={token_id}, "
                f"side=BUY, amount=${suggested_amount:.2f} "
                f"({suggested_pct:.0%} of ${balance:.2f} balance), "
                f"estimate_id={estimate_id}.\n"
                f"Verify this matches your research before placing the trade."
            )
        elif tradeable and edge < 0:
            opposite = self._find_opposite_token(token_id, market_id)
            if opposite:
                opp_token_id, opp_outcome = opposite
                recommendation = (
                    f"TRADE OPPORTUNITY — BUY \"{opp_outcome}\" shares. "
                    f"Market question: \"{market_question}\"\n"
                    f"You estimated {outcome} at {estimate:.3f}, but the market "
                    f"prices {outcome} even higher at {midpoint:.3f}. "
                    f"Edge: {abs_edge:.1%} on the opposite side.\n"
                    f"This means BETTING that the answer to "
                    f"\"{market_question}\" is \"{opp_outcome}\".\n"
                    f"Suggested: place_market_order with token_id={opp_token_id}, "
                    f"side=BUY, amount=${suggested_amount:.2f} "
                    f"({suggested_pct:.0%} of ${balance:.2f} balance), "
                    f"estimate_id={estimate_id}.\n"
                    f"*** SANITY CHECK: Your research argued {outcome} is likely "
                    f"({estimate:.0%}). Buying \"{opp_outcome}\" means betting "
                    f"AGAINST that conclusion. Only proceed if you genuinely "
                    f"believe the market is overpricing {outcome}, not just "
                    f"because this system told you to. If this contradicts your "
                    f"research, DO NOT TRADE. ***"
                )
            else:
                recommendation = (
                    f"POSSIBLE EDGE on the opposite side of this market, but "
                    f"the opposite token could not be resolved. "
                    f"Market question: \"{market_question}\". "
                    f"You estimated {outcome} at {estimate:.3f}, market is at "
                    f"{midpoint:.3f} (edge: {abs_edge:.1%}). "
                    f"If you want to trade the opposite, look up the other "
                    f"token_id from scan_markets results. "
                    f"But first: does betting AGAINST {outcome} match your analysis?"
                )
        else:
            recommendation = (
                f"No tradeable edge — your estimate {estimate:.3f} is close to "
                f"market {midpoint:.3f}. Edge: {abs_edge:.1%} "
                f"(below {self.settings.EDGE_THRESHOLD:.0%} threshold). "
                f"Move on to the next market."
            )

        result = {
            "estimate_id": estimate_id,
            "your_estimate": estimate,
            "market_midpoint": midpoint,
            "best_bid": real_price.best_bid,
            "best_ask": real_price.best_ask,
            "edge": round(edge, 4),
            "edge_pct": f"{abs_edge:.1%}",
            "tradeable": tradeable,
            "recommendation": recommendation,
        }
        if held_position:
            result["you_hold"] = {
                "position_id": held_position.position_id,
                "shares": held_position.size,
                "outcome": held_position.outcome,
            }
        return result

    def _handle_place_limit_order(self, input: dict) -> dict:
        result = self.backend.place_limit_order(
            token_id=input["token_id"],
            side=input["side"],
            price=input["price"],
            size=input["size"],
            estimate_id=input.get("estimate_id"),
        )
        return result.model_dump()

    def _handle_place_market_order(self, input: dict) -> dict:
        token_id = input["token_id"]
        result = self.backend.place_market_order(
            token_id=token_id,
            side=input["side"],
            amount=input["amount"],
            estimate_id=input.get("estimate_id"),
        )
        result_dict = result.model_dump()

        # Soft validation: warn if buying a token opposite to a high-confidence estimate
        if input["side"] == "BUY" and result.success:
            meta = self.backend._get_token_meta(token_id) if hasattr(self.backend, '_get_token_meta') else {}
            mid = meta.get("market_id")
            if mid and mid in self._last_estimate:
                last = self._last_estimate[mid]
                if last["token_id"] != token_id and last["estimate"] > 0.5:
                    result_dict["warning"] = (
                        f"NOTE: You bought the opposite of what you estimated. "
                        f"You estimated {last['outcome']} at {last['estimate']:.0%} "
                        f"but bought the other side. Make sure this was intentional."
                    )
        return result_dict

    def _handle_cancel_order(self, input: dict) -> dict:
        result = self.backend.cancel_order(input["order_id"])
        return result.model_dump()

    def _handle_cancel_all_orders(self, _input: dict) -> dict:
        result = self.backend.cancel_all_orders()
        return result.model_dump()

    def _handle_close_position(self, input: dict) -> dict:
        estimate_id = input.get("estimate_id")
        result = self.backend.close_position(input["position_id"])
        if result.success and estimate_id and result.order_id:
            # Link the close trade to the estimate that triggered the decision
            self.store.execute(
                "UPDATE trades SET estimate_id = ? WHERE order_id = ? AND estimate_id IS NULL",
                (estimate_id, result.order_id),
            )
            self.store.commit()
        return result.model_dump()

    def _handle_review_position(self, input: dict) -> dict:
        """Return position details WITHOUT price info for unbiased re-evaluation."""
        wallet = self.backend.get_wallet_state()
        position = next(
            (p for p in wallet.positions if p.position_id == input["position_id"]),
            None,
        )
        if not position:
            return {"error": f"Position {input['position_id']} not found"}

        # Deliberately exclude: avg_entry_price, current_value, unrealized_pnl
        end_date = self._market_end_dates.get(position.market_id, "unknown")
        return {
            "position_id": position.position_id,
            "market_id": position.market_id,
            "token_id": position.token_id,
            "market_question": position.market_question,
            "outcome": position.outcome,
            "shares_held": position.size,
            "market_end_date": end_date,
        }

    def _handle_flag_position(self, input: dict) -> dict:
        """Flag a position for senior close review."""
        position_id = input["position_id"]
        concern = input["concern"]

        ctx = self._position_context.get(position_id, {})
        if not ctx:
            return {"error": f"Position {position_id} not found in review context"}

        self._pending_close_reviews.append({
            "position_id": position_id,
            "concern": concern,
            **ctx,
        })

        logger.info(f"Position {position_id} flagged for close review: {concern[:100]}")
        return {
            "status": "flagged",
            "message": (
                "Position flagged for senior review. A senior model will evaluate "
                "your concern and decide whether to close. No further action needed."
            ),
        }
