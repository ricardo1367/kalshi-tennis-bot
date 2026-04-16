"""
odds_client.py — Fetches sharp moneyline odds from The Odds API.

We use Pinnacle as our reference because they:
  1. Accept winning bettors (no account restrictions)
  2. Have the sharpest, most efficient lines in the market
  3. Are widely used as the "true probability" benchmark

Sign up for a free API key at: https://the-odds-api.com
Free tier: 500 requests/month

MULTI-SPORT: This client covers every sport The Odds API supports —
NFL, NBA, MLB, NHL, soccer, tennis, golf, MMA, boxing, college sports, and more.
"""

import logging
import requests

import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# ALL SPORT KEYS supported by The Odds API
# Grouped by category for easy enabling/disabling
# ─────────────────────────────────────────────────────────────────────
SPORT_KEYS = {
    # ── American Football ──────────────────────────────────────────
    "americanfootball_nfl":                 "NFL",
    "americanfootball_nfl_super_bowl_winner":"NFL Super Bowl Futures",
    "americanfootball_ncaaf":               "NCAAF",
    "americanfootball_cfl":                 "CFL",

    # ── Basketball ─────────────────────────────────────────────────
    "basketball_nba":                       "NBA",
    "basketball_nba_championship_winner":   "NBA Futures",
    "basketball_ncaab":                     "NCAAB",
    "basketball_euroleague":                "EuroLeague",
    "basketball_wnba":                      "WNBA",

    # ── Baseball ───────────────────────────────────────────────────
    "baseball_mlb":                         "MLB",
    "baseball_mlb_world_series_winner":     "MLB World Series Futures",

    # ── Ice Hockey ─────────────────────────────────────────────────
    "icehockey_nhl":                        "NHL",
    "icehockey_nhl_championship_winner":    "NHL Futures",

    # ── Soccer ─────────────────────────────────────────────────────
    "soccer_epl":                           "English Premier League",
    "soccer_spain_la_liga":                 "La Liga",
    "soccer_germany_bundesliga":            "Bundesliga",
    "soccer_italy_serie_a":                 "Serie A",
    "soccer_france_ligue_one":              "Ligue 1",
    "soccer_uefa_champs_league":            "Champions League",
    "soccer_uefa_europa_league":            "Europa League",
    "soccer_usa_mls":                       "MLS",
    "soccer_conmebol_copa_libertadores":    "Copa Libertadores",
    "soccer_fifa_world_cup":                "FIFA World Cup",

    # ── Tennis ─────────────────────────────────────────────────────
    "tennis_atp":                           "ATP Tour",
    "tennis_wta":                           "WTA Tour",
    "tennis_atp_french_open":              "French Open (ATP)",
    "tennis_wta_french_open":              "French Open (WTA)",
    "tennis_atp_wimbledon":                "Wimbledon (ATP)",
    "tennis_wta_wimbledon":                "Wimbledon (WTA)",
    "tennis_atp_us_open":                  "US Open (ATP)",
    "tennis_wta_us_open":                  "US Open (WTA)",
    "tennis_atp_aus_open":                 "Australian Open (ATP)",
    "tennis_wta_aus_open":                 "Australian Open (WTA)",

    # ── Golf ───────────────────────────────────────────────────────
    "golf_masters_tournament_winner":       "The Masters",
    "golf_us_open_winner":                  "US Open Golf",
    "golf_the_open_championship_winner":    "The Open Championship",
    "golf_pga_championship_winner":         "PGA Championship",

    # ── MMA / Boxing ───────────────────────────────────────────────
    "mma_mixed_martial_arts":              "MMA",

    # ── Combat Sports ──────────────────────────────────────────────
    "boxing_boxing":                        "Boxing",

    # ── Cricket ────────────────────────────────────────────────────
    "cricket_icc_world_cup":               "ICC World Cup",
    "cricket_test_match":                  "Cricket Test Matches",

    # ── Rugby ──────────────────────────────────────────────────────
    "rugbyleague_nrl":                      "NRL Rugby League",
    "rugbyunion_premiership":              "Premiership Rugby",

    # ── Aussie Rules ───────────────────────────────────────────────
    "aussierules_afl":                      "AFL",
}


class OddsClient:
    def __init__(self):
        self.api_key = config.ODDS_API_KEY
        self.base_url = config.ODDS_API_BASE_URL
        self.bookmaker = config.REFERENCE_BOOKMAKER

    def _get(self, path: str, params: dict = None):
        params = params or {}
        params["apiKey"] = self.api_key
        resp = requests.get(self.base_url + path, params=params, timeout=10)
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining", "?")
        logger.debug(f"Odds API requests remaining: {remaining}")
        return resp.json()

    def get_active_sports(self) -> list[str]:
        """
        Ask The Odds API which sports currently have active/upcoming events.
        This is more efficient than polling every sport key blindly.
        """
        try:
            data = self._get("/sports", params={"all": "false"})
            active_keys = [s["key"] for s in data if s.get("active") or s.get("has_outrights")]
            logger.info(f"Active sports from Pinnacle: {len(active_keys)}")
            return active_keys
        except Exception as e:
            logger.warning(f"Could not fetch active sports list: {e}. Using full list.")
            return list(SPORT_KEYS.keys())

    def get_all_odds(self, sport_keys: list[str] = None) -> list[dict]:
        """
        Fetch live odds for all (or specified) sports.

        Returns a unified list of match dicts:
        {
            "home_team":      "Kansas City Chiefs",
            "away_team":      "San Francisco 49ers",
            "home_prob":      0.54,
            "away_prob":      0.46,
            "sport_key":      "americanfootball_nfl",
            "sport_label":    "NFL",
            "commence_time":  "2024-02-11T23:30:00Z",
        }
        """
        if sport_keys is None:
            sport_keys = self.get_active_sports()

        # Only pull sports we know about
        sport_keys = [k for k in sport_keys if k in SPORT_KEYS]

        all_matches = []
        for sport_key in sport_keys:
            try:
                data = self._get(f"/sports/{sport_key}/odds", params={
                    "regions": "us,eu",
                    "markets": "h2h",
                    "bookmakers": self.bookmaker,
                    "oddsFormat": "decimal",
                })
            except requests.exceptions.HTTPError as e:
                if e.response.status_code in (404, 422):
                    continue  # sport not active right now
                logger.warning(f"Error fetching {sport_key}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Error fetching {sport_key}: {e}")
                continue

            for event in data:
                match = self._parse_event(event, sport_key)
                if match:
                    all_matches.append(match)

        logger.info(f"Fetched {len(all_matches)} live matches across {len(sport_keys)} sports")
        return all_matches

    def _parse_event(self, event: dict, sport_key: str) -> dict | None:
        """
        Convert raw Odds API event → normalized match dict with clean probabilities.
        Removes bookmaker vig so probabilities sum to 1.0.
        """
        bookmakers = event.get("bookmakers", [])
        bm = next((b for b in bookmakers if b["key"] == self.bookmaker), None)
        if not bm:
            return None

        market = next((m for m in bm.get("markets", []) if m["key"] == "h2h"), None)
        if not market or len(market.get("outcomes", [])) < 2:
            return None

        outcomes = market["outcomes"]
        raw_probs = [1 / o["price"] for o in outcomes]
        total = sum(raw_probs)
        norm_probs = [p / total for p in raw_probs]

        return {
            "home_team":     outcomes[0]["name"],
            "away_team":     outcomes[1]["name"],
            "home_prob":     round(norm_probs[0], 4),
            "away_prob":     round(norm_probs[1], 4),
            "sport_key":     sport_key,
            "sport_label":   SPORT_KEYS.get(sport_key, sport_key),
            "commence_time": event.get("commence_time", ""),
            "event_id":      event.get("id", ""),
        }

    def find_team_in_matches(self, name: str, all_matches: list) -> dict | None:
        """
        Fuzzy-match a team or player name from a Kalshi title against
        the Pinnacle odds feed. Handles partial name matches like:
          "Chiefs" → "Kansas City Chiefs"
          "Djokovic" → "Novak Djokovic"
        """
        name_lower = name.lower().strip()
        name_parts = name_lower.split()

        for match in all_matches:
            home = match["home_team"].lower()
            away = match["away_team"].lower()

            # Direct substring match
            if name_lower in home or name_lower in away:
                side = "home" if name_lower in home else "away"
                return {"match": match, "side": side}

            # Partial word match (last name, city name, etc.)
            if any(part in home for part in name_parts if len(part) > 3):
                return {"match": match, "side": "home"}
            if any(part in away for part in name_parts if len(part) > 3):
                return {"match": match, "side": "away"}

        return None

    # Backwards compatibility alias for tennis-specific code
    def get_tennis_odds(self) -> list:
        tennis_keys = [k for k in SPORT_KEYS if k.startswith("tennis_")]
        return self.get_all_odds(sport_keys=tennis_keys)

    def find_match_for_player(self, player_name: str, all_matches: list) -> dict | None:
        result = self.find_team_in_matches(player_name, all_matches)
        if result:
            return {"match": result["match"], "player_side": result["side"]}
        return None

    def get_live_game_states(self, sport_keys: list[str]) -> dict[str, str]:
        """
        Fetch the current game state (period/inning/set/round) for in-progress events.

        Returns a dict mapping event_id -> description string, e.g.:
          {"abc123": "3rd Period", "def456": "8th Inning", "ghi789": "Set 3"}

        The Odds API /scores response has no top-level "description" field.
        The current period/inning/set lives in the "scores" array as the last
        entry's "name" field (e.g. "4th Quarter", "8th Inning", "Set 3").

        API cost: 1 request per sport_key. Call with only the sport_keys that
        appeared in today's live Pinnacle odds to minimise quota use.
        """
        states: dict[str, str] = {}
        for sport_key in sport_keys:
            if sport_key not in SPORT_KEYS:
                continue
            try:
                data = self._get(
                    f"/sports/{sport_key}/scores",
                    params={"daysFrom": "1"},
                )
            except requests.exceptions.HTTPError as e:
                if e.response.status_code in (404, 422):
                    continue  # sport has no scores available
                logger.debug(f"Scores fetch failed for {sport_key}: {e}")
                continue
            except Exception as e:
                logger.debug(f"Scores fetch failed for {sport_key}: {e}")
                continue

            for event in data:
                if event.get("completed"):
                    continue  # skip finished games
                event_id = event.get("id", "")
                # Current period/inning/set is the last entry's "name" in the scores array
                scores = event.get("scores") or []
                desc = scores[-1].get("name", "") if scores else ""
                if event_id and desc:
                    states[event_id] = desc

        logger.info(f"Live game states: {len(states)} in-progress events across {len(sport_keys)} sports")
        return states
