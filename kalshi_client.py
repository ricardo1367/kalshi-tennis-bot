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
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
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
        Kalshi signs: timestamp_ms + METHOD + FULL_PATH
        where FULL_PATH includes the /trade-api/v2 prefix, e.g.:
          /trade-api/v2/portfolio/balance   (NOT just /portfolio/balance)
        """
        from urllib.parse import urlparse
        base_path = urlparse(self.base_url).path  # e.g. "/trade-api/v2"
        full_path = base_path + path               # e.g. "/trade-api/v2/portfolio/balance"

        timestamp_ms = str(int(time.time() * 1000))
        message = timestamp_ms + method.upper() + full_path

        # Kalshi requires RSA-PSS (not PKCS1v15) with SHA-256 and MGF1
        signature = self.private_key.sign(
            message.encode("utf-8"),
            rsa_padding.PSS(
                mgf=rsa_padding.MGF1(hashes.SHA256()),
                salt_length=rsa_padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None, _retry: int = 3):
        """Make an authenticated GET request with retry on 429."""
        headers = self._sign_request("GET", path)
        url = self.base_url + path
        for attempt in range(_retry):
            resp = self.session.get(url, headers=headers, params=params)
            if resp.status_code == 429:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(f"Rate limited on GET {path} — waiting {wait}s (attempt {attempt+1}/{_retry})")
                time.sleep(wait)
                headers = self._sign_request("GET", path)  # fresh timestamp
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    def _post(self, path: str, body: dict, _retry: int = 3):
        """Make an authenticated POST request with retry on 429."""
        headers = self._sign_request("POST", path)
        url = self.base_url + path
        for attempt in range(_retry):
            resp = self.session.post(url, headers=headers, json=body)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Rate limited on POST {path} — waiting {wait}s (attempt {attempt+1}/{_retry})")
                time.sleep(wait)
                headers = self._sign_request("POST", path)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

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
        Fetch all open Kalshi markets with a tradeable YES price.

        Key insight: Kalshi's close_time does NOT correlate with "game in
        progress" — active live-game markets close days away, while markets
        closing in the next few hours are already settled (price = 0 or 1).
        We therefore skip the time filter entirely and instead rely on the
        price-based endgame detection in strategy.py.

        Performance: limit=1000 per page (Kalshi max) reduces round-trips
        from ~313 pages (limit=200) to ~62 pages.

        Args:
            sport_filter: Optional list of keywords to filter titles
                          (e.g. ["nfl", "nba"]).  None = all sports.

        Returns a list of market dicts with price and metadata.
        """
        import time as _time

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
                # Skip markets with no tradeable price (settled or empty)
                ask_raw = m.get("yes_ask_dollars") or m.get("yes_ask") or 0
                try:
                    ask_f = float(ask_raw)
                except (ValueError, TypeError):
                    ask_f = 0.0
                # Keep only markets with an active YES price (2¢ – 98¢)
                if not (0.02 <= ask_f <= 0.98):
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
            f"Market fetch: {len(markets)} tradeable markets "
            f"(scanned {pages} pages in {elapsed:.1f}s)"
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
