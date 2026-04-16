"""
kalshi_bot.py â Main orchestration script with high-frequency polling.

HOW POLLING WORKS:
  - GitHub Actions triggers this script every minute (via cron)
  - The script then runs an internal loop, polling Kalshi every 10 seconds
  - Each 1-minute Actions job = ~5 Kalshi checks (every 10s for 50s)
  - Effective check rate: once every ~10 seconds, 24/7, no server needed

SAFETY RULES (all enforced before any bet):
  â Win probability â¥ 70%
  â Positive edge over Kalshi market price
  â Game is in end-game phase (sport-specific thresholds)
  â Never hold both sides of the same market
  â Never bet the low-probability side
  â Max open positions cap
  â Min/max bet size limits
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
from strategy import evaluate_market, evaluate_market_watchlist, BetOpportunity, NearMiss

# âââââââââââââââââââââââââââââââââââââââââââââ
# LOGGING
# âââââââââââââââââââââââââââââââââââââââââââââ
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# âââââââââââââââââââââââââââââââââââââââââââââ
# CSV BET LOG
# âââââââââââââââââââââââââââââââââââââââââââââ
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


# âââââââââââââââââââââââââââââââââââââââââââââ
# POSITION TRACKER
# Tracks what sides we already hold to prevent both-side bets.
# Built fresh from Kalshi's open positions each poll cycle.
# âââââââââââââââââââââââââââââââââââââââââââââ

def build_position_map(kalshi: KalshiClient) -> dict[str, str]:
    """
    Returns a dict mapping ticker â side already held ("yes" or "no").
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


# âââââââââââââââââââââââââââââââââââââââââââââ
# MARKET MATCHING
# âââââââââââââââââââââââââââââââââââââââââââââ

def extract_team_names(market: dict) -> tuple[str, str] | None:
    """
    Extract two team/player names from a Kalshi market title.
    Handles common formats:
      "Will [Team A] beat [Team B]?"
      "[Team A] vs [Team B] â Winner"
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
    past â meaning the game has actually STARTED. Pre-game markets (even with
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
        # YES = team A wins â team B is the other side
        sharp_prob_yes = match["away_prob"] if side == "home" else match["home_prob"]

    # ââ KEY SAFETY CHECK: game must have started âââââââââââââââââââââ
    # Pinnacle's commence_time tells us when the game was scheduled to start.
    # If it's still in the future, this is a pre-game market â skip it.
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
                # Game hasn't started yet â skip
                return None
        except (ValueError, TypeError):
            pass  # If we can't parse it, allow it through

    return {
        "sharp_prob_yes":  sharp_prob_yes,
        "pinnacle_match":  f"{match['home_team']} vs {match['away_team']}",
        "sport_key":       match.get("sport_key", "default"),
        "commence_time":   commence_time_str,
        "event_id":        match.get("event_id", ""),   # Used to look up live game state
    }


# âââââââââââââââââââââââââââââââââââââââââââââ
# SINGLE SCAN CYCLE
# Called every POLL_INTERVAL_SECONDS.
# Markets are fetched ONCE per job and passed in (not re-fetched each cycle).
# âââââââââââââââââââââââââââââââââââââââââââââ

def run_scan_cycle(
    kalshi: KalshiClient,
    odds_client: OddsClient,
    bankroll: float,
    pinnacle_odds: list,
    all_markets: list,        # pre-fetched market list (passed in, not re-fetched)
    game_states: dict,        # event_id â game-state description from /scores API
    cycle_num: int,
    placed_tickers: set = None,  # tickers already ordered this job (persists across cycles)
) -> tuple[int, float, list]:
    """
    Run one full scan of all Kalshi markets.
    Returns (bets_placed, updated_bankroll, near_misses).
    """
    cycle_start = time.time()
    logger.info(f"âââ Scan cycle #{cycle_num} âââââââââââââââââââââââââââââââ")

    if placed_tickers is None:
        placed_tickers = set()

    if not all_markets:
        logger.info("No qualifying markets found on Kalshi.")
        return 0, bankroll, []

    # Fetch current open positions (for conflict detection)
    position_map = build_position_map(kalshi)
    open_count = len(position_map)
    logger.info(f"Open positions: {open_count}/{config.MAX_OPEN_POSITIONS} | Markets: {len(all_markets)}")

    bets_placed = 0
    evaluated = 0
    matched = 0
    near_misses: list[NearMiss] = []

    for market in all_markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")

        # Support both old field names (yes_ask) and new API (yes_ask_dollars Ã 100)
        # API may return string or float; normalize everything to int cents (0â100)
        raw = (market.get("yes_ask_dollars") or market.get("yes_ask")
               or market.get("last_price_dollars") or market.get("last_price") or 0)
        try:
            raw_f = float(raw)
            # If value is 0â1 range (dollars), convert to cents
            yes_ask = int(raw_f * 100) if raw_f <= 1.0 else int(raw_f)
        except (ValueError, TypeError):
            yes_ask = 0

        if not yes_ask or yes_ask <= 0 or yes_ask >= 100:
            continue

        # Skip if we already placed an order for this ticker in a prior cycle this job.
        # build_position_map() only sees filled positions; resting orders are invisible
        # to it â so without this guard the bot would re-order the same market every cycle.
        if ticker in placed_tickers:
            continue

        evaluated += 1

        # Match to Pinnacle odds
        match_data = match_market_to_pinnacle(market, pinnacle_odds)
        if not match_data:
            continue

        matched += 1

        # Use sport_key and commence_time from Pinnacle match (authoritative),
        # not from keyword guessing or Kalshi's close_time.
        sport_key = match_data.get("sport_key", "default")
        commence_time = match_data.get("commence_time", "")  # Pinnacle game start

        # Look up live game state (period/inning/set/round) from /scores API.
        # Falls back gracefully to elapsed-time logic if no state is available.
        event_id = match_data.get("event_id", "")
        game_state = game_states.get(event_id, "")
        if game_state:
            logger.debug(f"Game state for {ticker}: {game_state!r}")

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
            commence_time_str=commence_time,
            game_state=game_state,
            existing_position_side=existing_side,
        )

        if not opportunity:
            # Check if this market is close to triggering (watchlist)
            near = evaluate_market_watchlist(
                ticker=ticker,
                title=title,
                kalshi_yes_price_cents=yes_ask,
                sharp_prob_yes=match_data["sharp_prob_yes"],
                sport_key=sport_key,
                commence_time_str=commence_time,
                game_state=game_state,
                pinnacle_match=match_data.get("pinnacle_match", ""),
            )
            if near:
                near_misses.append(near)
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

            # Prevent re-ordering this ticker in subsequent cycles this job.
            # Resting orders are not returned by get_open_positions(), so without
            # this the bot would place a new order every scan cycle.
            placed_tickers.add(ticker)

            # Deduct from bankroll estimate
            bankroll = max(0, bankroll - opportunity.bet_dollars)
            bets_placed += 1

            logger.info(
                f"{'[DRY RUN] ' if config.DEMO_MODE else 'ð° LIVE BET: '}"
                f"{opportunity.side.upper()} {opportunity.contracts} Ã {ticker} "
                f"@ {limit_cents}Â¢ = ${opportunity.bet_dollars:.2f}"
            )

        except Exception as e:
            logger.error(f"Order failed for {ticker}: {e}")
            log_bet(opportunity, "FAILED", notes=str(e))

    cycle_secs = time.time() - cycle_start
    logger.info(
        f"Cycle #{cycle_num} done in {cycle_secs:.1f}s | "
        f"markets={len(all_markets)} evaluated={evaluated} "
        f"matched={matched} bets={bets_placed} "
        f"watchlist={len(near_misses)}"
    )
    return bets_placed, bankroll, near_misses


# âââââââââââââââââââââââââââââââââââââââââââââ
# MAIN â HIGH-FREQUENCY POLLING LOOP
# âââââââââââââââââââââââââââââââââââââââââââââ

def run():
    # ââ Kill switch (redundant safety check â workflow if: condition is primary) ââ
    trading_enabled = os.environ.get("TRADING_ENABLED", "true").lower()
    if trading_enabled == "false":
        logger.warning("ð TRADING_ENABLED=false â bot is paused. No bets will be placed.")
        return

    logger.info("=" * 60)
    logger.info(f"Kalshi Sports Bot | DEMO={config.DEMO_MODE} | "
                f"Poll every {config.POLL_INTERVAL_SECONDS}s "
                f"for {config.POLL_DURATION_SECONDS}s")
    logger.info(f"Rules: probâ¥{config.MIN_WIN_PROBABILITY:.0%} | "
                f"edgeâ¥{config.MIN_EDGE:.0%} | "
                f"max_pos={config.MAX_OPEN_POSITIONS} | "
                f"kelly={config.KELLY_FRACTION:.0%}")
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
    # This takes ~80s for 31k markets â we must not do it every cycle.
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

    # Fetch live game states (period/inning/set/round) ONCE per job.
    # Only for sport_keys that actually appear in today's Pinnacle odds â
    # no point querying cricket scores if there are no live cricket matches.
    # Cost: 1 Odds API request per active sport (same as /odds).
    active_sport_keys = list({m.get("sport_key", "") for m in pinnacle_odds if m.get("sport_key")})
    # Soccer uses elapsed-time fallback â no need to fetch its scores
    scores_sport_keys = [k for k in active_sport_keys if not k.startswith("soccer_")]
    logger.info(f"Fetching live game states for {len(scores_sport_keys)} non-soccer sports...")
    try:
        game_states = odds_client.get_live_game_states(scores_sport_keys)
    except Exception as e:
        logger.warning(f"Could not fetch game states: {e} â using elapsed-time fallback")
        game_states = {}

    # ââ Polling loop ââââââââââââââââââââââââââââââââââââââââââââââââ
    start_time = time.time()
    total_bets = 0
    cycle = 1
    latest_watchlist: list = []   # near-misses from most recent cycle
    placed_tickers: set[str] = set()  # tickers ordered this job â shared across cycles

    while True:
        elapsed = time.time() - start_time
        if elapsed >= config.POLL_DURATION_SECONDS:
            break

        bets, bankroll, near_misses = run_scan_cycle(
            kalshi, odds_client, bankroll, pinnacle_odds, all_markets, game_states, cycle,
            placed_tickers=placed_tickers,
        )
        total_bets += bets
        latest_watchlist = near_misses   # keep most recent snapshot
        cycle += 1

        # Wait for next interval (unless we're about to time out)
        remaining = config.POLL_DURATION_SECONDS - (time.time() - start_time)
        if remaining > config.POLL_INTERVAL_SECONDS:
            logger.info(f"Waiting {config.POLL_INTERVAL_SECONDS}s for next scan...")
            time.sleep(config.POLL_INTERVAL_SECONDS)
        else:
            break

    # ââ Write watchlist to bet log artifact âââââââââââââââââââââââââ
    # The bet log JSON is picked up by the dashboard. We append a
    # "watchlist" key so the dashboard can show near-miss opportunities.
    if latest_watchlist:
        import json as _json
        watchlist_path = os.path.join(config.BET_LOG_DIR, "watchlist.json")
        os.makedirs(config.BET_LOG_DIR, exist_ok=True)
        watchlist_data = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "bankroll": round(bankroll, 2),
            "items": [
                {
                    "ticker":       nm.ticker,
                    "title":        nm.title,
                    "sport_key":    nm.sport_key,
                    "pinnacle_match": nm.pinnacle_match,
                    "sharp_prob":   nm.sharp_prob,
                    "kalshi_price": nm.kalshi_price,
                    "edge":         nm.edge,
                    "prob_gap":     nm.prob_gap,
                    "edge_gap":     nm.edge_gap,
                    "endgame":      nm.endgame,
                    "blocking_rule": nm.blocking_rule,
                }
                for nm in sorted(latest_watchlist,
                                  key=lambda x: x.sharp_prob, reverse=True)[:20]
            ],
        }
        with open(watchlist_path, "w") as f:
            _json.dump(watchlist_data, f, indent=2)
        # Also emit as a single parseable line so the dashboard can extract it
        # from the GitHub Actions run log without needing to unzip an artifact.
        logger.info(f"WATCHLIST_JSON:{_json.dumps(watchlist_data)}")
        logger.info(f"Watchlist: {len(latest_watchlist)} near-misses â {watchlist_path}")
    else:
        logger.info("Watchlist: 0 near-misses this job")

    logger.info("=" * 60)
    logger.info(
        f"Job complete | Cycles: {cycle - 1} | "
        f"Bets {'logged' if config.DEMO_MODE else 'placed'}: {total_bets} | "
        f"Remaining bankroll estimate: ${bankroll:.2f}"
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    run()
