"""
strategy.py — Core betting logic with strict safety rules.

SAFETY RULES (all must pass before any bet is placed):
  1. Our estimated win probability must be ≥ 70% (MIN_WIN_PROBABILITY)
  2. We must have a positive edge over the Kalshi market price (MIN_EDGE)
  3. The game must be in its final phase (end-game price threshold per sport)
  4. We must not already hold a position in this market (checked in bot)
  5. We never bet the LOSING side — only the side with 70%+ probability
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import config

logger = logging.getLogger(__name__)


@dataclass
class NearMiss:
    """
    A market that is close to triggering but doesn't yet pass all filters.
    Used to populate the dashboard's Watchlist panel.
    """
    ticker: str
    title: str
    sport_key: str
    sharp_prob: float          # Our best probability estimate (leading side)
    kalshi_price: float        # Kalshi's current price for that side (0–1)
    edge: float                # sharp_prob - kalshi_price (can be negative)
    prob_gap: float            # How far below 70% probability (0 = at threshold)
    edge_gap: float            # How far below 3% edge (0 = at threshold)
    endgame: bool              # Whether the endgame check passes
    endgame_reason: str        # Reason string from is_endgame()
    blocking_rule: str         # Which rule is preventing the bet
    pinnacle_match: str = ""   # "Team A vs Team B" from Pinnacle


@dataclass
class BetOpportunity:
    """Represents a single validated betting opportunity."""
    ticker: str
    title: str
    side: str             # "yes" or "no" — ALWAYS the high-probability side
    kalshi_price: float   # Kalshi's current price for this side (0.0–1.0)
    sharp_prob: float     # Our probability estimate (always ≥ MIN_WIN_PROBABILITY)
    edge: float           # sharp_prob - kalshi_price
    kelly_fraction: float
    bet_dollars: float
    contracts: int
    sport_key: str        # e.g. "americanfootball_nfl"
    skip_reason: str = "" # Populated when opportunity is rejected


# ─────────────────────────────────────────────
# END-GAME DETECTION
# ─────────────────────────────────────────────

def is_endgame(
    sport_key: str,
    commence_time_str: str,
) -> tuple[bool, str]:
    """
    Determine whether a game is in its final phase using elapsed wall-clock
    time since the game started (Pinnacle's commence_time).

    This is INDEPENDENT of market price — price is only used for the ≥70%
    probability check in evaluate_market(). Separating these two concerns
    prevents pre-game favorites from falsely triggering the endgame filter.

    Sport-specific thresholds (wall-clock minutes from kickoff):
      ⚽ Soccer      90 min  → ~75th minute of play (after 15 min halftime)
      🏒 Hockey      86 min  → Final 10 min of 3rd period
      ⚾ MLB        140 min  → 8th inning (7 full innings × ~20 min avg)
      🏀 NBA        110 min  → 4th quarter, ~10 min remaining
      🏈 NFL        175 min  → 4th quarter, last 5 min
      🎾 Tennis      90 min  → Final set well underway
      🥊 MMA         20 min  → Final round

    Returns (is_endgame: bool, reason: str)
    """
    if not commence_time_str:
        return False, "no commence_time available — cannot determine game phase"

    try:
        commence_time = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        elapsed_minutes = (now - commence_time).total_seconds() / 60
    except (ValueError, TypeError):
        return False, f"could not parse commence_time: {commence_time_str!r}"

    threshold = config.ENDGAME_ELAPSED_MINUTES.get(
        sport_key, config.ENDGAME_ELAPSED_MINUTES["default"]
    )

    if elapsed_minutes < 0:
        return False, f"game hasn't started yet ({abs(elapsed_minutes):.0f} min from now)"

    if elapsed_minutes >= threshold:
        return True, (
            f"{elapsed_minutes:.0f} min elapsed ≥ {threshold} min threshold "
            f"for {sport_key}"
        )

    return False, (
        f"{elapsed_minutes:.0f} of {threshold} min elapsed for {sport_key} "
        f"— {threshold - elapsed_minutes:.0f} min until endgame window"
    )


# ─────────────────────────────────────────────
# EDGE & KELLY CALCULATION
# ─────────────────────────────────────────────

def calculate_edge(kalshi_price: float, sharp_prob: float) -> float:
    """Edge = how much better our probability is vs what Kalshi is charging."""
    return round(sharp_prob - kalshi_price, 4)


def kelly_bet_size(prob: float, price: float, bankroll: float) -> float:
    """
    Fractional Kelly bet size in dollars.

    Args:
        prob:     Our estimated win probability (e.g. 0.75)
        price:    Kalshi's current price for this side (e.g. 0.65)
        bankroll: Current available bankroll

    Returns: Dollar amount to bet (0 if no positive expectation)
    """
    if price <= 0 or price >= 1:
        return 0.0

    b = (1 - price) / price   # profit per dollar staked
    q = 1 - prob
    full_kelly = (b * prob - q) / b
    fractional = full_kelly * config.KELLY_FRACTION

    fractional = max(0.0, fractional)
    fractional = min(fractional, config.MAX_BET_PCT)

    return round(fractional * bankroll, 2)


# ─────────────────────────────────────────────
# MAIN EVALUATION
# ─────────────────────────────────────────────

def evaluate_market(
    ticker: str,
    title: str,
    kalshi_yes_price_cents: int,
    sharp_prob_yes: float,
    bankroll: float,
    open_positions: int,
    sport_key: str = "default",
    commence_time_str: str = "",   # Pinnacle's game start time (used for endgame check)
    existing_position_side: str = None,  # "yes", "no", or None
) -> BetOpportunity | None:
    """
    Evaluate a single Kalshi market. Returns a BetOpportunity only if
    ALL safety rules pass. Returns None (with logged reason) otherwise.

    Safety rules enforced here:
      ✅ Rule 1: Win probability ≥ 70%
      ✅ Rule 2: Positive edge over market price
      ✅ Rule 3: Game is in end-game phase
      ✅ Rule 4: Not already holding this market (conflict check)
      ✅ Rule 5: Only betting the HIGH-probability side (never the losing side)
      ✅ Rule 6: Sufficient bankroll and bet size
      ✅ Rule 7: Max open positions not exceeded
    """
    def reject(reason: str) -> None:
        logger.debug(f"SKIP {ticker[:40]}: {reason}")
        return None

    # ── Rule 7: position cap ───────────────────────────────────────
    if open_positions >= config.MAX_OPEN_POSITIONS:
        return reject(f"max positions ({config.MAX_OPEN_POSITIONS}) reached")

    kalshi_price_yes = kalshi_yes_price_cents / 100.0

    # ── Rule 5: identify which side has 70%+ probability ──────────
    # We evaluate both YES and NO, but ONLY allow betting on the
    # side where our estimated probability is ≥ MIN_WIN_PROBABILITY.
    # This prevents ever betting on the losing/underdog side.
    candidates = []

    # Can we bet YES?
    if sharp_prob_yes >= config.MIN_WIN_PROBABILITY:
        edge_yes = calculate_edge(kalshi_price_yes, sharp_prob_yes)
        candidates.append(("yes", kalshi_price_yes, sharp_prob_yes, edge_yes))

    # Can we bet NO? (flip perspective)
    sharp_prob_no = 1 - sharp_prob_yes
    kalshi_price_no = 1 - kalshi_price_yes
    if sharp_prob_no >= config.MIN_WIN_PROBABILITY:
        edge_no = calculate_edge(kalshi_price_no, sharp_prob_no)
        candidates.append(("no", kalshi_price_no, sharp_prob_no, edge_no))

    if not candidates:
        return reject(
            f"neither side has ≥ {config.MIN_WIN_PROBABILITY:.0%} probability "
            f"(YES={sharp_prob_yes:.0%}, NO={sharp_prob_no:.0%})"
        )

    # Pick the best edge among qualifying sides
    best = max(candidates, key=lambda c: c[3])
    side, bet_price, bet_prob, edge = best

    # ── Rule 4: no conflicting position ───────────────────────────
    if existing_position_side is not None:
        if existing_position_side == side:
            return reject(f"already hold {side.upper()} on this market")
        else:
            return reject(
                f"already hold {existing_position_side.upper()} — "
                f"refusing to buy {side.upper()} (would bet both sides)"
            )

    # ── Rule 2: minimum edge ───────────────────────────────────────
    if edge < config.MIN_EDGE:
        return reject(
            f"edge {edge:.1%} < minimum {config.MIN_EDGE:.1%} "
            f"(our prob={bet_prob:.0%}, market={bet_price:.0%})"
        )

    # ── Rule 3: end-game check ────────────────────────────────────
    endgame, endgame_reason = is_endgame(sport_key, commence_time_str)
    if not endgame:
        return reject(f"not end-game: {endgame_reason}")

    # ── Rule 1: confirmed — probability is already ≥ 70% ──────────
    # (validated above in candidates filter)

    # ── Rule 6: bet sizing ─────────────────────────────────────────
    bet_dollars = kelly_bet_size(bet_prob, bet_price, bankroll)
    if bet_dollars < config.MIN_BET_DOLLARS:
        return reject(
            f"bet size ${bet_dollars:.2f} < minimum ${config.MIN_BET_DOLLARS:.2f}"
        )

    contracts = max(1, math.floor(bet_dollars / bet_price))
    actual_cost = round(contracts * bet_price, 2)

    opportunity = BetOpportunity(
        ticker=ticker,
        title=title,
        side=side,
        kalshi_price=bet_price,
        sharp_prob=bet_prob,
        edge=edge,
        kelly_fraction=actual_cost / bankroll,
        bet_dollars=actual_cost,
        contracts=contracts,
        sport_key=sport_key,
    )

    logger.info(
        f"✅ BET FOUND | {title[:45]} | "
        f"Side={side.upper()} | "
        f"Our prob={bet_prob:.0%} | "
        f"Market={bet_price:.0%} | "
        f"Edge={edge:.1%} | "
        f"Bet=${actual_cost:.2f} ({contracts} contracts) | "
        f"End-game: {endgame_reason}"
    )

    return opportunity


# ─────────────────────────────────────────────
# WATCHLIST / NEAR-MISS DETECTION
# ─────────────────────────────────────────────

# How close to each threshold before appearing on the watchlist:
WATCHLIST_PROB_GAP  = 0.10   # show if within 10pp of 70% → prob ≥ 60%
WATCHLIST_EDGE_GAP  = 0.03   # show if edge within 3pp of minimum → edge ≥ 0%


def evaluate_market_watchlist(
    ticker: str,
    title: str,
    kalshi_yes_price_cents: int,
    sharp_prob_yes: float,
    sport_key: str = "default",
    commence_time_str: str = "",   # Pinnacle's game start time
    pinnacle_match: str = "",
) -> NearMiss | None:
    """
    Check if a market is "close" to triggering — within WATCHLIST_PROB_GAP
    of the probability threshold or WATCHLIST_EDGE_GAP of the edge threshold.

    Returns a NearMiss if the market is worth watching, None otherwise.
    This is called AFTER evaluate_market returns None (i.e. the bet didn't
    qualify), so we don't double-count actual bet opportunities.
    """
    kalshi_price_yes = kalshi_yes_price_cents / 100.0

    # Evaluate both sides and pick the most promising
    best_prob = max(sharp_prob_yes, 1 - sharp_prob_yes)
    if best_prob >= 0.5:
        if sharp_prob_yes >= 1 - sharp_prob_yes:
            side_prob  = sharp_prob_yes
            side_price = kalshi_price_yes
        else:
            side_prob  = 1 - sharp_prob_yes
            side_price = 1 - kalshi_price_yes
    else:
        return None  # Neither side is favored — not watchlist-worthy

    # Only watch if we're within WATCHLIST_PROB_GAP of the min threshold
    prob_gap = max(0.0, config.MIN_WIN_PROBABILITY - side_prob)
    if prob_gap > WATCHLIST_PROB_GAP:
        return None

    edge = side_prob - side_price
    edge_gap = max(0.0, config.MIN_EDGE - edge)

    # Endgame check
    endgame, endgame_reason = is_endgame(sport_key, commence_time_str)

    # Determine what's blocking a real bet
    if side_prob < config.MIN_WIN_PROBABILITY:
        blocking = f"prob {side_prob:.0%} < {config.MIN_WIN_PROBABILITY:.0%} — needs {prob_gap:.1%} more"
    elif edge < config.MIN_EDGE:
        blocking = f"edge {edge:.1%} < {config.MIN_EDGE:.1%} — needs {edge_gap:.1%} more"
    elif not endgame:
        blocking = f"not end-game: {endgame_reason}"
    else:
        blocking = "sizing below minimum"

    return NearMiss(
        ticker=ticker,
        title=title,
        sport_key=sport_key,
        sharp_prob=round(side_prob, 4),
        kalshi_price=round(side_price, 4),
        edge=round(edge, 4),
        prob_gap=round(prob_gap, 4),
        edge_gap=round(edge_gap, 4),
        endgame=endgame,
        endgame_reason=endgame_reason,
        blocking_rule=blocking,
        pinnacle_match=pinnacle_match,
    )
