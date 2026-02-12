import json
import logging
from datetime import datetime, timezone

import requests

from buckfiddy.config import Settings
from buckfiddy.trading.models import MarketInfo

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


class MarketScanner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def scan(self, max_results: int | None = None) -> list[MarketInfo]:
        """Fetch active, liquid, binary markets from Polymarket."""
        limit = max_results or self.settings.MAX_MARKETS_PER_SCAN
        markets: list[MarketInfo] = []
        offset = 0
        batch_size = 100

        while len(markets) < limit:
            try:
                resp = requests.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": batch_size,
                        "offset": offset,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch markets at offset {offset}: {e}")
                break

            if not batch:
                break

            for raw in batch:
                info = self._parse_market(raw)
                if info and self._passes_filters(info):
                    markets.append(info)
                    if len(markets) >= limit:
                        break

            offset += batch_size

        logger.info(f"Scanned {offset} markets, {len(markets)} passed filters")
        return markets

    def _passes_filters(self, m: MarketInfo) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        return (
            m.liquidity >= self.settings.MIN_MARKET_LIQUIDITY
            and m.volume >= self.settings.MIN_MARKET_VOLUME
            and len(m.outcomes) == 2  # Binary markets only
            and len(m.clob_token_ids) == 2
            and m.end_date > now  # Not expired
        )

    def _parse_market(self, raw: dict) -> MarketInfo | None:
        try:
            # Handle outcomes — can be JSON string or list
            outcomes = raw.get("outcomes", [])
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = [o.strip() for o in outcomes.split(",")]

            # Handle CLOB token IDs — can be JSON string or list
            token_ids = raw.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except json.JSONDecodeError:
                    token_ids = [t.strip() for t in token_ids.split(",")]

            return MarketInfo(
                market_id=raw["conditionId"],
                question=raw["question"],
                description=raw.get("description", ""),
                outcomes=outcomes,
                clob_token_ids=token_ids,
                end_date=raw.get("endDate", ""),
                volume=float(raw.get("volumeNum", 0) or 0),
                liquidity=float(raw.get("liquidityNum", 0) or 0),
                slug=raw.get("slug", ""),
                tags=[],
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"Failed to parse market: {e}")
            return None
