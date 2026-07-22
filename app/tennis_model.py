"""Independent in-play tennis win-probability model.

This is a genuine *second opinion* for tennis moneylines: it derives a win
probability from the live match score, anchored to a pre-match prior, and is
independent of the current live price. The edge it produces (model minus
executable price) is therefore real, unlike a single-source gross gap.

Data reality (Polymarket sports feed): we receive per-set game scores and the
current set/tiebreak, but NOT point scores or the server. So this is a
serve-neutral, games/sets-level model. Documented simplifications:

* Each game is an i.i.d. Bernoulli(``g``) for the anchored favorite; there is
  no server alternation because the feed does not expose it.
* A set is first-to-six, win-by-two, with a tiebreak at 6-6 approximated as one
  more Bernoulli(``g``) game.
* Sets are independent; ``g`` is constant across the match.
* Best-of-three by default (tour/Challenger/WTA); pass ``best_of=5`` for
  men's Grand Slam main draws.

These are the usual coarse-model assumptions. The output is a display-grade
independent estimate for the paper harness, not a validated calibration
artifact.
"""
from __future__ import annotations

from functools import lru_cache


def _sets_to_win(best_of: int) -> int:
    return best_of // 2 + 1


def set_complete(home_games: int, away_games: int) -> bool:
    """True when a set score is final (6-4, 7-5, or 7-6 tiebreak)."""
    high, low = max(home_games, away_games), min(home_games, away_games)
    if high >= 6 and high - low >= 2:
        return True
    return high == 7 and low == 6


@lru_cache(maxsize=None)
def _set_win_prob(home_games: int, away_games: int, g: float) -> float:
    """P(favorite wins the current set) from games ``home-away``.

    ``g`` is the per-game win probability (a float; ``lru_cache`` keys on the
    exact value). Tiebreak at 6-6 is treated as one Bernoulli(g) game.
    """
    if home_games >= 6 and home_games - away_games >= 2:
        return 1.0
    if away_games >= 6 and away_games - home_games >= 2:
        return 0.0
    if home_games == 7 and away_games == 6:
        return 1.0
    if away_games == 7 and home_games == 6:
        return 0.0
    if home_games == 6 and away_games == 6:
        return g  # tiebreak approximated as a single Bernoulli(g) game
    return (g * _set_win_prob(home_games + 1, away_games, g)
            + (1.0 - g) * _set_win_prob(home_games, away_games + 1, g))


@lru_cache(maxsize=None)
def _race_prob(sets_home: int, sets_away: int, set_prob: float, need: int) -> float:
    """P(favorite reaches ``need`` sets first) with every remaining set won at a
    fixed per-set win probability ``set_prob``."""
    if sets_home >= need:
        return 1.0
    if sets_away >= need:
        return 0.0
    return (set_prob * _race_prob(sets_home + 1, sets_away, set_prob, need)
            + (1.0 - set_prob) * _race_prob(sets_home, sets_away + 1, set_prob, need))


def match_win_prob(sets_home: int, sets_away: int, cur_home_games: int,
                   cur_away_games: int, g: float, *, best_of: int = 3) -> float:
    """P(favorite wins the match) from full live state and per-game prob ``g``.

    ``g`` is the favorite's serve-neutral game-win probability. The current set
    is resolved from its game score; all subsequent sets start level (0-0).
    """
    g = min(max(g, 1e-6), 1.0 - 1e-6)
    need = _sets_to_win(best_of)
    if sets_home >= need:
        return 1.0
    if sets_away >= need:
        return 0.0
    cur_set = _set_win_prob(cur_home_games, cur_away_games, g)
    set0 = _set_win_prob(0, 0, g)
    win_cur = cur_set * _race_prob(sets_home + 1, sets_away, set0, need)
    lose_cur = (1.0 - cur_set) * _race_prob(sets_home, sets_away + 1, set0, need)
    return win_cur + lose_cur


def game_prob_from_prematch(p0: float, *, best_of: int = 3) -> float:
    """Invert a pre-match match-win probability ``p0`` into a per-game prob ``g``.

    Match win probability at 0-0 is strictly increasing in ``g``, so a bisection
    recovers the ``g`` that reproduces the market's pre-match assessment. That
    ``g`` is then propagated through the live score by :func:`match_win_prob`.
    """
    p0 = min(max(p0, 1e-6), 1.0 - 1e-6)
    low, high = 0.0, 1.0
    for _ in range(60):
        mid = (low + high) / 2.0
        if match_win_prob(0, 0, 0, 0, mid, best_of=best_of) < p0:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def parse_tennis_score(score: str, period: str) -> tuple[int, int, int, int] | None:
    """Parse the feed's tennis score into ``(sets_home, sets_away, cur_home_games,
    cur_away_games)``.

    Accepts strings like ``"6-3, 3-3"`` (completed set 1, current set 2) or
    ``"1-1"``. Home is listed first in every pair. Returns ``None`` when the
    string is not a recognizable set-by-set tennis score. During a tiebreak
    (``period`` starting with ``"TB"``) the current set is pinned to 6-6, since
    the feed then reports tiebreak points rather than games.
    """
    pairs: list[tuple[int, int]] = []
    for chunk in score.split(","):
        parts = chunk.strip().split("-")
        if len(parts) != 2:
            return None
        try:
            pairs.append((int(parts[0]), int(parts[1])))
        except ValueError:
            return None
    if not pairs:
        return None

    sets_home = sets_away = 0
    cur_home = cur_away = 0
    for index, (home, away) in enumerate(pairs):
        is_last = index == len(pairs) - 1
        if set_complete(home, away):
            if home > away:
                sets_home += 1
            else:
                sets_away += 1
            if is_last:
                cur_home = cur_away = 0
        elif is_last:
            cur_home, cur_away = home, away
        else:
            # A non-final, non-last pair is malformed for tennis.
            return None

    if period.strip().upper().startswith("TB"):
        cur_home = cur_away = 6
    return sets_home, sets_away, cur_home, cur_away
