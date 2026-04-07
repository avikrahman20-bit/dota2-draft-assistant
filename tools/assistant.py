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
from tools.utils import hero_name as _hero_name_util

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
    return _hero_name_util(hero_id, heroes)


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


def _fmt_counter(c: dict) -> str:
    """Format a counter detail entry with win rate and game count when available."""
    name = c.get("vs_hero", "?")
    wr = c.get("win_rate")
    games = c.get("games", 0)
    adv = c.get("advantage", 0)
    if wr is not None and games > 0:
        return f"{name} {wr*100:.1f}%WR({games:,}g, adv {adv:+.2f})"
    return f"{name}(adv {adv:+.2f})"


def _build_draft_matchup_grid(
    enemy_ids: list[int],
    heroes: dict,
    matchups: dict,
    bracket: str,
    hero_pool: list[int] | None = None,
    recommendations: list[dict] | None = None,
) -> str:
    """
    Build a matchup grid: every hero with data vs the current enemy picks.
    This lets the AI answer "how does X do vs Y?" for any hero.
    """
    if not enemy_ids:
        return ""

    matchup_bracket = MATCHUP_BRACKET_ENUM.get(bracket, "DIVINE_IMMORTAL")
    vs_data = matchups.get("vs", {}).get(matchup_bracket, {})
    if not vs_data:
        return ""

    enemy_set = set(enemy_ids)
    pool_set = set(hero_pool or [])

    # Include every hero that has matchup data vs at least one enemy
    lines = ["\n=== Matchup Grid (all heroes vs enemy picks) ==="]
    for hid in sorted(vs_data.keys()):
        if hid in enemy_set:
            continue
        hid_data = vs_data.get(hid, {})
        matchup_strs = []
        has_data = False
        for eid in enemy_ids:
            entry = hid_data.get(eid, {})
            wr = entry.get("win_rate")
            games = entry.get("games", 0)
            ename = _hero_name(eid, heroes)
            if wr is not None and games > 0:
                matchup_strs.append(f"vs {ename}: {wr*100:.1f}%({games}g)")
                has_data = True
            else:
                matchup_strs.append(f"vs {ename}: -")
        if has_data:
            hname = _hero_name(hid, heroes)
            pool_tag = " [POOL]" if hid in pool_set else ""
            lines.append(f"  {hname}{pool_tag}: {' | '.join(matchup_strs)}")

    return "\n".join(lines)


def _build_draft_context(
    radiant_ids: list[int],
    dire_ids: list[int],
    heroes: dict,
    matchups: dict,
    bracket: str,
    recommendations: list[dict] | None = None,
    my_team: str = "radiant",
    hero_pool: list[int] | None = None,
) -> str:
    """Summarizes the current draft state + scoring engine recommendations for the LLM."""
    if not radiant_ids and not dire_ids:
        return ""

    matchup_bracket = MATCHUP_BRACKET_ENUM.get(bracket, "DIVINE_IMMORTAL")
    vs_data = matchups.get("vs", {}).get(matchup_bracket, {})

    def names(ids): return ", ".join(_hero_name(i, heroes) for i in ids) or "none"

    ally_ids = radiant_ids if my_team == "radiant" else dire_ids
    enemy_ids = dire_ids if my_team == "radiant" else radiant_ids

    lines = ["=== Current Draft ==="]
    lines.append(f"User's team ({my_team.title()}): {names(ally_ids)}")
    lines.append(f"Enemy team: {names(enemy_ids)}")

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

    # Scoring engine recommendations (same data as the on-screen panel)
    if recommendations:
        lines.append("\n=== Top Recommendations (from scoring engine — matches the on-screen panel) ===")
        lines.append("These are scored by: counter matchups (55%), synergy (20%), win rate (15%), hero pool (10%)")
        for i, rec in enumerate(recommendations[:10], 1):
            name = rec.get("localized_name", "?")
            score = rec.get("total_score", 0)
            bd = rec.get("breakdown", {})
            wr = bd.get("win_rate_pct", 50)
            pool = " [IN HERO POOL]" if rec.get("in_hero_pool") else ""
            # Show counter detail if available
            counters = bd.get("counters_detail", [])
            counter_info = ""
            if counters:
                best = [c for c in counters if c["advantage"] > 0][:2]
                worst = [c for c in counters if c["advantage"] < 0][:1]
                if best:
                    counter_info += " | Good vs: " + ", ".join(
                        _fmt_counter(c) for c in best
                    )
                if worst:
                    counter_info += " | Weak vs: " + ", ".join(
                        _fmt_counter(c) for c in worst
                    )
            lines.append(f"  {i}. {name} (score={score:.3f}, WR={wr}%{pool}{counter_info})")

    # Matchup grid: hero pool + recommendations vs all enemy picks
    grid = _build_draft_matchup_grid(
        enemy_ids, heroes, matchups, bracket,
        hero_pool=hero_pool, recommendations=recommendations,
    )
    if grid:
        lines.append(grid)

    return "\n".join(lines)


def _build_matchup_lookup(
    heroes: dict,
    hero_stats: dict,
    matchups: dict,
    role_map: dict,
    bracket: str = "7",
) -> str:
    """
    Pre-compute a matchup reference table: for each role, which heroes best
    counter the top-WR meta carries/mids/offlaners.  This lets the LLM answer
    "what counters X?" even when no draft is active.
    """
    stats_bracket = STATS_BRACKET_ENUM.get(bracket, "IMMORTAL")
    matchup_bracket = MATCHUP_BRACKET_ENUM.get(bracket, "DIVINE_IMMORTAL")
    vs_data = matchups.get("vs", {}).get(matchup_bracket, {})
    if not vs_data:
        return ""

    # Find top-WR heroes in core roles (the threats people ask about)
    threat_roles = ["carry", "mid", "offlane"]
    threats = []  # (hero_id, hero_name, wr, role)
    for role in threat_roles:
        role_ids = set(role_map.get(role, []))
        for hid_str, stat in hero_stats.items():
            hid = int(hid_str)
            if hid not in role_ids:
                continue
            bdata = stat.get(stats_bracket, {})
            picks = bdata.get("picks", 0)
            wins = bdata.get("wins", 0)
            if picks >= 500:
                threats.append((hid, _hero_name(hid, heroes), wins / picks, role))
    # Take top 5 threats by WR
    threats.sort(key=lambda x: x[2], reverse=True)
    threats = threats[:5]

    if not threats:
        return ""

    threat_ids = [t[0] for t in threats]
    lines = ["=== Matchup Reference (top meta threats vs all roles) ==="]
    lines.append("Threats: " + ", ".join(
        f"{t[1]} ({t[3]}, {t[2]*100:.1f}% WR)" for t in threats
    ))

    # For each role, find top counters against these threats
    counter_roles = ["carry", "mid", "offlane", "support", "hard_support"]
    for role in counter_roles:
        role_ids = set(role_map.get(role, []))
        if not role_ids:
            continue
        # Score each hero in this role by average advantage vs threats
        hero_scores = []
        for hid in role_ids:
            hid_data = vs_data.get(hid, {})
            if not hid_data:
                continue
            advantages = []
            detail = []
            for tid, tname, twr, trole in threats:
                entry = hid_data.get(tid, {})
                wr = entry.get("win_rate")
                games = entry.get("games", 0)
                if wr is not None and games >= 100:
                    adv = wr - 0.5
                    advantages.append(adv)
                    detail.append((tname, wr, games))
            if len(advantages) >= 2:  # need data vs at least 2 threats
                avg_adv = sum(advantages) / len(advantages)
                hero_scores.append((hid, avg_adv, detail))

        hero_scores.sort(key=lambda x: x[1], reverse=True)
        top = hero_scores[:5]
        if not top:
            continue

        lines.append(f"\n{role.upper().replace('_', ' ')} counters:")
        for hid, avg_adv, detail in top:
            name = _hero_name(hid, heroes)
            matchup_strs = [f"{tn} {wr*100:.1f}%({g}g)" for tn, wr, g in detail]
            lines.append(
                f"  • {name} (avg +{avg_adv*100:.1f}%): vs {', '.join(matchup_strs)}"
            )

    lines.append("")
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

    # Recent match history
    recent = stats.get("recent_matches", [])
    if recent:
        lines.append(f"\n=== Recent Match History (last {len(recent)} games) ===")
        wins = sum(1 for m in recent if m.get("won"))
        lines.append(f"Record: {wins}W-{len(recent) - wins}L")
        for m in recent:
            result = "W" if m.get("won") else "L"
            kda = f"{m.get('kills', 0)}/{m.get('deaths', 0)}/{m.get('assists', 0)}"
            imp = m.get("imp")
            imp_str = f" | IMP:{imp}" if imp is not None else ""
            award = f" [{m['award']}]" if m.get("award") else ""
            role = m.get("role", "")
            dur = m.get("duration_min", 0)
            enemies = m.get("enemy_heroes", [])
            enemy_str = ""
            if enemies:
                enemy_str = " | vs: " + ", ".join(
                    f"{e['hero_name']}({e['role']})" for e in enemies
                )
            lines.append(
                f"  {result} {m.get('hero_name', '?')} ({role}) — "
                f"{kda} KDA | {m.get('networth', 0):,} NW | {dur:.0f}min{imp_str}{award}{enemy_str}"
            )

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
    result = "\n".join(lines)
    # Cap user context to ~4000 chars to avoid bloating the system prompt
    if len(result) > 4000:
        result = result[:3950] + "\n...[profile truncated]"
    return result


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
    recommendations: list[dict] | None = None,
    my_team: str = "radiant",
) -> str:
    """
    Main entry point. Returns Claude's answer as a string.
    conversation_history: list of {"role": "user"|"assistant", "content": str}
    user_profile: optional dict with player's preferences, hero pool, feedback
    recommendations: top scored heroes from the scoring engine (same as on-screen panel)
    my_team: which side the user is on ("radiant" or "dire")
    """
    patch_notes = get_patch_notes()
    meta_ctx    = _build_meta_context(heroes, hero_stats, matchups, role_map, bracket)
    hero_pool = (user_profile or {}).get("hero_pool", [])
    draft_ctx   = _build_draft_context(
        radiant_ids or [], dire_ids or [], heroes, matchups, bracket,
        recommendations=recommendations,
        my_team=my_team,
        hero_pool=hero_pool,
    )
    user_ctx    = _build_user_context(user_profile, heroes)
    matchup_ref = _build_matchup_lookup(heroes, hero_stats, matchups, role_map, bracket)

    personalization = ""
    if user_ctx:
        personalization = f"""
The user is logged in and has a saved profile. Tailor your advice to their hero pool and role preferences.
If they ask what to pick, prioritize heroes from their pool that fit the draft.
If their pool heroes are bad here, say so honestly and suggest alternatives.

{user_ctx}
"""

    # -- Build system prompt as content blocks for prompt caching --
    # Static block: instructions + personalization + meta + patch notes + matchup ref
    # These don't change between turns in a conversation, so they get cached.
    static_parts = f"""You are a Dota 2 draft assistant embedded in a real-time drafting tool.

DATA YOU HAVE (use it, don't say you don't have it):
- Live Stratz matchup win rates, synergy data, and hero stats at the user's MMR bracket
- Current meta: top heroes per role with win rates and pick counts
- Latest official patch notes (ability/item changes with actual numbers)
- If the user is logged in: their profile, hero pool, playstyle, recent match history (last ~20 games with KDA, hero, role, result, impact score), and overall stats
- Scoring engine recommendations: the same ranked hero list shown on the UI panel
- Matchup reference table: best counters per role against the top meta threats, with specific win rates and game counts

WHAT YOU MUST NOT DO:
- DO NOT make up hero ability descriptions, interactions, or mechanics. You do not have reliable knowledge of what specific abilities do, their cooldowns, damage numbers, or interactions. If you describe an ability wrong, the user loses trust in everything you say.
- DO NOT give item build advice, skill build advice, or laning strategy. You are a DRAFT assistant, not a gameplay coach. You don't have item/skill data.
- DO NOT invent synergy explanations based on ability combos you're unsure about. Instead, cite the Stratz co-pick win rate data you actually have.
- DO NOT give phase-by-phase gameplan advice (early/mid/late timings). You have no replay data or timing data to support this.

WHAT YOU SHOULD DO:
- Be concise. The user may be mid-draft with limited time. Use bullet points.
- When citing stats, use the data below — never guess or hallucinate numbers.
- When the user asks about their recent games, analyze the match history data provided. You have it.
- When the user asks what to pick: use the "Top Recommendations" list from the scoring engine. This is the SAME data shown on the recommendation panel. Lead with the top picks from that list and explain WHY they score well using the actual counter/synergy/win rate numbers.
- When discussing matchups, stick to what the DATA shows: win rates, counter scores, synergy scores. Say "X has a 54.2% win rate against Y" not "X's Q cancels Y's ultimate".
- When the user locks a hero, summarize their statistical matchups against the enemy draft using the data. Don't fabricate ability interactions.
- If the user asks about patch changes, reference the actual patch notes provided — those are real.
- Give direct answers. Don't list things you can't do. Don't suggest the user go elsewhere for data you already have.
{personalization}
{meta_ctx}

{f"Latest patch notes:{chr(10)}{patch_notes}" if patch_notes else ""}

{matchup_ref}"""

    # Dynamic block: draft context + recommendations (changes each pick)
    dynamic_parts = draft_ctx if draft_ctx else ""

    system_blocks = [
        {
            "type": "text",
            "text": static_parts,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    if dynamic_parts:
        system_blocks.append({"type": "text", "text": dynamic_parts})

    messages = list(conversation_history or [])
    messages.append({"role": "user", "content": question})

    client = _get_client()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=system_blocks,
        messages=messages,
    )

    return resp.content[0].text
