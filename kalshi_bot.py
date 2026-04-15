"""
kalshi_bot.py — Main orchestration script with high-frequency polling.

HOW POLLING WORKS:
  - GitHub Actions triggers this script every minute (via cron)
  - The script then runs an internal loop, polling Kalshi every 10 seconds
  - Each 1-minute Actions job = ~5 Kalshi checks (every 10s for 50s)
  - Effective check rate: once every ~10 seconds, 24/7, no server needed

SAFETY RULES (all enforced before any bet):
  ✅ Win probability ≥ 70%
  ✅ Positive edge over Kalshi market price
  ✅ Game is in end-game phase (sport-specific thresholds)
  ✅ Never hold both sides of the same market
  ✅ Never bet the low-probability side
  ✅ Max open positions cap
  ✅ Min/max bet size limits
"""

import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone

import config
from kalshi_client import KalshiClient
from odds_client import OddsClient
from strategy import evaluate_market, BetOpportunity

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# CSV BET LOG
# ─────────────────────────────────────────────
LOG_HEADERS = [
    "timestamp", "ticker", "title", "sport", "side",
    "our_prob", "market_price", "edge",
    "contracts", "bet_dollars", "action", "notes"
]

def log_bet(opp: BetOpportunity, action: str, notes: str = ""):
    file_exists = os.path.isfile(config.LOG_FILE)
    with open(config.LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_HEADERS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "ticker":       opp.ticker,
            "title":        opp.title,
            "sport":        opp.sport_key,
            "side":         opp.side,
            "our_prob":     f"{opp.sharp_prob:.3f}",
            "market_price": f"{opp.kalshi_price:.3f}",
            "edge":         f"{opp.edge:.3f}",
            "contracts":    opp.contracts,
            "bet_dollars":  f"{opp.bet_dollars:.2f}",
            "action":       action,
            "notes":        notes,
        })


# ─────────────────────────────────────────────
# POSITION TRACKER
# Tracks what sides we already hold to prevent both-side bets.
# Built fresh from Kalshi's open positions each poll cycle.
# ─────────────────────────────────────────────

def build_position_map(kalshi: KalshiClient) -> dict[str, str]:
    """
    Returns a dict mapping ticker → side already held ("yes" or "no").
    Used to prevent accidentally holding both sides of the same market.
    """
    position_map = {}
    try:
        positions = kalshi.get_open_positions()
        for pos in positions:
            ticker = pos.get("market_ticker", "")
            yes_count = pos.get("position", 0)  # positive = YES, negative = NO
            if yes_count > 0:
                position_map[ticker] = "yes"
            elif yes_count < 0:
                position_map[ticker] = "no"
    except Exception as e:
        logger.warning(f"Could not fetch open positions: {e}")
    return position_map


# ─────────────────────────────────────────────
# MARKET MATCHING
# ─────────────────────────────────────────────

def extract_team_names(market: dict) -> tuple[str, str] | None:
    """
    Extract two team/player names from a Kalshi market title.
    Handles common formats:
      "Will [Team A] beat [Team B]?"
      "[Team A] vs [Team B] — Winner"
      "[Player A] to win vs [Player B]"
    """
    title = market.get("title", "")
    subtitle = market.get("subtitle", "")
    full = (title + " " + subtitle).lower()

    for sep in [" vs ", " v ", " beat ", " defeat ", " over "]:
        if sep in full:
            parts = full.split(sep, 1)
            for noise in ["will ", "who wins: ", "winner: ", "?", "to win "]:
                parts[0] = parts[0].replace(noise, "")
                parts[1] = parts[1].replace(noise, "")
            a = parts[0].strip().title()
            b = parts[1].strip().split("?")[0].split(" - ")[0].strip().title()
            if a and b:
                return a, b
    return None


def detect_sport_key(market: dict, all_odds: list) -> str:
    """
    Try to identify the sport of a Kalshi market by matching it
    against the sport keys from our odds data. Falls back to "default".
    """
    title = (market.get("title", "") + " " + market.get("subtitle", "")).lower()
    sport_hints = {
        "nfl": "americanfootball_nfl",
        "nba": "basketball_nba",
        "mlb": "baseball_mlb",
        "nhl": "icehockey_nhl",
        "atp": "tennis_atp",
        "wta": "tennis_wta",
        "tennis": "tennis_atp",
        "wimbledon": "tennis_atp_wimbledon",
        "us open": "tennis_atp_us_open",
        "premier league": "soccer_epl",
        "epl": "soccer_epl",
        "mls": "soccer_usa_mls",
        "champions league": "soccer_uefa_champs_league",
        "mma": "mma_mixed_martial_arts",
        "ufc": "mma_mixed_martial_arts",
        "boxing": "boxing_boxing",
        "ncaaf": "americanfootball_ncaaf",
        "ncaab": "basketball_ncaab",
    }
    for hint, key in sport_hints.items():
        if hint in title:
            return key
    return "default"


def match_market_to_pinnacle(market: dict, all_odds: list) -> dict | None:
    """
    Match a Kalshi market to a Pinnacle line.

    CRITICAL FILTER: Only returns a match if Pinnacle's commence_time is in the
    past — meaning the game has actually STARTED. Pre-game markets (even with
    high prices for heavy favorites) are excluded entirely. This prevents betting
    on future markets that Kalshi has priced but aren't yet in play.

    Returns {"sharp_prob_yes": float, "pinnacle_match": str,
             "sport_key": str, "commence_time": str} or None.
    """
    from odds_client import OddsClient
    odds_client = OddsClient()

    names = extract_team_names(market)
    if not names:
        return None

    team_a, team_b = names

    # Try matching team A first, then team B
    result = odds_client.find_team_in_matches(team_a, all_odds)
    if result:
        match = result["match"]
        side = result["side"]
        sharp_prob_yes = match["home_prob"] if side == "home" else match["away_prob"]
    else:
        result = odds_client.find_team_in_matches(team_b, all_odds)
        if not result:
            return None
        match = result["match"]
        side = result["side"]
        # YES = team A wins → team B is the other side
        sharp_prob_yes = match["away_prob"] if side == "home" else match["home_prob"]

    # ── KEY SAFETY CHECK: game must have started ─────────────────────
    # Pinnacle's commence_time tells us when the game was scheduled to start.
    # If it's still in the future, this is a pre-game market — skip it.
    # Pre-game markets can have high prices (heavy favorites) that falsely
    # trigger our endgame threshold, leading to bets on future events.
    commence_time_str = match.get("commence_time", "")
    if commence_time_str:
        try:
            from datetime import datetime, timezone
            commence_time = datetime.fromisoformat(
                commence_time_str.replace("Z", "+00:00")
            )
            if commence_time > datetime.now(timezone.utc):
                # Game hasn't started yet — skip
                return None
        except (ValueError, TypeError):
            pass  # If we can't parse it, allow it through

    return {
        "sharp_prob_yes":  sharp_prob_yes,
        "pinnacle_match":  f"{match['home_team']} vs {match['away_team']}",
        "sport_key":       match.get("sport_key", "default"),
        "commence_time":   commence_time_str,
    }


# ─────────────────────────────────────────────
# SINGLE SCAN CYCLE
# Called every POLL_INTERVAL_SECONDS.
# Markets are fetched ONCE per job and passed in (not re-fetched each cycle).
# ─────────────────────────────────────────────

def run_scan_cycle(
    kalshi: KalshiClient,
    odds_client: OddsClient,
    bankroll: float,
    pinnacle_odds: list,
    all_markets: list,      # pre-fetched market list (passed in, not re-fetched)
    cycle_num: int,
) -> tuple[int, float]:
    """
    Run one full scan of all Kalshi markets.
    Returns (bets_placed, updated_bankroll).
    """
    cycle_start = time.time()
    logger.info(f"─── Scan cycle #{cycle_num} ───────────────────────────────")

    if not all_markets:
        logger.info("No qualifying markets found on Kalshi.")
        return 0, bankroll

    # Fetch current open positions (for conflict detection)
    position_map = build_position_map(kalshi)
    open_count = len(position_map)
    logger.info(f"Open positions: {open_count}/{config.MAX_OPEN_POSITIONS} | Markets: {len(all_markets)}")

    bets_placed = 0
    evaluated = 0
    matched = 0

    for market in all_markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")

        # Support both old field names (yes_ask) and new API (yes_ask_dollars × 100)
        # API may return string or float; normalize everything to int cents (0–100)
        raw = (market.get("yes_ask_dollars") or market.get("yes_ask")
               or market.get("last_price_dollars") or market.get("last_price") or 0)
        try:
            raw_f = float(raw)
            # If value is 0–1 range (dollars), convert to cents
            yes_ask = int(raw_f * 100) if raw_f <= 1.0 else int(raw_f)
        except (ValueError, TypeError):
            yes_ask = 0

        if not yes_ask or yes_ask <= 0 or yes_ask >= 100:
            continue

        evaluated += 1

        # Match to Pinnacle odds
        match_data = match_market_to_pinnacle(market, pinnacle_odds)
        if not match_data:
            continue

        matched += 1

        sport_key = detect_sport_key(market, pinnacle_odds)
        close_time = market.get("close_time", "")

        # Check if we already hold a position in this market
        existing_side = position_map.get(ticker, None)

        # Evaluate through all safety rules
        opportunity = evaluate_market(
            ticker=ticker,
            title=title,
            kalshi_yes_price_cents=yes_ask,
            sharp_prob_yes=match_data["sharp_prob_yes"],
            bankroll=bankroll,
            open_positions=open_count + bets_placed,
            sport_key=sport_key,
            close_time_str=close_time,
            existing_position_side=existing_side,
        )

        if not opportunity:
            continue

        # Place the bet
        try:
            limit_cents = int(opportunity.kalshi_price * 100)
            result = kalshi.place_order(
                ticker=opportunity.ticker,
                side=opportunity.side,
                count=opportunity.contracts,
                limit_price=limit_cents,
                dry_run=config.DEMO_MODE,
            )

            action = "DRY_RUN" if config.DEMO_MODE else "PLACED"
            log_bet(opportunity, action, notes=str(result.get("status", "")))

            # Deduct from bankroll estimate
            bankroll = max(0, bankroll - opportunity.bet_dollars)
            bets_placed += 1

            logger.info(
                f"{'[DRY RUN] ' if config.DEMO_MODE else '💰 LIVE BET: '}"
                f"{opportunity.side.upper()} {opportunity.contracts} × {ticker} "
                f"@ {limit_cents}¢ = ${opportunity.bet_dollars:.2f}"
            )

        except Exception as e:
            logger.error(f"Order failed for {ticker}: {e}")
            log_bet(opportunity, "FAILED", notes=str(e))

    cycle_secs = time.time() - cycle_start
    logger.info(
        f"Cycle #{cycle_num} done in {cycle_secs:.1f}s | "
        f"markets={len(all_markets)} evaluated={evaluated} "
        f"matched={matched} bets={bets_placed}"
    )
    return bets_placed, bankroll


# ─────────────────────────────────────────────
# MAIN — HIGH-FREQUENCY POLLING LOOP
# ─────────────────────────────────────────────

def run():
    # ── Kill switch (redundant safety check — workflow if: condition is primary) ──
    trading_enabled = os.environ.get("TRADING_ENABLED", "true").lower()
    if trading_enabled == "false":
        logger.warning("🛑 TRADING_ENABLED=false — bot is paused. No bets will be placed.")
        return

    logger.info("=" * 60)
    logger.info(f"Kalshi Sports Bot | DEMO={config.DEMO_MODE} | "
                f"Poll every {config.POLL_INTERVAL_SECONDS}s "
                f"for {config.POLL_DURATION_SECONDS}s")
    logger.info(f"Rules: prob≥{config.MIN_WIN_PROBABILITY:.0%} | "
                f"edge≥{config.MIN_EDGE:.0%} | "
                f"max_pos={config.MAX_OPEN_POSITIONS} | "
                f"max_hours_to_close={config.MAX_HOURS_TO_CLOSE}h")
    logger.info("=" * 60)

    kalshi = KalshiClient()
    odds_client = OddsClient()

    # Get current bankroll
    try:
        bankroll = kalshi.get_balance()
        logger.info(f"Kalshi balance: ${bankroll:.2f}")
        if bankroll <= 0:
            bankroll = config.BANKROLL
    except Exception as e:
        logger.warning(f"Could not fetch balance: {e}. Using config value.")
        bankroll = config.BANKROLL

    # Fetch Pinnacle odds ONCE per job (saves API quota).
    # Odds don't change dramatically within 50 seconds.
    logger.info("Fetching Pinnacle odds snapshot...")
    try:
        pinnacle_odds = odds_client.get_all_odds()
        logger.info(f"Got {len(pinnacle_odds)} live matches from Pinnacle")
    except Exception as e:
        logger.error(f"Could not fetch Pinnacle odds: {e}")
        return

    if not pinnacle_odds:
        logger.info("No live odds available right now. Exiting.")
        return

    # Fetch Kalshi markets ONCE per job.
    # This takes ~80s for 31k markets — we must not do it every cycle.
    # Markets don't change dramatically within 50 seconds.
    logger.info("Fetching Kalshi markets snapshot...")
    try:
        all_markets = kalshi.get_all_markets(sport_filter=config.SPORT_FILTER)
        logger.info(f"Got {len(all_markets)} tradeable Kalshi markets")
    except Exception as e:
        logger.error(f"Could not fetch Kalshi markets: {e}")
        return

    if not all_markets:
        logger.info("No tradeable markets on Kalshi right now. Exiting.")
        return

    # ── Polling loop ────────────────────────────────────────────────
    start_time = time.time()
    total_bets = 0
    cycle = 1

    while True:
        elapsed = time.time() - start_time
        if elapsed >= config.POLL_DURATION_SECONDS:
            break

        bets, bankroll = run_scan_cycle(
            kalshi, odds_client, bankroll, pinnacle_odds, all_markets, cycle
        )
        total_bets += bets
        cycle += 1

        # Wait for next interval (unless we're about to time out)
        remaining = config.POLL_DURATION_SECONDS - (time.time() - start_time)
        if remaining > config.POLL_INTERVAL_SECONDS:
            logger.info(f"Waiting {config.POLL_INTERVAL_SECONDS}s for next scan...")
            time.sleep(config.POLL_INTERVAL_SECONDS)
        else:
            break

    logger.info("=" * 60)
    logger.info(
        f"Job complete | Cycles: {cycle - 1} | "
        f"Bets {'logged' if config.DEMO_MODE else 'placed'}: {total_bets} | "
        f"Remaining bankroll estimate: ${bankroll:.2f}"
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    run()
