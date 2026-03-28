"""
Fetches hero list and hero win-rate stats from the Stratz API.
Hero list  : GET https://api.stratz.com/api/v1/Hero
Win rates  : GraphQL heroStats.winDay  (aggregated per bracket)

Run directly: python tools/fetch_hero_data.py
"""

import json
import time
from pathlib import Path

from curl_cffi import requests as cffi_requests

BASE_URL  = "https://api.stratz.com"
GRAPHQL   = f"{BASE_URL}/graphql"
TMP_DIR   = Path(__file__).parent.parent / ".tmp"
IMG_BASE  = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes"

# roleId → role string (matches Valve / OpenDota convention)
ROLE_NAMES = [
    "Carry", "Escape", "Nuker", "Initiator",
    "Durable", "Disabler", "Jungler", "Support", "Pusher",
]

# Maps UI bracket value → Stratz RankBracket enum for win-rate queries
BRACKET_ENUM = {
    "7": "IMMORTAL",
    "6": "DIVINE",
    "5": "ANCIENT",
    "4": "LEGEND",
    "3": "ARCHON",
    "2": "CRUSADER",
    "1": "HERALD",
}


def get_api_key() -> str:
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("STRATZ_API_KEY="):
                key = line.split("=", 1)[1].strip()
                if key:
                    return key
    return ""


def is_cache_valid(path: Path, max_age_hours: int = 24) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < max_age_hours * 3600


def fetch_heroes(session) -> dict:
    """
    Fetch hero list from Stratz REST API.
    Returns {hero_id_str: {id, name, localized_name, roles, attack_type, img_url}}
    """
    resp = session.get(f"{BASE_URL}/api/v1/Hero", timeout=30)
    resp.raise_for_status()
    raw = resp.json()

    heroes = {}
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        hero_id = entry.get("id", 0)
        if hero_id <= 0:
            continue

        short_name = entry.get("shortName") or entry.get("name", "").replace("npc_dota_hero_", "")
        display_name = entry.get("displayName") or short_name
        attack_type = (entry.get("stat") or {}).get("attackType", "")
        roles = [
            ROLE_NAMES[r["roleId"]]
            for r in (entry.get("roles") or [])
            if r.get("roleId") is not None and r["roleId"] < len(ROLE_NAMES)
        ]

        heroes[str(hero_id)] = {
            "id": hero_id,
            "name": short_name,
            "localized_name": display_name,
            "attack_type": attack_type,
            "roles": roles,
            "img_url": f"{IMG_BASE}/{short_name}.png",
        }
    return heroes


def fetch_hero_stats(session) -> dict:
    """
    Fetch win rates per hero per bracket via Stratz GraphQL winDay.
    Aggregates multiple days of data into totals.
    Returns {hero_id_str: {bracket_enum_str: {wins, picks}}}
    e.g. {"2": {"IMMORTAL": {"wins": 5000, "picks": 10000}, ...}}
    """
    aliases = " ".join(
        f'{b.lower()}: heroStats {{ winDay(bracketIds: {b}) {{ heroId winCount matchCount }} }}'
        for b in BRACKET_ENUM.values()
    )
    query = f"{{ {aliases} }}"

    resp = session.post(
        GRAPHQL,
        json={"query": query},
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise ValueError(f"GraphQL errors: {body['errors']}")

    stats: dict[str, dict[str, dict]] = {}

    for bracket_enum in BRACKET_ENUM.values():
        alias = bracket_enum.lower()
        entries = (body["data"].get(alias) or {}).get("winDay") or []
        for e in entries:
            hero_id = str(e["heroId"])
            if hero_id not in stats:
                stats[hero_id] = {}
            if bracket_enum not in stats[hero_id]:
                stats[hero_id][bracket_enum] = {"wins": 0, "picks": 0}
            stats[hero_id][bracket_enum]["wins"]  += e.get("winCount", 0) or 0
            stats[hero_id][bracket_enum]["picks"] += e.get("matchCount", 0) or 0

    return stats


def run(force: bool = False) -> tuple[dict, dict]:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError(
            "STRATZ_API_KEY not set in .env\n"
            "Get your free key at https://stratz.com/api-token"
        )

    heroes_path = TMP_DIR / "heroes.json"
    stats_path  = TMP_DIR / "hero_stats.json"

    with cffi_requests.Session(impersonate="chrome110") as session:
        session.headers.update({"Authorization": f"Bearer {api_key}"})

        if force or not is_cache_valid(heroes_path):
            print("Fetching hero list from Stratz...", flush=True)
            heroes = fetch_heroes(session)
            heroes_path.write_text(json.dumps(heroes, indent=2))
            print(f"  Saved {len(heroes)} heroes to {heroes_path}", flush=True)
        else:
            print(f"Using cached heroes ({heroes_path})", flush=True)
            heroes = json.loads(heroes_path.read_text())

        if force or not is_cache_valid(stats_path):
            print("Fetching hero win rates from Stratz...", flush=True)
            stats = fetch_hero_stats(session)
            stats_path.write_text(json.dumps(stats, indent=2))
            print(f"  Saved stats for {len(stats)} heroes to {stats_path}", flush=True)
        else:
            print(f"Using cached hero stats ({stats_path})", flush=True)
            stats = json.loads(stats_path.read_text())

    return heroes, stats


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    heroes, stats = run(force=force)
    print(f"\nDone. {len(heroes)} heroes, {len(stats)} stat entries.")
