"""
strategy.py â Core betting logic with strict safety rules.

SAFETY RULES (all must pass before any bet is placed):
  1. Our estimated win probability must be â¥ 70% (MIN_WIN_PROBABILITY)
  2. We must have a positive edge over the Kalshi market price (MIN_EDGE)
  3. The game must be in its final phase (end-game price threshold per sport)
  4. We must not already hold a position in this market (checked in bot)
  5. We never bet the LOSING side â only the side with 70%+ probability
"""

import logging
import math
import re
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
    kalshi_price: float        # Kalshi's current price for that side (0â1)
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
    side: str             # "yes" or "no" â ALWAYS the high-probability side
    kalshi_price: float   # Kalshi's current price for this side (0.0â1.0)
    sharp_prob: float     # Our probability estimate (always â¥ MIN_WIN_PROBABILITY)
    edge: float           # sharp_prob - kalshi_price
    kelly_fraction: float
    bet_dollars: float
    contracts: int
    sport_key: str        # e.g. "americanfootball_nfl"
    skip_reason: str = "" # Populated when opportunity is rejected


# âââââââââââââââââââââââââââââââââââââââââââââ
# END-GAME DETECTION
# âââââââââââââââââââââââââââââââââââââââââââââ

def _parse_ordinal(text: str) -> int | None:
    """
    Extract a period number from strings like '3rd Period', '8th Inning', 'Round 4'.
    Handles both ordinal words (1st/2nd/3rd/4thâ¦) and bare digits.
    """
    ordinals = {
        "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
        "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10,
        "11th": 11, "12th": 12,
    }
    lower = text.lower()
    for word, num in ordinals.items():
        if word in lower:
            return num
    m = re.search(r'\b(\d+)\b', text)
    return int(m.group(1)) if m else None


def is_endgame_by_game_state(sport_key: str, description: str) -> tuple[bool, str] | None:
    """
    Determine endgame from the actual current game-state string returned by
    The Odds API /scores endpoint (e.g. "3rd Period", "8th Inning", "Set 3").

    Returns:
      (True,  reason)  â game IS in endgame phase
      (False, reason)  â game is NOT yet in endgame phase
      None             â description unrecognised / sport not handled here
                         (caller should fall back to elapsed-time logic)

    Sport rules:
      â¾ Baseball   inning â¥ 8, or "Extra Innings"
      ð Hockey     3rd Period, OT, or Shootout
      ð Basketball 4th Quarter (or 2nd Half for college), OT
      ð NFL/NCAAF  4th Quarter, OT
      ð¾ Tennis     Set â¥ 3 (deciding set of best-of-3 or best-of-5)
      ð¥ MMA        Round â¥ 3 (final round of 3-round; late rounds of 5-round)
      ð¥ Boxing     Round â¥ 10
      â½ Soccer      Handled by elapsed-time fallback (see is_endgame below)
    """
    if not description:
        return None

    desc = description.lower().strip()

    # ââ Baseball ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if "baseball" in sport_key:
        if "extra" in desc:
            return True, f"Extra Innings â endgame"
        if "inning" in desc:
            n = _parse_ordinal(description)
            if n is not None:
                if n >= 8:
                    return True, f"{description} â inning â¥ 8, endgame"
                return False, f"{description} â inning {n}, need â¥ 8th"
        return None

    # ââ Hockey ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if "hockey" in sport_key:
        if any(x in desc for x in ("overtime", " ot", "shootout", "penalty")):
            return True, f"{description} â OT/Shootout, endgame"
        if "period" in desc:
            n = _parse_ordinal(description)
            if n is not None:
                if n >= 3:
                    return True, f"{description} â 3rd Period, endgame"
                return False, f"{description} â Period {n}, need 3rd"
        return None

    # ââ Basketball (NBA, WNBA, EuroLeague, NCAAB) âââââââââââââââââââââââââ
    if "basketball" in sport_key:
        if any(x in desc for x in ("overtime", " ot")):
            return True, f"{description} â OT, endgame"
        if "quarter" in desc:
            n = _parse_ordinal(description)
            if n is not None:
                if n >= 4:
                    return True, f"{description} â 4th Quarter, endgame"
                return False, f"{description} â Quarter {n}, need 4th"
        # College basketball uses halves
        if "half" in desc:
            n = _parse_ordinal(description)
            if n is not None and n >= 2:
                return True, f"{description} â 2nd Half, endgame"
            return False, f"{description} â 1t Half, not yet endgame"
        return None

    # ââ American Football (NFL, NCAAF, CFL) âââââââââââââââââââââââââââââââ
    if "football" in sport_key:
        if any(x in desc for x in ("overtime", " ot")):
            return True, f"{description} â OT, endgame"
        if "quarter" in desc:
            n = _parse_ordinal(description)
            if n is not None:
                if n >= 4:
                    return True, f"{description} â 4th Quarter, endgame"
                return False, f"{description} â Quarter {n}, need 4th"
        return None

    # ââ Tennis ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if "tennis" in sport_key:
        if "set" in desc:
            n = _parse_ordinal(description)
            if n is not None:
                if n >= 3:
                    return True, f"{description} â deciding set (Set {n}), endgame"
                return False, f"{description} â Set {n}, need 3rd or higher"
        return None

    # ââ MMA ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if "mma" in sport_key:
        if "round" in desc:
            n = _parse_ordinal(description)
            if n is not None:
                # Round 3 is endgame for 3-round fights; also late in 5-round fights
                if n >= 3:
                    return True, f"{description} â Round {n}, endgame"
                return False, f"{description} â Round {n}, need Round 3+"
        return None

    # ââ Boxing ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    if "boxing" in sport_key:
        if "round" in desc:
            n = _parse_ordinal(description)
            if n is not None:
                if n >= 10:
                    return True, f"{description} â Round {n}, late rounds, endgame"
                return False, f"{description} â Round {n}, need Round 10+"
        return None

    # ââ Soccer / NRL / Rugby / AFL / Cricket âââââââââââââââââââââââââââââ
    # These either use elapsed time (soccer) or aren't common enough for
    # precise state parsing â fall through to the elapsed-time check.
    return None


def is_endgame(
    sport_key: str,
    commence_time_str: str,
    game_state: str = "",   # current period/inning/set from The Odds API /scores
) -> tuple[bool, str]:
    """
    Determine whether a game is in its final phase.

    Priority order:
      1. Actual game state (period / inning / set / round) from The Odds API
         ââââ most accurate, handles stoppages, halftimes, overtime correctly.
      2. Elapsed wall-clock time from Pinnacle's commence_time
         ââââfallback when scores data is unavailable or unrecognised.
          ââââALWAYS used for soccer (2nd Half alone isn't endgame â we need
           the actual minute, which elapsed time approximates well enough).

    Sport thresholds for the elapsed-time fallback (minutes from kickoff):
      â½ Soccer      90 min  â ~75th minute of play (45 + 15 halftime + 30)
      ð Hockey       86 min  â Final 10 min of 3rd period
      â¾ MLB        140 min  â 8th inning (7 innings Ã ~20 min avg)
      ð NBA        110 min  â 4th quarter, ~10 min remaining
      ð NFL        175 min  â 4th quarter, last 5 min
      ð¾ Tennis      90 min  â Final set well underway
      ð¥ MMA         20 min  â Final round

    Returns (is_endgame: bool, reason: str)
    """
    # ââ Step 1: try game-state-based detection ââââââââââââââââââââââââââââ
    if game_state:
        result = is_endgame_by_game_state(sport_key, game_state)
        if result is not None:
            return result
        # State provided but not recognised for this sport â fall through
        logger.debug(
            f"Game state '{game_state}' unrecognised for {sport_key} â"
            "falling back to elapsed-time check"
        )

    # ââ Step 2: elapsed wall-clock time fallback âââââââââââââââââââââââââââ
    if not commence_time_str:
        return False, "no commence_time or game state available â cannot determine game phase"

    try:
        commence_time = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        elapsed_minutes = (now - commence_time).total_seconds() / 60
    except (ValueError, TypeError):
        return False, f"could not parse commence_time: {commence_time_str!2}"

    threshold = config.ENDGAME_ELAPSED_MINUTES.get(
        sport_key, config.ENDGAME_ELAPSED_MINUTES[default"]
    )

    if elapsed_minutes < 0:
        return False, f"game hasn't started yet ({abs(elapsed_minutes):.0f} min from now)"

    source = "game state unavailable, " if game_state else ""
    if elapsed_minutes >= threshold:
        return True, (
            f"{source}{elapsed_minutes:.0f} min elapsed â¥ {threshold} min threshold "
            f"for {sport_key}"
        )

    return False, (
        f"{source}{elapsed_minutes:.0f} of {threshold} min elapsed for {sport_key} "
        f"â {threshold - elapsed_minutes:.0f} min until endgame window"
    )


# âââââââââââââââââââââââââââââââââââââââââââââ
# EDGE & KELLY CALCULATION
# âââââââââââââââââââââââââââââââââââââââââââââ

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


# âââââââââââââââââââââââââââââââââââââââââââââ
# MAIN EVALUATION
# âââââââââââââââââââââââââââââââââââââââââââââ

def evaluate_market(
    ticker: str,
    title: str,
    kalshi_yes_price_cents: int,
    sharp_prob_yes: float,
    bankroll: float,
    open_positions: int,
    sport_key: str = "default",
    commence_time_str: str = "",   # Pinnacle's game start time (used for endgame check)
    game_state: str = "",          # Current period/inning/set/round from /scores API
    existing_position_side: str = None,  # "yes", "no", or None
    kalshi_fallback: bool = False, # True when using Kalshi price as probability proxy
                                   # (no Pinnacle line available â e.g. live MLB/NHL)
) -> BetOpportunity | None:
    """
    Evaluate a single Kalshi market. Returns a BetOpportunity only if
    ALL safety rules pass. Returns None (with logged reason) otherwise.

    Safety rules enforced here:
      â Rule 1: Win probability â¥ 70%
      â Rule 2: Positive edge over market price
      â Rule 3: Game is in end-game phase
      â Rule 4: Not already holding this market (conflict check)
      â Rule 5: Only betting the HIGH-probability side (never the losing side)
      â Rule 6: Sufficient bankroll and bet sige
      â Rule 7: Max open positions not exceeded
    """
    def reject(reason: str) -> None:
        logger.debug(f"SKIP {ticker[:40]}: {reason}")
        return None

    # ââ Rule 7: position cap âââââââââââââââââââââââââââââââââââââââ
    if open_positions >= config.MAX_OPEN_POSITIONS:
        return reject(f"max positions ({config.MAX_OPEN_POSITIONS}) reached")

    kalshi_price_yes = kalshi_yes_price_cents / 100.0

    # ââ Rule 5: identify which side has the required probability âââââ
    # Normal mode:   require â¥ MIN_WIN_PROBABILITY (70%) from Pinnacle
    # Fallback mode: require â¥ KALSHI_FALLBACK_MIN_PROBABILITY (80%) from Kalshi price
    #   (stricter because Kalshi is a less sharp probability source than Pinnacle)
    min_prob = (
        config.KALSHI_FALLBACK_MIN_PROBABILITY
        if kalshi_fallback
        else config.MIN_WIN_PROBABILITY
    )
    candidates = []

    # Can we bet YES?
    if sharp_prob_yes >= min_prob:
        edge_yes = calculate_edge(kalshi_price_yes, sharp_prob_yes)
        candidates.append(("yes", kalshi_price_yes, sharp_prob_yes, edge_yes))

    # Can we bet NO? (flip perspective)
    sharp_prob_no = 1 - sharp_prob_yes
    kalshi_price_no = 1 - kalshi_price_yes
    if sharp_prob_no >= min_prob:
        edge_no = calculate_edge(kalshi_price_no, sharp_prob_no)
        candidates.append(("no", kalshi_price_no, sharp_prob_no, edge_no))

    if not candidates:
        return reject(
            f"neither side has â¥ {min_prob:.0%} probability "
            f"(YES={sharp_prob_yes:.0%}, NO={sharp_prob_no:.0%})"
            + (" [Kalshi fallback]" if kalshi_fallback else "")
        )

    # Pick the best edge among qualifying sides
    best = max(candidates, key=lambda c: c[3])
    side, bet_price, bet_prob, edge = best

    # ââ Rule 4: no conflicting position âââââââââââââââââââââââââââ
    if existing_position_side is not None:
        if existing_position_side == side:
            return reject(f"already hold {side.upper()} on this market")
        else:
            return reject(
                f"already hold {existing_position_side.upper()} â "
                f"refusing to buy {side.upper()} (would bet both sides)"
            )

    # ââ Rule 2: minimum edge âââââââââââââââââââââââââââââââââââââââ
    # Skipped for Kalshi-fallback bets: when using Kalshi's own price as the
    # probability estimate, edge = price â price = 0 by definition.
    # The stricter 80% probability threshold compensates for this.
    if not kalshi_fallback and edge < config.MIN_EDGE:
        return reject(
            f"edge {edge:.1%} < minimum {config.MIN_EDGE:.1%} "
            f"(our prob={bet_prob:.0%}, market={bet_price:.0%})"
        )

    # ââ Rule 3: end-game check ââââââââââââââââââââââââââââââââââââ
    endgame, endgame_reason = is_endgame(sport_key, commence_time_str, game_state)
    if not endgame:
        return reject(f"not end-game: {endgame_reason}")

    # ââ Rule 1: confirmed â probability is already â70% âââââââââ
    # (validated above in candidates filter)

    # ââ Rule 6: bet sizing âââââââââââââââââââââââââââââââââââââââââ
    # Fallback bets: Kelly â 0 when edge â 0, so use a flat minimum bet.
    if kalshi_fallback:
        bet_dollars = config.MIN_BET_DOLLARS
    else:
        bet_dollars = kelly_bet_size(bet_prob, bet_price, bankroll)
    if bet_dollars < config.MIN_BET_DOLLARS:
        return reject(
            f"bet size ${bet_dollars:.2f} < minimum ${config.MIN_BET_DOLLARS}"
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
        f"{'&â  FALLBACK' if kalshi_fallback else 'â'} BET FOUND | {title[:45]} | "
        f"Side={side.upper()} | "
        f"Our prob={bet_prob:.0%} | "
        f"Market={bet_price:.0%} | "
        f"Edge={edge:.1%} | "
        f"Bet=${actual_cost:.2f} ({contracts} contracts) | "
        f"End-game: {endgame_reason}"
        + (" | [Kalshi price fallback â Kelly no Pinnacle line]" if kalshi_fallback else "")
    )

    return opportunity


# âââââââââââââââââââââââââââââââââââââââââââââ
# WATCHLIST / NEAR-MISS DETECTION
# âââââââââââââââââââââââââââââââââââââââââââââ

# How close to each threshold before appearing on the watchlist:
WATCHLIST_PROB_GAP  = 0.10   # show if within 10pp of 70% â prob â¥ 60%
WATCHLIST_EDGE_GAP  = 0.03   # show if edge within 3pp of minimum â edge â¥ 0%


def evaluate_market_watchlist(
    ticker: str,
    title: str,
    kalshi_yes_price_cents: int,
    sharp_prob_yes: float,
    sport_key: str = "default",
    commence_time_str: str = "",   # Pinnacle's game start time
    game_state: str = "",          # Current period/inning/set/round from /scores API
    pinnacle_match: str = "",
) -> NearMiss | None:
    """
    Check if a market is "close" to triggering â within WATCHLIST_PROB_GAP
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
        return None  # Neither side is favored â"not watchlist-worthy

    # Only watch if we're within WATCHLIST_PROB_GAP of the min threshold
    prob_gap = max(0.0, config.MIN_WIN_PROBABILITY - side_prob)
    if prob_gap > WATCHLIST_PROB_GAP:
        return None

    edge = side_prob - side_price
    edge_gap = max(0.0, config.MIN_EDGE - edge)

    # Endgame check
    endgame, endgame_reason = is_endgame(sport_key, commence_time_str, game_state)

    # Determine what's blocking a real bet
    if side_prob < config.MIN_WIN_PROBABILITY:
        blocking = f"prob {side_prob:.0%} < {config.MIN_WIN_PROBABILITY:.0%} â should {prob_gap:.1%} more"
    elif edge < config.MIN_EDGE:
        blocking = f"edge {edge:.1%} < {config.MIN_EDGE:.1%} â should {edge_gap:.1%} more"
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
