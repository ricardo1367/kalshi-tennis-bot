"""
config.py â All settings for the Kalshi Sports Bot.
"""

import os

# âââââââââââââââââââââââââââââââââââââââââââââ
# KALSHI API CREDENTIALS
# Get these from: kalshi.com â Settings â API
# âââââââââââââââââââââââââââââââââââââââââââââ
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "your_api_key_id_here")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
MAX_HOURS_TO_CLOSE = 24  # legacy log-format compat; actual endgame logic in strategy.py

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

KALSHI_BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if DEMO_MODE
    else "https://api.elections.kalshi.com/trade-api/v2"   # New production URL (Apr 2026)
)

# âââââââââââââââââââââââââââââââââââââââââââââ
# THE ODDS API
# Sign up at: the-odds-api.com (free: 500 req/month)
# âââââââââââââââââââââââââââââââââââââââââââââ
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "your_odds_api_key_here")
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
REFERENCE_BOOKMAKER = os.getenv("REFERENCE_BOOKMAKER", "pinnacle")

BET_LOG_DIR = "."  # directory for watchlist.json + bet log artifacts
# âââââââââââââââââââââââââââââââââââââââââââââ
# SPORT FILTER
# None = scan all sports. Example: "nfl,nba" to narrow scope.
# âââââââââââââââââââââââââââââââââââââââââââââ
_sport_filter_env = os.getenv("SPORT_FILTER", "")
SPORT_FILTER = (
    [s.strip().lower() for s in _sport_filter_env.split(",") if s.strip()]
    if _sport_filter_env else None
)

# âââââââââââââââââââââââââââââââââââââââââââââ
# BANKROLL & KELLY SETTINGS
# âââââââââââââââââââââââââââââââââââââââââââââ
BANKROLL          = float(os.getenv("BANKROLL", "30.0"))
KELLY_FRACTION    = float(os.getenv("KELLY_FRACTION", "0.5"))   # Half Kelly
MIN_BET_DOLLARS   = float(os.getenv("MIN_BET_DOLLARS", "0.50"))
MAX_BET_PCT       = float(os.getenv("MAX_BET_PCT", "0.10"))     # Max 10% per bet
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))

# âââââââââââââââââââââââââââââââââââââââââââââ
# PROBABILITY & EDGE THRESHOLDS
# Core safety rules: never bet unless BOTH conditions are met.
# âââââââââââââââââââââââââââââââââââââââââââââ

# Rule 1: Our estimated win probability must be at least this high.
# This is the primary safety filter â we only bet on near-certain outcomes.
MIN_WIN_PROBABILITY = float(os.getenv("MIN_WIN_PROBABILITY", "0.70"))   # 70% minimum

# Rule 2: We must still have an edge over the market price.
# This ensures we're getting good value even on high-probability bets.
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))   # Lowered to 3% since probability filter is strict

# Kalshi-price fallback: used when no Pinnacle/sharp line is available
# (e.g., Pinnacle suspends live odds for MLB/NHL during play).
# Requires a stricter probability threshold since Kalshi is less sharp than Pinnacle.
# Edge check is skipped; flat MIN_BET_DOLLARS is used instead of Kelly sizing.
KALSHI_FALLBACK_MIN_PROBABILITY = float(os.getenv("KALSHI_FALLBACK_MIN_PROBABILITY", "0.80"))

# âââââââââââââââââââââââââââââââââââââââââââââ
# END-GAME DETECTION â TIME BASED
#
# The bot only bets when a game is in its final phase.
# Endgame is determined purely by wall-clock minutes elapsed since
# the game started (Pinnacle's commence_time), NOT by market price.
#
# Price is ONLY used for the â¥70% probability check.
# This prevents a heavy pre-game favorite from falsely triggering
# the endgame filter â a game must be physically near its end.
#
# Thresholds are wall-clock minutes from kickoff/puck-drop/tip-off:
#
#  â½ Soccer (90 min + 15 min halftime):
#     Bet from ~75th minute of play.
#     Wall clock: 45 + 15 (halftime) + 30 = 90 min â 75th game minute
#
#  ð Hockey (3 Ã 20 min + 2 Ã 18 min intermissions):
#     Final 10 min of 3rd period.
#     Wall clock: 20 + 18 + 20 + 18 + 10 = 86 min
#
#  â¾ MLB (9 innings, ~20 min/inning avg):
#     8th inning (7 full innings done).
#     Wall clock: 7 Ã 20 = 140 min
#
#  ð NBA (4 Ã 12 min play + stoppages + halftime):
#     4th quarter, ~10 min remaining.
#     Wall clock: ~110 min from tip-off
#
#  ð NFL (4 Ã 15 min play + stoppages, ~3.5 hr game):
#     4th quarter, last 5 minutes.
#     Wall clock: ~175 min from kickoff
#
#  ð¾ Tennis (set-based, variable):
#     Final set well underway â conservative 90 min threshold.
#
#  ð¥ MMA (5 rounds Ã 5 min or 3 Ã 5 min):
#     Final round.
#     Wall clock: 20 min (covers both 3-round and 5-round)
# âââââââââââââââââââââââââââââââââââââââââââââ
ENDGAME_ELAPSED_MINUTES = {
    # ââ Soccer ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    # 82 min (not 90) â gives ~8 min buffer so orders can fill
    # before the market closes at the final whistle.
    "soccer_epl":                       82,
    "soccer_spain_la_liga":             82,
    "soccer_germany_bundesliga":        82,
    "soccer_italy_serie_a":             82,
    "soccer_france_ligue_one":          82,
    "soccer_uefa_champs_league":        82,
    "soccer_uefa_europa_league":        82,
    "soccer_usa_mls":                   82,
    "soccer_conmebol_copa_libertadores":82,
    "soccer_fifa_world_cup":            82,

    # ââ Hockey ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    "icehockey_nhl":                    86,   # Final 10 min of 3rd period

    # ââ Baseball ââââââââââââââââââââââââââââââââââââââââââââââââââ
    "baseball_mlb":                    140,   # 8th inning (7 innings Ã 20 min avg)

    # ââ Basketball ââââââââââââââââââââââââââââââââââââââââââââââââ
    "basketball_nba":                  110,   # 4th quarter, ~10 min left
    "basketball_ncaab":                100,
    "basketball_euroleague":           110,
    "basketball_wnba":                 100,

    # ââ American Football âââââââââââââââââââââââââââââââââââââââââ
    "americanfootball_nfl":            175,   # 4th quarter, last 5 min
    "americanfootball_ncaaf":          175,
    "americanfootball_cfl":            175,

    # ââ Tennis ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    "tennis_atp":                       90,   # Final set well underway
    "tennis_wta":                       90,
    "tennis_atp_french_open":           90,
    "tennis_wta_french_open":           90,
    "tennis_atp_wimbledon":             90,
    "tennis_wta_wimbledon":             90,
    "tennis_atp_us_open":               90,
    "tennis_wta_us_open":               90,
    "tennis_atp_aus_open":              90,
    "tennis_wta_aus_open":              90,

    # ââ Combat Sports âââââââââââââââââââââââââââââââââââââââââââââ
    "mma_mixed_martial_arts":           20,   # Final round
    "boxing_boxing":                    35,   # Late rounds (35 min for 10-round fight)

    # ââ Rugby / AFL âââââââââââââââââââââââââââââââââââââââââââââââ
    "rugbyleague_nrl":                  70,   # Final 10 min (80 min game + breaks)
    "rugbyunion_premiership":           70,
    "aussierules_afl":                 110,   # Final quarter (AFL ~120 min wall clock)

    # ââ Cricket âââââââââââââââââââââââââââââââââââââââââââââââââââ
    "cricket_icc_world_cup":           200,   # Late overs (T20: ~200 min for both innings)

    "default":                          90,
}

# âââââââââââââââââââââââââââââââââââââââââââââ
# POLLING RATE
# The bot polls Kalshi every N seconds within each GitHub Actions run.
# GitHub Actions triggers once per minute; the bot polls internally
# every POLL_INTERVAL_SECONDS for the duration of the job.
# Kalshi rate limit: ~10 requests/second. We use 10s intervals to be safe.
# âââââââââââââââââââââââââââââââââââââââââââââ
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))   # Poll every 10 seconds
POLL_DURATION_SECONDS = int(os.getenv("POLL_DURATION_SECONDS", "50"))   # Run for 50s per Actions job

# âââââââââââââââââââââââââââââââââââââââââââââ
# LOGGING
# âââââââââââââââââââââââââââââââââââââââââââââ
LOG_FILE = os.getenv("LOG_FILE", "bot_log.csv")
