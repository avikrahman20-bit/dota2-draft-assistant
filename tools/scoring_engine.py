"""
Pure scoring logic for the Dota 2 draft assistant.
No I/O — takes data as arguments, returns a ranked list of candidates.

Usage:
  from tools.scoring_engine import score_candidates, DEFAULT_WEIGHTS
"""

import math as _math


DEFAULT_WEIGHTS = {
    "counter":  0.65,
    "win_rate": 0.15,
    "synergy":  0.20,
}

def get_win_rate(hero_id: int, hero_stats: dict, bracket: str = "7") -> float:
    """
    Return hero win rate from the given MMR bracket. Falls back to 0.5.
    Expects Stratz format: {hero_id_str: {bracket_enum: {wins, picks}}}
    bracket param is the UI value ("7"=Immortal … "1"=Herald).
    """
    from tools.fetch_hero_data import BRACKET_ENUM
    bracket_enum = BRACKET_ENUM.get(bracket, "IMMORTAL")
    stats = hero_stats.get(str(hero_id), {}).get(bracket_enum, {})
    picks = stats.get("picks", 0)
    wins  = stats.get("wins", 0)
    if picks == 0:
        return 0.5
    return wins / picks


def get_counter_score(
    candidate_id: int,
    enemy_ids: list[int],
    vs_matchups: dict[int, dict[int, dict]],
) -> tuple[float, list[dict]]:
    """
    Average win-rate advantage of the candidate against all enemy picks.
    Returns (score, detail_list) where detail_list has per-enemy breakdown.

    vs_matchups: {hero_id: {opponent_id: {"win_rate": float, "games": int}}}
    win_rate is the candidate hero's win rate against the opponent (0.0–1.0).
    win_rate > 0.5 → candidate counters that opponent.
    """
    if not enemy_ids:
        return 0.0, []

    candidate_matchups = vs_matchups.get(candidate_id, {})
    details = []
    advantages = []

    for enemy_id in enemy_ids:
        matchup = candidate_matchups.get(enemy_id)
        if matchup and matchup.get("games", 0) > 0:
            advantage = matchup["win_rate"] - 0.50
        else:
            advantage = 0.0
        advantages.append(advantage)
        details.append({"hero_id": enemy_id, "advantage": round(advantage, 4)})

    return sum(advantages) / len(advantages), details


def get_synergy_score(
    candidate_id: int,
    ally_ids: list[int],
    with_matchups: dict[int, dict[int, dict]],
) -> float:
    """
    Average win-rate advantage when candidate is on the same team as each ally.
    Uses real Stratz co-pick matchup data.

    with_matchups: {hero_id: {partner_id: {"win_rate": float, "games": int}}}
    win_rate is the candidate's win rate when paired WITH that ally (0.0–1.0).
    win_rate > 0.5 → they synergize well.
    """
    if not ally_ids:
        return 0.0

    candidate_with = with_matchups.get(candidate_id, {})
    advantages = []

    for ally_id in ally_ids:
        matchup = candidate_with.get(ally_id)
        if matchup and matchup.get("games", 0) > 0:
            advantage = matchup["win_rate"] - 0.50
        else:
            advantage = 0.0
        advantages.append(advantage)

    return sum(advantages) / len(advantages)


def score_candidates(
    candidate_ids: list[int],
    enemy_pick_ids: list[int],
    ally_pick_ids: list[int],
    all_matchups: dict,
    hero_stats: dict,
    heroes: dict,
    mmr_bracket: str = "7",
    weights: dict | None = None,
    top_n: int = 10,
) -> dict:
    """
    Score and rank hero candidates for the current draft state.

    Args:
        candidate_ids: Hero IDs not yet picked or banned.
        enemy_pick_ids: Enemy team's current picks.
        ally_pick_ids: Your team's current picks.
        all_matchups: {"vs": {hero_id: {opp_id: {win_rate, games}}},
                       "with": {hero_id: {ally_id: {win_rate, games}}}}
        hero_stats: {hero_id_str: {bracket_enum: {wins, picks}}}
        heroes: {hero_id_str: {name, localized_name, roles, img_url, ...}}
        weights: Override DEFAULT_WEIGHTS.
        top_n: Number of top results to include.

    Returns:
        {
          "top": list[dict],              # top_n heroes, sorted by score descending
          "all_scores": {str: float},     # hero_id (str) → total_score, all candidates
        }
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    vs_matchups   = all_matchups.get("vs", {})
    with_matchups = all_matchups.get("with", {})

    # --- Pass 1: compute raw components for all valid candidates ---
    raw = []
    for hero_id in candidate_ids:
        hero = heroes.get(str(hero_id))
        if not hero:
            continue
        win_rate = get_win_rate(hero_id, hero_stats, bracket=mmr_bracket)
        counter_score, counter_detail = get_counter_score(
            hero_id, enemy_pick_ids, vs_matchups
        )
        synergy_score = get_synergy_score(hero_id, ally_pick_ids, with_matchups)
        raw.append({
            "hero_id": hero_id,
            "hero": hero,
            "win_rate": win_rate,
            "counter_score": counter_score,
            "counter_detail": counter_detail,
            "synergy_score": synergy_score,
        })

    if not raw:
        return {"top": [], "all_scores": {}}

    # --- Normalize each component to [0, 1] relative to this candidate pool ---
    def _norm(values: list[float]) -> list[float]:
        mn, mx = min(values), max(values)
        if mx == mn:
            return [0.5] * len(values)
        span = mx - mn
        return [(v - mn) / span for v in values]

    counter_norms = _norm([r["counter_score"] for r in raw])
    wr_norms      = _norm([r["win_rate"] for r in raw])
    synergy_norms = _norm([r["synergy_score"] for r in raw])

    # --- Pass 2: apply weights to normalized scores ---
    results = []
    for i, r in enumerate(raw):
        total = (
            w["counter"]  * counter_norms[i]
            + w["win_rate"] * wr_norms[i]
            + w["synergy"]  * synergy_norms[i]
        )

        # Attach enemy hero names to counter detail
        detailed_counters = []
        for entry in r["counter_detail"]:
            enemy = heroes.get(str(entry["hero_id"]), {})
            detailed_counters.append(
                {
                    "vs_hero_id": entry["hero_id"],
                    "vs_hero": enemy.get("localized_name", str(entry["hero_id"])),
                    "advantage": entry["advantage"],
                }
            )
        detailed_counters.sort(key=lambda x: x["advantage"], reverse=True)

        results.append(
            {
                "hero_id": r["hero_id"],
                "localized_name": r["hero"].get("localized_name", ""),
                "name": r["hero"].get("name", ""),
                "img_url": r["hero"].get("img_url", ""),
                "roles": r["hero"].get("roles", []),
                "total_score": round(total, 4),
                "breakdown": {
                    "counter_score": round(r["counter_score"], 4),
                    "win_rate_score": round(wr_norms[i], 4),
                    "win_rate_pct": round(r["win_rate"] * 100, 1),
                    "synergy_score": round(synergy_norms[i], 4),
                    "counters_detail": detailed_counters,
                },
            }
        )

    results.sort(key=lambda x: x["total_score"], reverse=True)

    all_scores = {str(r["hero_id"]): r["total_score"] for r in results}

    return {"top": results[:top_n], "all_scores": all_scores}


def analyze_draft(
    radiant_ids: list[int],
    dire_ids: list[int],
    vs_matchups: dict,    # {hero_id: {opp_id: {win_rate, games}}}
    with_matchups: dict,  # {hero_id: {ally_id: {win_rate, games}}}
    hero_stats: dict,     # {hero_id_str: {bracket_enum: {wins, picks}}}
    heroes: dict,         # {hero_id_str: hero_dict}
    bracket: str = "7",
) -> dict:
    """
    Compute win probability for a completed 5v5 draft and explain the key factors.

    Win probability is based on:
      - Cross-team matchup win rates (60% weight): how Radiant heroes perform vs Dire heroes
      - Overall hero win rates (25% weight): each team's heroes' general strength
      - Intra-team synergy (15% weight): how well each team's heroes pair together

    Returns a dict with win probabilities, factor breakdown, and top matchups.
    """
    from tools.fetch_hero_data import BRACKET_ENUM
    bracket_enum = BRACKET_ENUM.get(bracket, "IMMORTAL")

    def hero_wr(hero_id: int) -> float:
        stats = hero_stats.get(str(hero_id), {}).get(bracket_enum, {})
        picks = stats.get("picks", 0)
        wins  = stats.get("wins", 0)
        return wins / picks if picks > 0 else 0.5

    def hname(hero_id: int) -> str:
        return heroes.get(str(hero_id), {}).get("localized_name", str(hero_id))

    def himg(hero_id: int) -> str:
        return heroes.get(str(hero_id), {}).get("img_url", "")

    # ── 1. Cross-team matchup analysis (5×5 = 25 pairs) ──────────────────────
    matchup_pairs = []
    for r_id in radiant_ids:
        for d_id in dire_ids:
            entry = vs_matchups.get(r_id, {}).get(d_id, {})
            wr    = entry.get("win_rate", 0.5)
            games = entry.get("games", 0)
            matchup_pairs.append({
                "radiant_id":   r_id,
                "dire_id":      d_id,
                "radiant_name": hname(r_id),
                "dire_name":    hname(d_id),
                "radiant_img":  himg(r_id),
                "dire_img":     himg(d_id),
                "win_rate":     round(wr, 4),   # Radiant hero's WR against Dire hero
                "advantage":    round(wr - 0.5, 4),
                "games":        games,
            })

    avg_matchup = (
        sum(p["advantage"] for p in matchup_pairs) / len(matchup_pairs)
        if matchup_pairs else 0.0
    )

    # ── 2. Intra-team synergy (C(5,2) = 10 pairs per team) ───────────────────
    def synergy_pairs(team_ids: list[int]) -> list[dict]:
        pairs = []
        for i, h1 in enumerate(team_ids):
            for h2 in team_ids[i + 1:]:
                entry = with_matchups.get(h1, {}).get(h2, {})
                wr    = entry.get("win_rate", 0.5)
                games = entry.get("games", 0)
                pairs.append({
                    "hero1_id":   h1,
                    "hero2_id":   h2,
                    "hero1_name": hname(h1),
                    "hero2_name": hname(h2),
                    "hero1_img":  himg(h1),
                    "hero2_img":  himg(h2),
                    "win_rate":   round(wr, 4),
                    "advantage":  round(wr - 0.5, 4),
                    "games":      games,
                })
        return pairs

    radiant_syn = synergy_pairs(radiant_ids)
    dire_syn    = synergy_pairs(dire_ids)
    radiant_avg_syn = sum(p["advantage"] for p in radiant_syn) / len(radiant_syn) if radiant_syn else 0.0
    dire_avg_syn    = sum(p["advantage"] for p in dire_syn)    / len(dire_syn)    if dire_syn    else 0.0
    synergy_diff = radiant_avg_syn - dire_avg_syn

    # ── 3. Base win rates ─────────────────────────────────────────────────────
    radiant_wrs = [hero_wr(h) for h in radiant_ids]
    dire_wrs    = [hero_wr(h) for h in dire_ids]
    radiant_avg_wr = sum(radiant_wrs) / len(radiant_wrs) if radiant_wrs else 0.5
    dire_avg_wr    = sum(dire_wrs)    / len(dire_wrs)    if dire_wrs    else 0.5
    wr_diff = radiant_avg_wr - dire_avg_wr

    # ── 4. Win probability ────────────────────────────────────────────────────
    raw      = 0.60 * avg_matchup + 0.25 * wr_diff + 0.15 * synergy_diff
    win_prob = 1.0 / (1.0 + _math.exp(-raw * 15)) * 100

    # ── 5. Sort for display ───────────────────────────────────────────────────
    matchup_pairs.sort(key=lambda x: x["advantage"], reverse=True)
    radiant_syn.sort(key=lambda x: x["advantage"], reverse=True)
    dire_syn.sort(key=lambda x: x["advantage"], reverse=True)

    # Best matchups for each side
    radiant_best_matchups = matchup_pairs[:3]           # most favourable for Radiant
    dire_best_matchups    = matchup_pairs[-3:][::-1]    # least favourable for Radiant = Dire's best

    return {
        "radiant_win_prob": round(win_prob, 1),
        "dire_win_prob":    round(100 - win_prob, 1),
        "components": {
            "matchup_adv": round(avg_matchup  * 100, 2),   # +ve = Radiant favoured
            "wr_adv":      round(wr_diff       * 100, 2),
            "synergy_adv": round(synergy_diff  * 100, 2),
        },
        "radiant_avg_wr": round(radiant_avg_wr * 100, 1),
        "dire_avg_wr":    round(dire_avg_wr    * 100, 1),
        "key_matchups": {
            "radiant_best": radiant_best_matchups,
            "dire_best":    dire_best_matchups,
        },
        "synergies": {
            "radiant_best": radiant_syn[:2],
            "dire_best":    dire_syn[:2],
        },
    }
