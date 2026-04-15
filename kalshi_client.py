"""
kalshi_client.py — Handles all communication with the Kalshi API.

Kalshi uses RSA key-based authentication (API v2).
Docs: https://trading-api.kalshi.com/trade-api/v2/swagger-ui
"""

import base64
import datetime
import json
import time
import logging
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

import config

logger = logging.getLogger(__name__)


class KalshiClient:
    def __init__(self):
        self.base_url = config.KALSHI_BASE_URL
        self.api_key_id = config.KALSHI_API_KEY_ID
        self.session = requests.Session()
        self._load_private_key()

    def _load_private_key(self):
        """Load the RSA private key from the .pem file."""
        try:
            with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
                self.private_key = serialization.load_pem_private_key(
                    f.read(), password=None, backend=default_backend()
                )
        except FileNotFoundError:
            logger.warning(
                f"Private key not found at {config.KALSHI_PRIVATE_KEY_PATH}. "
                "Generate one at kalshi.com → Settings → API."
            )
            self.private_key = None

    def _sign_request(self, method: str, path: str) -> dict:
        """
        Generate the auth headers Kalshi requires for every request.
        They sign: timestamp + method + path
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = timestamp_ms + method.upper() + path

        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None):
        """Make an authenticated GET request."""
        headers = self._sign_request("GET", path)
        url = self.base_url + path
        resp = self.session.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict):
        """Make an authenticated POST request."""
        headers = self._sign_request("POST", path)
        url = self.base_url + path
        resp = self.session.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()

    # ─────────────────────────────────────────
    # MARKET METHODS
    # ─────────────────────────────────────────

    def get_balance(self) -> float:
        """Returns your current available balance in USD."""
        data = self._get("/portfolio/balance")
        # Kalshi returns balance in cents
        return data.get("balance", 0) / 100.0

    def get_all_markets(self, sport_filter: list = None) -> list:
        """
        Fetch all open Kalshi markets, with optional keyword filtering.

        Uses limit=1000 per page (Kalshi max) to minimise API round-trips.
        Client-side filtering by close_time keeps only markets that expire
        within MAX_HOURS_TO_CLOSE, so strategy.py only sees live/near-end games.

        Args:
            sport_filter: Optional list of keywords to filter titles
                          (e.g. ["nfl", "nba"]).  None = all sports.

        Returns a list of market dicts with price and metadata.
        """
        import time as _time

        now = datetime.datetime.utcnow()
        close_window = now + datetime.timedelta(hours=config.MAX_HOURS_TO_CLOSE)
        close_window_str = close_window.strftime("%Y-%m-%dT%H:%M:%SZ")
        now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        markets = []
        cursor = None
        pages = 0
        t0 = _time.time()

        while True:
            params = {"status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor

            data = self._get("/markets", params=params)
            batch = data.get("markets", [])
            pages += 1

            for m in batch:
                close_time = m.get("close_time", "")

                # Client-side close_time filter: only keep near-closing markets
                if close_time and (close_time < now_str or close_time > close_window_str):
                    continue

                if sport_filter:
                    title = (m.get("title", "") + m.get("subtitle", "")).lower()
                    if not any(kw in title for kw in sport_filter):
                        continue

                markets.append(m)

            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        elapsed = _time.time() - t0
        logger.info(
            f"Market fetch: {len(markets)} qualifying markets "
            f"(scanned {pages} pages in {elapsed:.1f}s, "
            f"window=next {config.MAX_HOURS_TO_CLOSE}h)"
        )
        return markets

    def get_tennis_markets(self) -> list:
        """Backwards-compatible alias — fetches only tennis markets."""
        tennis_keywords = ["tennis", "atp", "wta", "wimbledon", "us open",
                           "french open", "australian open", "roland garros"]
        return self.get_all_markets(sport_filter=tennis_keywords)

    def get_market(self, ticker: str) -> dict:
        """Get details for a single market by ticker."""
        return self._get(f"/markets/{ticker}")

    def get_open_positions(self) -> list:
        """Return all currently open positions."""
        data = self._get("/portfolio/positions")
        return data.get("market_positions", [])

    # ─────────────────────────────────────────
    # ORDER METHODS
    # ─────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: str,        # "yes" or "no"
        count: int,       # number of contracts (each contract = $1 max)
        limit_price: int, # price in cents (1–99)
        dry_run: bool = False,
    ) -> dict:
        """
        Place a limit order on Kalshi.

        Args:
            ticker:      Market ticker (e.g., "TENNIS-ATP-DJOK-2024")
            side:        "yes" to buy YES contracts, "no" to buy NO contracts
            count:       Number of contracts
            limit_price: Price in cents (e.g., 65 = $0.65 = 65% implied prob)
            dry_run:     If True, logs the order but doesn't send it
        """
        if dry_run or config.DEMO_MODE:
            logger.info(
                f"[DRY RUN] Would place order: {side.upper()} {count} contracts "
                f"of {ticker} @ {limit_price}¢"
            )
            return {"status": "dry_run", "ticker": ticker}

        body = {
            "ticker": ticker,
            "action": "buy",
            "type": "limit",
            "side": side,
            "count": count,
            "yes_price": limit_price if side == "yes" else 100 - limit_price,
            "no_price": limit_price if side == "no" else 100 - limit_price,
        }

        logger.info(
            f"Placing LIVE order: {side.upper()} {count} contracts "
            f"of {ticker} @ {limit_price}¢  (${count * limit_price / 100:.2f})"
        )
        return self._post("/portfolio/orders", body)
