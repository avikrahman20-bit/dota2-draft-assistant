"""
Fetches player data from Stratz API — recent matches, hero stats, win rates.
Uses the same Stratz bearer token as other modules.
"""

from pathlib import Path
from curl_cffi import requests as cffi_requests


BASE_URL = "https://api.stratz.com"
GRAPHQL = f"{BASE_URL}/graphql"


def get_api_key() -> str:
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("STRATZ_API_KEY="):
                key = line.split("=", 1)[1].strip()
                if key:
                    return key
    return ""


def fetch_player_summary(account_id: str, heroes_cache: dict) -> dict | None:
    """
    Fetch a player's profile + recent hero performance + recent matches from Stratz.
    account_id: Dota 2 Friend ID (numeric string)
    heroes_cache: {hero_id_str: {localized_name, ...}} for name resolution
    Returns dict with player info + top heroes + recent matches, or None on error.
    """
    api_key = get_api_key()
    if not api_key:
        return None

    session = cffi_requests.Session(impersonate="chrome110")
    headers = {"Authorization": f"Bearer {api_key}"}

    # GraphQL query: player profile + hero performance + recent matches
    query = """
    query ($steamAccountId: Long!) {
      player(steamAccountId: $steamAccountId) {
        steamAccountId
        steamAccount {
          name
          avatar
          seasonRank
          isDotaPlusSubscriber
        }
        heroesPerformance(request: { take: 20 }) {
          heroId
          winCount
          matchCount
        }
        matchCount
        winCount
        matches(request: { take: 20 }) {
          id
          didRadiantWin
          durationSeconds
          startDateTime
          players {
            steamAccountId
            heroId
            isRadiant
            kills
            deaths
            assists
            networth
            imp
            role
            lane
            position
            award
          }
        }
      }
    }
    """

    try:
        resp = session.post(
            GRAPHQL,
            json={"query": query, "variables": {"steamAccountId": int(account_id)}},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    player = data.get("data", {}).get("player")
    if not player:
        return None

    steam = player.get("steamAccount") or {}
    rank_num = steam.get("seasonRank")

    # Build top heroes list
    top_heroes = []
    for hp in (player.get("heroesPerformance") or []):
        hero_id = hp.get("heroId")
        hero_name = heroes_cache.get(str(hero_id), {}).get("localized_name", f"Hero {hero_id}")
        matches = hp.get("matchCount", 0)
        wins = hp.get("winCount", 0)
        wr = (wins / matches * 100) if matches > 0 else 0
        top_heroes.append({
            "hero_id": hero_id,
            "hero_name": hero_name,
            "matches": matches,
            "wins": wins,
            "win_rate": round(wr, 1),
        })

    top_heroes.sort(key=lambda x: x["matches"], reverse=True)

    # Build recent matches list
    recent_matches = []
    # Stratz position enum → display name
    position_map = {
        "POSITION_1": "Carry", "POSITION_2": "Mid", "POSITION_3": "Offlane",
        "POSITION_4": "Soft Sup", "POSITION_5": "Hard Sup",
    }
    # Fallback: Stratz role + lane → display name
    role_str_map = {
        "CORE": "Core", "LIGHT_SUPPORT": "Soft Sup", "HARD_SUPPORT": "Hard Sup",
    }
    lane_str_map = {
        "SAFE_LANE": "Safe", "MID_LANE": "Mid", "OFF_LANE": "Off", "JUNGLE": "Jungle",
    }
    award_map = {1: "MVP", 2: "Top Core", 3: "Top Support"}
    acct_int = int(account_id)

    for m in (player.get("matches") or []):
        all_players = m.get("players") or []

        # Find our player in the full player list
        p = next((pl for pl in all_players if pl.get("steamAccountId") == acct_int), None)
        if not p:
            continue

        hero_id = p.get("heroId")
        hero_name = heroes_cache.get(str(hero_id), {}).get("localized_name", f"Hero {hero_id}")
        is_radiant = p.get("isRadiant", False)
        won = (m.get("didRadiantWin") == is_radiant)
        duration_min = (m.get("durationSeconds") or 0) / 60

        def _resolve_role(pl):
            """Resolve player role from position (best) or role+lane (fallback)."""
            pos = pl.get("position")
            if pos and pos in position_map:
                return position_map[pos]
            role = pl.get("role")
            lane = pl.get("lane")
            if role in role_str_map:
                if role == "CORE":
                    # Distinguish core roles by lane
                    if lane == "MID_LANE":
                        return "Mid"
                    elif lane == "OFF_LANE":
                        return "Offlane"
                    elif lane == "SAFE_LANE":
                        return "Carry"
                    return "Core"
                return role_str_map[role]
            return "Unknown"

        # Build enemy team list
        enemy_heroes = []
        for pl in all_players:
            if pl.get("isRadiant") != is_radiant:
                ehid = pl.get("heroId")
                ename = heroes_cache.get(str(ehid), {}).get("localized_name", f"Hero {ehid}")
                erole = _resolve_role(pl)
                enemy_heroes.append({"hero_id": ehid, "hero_name": ename, "role": erole})

        recent_matches.append({
            "match_id": m.get("id"),
            "hero_id": hero_id,
            "hero_name": hero_name,
            "won": won,
            "kills": p.get("kills", 0),
            "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0),
            "networth": p.get("networth", 0),
            "imp": p.get("imp"),  # Stratz impact score
            "role": _resolve_role(p),
            "lane": lane_str_map.get(p.get("lane"), "Unknown"),
            "award": award_map.get(p.get("award"), None),
            "duration_min": round(duration_min, 1),
            "start_time": m.get("startDateTime"),
            "enemy_heroes": enemy_heroes,
        })

    # Sort by most recent first
    recent_matches.sort(key=lambda x: x.get("start_time") or 0, reverse=True)

    total_matches = player.get("matchCount", 0)
    total_wins = player.get("winCount", 0)

    # Rank tier decode: seasonRank is an int like 80 = Immortal, 71 = Divine 1, etc.
    rank_label = _decode_rank(rank_num) if rank_num else "Unknown"

    return {
        "account_id": account_id,
        "name": steam.get("name", "Unknown"),
        "avatar": steam.get("avatar"),
        "rank": rank_label,
        "rank_num": rank_num,
        "total_matches": total_matches,
        "total_wins": total_wins,
        "overall_wr": round(total_wins / total_matches * 100, 1) if total_matches > 0 else 0,
        "top_heroes": top_heroes[:15],
        "recent_matches": recent_matches,
        "dota_plus": steam.get("isDotaPlusSubscriber", False),
    }


def _decode_rank(rank: int) -> str:
    tiers = {
        1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
        5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal",
    }
    medal = rank // 10
    stars = rank % 10
    name = tiers.get(medal, "Unknown")
    if medal == 8:
        return "Immortal"
    return f"{name} {stars}" if stars else name
