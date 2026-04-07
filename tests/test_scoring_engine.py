"""
Unit tests for tools/scoring_engine.py.
Run: python -m pytest tests/ -v
"""
import sys
from pathlib import Path

# Make tools importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

# Patch out the fetch_hero_data import inside scoring_engine so tests don't
# require the .tmp cache files to exist.
import types
_fake_fhd = types.ModuleType("tools.fetch_hero_data")
_fake_fhd.BRACKET_ENUM = {"7": "IMMORTAL", "6": "DIVINE", "1": "HERALD"}
sys.modules.setdefault("tools.fetch_hero_data", _fake_fhd)

# Provide a minimal tools.utils stub if not already importable
import importlib
try:
    importlib.import_module("tools.utils")
except ModuleNotFoundError:
    _fake_utils = types.ModuleType("tools.utils")
    _fake_utils.hero_name = lambda hero_id, heroes: heroes.get(str(hero_id), {}).get("localized_name", str(hero_id))
    sys.modules["tools.utils"] = _fake_utils

from tools.scoring_engine import (
    _shrunk_advantage,
    _role_probabilities,
    get_counter_score,
    get_synergy_score,
    get_win_rate,
    score_candidates,
    DEFAULT_WEIGHTS,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _hero(hero_id: int, name: str) -> dict:
    return {"localized_name": name, "name": name.lower(), "img_url": "", "roles": []}


HEROES = {
    "1": _hero(1, "Anti-Mage"),
    "2": _hero(2, "Axe"),
    "3": _hero(3, "Bane"),
    "4": _hero(4, "Bloodseeker"),
    "5": _hero(5, "Crystal Maiden"),
}

HERO_STATS = {
    "1": {"IMMORTAL": {"wins": 550, "picks": 1000}},  # 55% WR
    "2": {"IMMORTAL": {"wins": 480, "picks": 1000}},  # 48% WR
    "3": {"IMMORTAL": {"wins": 500, "picks": 1000}},  # 50% WR
    "4": {"IMMORTAL": {"wins": 0, "picks": 0}},        # no data → 0.5
    "5": {"IMMORTAL": {"wins": 600, "picks": 1000}},  # 60% WR
}


def _matchups(hero_id: int, opponents: dict[int, tuple[float, int]]) -> dict:
    """Build a vs/with matchup dict: {hero_id: {opp_id: {win_rate, games}}}"""
    return {hero_id: {opp: {"win_rate": wr, "games": g} for opp, (wr, g) in opponents.items()}}


# ── _shrunk_advantage ────────────────────────────────────────────────────────

def test_shrunk_advantage_zero_games():
    assert _shrunk_advantage({}) == 0.0
    assert _shrunk_advantage({"win_rate": 0.7, "games": 0}) == 0.0


def test_shrunk_advantage_large_sample_near_raw():
    # With 10_000 games, shrinkage is tiny; result ≈ win_rate - 0.5
    result = _shrunk_advantage({"win_rate": 0.60, "games": 10_000})
    assert abs(result - 0.10) < 0.005


def test_shrunk_advantage_small_sample_near_zero():
    # With only 10 games the estimate regresses almost fully to prior
    result = _shrunk_advantage({"win_rate": 0.80, "games": 10})
    assert 0.0 < result < 0.01


def test_shrunk_advantage_neutral():
    # 50% win rate → 0 advantage regardless of sample
    result = _shrunk_advantage({"win_rate": 0.50, "games": 1000})
    assert abs(result) < 1e-9


# ── _role_probabilities ──────────────────────────────────────────────────────

def test_role_probabilities_unknown_hero():
    probs = _role_probabilities(9999, {})
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    for v in probs.values():
        assert abs(v - 0.2) < 1e-9


def test_role_probabilities_single_role():
    role_map = {"carry": [1, 2, 3], "mid": [4, 5]}
    probs = _role_probabilities(1, role_map)
    assert probs["carry"] == 1.0
    assert probs["mid"] == 0.0


def test_role_probabilities_two_roles():
    role_map = {"carry": [1], "mid": [1]}
    probs = _role_probabilities(1, role_map)
    assert abs(probs["carry"] - 0.5) < 1e-9
    assert abs(probs["mid"] - 0.5) < 1e-9


# ── get_win_rate ─────────────────────────────────────────────────────────────

def test_get_win_rate_normal():
    assert abs(get_win_rate(1, HERO_STATS, "7") - 0.55) < 1e-9


def test_get_win_rate_no_data():
    assert get_win_rate(4, HERO_STATS, "7") == 0.5


def test_get_win_rate_missing_hero():
    assert get_win_rate(999, HERO_STATS, "7") == 0.5


# ── get_counter_score ────────────────────────────────────────────────────────

def test_get_counter_score_no_enemies():
    score, detail = get_counter_score(1, [], {})
    assert score == 0.0
    assert detail == []


def test_get_counter_score_no_matchup_data():
    # No data → _shrunk_advantage returns 0 for every enemy
    score, detail = get_counter_score(1, [2, 3], {})
    assert score == 0.0


def test_get_counter_score_strong_counter():
    # Hero 1 dominates hero 2 with many games
    vs = _matchups(1, {2: (0.65, 5000)})
    score, _ = get_counter_score(1, [2], vs)
    assert score > 0.05  # positive advantage


def test_get_counter_score_countered():
    vs = _matchups(1, {2: (0.35, 5000)})
    score, _ = get_counter_score(1, [2], vs)
    assert score < -0.05  # negative advantage (bad pick)


# ── get_synergy_score ────────────────────────────────────────────────────────

def test_get_synergy_score_no_allies():
    assert get_synergy_score(1, [], {}) == 0.0


def test_get_synergy_score_positive():
    with_m = _matchups(1, {2: (0.58, 2000)})
    score = get_synergy_score(1, [2], with_m)
    assert score > 0.0


# ── score_candidates ─────────────────────────────────────────────────────────

def _empty_matchups():
    return {"vs": {}, "with": {}}


def test_score_candidates_empty():
    result = score_candidates([], [], [], _empty_matchups(), {}, {})
    assert result == {"top": [], "all_scores": {}}


def test_score_candidates_returns_top_n():
    candidates = [1, 2, 3, 4, 5]
    result = score_candidates(
        candidates, [], [], _empty_matchups(), HERO_STATS, HEROES, top_n=2
    )
    assert len(result["top"]) == 2


def test_score_candidates_all_scores_populated():
    candidates = [1, 2, 3]
    result = score_candidates(
        candidates, [], [], _empty_matchups(), HERO_STATS, HEROES
    )
    assert set(result["all_scores"].keys()) == {"1", "2", "3"}


def test_score_candidates_sorted_descending():
    candidates = [1, 2, 3, 4, 5]
    result = score_candidates(
        candidates, [], [], _empty_matchups(), HERO_STATS, HEROES
    )
    scores = [r["total_score"] for r in result["top"]]
    assert scores == sorted(scores, reverse=True)


def test_score_candidates_hero_in_pool_boosted():
    """A hero in the user's pool should score higher than an identical hero not in pool."""
    # Use two heroes with the same stats; only hero 1 is in pool
    candidates = [1, 2]
    same_stats = {
        "1": {"IMMORTAL": {"wins": 500, "picks": 1000}},
        "2": {"IMMORTAL": {"wins": 500, "picks": 1000}},
    }
    result = score_candidates(
        candidates, [], [], _empty_matchups(), same_stats, HEROES, hero_pool=[1]
    )
    scores = {r["hero_id"]: r["total_score"] for r in result["top"]}
    assert scores[1] > scores[2]


def test_score_candidates_counter_weight_applied():
    """Hero that hard-counters the enemy should rank higher than one that doesn't."""
    vs = {
        1: {10: {"win_rate": 0.65, "games": 3000}},  # hero 1 counters enemy 10
        2: {10: {"win_rate": 0.45, "games": 3000}},  # hero 2 countered by enemy 10
    }
    matchups = {"vs": vs, "with": {}}
    same_stats = {
        "1": {"IMMORTAL": {"wins": 500, "picks": 1000}},
        "2": {"IMMORTAL": {"wins": 500, "picks": 1000}},
    }
    result = score_candidates(
        [1, 2], [10], [], matchups, same_stats, HEROES
    )
    scores = {r["hero_id"]: r["total_score"] for r in result["top"]}
    assert scores[1] > scores[2]


def test_score_candidates_missing_hero_skipped():
    """Candidates with no entry in heroes dict should be silently skipped."""
    result = score_candidates(
        [999, 1], [], [], _empty_matchups(), HERO_STATS, HEROES
    )
    hero_ids = [r["hero_id"] for r in result["top"]]
    assert 999 not in hero_ids
    assert 1 in hero_ids
