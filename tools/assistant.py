"""
Dota 2 Draft Assistant — LLM chat helper.
Builds context from live Stratz data + latest patch notes, then calls Claude.
"""

import os
from pathlib import Path

import anthropic

from tools.patch_notes import get_patch_notes
from tools.fetch_hero_data import BRACKET_ENUM as STATS_BRACKET_ENUM
from tools.fetch_matchups import BRACKET_ENUM as MATCHUP_BRACKET_ENUM

# Load API key from .env (already loaded by app.py via python-dotenv or os.environ)
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            env_path = Path(__file__).parent.parent / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
        _client = anthropic.Anthropic(api_key=key)
    return _client


def _hero_name(hero_id: int, heroes: dict) -> str:
    return heroes.get(str(hero_id), {}).get("localized_name", str(hero_id))


def _build_meta_context(
    heroes: dict,
    hero_stats: dict,
    matchups: dict,
    role_map: dict,
    bracket: str = "7",
) -> str:
    """
    Builds a compact summary of the current meta from Stratz live data:
    - Top 5 heroes per role by win rate at the selected bracket
    - Brief matchup quality notes for the top carry/mid/offlane
    """
    stats_bracket = STATS_BRACKET_ENUM.get(bracket, "IMMORTAL")

    # Collect win rate + pick count per hero
    hero_data = {}
    for hid_str, stat in hero_stats.items():
        bdata = stat.get(stats_bracket, {})
        picks = bdata.get("picks", 0)
        wins  = bdata.get("wins",  0)
        if picks >= 500:   # require meaningful sample
            hero_data[int(hid_str)] = {"wr": wins / picks, "picks": picks}

    # Find max picks across all heroes for normalisation
    max_picks = max((v["picks"] for v in hero_data.values()), default=1)

    # Meta score = win_rate * (picks / max_picks)^0.3
    # This rewards heroes that are both strong AND frequently picked
    import math
    def meta_score(d):
        return d["wr"] * (d["picks"] / max_picks) ** 0.3

    lines = [f"=== Live Meta Data (Stratz, bracket={stats_bracket}) ===\n"]
    roles = ["carry", "mid", "offlane", "support", "hard_support"]
    for role in roles:
        role_ids = set(role_map.get(role, []))
        if not role_ids:
            continue
        ranked = sorted(
            [(hid, d) for hid, d in hero_data.items() if hid in role_ids],
            key=lambda x: meta_score(x[1]), reverse=True
        )[:7]
        if not ranked:
            continue
        lines.append(f"{role.upper().replace('_', ' ')}:")
        for hid, d in ranked:
            name = _hero_name(hid, heroes)
            lines.append(f"  • {name}: {d['wr']*100:.1f}% WR ({d['picks']:,} games)")
        lines.append("")

    return "\n".join(lines)


def _build_draft_context(
    radiant_ids: list[int],
    dire_ids: list[int],
    heroes: dict,
    matchups: dict,
    bracket: str,
) -> str:
    """Summarizes the current draft state for the LLM."""
    if not radiant_ids and not dire_ids:
        return ""

    matchup_bracket = MATCHUP_BRACKET_ENUM.get(bracket, "DIVINE_IMMORTAL")
    vs_data = matchups.get("vs", {}).get(matchup_bracket, {})

    def names(ids): return ", ".join(_hero_name(i, heroes) for i in ids) or "none"

    lines = ["=== Current Draft ==="]
    lines.append(f"Radiant: {names(radiant_ids)}")
    lines.append(f"Dire:    {names(dire_ids)}")

    # Top counter matchups
    if radiant_ids and dire_ids:
        pairs = []
        for r in radiant_ids:
            for d in dire_ids:
                wr = vs_data.get(r, {}).get(d, {}).get("win_rate")
                if wr:
                    pairs.append((r, d, wr))
        pairs.sort(key=lambda x: abs(x[2] - 0.5), reverse=True)
        if pairs:
            lines.append("\nKey matchups (most decisive):")
            for r, d, wr in pairs[:4]:
                rn, dn = _hero_name(r, heroes), _hero_name(d, heroes)
                adv = "Radiant" if wr > 0.5 else "Dire"
                pct = wr * 100 if wr > 0.5 else (1 - wr) * 100
                lines.append(f"  • {rn} vs {dn}: {adv} favored ({pct:.1f}%)")

    return "\n".join(lines)


PLAYSTYLE_DESCRIPTIONS = {
    "aggressive": "Prefers aggressive, high-kill playstyle",
    "tempo": "Plays tempo — wants to hit power spikes and push advantages fast",
    "teamfight": "Loves teamfight heroes with big ultimates",
    "farming": "Prefers farming-oriented, scaling heroes",
    "lane_domination": "Wants to win the lane hard and snowball from there",
    "control": "Prefers control/utility heroes that set up plays",
    "split_push": "Likes split-push and map pressure over grouping",
    "ganking": "Prefers ganking and roaming, making plays around the map",
    "late_game": "Prefers hard carry / late game insurance",
    "early_pressure": "Wants to end games early with aggressive timings",
}


def _build_user_context(user_profile: dict | None, heroes: dict) -> str:
    """Build personalized context from the user's profile, playstyle, and Stratz stats."""
    if not user_profile:
        return ""

    lines = [f"=== Player Profile: {user_profile.get('username', 'Unknown')} ==="]

    # Roles
    roles = user_profile.get("preferred_roles", [])
    if roles:
        lines.append(f"Preferred roles: {', '.join(roles)}")

    # Hero pool
    pool = user_profile.get("hero_pool", [])
    if pool:
        pool_names = [_hero_name(hid, heroes) for hid in pool]
        lines.append(f"Hero pool: {', '.join(pool_names)}")

    # Playstyle tags (structured)
    tags = user_profile.get("playstyle_tags", [])
    if tags:
        descs = [PLAYSTYLE_DESCRIPTIONS.get(t, t) for t in tags]
        lines.append(f"Playstyle: {'; '.join(descs)}")

    # Free-text notes
    notes = user_profile.get("playstyle_notes", "")
    if notes:
        lines.append(f"Extra notes: {notes}")

    # Stratz player stats (real match data)
    stats = user_profile.get("player_stats", {})
    if stats and stats.get("top_heroes"):
        rank = stats.get("rank", "")
        wr = stats.get("overall_wr", 0)
        total = stats.get("total_matches", 0)
        lines.append(f"\nStratz verified stats: {rank} | {wr}% overall WR | {total:,} matches")
        lines.append("Most played heroes (recent):")
        for h in stats["top_heroes"][:8]:
            lines.append(f"  • {h['hero_name']}: {h['win_rate']}% WR over {h['matches']} games")

    # Feedback history
    feedback = user_profile.get("recent_feedback", [])
    if feedback:
        lines.append("\nRecent feedback from this player:")
        for fb in feedback[:5]:
            hero_note = ""
            if fb.get("hero_id"):
                hero_note = f" (about {_hero_name(fb['hero_id'], heroes)})"
            lines.append(f"  • {fb['feedback']}{hero_note}")

    lines.append("")
    return "\n".join(lines)


def answer(
    question: str,
    heroes: dict,
    hero_stats: dict,
    matchups: dict,
    role_map: dict,
    radiant_ids: list[int] | None = None,
    dire_ids: list[int] | None = None,
    bracket: str = "7",
    conversation_history: list[dict] | None = None,
    user_profile: dict | None = None,
) -> str:
    """
    Main entry point. Returns Claude's answer as a string.
    conversation_history: list of {"role": "user"|"assistant", "content": str}
    user_profile: optional dict with player's preferences, hero pool, feedback
    """
    patch_notes = get_patch_notes()
    meta_ctx    = _build_meta_context(heroes, hero_stats, matchups, role_map, bracket)
    draft_ctx   = _build_draft_context(
        radiant_ids or [], dire_ids or [], heroes, matchups, bracket
    )
    user_ctx    = _build_user_context(user_profile, heroes)

    personalization = ""
    if user_ctx:
        personalization = f"""
The user is logged in and has a saved profile. Tailor your advice to their hero pool and role preferences.
If they ask what to pick, prioritize heroes from their pool that fit the draft.
If their pool heroes are bad here, say so honestly and suggest alternatives.

{user_ctx}
"""

    system_prompt = f"""You are a Dota 2 draft assistant embedded in a real-time drafting tool.
You have access to live Stratz data (win rates, matchup scores, synergy co-pick data) at the selected MMR bracket, and the latest official patch notes.

Answer questions about hero picks, counters, synergies, meta trends, and draft strategy.
Be concise — the user is in a draft with limited time. Use bullet points for lists.
When citing win rates or matchup data, pull from the context below, not from memory.
{personalization}
{meta_ctx}

{f"Latest patch notes:{chr(10)}{patch_notes}" if patch_notes else ""}

{draft_ctx}
"""

    messages = list(conversation_history or [])
    messages.append({"role": "user", "content": question})

    client = _get_client()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=system_prompt,
        messages=messages,
    )

    return resp.content[0].text
