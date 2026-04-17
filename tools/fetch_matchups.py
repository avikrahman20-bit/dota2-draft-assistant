"""
Fetches per-hero matchup data from the Stratz GraphQL API and caches to .tmp/matchups_stratz/.
One JSON file per hero containing win rate data for all 4 rank brackets.

New format (v2): each bracket stores {"vs": {...}, "with": {...}} sub-dicts.
  - "vs"   → opponent_id: win rate of this hero AGAINST that opponent
  - "with" → ally_id:     win rate of this hero when ON THE SAME TEAM as that ally

Data source: public ranked matches only (no pro games), bracket-filtered.
Stratz API key required — add STRATZ_API_KEY=<token> to .env
Get a free key at: https://stratz.com/api-token (Steam login)

Uses curl-cffi to pass Cloudflare's bot protection on api.stratz.com.
"""

import json
import sys
import time
from pathlib import Path

from curl_cffi import requests as cffi_requests

STRATZ_URL = "https://api.stratz.com/graphql"
TMP_DIR = Path(__file__).parent.parent / ".tmp"
MATCHUPS_DIR = TMP_DIR / "matchups_stratz"
DELAY = 0.5  # seconds between requests (conservative for free tier)

_BRACKETS = ["DIVINE_IMMORTAL", "LEGEND_ANCIENT", "CRUSADER_ARCHON", "HERALD_GUARDIAN"]

# Maps UI bracket value (str) → Stratz RankBracketBasicEnum
BRACKET_ENUM: dict[str, str] = {
    "7": "DIVINE_IMMORTAL",
    "6": "DIVINE_IMMORTAL",
    "5": "LEGEND_ANCIENT",
    "4": "LEGEND_ANCIENT",
    "3": "CRUSADER_ARCHON",
    "2": "CRUSADER_ARCHON",
    "1": "HERALD_GUARDIAN",
}

_BRACKET_ALIASES = {
    "d": "DIVINE_IMMORTAL",
    "l": "LEGEND_ANCIENT",
    "c": "CRUSADER_ARCHON",
    "h": "HERALD_GUARDIAN",
}


def get_api_key() -> str:
    import os
    key = os.environ.get("STRATZ_API_KEY", "").strip()
    if key:
        return key
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


def _is_new_format(path: Path) -> bool:
    """Return True if the cache file uses the new {"vs": ..., "with": ...} format."""
    try:
        raw = json.loads(path.read_text())
        first = next(iter(raw.values()), None)
        return isinstance(first, dict) and "vs" in first
    except Exception as e:
        print(f"[warn] Could not read cache file {path.name}: {e}", flush=True)
        return False


def load_hero_ids() -> list[int]:
    heroes_path = TMP_DIR / "heroes.json"
    if not heroes_path.exists():
        raise FileNotFoundError("heroes.json not found. Run tools/fetch_hero_data.py first.")
    return [int(k) for k in json.loads(heroes_path.read_text()).keys()]


def fetch_matchups_for_hero(session, hero_id: int, api_key: str) -> dict:
    """
    Fetches matchup data for hero_id across all 4 rank brackets in one GraphQL request.

    Returns:
        {
          "DIVINE_IMMORTAL": {
            "vs":   {opponent_id: {"win_rate": 0.52, "games": 45000}},
            "with": {ally_id:     {"win_rate": 0.53, "games": 38000}},
          },
          "LEGEND_ANCIENT":  {...},
          "CRUSADER_ARCHON": {...},
          "HERALD_GUARDIAN": {...},
        }

    "vs"   win_rate > 0.5 → hero beats that opponent (counter pick).
    "with" win_rate > 0.5 → hero's team wins more when paired with that ally (synergy).
    """
    query = f"""
    {{
      heroStats {{
        d: matchUp(heroId: {hero_id}, bracketBasicIds: DIVINE_IMMORTAL, take: 150) {{
          vs   {{ heroId2 winsAverage matchCount }}
          with {{ heroId2 winsAverage matchCount }}
        }}
        l: matchUp(heroId: {hero_id}, bracketBasicIds: LEGEND_ANCIENT, take: 150) {{
          vs   {{ heroId2 winsAverage matchCount }}
          with {{ heroId2 winsAverage matchCount }}
        }}
        c: matchUp(heroId: {hero_id}, bracketBasicIds: CRUSADER_ARCHON, take: 150) {{
          vs   {{ heroId2 winsAverage matchCount }}
          with {{ heroId2 winsAverage matchCount }}
        }}
        h: matchUp(heroId: {hero_id}, bracketBasicIds: HERALD_GUARDIAN, take: 150) {{
          vs   {{ heroId2 winsAverage matchCount }}
          with {{ heroId2 winsAverage matchCount }}
        }}
      }}
    }}
    """

    resp = session.post(
        STRATZ_URL,
        json={"query": query},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        impersonate="chrome110",
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()

    if "errors" in body:
        raise ValueError(f"GraphQL errors: {body['errors']}")

    hs = body["data"]["heroStats"]
    result = {}

    def _parse_items(items):
        out = {}
        for item in items:
            if item.get("heroId2") is not None and item.get("winsAverage") is not None:
                out[item["heroId2"]] = {
                    "win_rate": item["winsAverage"],
                    "games": int(item.get("matchCount") or 0),
                }
        return out

    for alias, bracket_name in _BRACKET_ALIASES.items():
        entries = hs.get(alias) or []
        vs_items, with_items = [], []
        for entry in entries:
            vs_items.extend(entry.get("vs") or [])
            with_items.extend(entry.get("with") or [])
        result[bracket_name] = {
            "vs":   _parse_items(vs_items),
            "with": _parse_items(with_items),
        }

    return result


def run(force: bool = False, progress_callback=None) -> int:
    """
    Fetch matchup data for all heroes from Stratz.
    progress_callback(done, total) called after each hero.
    Returns number of heroes actually fetched (not from cache).
    """
    MATCHUPS_DIR.mkdir(parents=True, exist_ok=True)

    api_key = get_api_key()
    if not api_key:
        raise RuntimeError(
            "STRATZ_API_KEY not set in .env\n"
            "Get your free key at https://stratz.com/api-token then add:\n"
            "  STRATZ_API_KEY=your_token_here"
        )

    hero_ids = load_hero_ids()
    total = len(hero_ids)
    fetched = 0

    with cffi_requests.Session(impersonate="chrome110") as session:
        for i, hero_id in enumerate(hero_ids):
            path = MATCHUPS_DIR / f"{hero_id}.json"

            if not force and is_cache_valid(path) and _is_new_format(path):
                if progress_callback:
                    progress_callback(i + 1, total)
                continue

            try:
                data = fetch_matchups_for_hero(session, hero_id, api_key)
                path.write_text(json.dumps(data))
                fetched += 1
                div = data.get("DIVINE_IMMORTAL", {}).get("vs", {})
                sample = next(iter(div.values()), {})
                print(
                    f"  [{i+1}/{total}] Hero {hero_id}: "
                    f"{len(div)} vs matchups  "
                    f"(sample games: {sample.get('games', '?')})",
                    flush=True,
                )
            except Exception as e:
                print(f"  [{i+1}/{total}] Hero {hero_id}: Error — {e}", flush=True)

            if progress_callback:
                progress_callback(i + 1, total)

            if fetched > 0:
                time.sleep(DELAY)

    return fetched


def load_all_matchups() -> dict:
    """
    Load all cached Stratz matchup files into memory.

    Returns:
        {
          "vs": {
            "DIVINE_IMMORTAL": {hero_id: {opponent_id: {"win_rate": float, "games": int}}},
            "LEGEND_ANCIENT":  {...},
            "CRUSADER_ARCHON": {...},
            "HERALD_GUARDIAN": {...},
          },
          "with": {
            "DIVINE_IMMORTAL": {hero_id: {ally_id: {"win_rate": float, "games": int}}},
            ...
          },
        }
    """
    MATCHUPS_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "vs":   {b: {} for b in _BRACKETS},
        "with": {b: {} for b in _BRACKETS},
    }

    for path in MATCHUPS_DIR.glob("*.json"):
        try:
            hero_id = int(path.stem)
        except ValueError:
            continue
        raw = json.loads(path.read_text())
        for bracket, data in raw.items():
            if bracket not in result["vs"]:
                continue
            if isinstance(data, dict) and "vs" in data:
                # New format
                result["vs"][bracket][hero_id]   = {int(k): v for k, v in data["vs"].items()}
                result["with"][bracket][hero_id] = {int(k): v for k, v in data.get("with", {}).items()}
            else:
                # Old flat format — treat as vs-only
                result["vs"][bracket][hero_id]   = {int(k): v for k, v in data.items()}
                result["with"][bracket][hero_id] = {}

    return result


if __name__ == "__main__":
    force = "--force" in sys.argv
    print(f"Fetching Stratz matchup data {'(force)' if force else '(skip cached)'}...")
    n = run(force=force)
    files = list(MATCHUPS_DIR.glob("*.json"))
    print(f"\nDone. Fetched {n} new entries. {len(files)} total cached.")
