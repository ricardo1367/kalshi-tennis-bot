"""
config.py — All settings for the Kalshi Sports Bot.
"""

import os

# ─────────────────────────────────────────────
# KALSHI API CREDENTIALS
# Get these from: kalshi.com → Settings → API
# ─────────────────────────────────────────────
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "your_api_key_id_here")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

KALSHI_BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if DEMO_MODE
    else "https://api.elections.kalshi.com/trade-api/v2"   # New production URL (Apr 2026)
)

# ─────────────────────────────────────────────
# THE ODDS API
# Sign up at: the-odds-api.com (free: 500 req/month)
# ─────────────────────────────────────────────
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "your_odds_api_key_here")
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
REFERENCE_BOOKMAKER = os.getenv("REFERENCE_BOOKMAKER", "pinnacle")

# ─────────────────────────────────────────────
# SPORT FILTER
# None = scan all sports. Example: "nfl,nba" to narrow scope.
# ─────────────────────────────────────────────
_sport_filter_env = os.getenv("SPORT_FILTER", "")
SPORT_FILTER = (
    [s.strip().lower() for s in _sport_filter_env.split(",") if s.strip()]
    if _sport_filter_env else None
)

# ─────────────────────────────────────────────
# BANKROLL & KELLY SETTINGS
# ─────────────────────────────────────────────
BANKROLL          = float(os.getenv("BANKROLL", "30.0"))
KELLY_FRACTION    = float(os.getenv("KELLY_FRACTION", "0.5"))   # Half Kelly
MIN_BET_DOLLARS   = float(os.getenv("MIN_BET_DOLLARS", "0.50"))
MAX_BET_PCT       = float(os.getenv("MAX_BET_PCT", "0.10"))     # Max 10% per bet
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))

# ─────────────────────────────────────────────
# PROBABILITY & EDGE THRESHOLDS
# Core safety rules: never bet unless BOTH conditions are met.
# ─────────────────────────────────────────────

# Rule 1: Our estimated win probability must be at least this high.
# This is the primary safety filter — we only bet on near-certain outcomes.
MIN_WIN_PROBABILITY = float(os.getenv("MIN_WIN_PROBABILITY", "0.70"))   # 70% minimum

# Rule 2: We must still have an edge over the market price.
# This ensures we're getting good value even on high-probability bets.
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))   # Lowered to 3% since probability filter is strict

# ─────────────────────────────────────────────
# END-GAME ONLY SETTINGS
#
# The bot only bets when a game is in its final phase.
# We detect "end of game" using:
#   1. The Kalshi market price (high price = one side heavily favored = near end)
#   2. Sport-specific market close time proximity
#
# Each sport has a "late-game price threshold" — the minimum YES/NO price
# that indicates the game is nearly over. These are calibrated per sport
# based on typical scoring patterns and volatility:
#
#   NFL:     0.82  → Roughly 4th quarter, 3+ score lead
#   NBA:     0.80  → Final 5 min, clear lead
#   MLB:     0.78  → 7th inning stretch or later
#   NHL:     0.83  → Final 5 min, 2-goal lead (hockey swings fast)
#   Soccer:  0.87  → 75th min+, 2-goal lead (very hard to overcome)
#   Tennis:  0.72  → Serving for the match / final set tiebreak
#   MMA:     0.80  → Final round, clear dominance
#   Boxing:  0.80  → Final rounds, points lead
#   Default: 0.80  → Generic threshold for other sports
# ─────────────────────────────────────────────
ENDGAME_PRICE_THRESHOLDS = {
    "americanfootball_nfl":    0.82,
    "americanfootball_ncaaf":  0.80,
    "basketball_nba":          0.80,
    "basketball_ncaab":        0.78,
    "basketball_wnba":         0.80,
    "baseball_mlb":            0.78,
    "icehockey_nhl":           0.83,
    "soccer_epl":              0.87,
    "soccer_spain_la_liga":    0.87,
    "soccer_germany_bundesliga": 0.87,
    "soccer_italy_serie_a":    0.87,
    "soccer_france_ligue_one": 0.87,
    "soccer_uefa_champs_league": 0.87,
    "soccer_usa_mls":          0.87,
    "tennis_atp":              0.72,
    "tennis_wta":              0.72,
    "tennis_atp_french_open":  0.72,
    "tennis_wta_french_open":  0.72,
    "tennis_atp_wimbledon":    0.72,
    "tennis_wta_wimbledon":    0.72,
    "tennis_atp_us_open":      0.72,
    "tennis_wta_us_open":      0.72,
    "tennis_atp_aus_open":     0.72,
    "tennis_wta_aus_open":     0.72,
    "mma_mixed_martial_arts":  0.80,
    "boxing_boxing":           0.80,
    "default":                 0.80,
}

# How many hours before market close to allow betting.
# This is a secondary check on top of price threshold.
# 4.0 = only bet on markets expiring within 4 hours
MAX_HOURS_TO_CLOSE = float(os.getenv("MAX_HOURS_TO_CLOSE", "4.0"))

# ─────────────────────────────────────────────
# POLLING RATE
# The bot polls Kalshi every N seconds within each GitHub Actions run.
# GitHub Actions triggers once per minute; the bot polls internally
# every POLL_INTERVAL_SECONDS for the duration of the job.
# Kalshi rate limit: ~10 requests/second. We use 10s intervals to be safe.
# ─────────────────────────────────────────────
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))   # Poll every 10 seconds
POLL_DURATION_SECONDS = int(os.getenv("POLL_DURATION_SECONDS", "50"))   # Run for 50s per Actions job

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
LOG_FILE = os.getenv("LOG_FILE", "bot_log.csv")
