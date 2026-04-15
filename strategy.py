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
    kalshi_yes_price_cents: int,
    sport_key: str,
    close_time_str: str = "",
) -> tuple[bool, str]:
    """
    Determine whether a game is in its final phase using two signals:

    Signal 1 — Market price level:
      Each sport has a calibrated threshold. When the YES price exceeds that
      threshold, it strongly indicates one team is near-certain to win, which
      typically only happens in the final minutes/period of a game.

      Sport-specific thresholds (all configured in config.py):
        NFL  82¢ → 4th quarter, 3+ score lead, clock running out
        NBA  80¢ → Final ~5 minutes, clear lead
        MLB  78¢ → 7th inning or later
        NHL  83¢ → Final ~5 min, 2-goal lead
        Soccer 87¢ → 75th+ min, 2-goal lead
        Tennis 72¢ → Serving for match / final-set tiebreak
        MMA  80¢ → Final round with clear dominance
        Default 80¢ → Generic fallback

    Signal 2 — Time to market close:
      If the market expires within MAX_HOURS_TO_CLOSE hours, the event
      is physically near its end. This catches cases where price alone
      might not signal end-game (e.g. a tied game in the final minute).

    Returns (is_endgame: bool, reason: str)
    """
    price = kalshi_yes_price_cents / 100.0
    leading_side_price = max(price, 1 - price)  # whichever side is ahead

    # Get sport-specific threshold
    threshold = config.ENDGAME_PRICE_THRESHOLDS.get(
        sport_key, config.ENDGAME_PRICE_THRESHOLDS["default"]
    )

    # Signal 1: price threshold
    if leading_side_price >= threshold:
        return True, f"price {leading_side_price:.0%} ≥ {threshold:.0%} threshold for {sport_key}"

    # Signal 2: close time proximity
    if close_time_str:
        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_remaining = (close_time - now).total_seconds() / 3600
            if 0 < hours_remaining <= config.MAX_HOURS_TO_CLOSE:
                return True, f"market closes in {hours_remaining:.1f}h (≤ {config.MAX_HOURS_TO_CLOSE}h)"
        except (ValueError, TypeError):
            pass

    return False, (
        f"price {leading_side_price:.0%} < {threshold:.0%} threshold "
        f"for {sport_key} — game not near end"
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
    close_time_str: str = "",
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
    endgame, endgame_reason = is_endgame(
        kalshi_yes_price_cents, sport_key, close_time_str
    )
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
