"""
Dota 2 Draft Assistant — FastAPI server.
Run: python app.py
Opens at http://127.0.0.1:8000
"""

import json
import sys
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Ensure tools/ is importable
sys.path.insert(0, str(Path(__file__).parent))

from tools import fetch_hero_data, fetch_matchups
from tools.scoring_engine import DEFAULT_WEIGHTS, analyze_draft, score_candidates
from tools.assistant import answer as assistant_answer
from tools.fetch_player import fetch_player_summary

import database as db
import auth as auth_module

# Role map: populated dynamically from Stratz position data during cache load
_role_map: dict[str, list[int]] = {}

# Initialize user database
db.init_db()

app = FastAPI(title="Dota 2 Draft Assistant")

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
_cache: dict = {
    "heroes": {},       # {hero_id_str: hero_dict}
    "hero_stats": {},   # {hero_id_str: stats_dict}
    "matchups": {},     # {"vs": {bracket: {hero_id: {opp_id: {...}}}}, "with": {...}}
    "ready": False,
    "progress": 0,
    "total": 0,
    "error": None,
}

TMP_DIR = Path(__file__).parent / ".tmp"


# ---------------------------------------------------------------------------
# Startup cache loading
# ---------------------------------------------------------------------------

def _progress_callback(done: int, total: int) -> None:
    _cache["progress"] = done
    _cache["total"] = total


def _load_cache() -> None:
    try:
        # Step 1: Hero list + stats
        _cache["total"] = 1
        _cache["progress"] = 0
        global _role_map
        heroes, stats, role_map = fetch_hero_data.run()
        _cache["heroes"] = heroes
        _cache["hero_stats"] = stats
        _role_map = role_map

        # Step 2: Matchups (with progress updates)
        _cache["total"] = len(heroes)
        _cache["progress"] = 0
        fetch_matchups.run(progress_callback=_progress_callback)

        # Step 3: Load matchup files into memory
        _cache["matchups"] = fetch_matchups.load_all_matchups()

        _cache["ready"] = True
        vs_count = sum(len(v) for v in _cache["matchups"]["vs"].values())
        print(
            f"\nServer ready. {len(heroes)} heroes, "
            f"{vs_count} vs matchup entries loaded.",
            flush=True,
        )
    except Exception as e:
        _cache["error"] = str(e)
        print(f"\nStartup error: {e}", flush=True)


@app.on_event("startup")
async def startup_event() -> None:
    thread = threading.Thread(target=_load_cache, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_threats(
    enemy_ids: list[int],
    ally_ids: list[int],
    vs_matchups: dict[int, dict[int, dict]],
    heroes: dict,
    top_n: int = 5,
) -> list[dict]:
    """
    For each (enemy, ally) pair, find how much the enemy counters the ally.
    Returns top_n threats sorted by win rate descending (most dangerous first).

    vs_matchups[enemy_id][ally_id]["win_rate"] = enemy's win rate AGAINST ally.
    > 0.5 means the enemy hero wins more against that ally.
    """
    if not enemy_ids or not ally_ids:
        return []

    threats = []
    for enemy_id in enemy_ids:
        enemy_matchups = vs_matchups.get(enemy_id, {})
        for ally_id in ally_ids:
            matchup = enemy_matchups.get(ally_id)
            win_rate = matchup["win_rate"] if matchup and matchup.get("games", 0) > 0 else 0.5
            threats.append({
                "enemy_id":    enemy_id,
                "enemy_name":  heroes.get(str(enemy_id), {}).get("localized_name", str(enemy_id)),
                "vs_ally_id":  ally_id,
                "vs_ally_name": heroes.get(str(ally_id), {}).get("localized_name", str(ally_id)),
                "win_rate":    round(win_rate, 4),
            })

    threats.sort(key=lambda x: x["win_rate"], reverse=True)
    return threats[:top_n]


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/status")
def get_status():
    return {
        "ready": _cache["ready"],
        "progress": _cache["progress"],
        "total": _cache["total"],
        "error": _cache["error"],
    }


@app.get("/api/heroes")
def get_heroes():
    if not _cache["heroes"]:
        raise HTTPException(503, "Hero data not loaded yet")
    return _cache["heroes"]


# ---------------------------------------------------------------------------
# Auth helpers + endpoints
# ---------------------------------------------------------------------------

def _get_current_user(authorization: Optional[str] = None) -> dict | None:
    """Extract user from Bearer token. Returns None if no/invalid token."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    payload = auth_module.decode_token(authorization[7:])
    if not payload:
        return None
    return {"id": payload["sub"], "username": payload["username"]}


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class ProfileUpdateRequest(BaseModel):
    preferred_roles: list[str] | None = None
    hero_pool: list[int] | None = None
    playstyle_tags: list[str] | None = None
    playstyle_notes: str | None = None
    mmr_bracket: str | None = None
    custom_weights: dict | None = None
    dota_account_id: str | None = None


class FeedbackRequest(BaseModel):
    hero_id: int | None = None
    feedback: str
    draft_context: str = ""


@app.post("/api/register")
def register(req: RegisterRequest):
    username = req.username.strip()
    if len(username) < 3 or len(username) > 30:
        raise HTTPException(400, "Username must be 3-30 characters")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if db.get_user_by_username(username):
        raise HTTPException(409, "Username already taken")

    hashed = auth_module.hash_password(req.password)
    user_id = db.create_user(username, hashed)
    token = auth_module.create_token(user_id, username)
    return {"token": token, "user": {"id": user_id, "username": username}}


@app.post("/api/login")
def login(req: LoginRequest):
    user = db.get_user_by_username(req.username.strip())
    if not user or not auth_module.verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")

    token = auth_module.create_token(user["id"], user["username"])
    return {"token": token, "user": {"id": user["id"], "username": user["username"]}}


@app.get("/api/profile")
def get_profile(authorization: Optional[str] = Header(None)):
    user = _get_current_user(authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    profile = db.get_profile(user["id"])
    profile["username"] = user["username"]
    return profile


@app.put("/api/profile")
def update_profile(req: ProfileUpdateRequest, authorization: Optional[str] = Header(None)):
    user = _get_current_user(authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")

    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    profile = db.update_profile(user["id"], **fields)
    profile["username"] = user["username"]
    return profile


@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest, authorization: Optional[str] = Header(None)):
    user = _get_current_user(authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    db.add_feedback(user["id"], req.hero_id, req.feedback, req.draft_context)
    return {"ok": True}


class LinkAccountRequest(BaseModel):
    dota_account_id: str


@app.post("/api/link_account")
def link_account(req: LinkAccountRequest, authorization: Optional[str] = Header(None)):
    user = _get_current_user(authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not req.dota_account_id.strip().isdigit():
        raise HTTPException(400, "Invalid Dota 2 Friend ID — must be numeric")

    account_id = req.dota_account_id.strip()
    heroes = _cache.get("heroes", {})

    player_data = fetch_player_summary(account_id, heroes)
    if not player_data:
        raise HTTPException(404, "Could not find player — check the Friend ID and ensure match data is public")

    # Save to profile
    db.update_profile(user["id"], dota_account_id=account_id, player_stats=player_data)
    return player_data


@app.post("/api/unlink_account")
def unlink_account(authorization: Optional[str] = Header(None)):
    user = _get_current_user(authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    db.update_profile(user["id"], dota_account_id="", player_stats={})
    return {"ok": True}


@app.get("/api/player_stats")
def get_player_stats(authorization: Optional[str] = Header(None)):
    """Re-fetch latest player stats from Stratz."""
    user = _get_current_user(authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    profile = db.get_profile(user["id"])
    account_id = profile.get("dota_account_id", "")
    if not account_id:
        raise HTTPException(400, "No Dota 2 account linked")
    heroes = _cache.get("heroes", {})
    player_data = fetch_player_summary(account_id, heroes)
    if not player_data:
        raise HTTPException(502, "Could not fetch player data from Stratz")
    db.update_profile(user["id"], player_stats=player_data)
    return player_data


class RecommendRequest(BaseModel):
    ally_picks: list[int] = []
    enemy_picks: list[int] = []
    bans: list[int] = []
    my_team: str = "radiant"
    weights: dict = {}
    mmr_bracket: str = "7"   # "1"=Herald … "7"=Immortal
    role_filter: str = ""    # "" = All Roles


@app.post("/api/recommend")
def recommend(req: RecommendRequest, authorization: Optional[str] = Header(None)):
    if not _cache["ready"]:
        raise HTTPException(503, "Cache not ready yet")

    # Resolve hero pool from logged-in user's profile
    hero_pool = []
    user = _get_current_user(authorization)
    if user:
        profile = db.get_profile(user["id"])
        hero_pool = profile.get("hero_pool", []) if profile else []

    all_hero_ids = [int(k) for k in _cache["heroes"].keys()]
    excluded = set(req.ally_picks + req.enemy_picks + req.bans)
    candidates = [h for h in all_hero_ids if h not in excluded]

    # Apply positional role filter
    if req.role_filter and req.role_filter in _role_map:
        role_set = set(_role_map[req.role_filter])
        candidates = [h for h in candidates if h in role_set]

    weights = {**DEFAULT_WEIGHTS, **req.weights} if req.weights else None

    # Select the matchup slice for the requested bracket
    bracket_enum = fetch_matchups.BRACKET_ENUM.get(req.mmr_bracket, "DIVINE_IMMORTAL")
    vs_for_bracket   = _cache["matchups"]["vs"].get(bracket_enum, {})
    with_for_bracket = _cache["matchups"]["with"].get(bracket_enum, {})
    matchups_for_bracket = {"vs": vs_for_bracket, "with": with_for_bracket}

    result = score_candidates(
        candidate_ids=candidates,
        enemy_pick_ids=req.enemy_picks,
        ally_pick_ids=req.ally_picks,
        all_matchups=matchups_for_bracket,
        hero_stats=_cache["hero_stats"],
        heroes=_cache["heroes"],
        mmr_bracket=req.mmr_bracket,
        weights=weights,
        top_n=20,
        hero_pool=hero_pool,
    )

    threats = compute_threats(
        enemy_ids=req.enemy_picks,
        ally_ids=req.ally_picks,
        vs_matchups=vs_for_bracket,
        heroes=_cache["heroes"],
    )

    return {"top": result["top"], "all_scores": result["all_scores"], "threats": threats}


class DraftAnalysisRequest(BaseModel):
    radiant: list[int]
    dire: list[int]
    mmr_bracket: str = "7"


@app.post("/api/draft_analysis")
def draft_analysis(req: DraftAnalysisRequest):
    if not _cache.get("ready"):
        raise HTTPException(503, "Cache not ready yet")
    if len(req.radiant) != 5 or len(req.dire) != 5:
        raise HTTPException(400, "Need exactly 5 heroes per team")

    bracket_enum     = fetch_matchups.BRACKET_ENUM.get(req.mmr_bracket, "DIVINE_IMMORTAL")
    vs_for_bracket   = _cache["matchups"].get("vs",   {}).get(bracket_enum, {})
    with_for_bracket = _cache["matchups"].get("with", {}).get(bracket_enum, {})

    return analyze_draft(
        radiant_ids=req.radiant,
        dire_ids=req.dire,
        vs_matchups=vs_for_bracket,
        with_matchups=with_for_bracket,
        hero_stats=_cache["hero_stats"],
        heroes=_cache["heroes"],
        bracket=req.mmr_bracket,
        role_map=_role_map,
    )


class ChatRequest(BaseModel):
    question: str
    radiant: list[int] = []
    dire: list[int] = []
    my_team: str = "radiant"
    mmr_bracket: str = "7"
    history: list[dict] = []   # [{"role": "user"|"assistant", "content": str}]


@app.post("/api/chat")
def chat(req: ChatRequest, authorization: Optional[str] = Header(None)):
    if not _cache.get("ready"):
        raise HTTPException(503, "Cache not ready yet")
    if not req.question.strip():
        raise HTTPException(400, "Empty question")

    # Load user profile if logged in, refresh Stratz stats if linked
    user_profile = None
    user = _get_current_user(authorization)
    if user:
        user_profile = db.get_profile(user["id"])
        user_profile["username"] = user["username"]
        # Auto-refresh player stats from Stratz so match history is current
        account_id = user_profile.get("dota_account_id", "")
        if account_id:
            try:
                fresh_stats = fetch_player_summary(account_id, _cache.get("heroes", {}))
                if fresh_stats:
                    db.update_profile(user["id"], player_stats=fresh_stats)
                    user_profile["player_stats"] = fresh_stats
            except Exception:
                pass  # Use cached stats if refresh fails
        # Include recent feedback for AI context
        user_profile["recent_feedback"] = db.get_recent_feedback(user["id"], limit=10)

    # Run the scoring engine so the AI sees the same recommendations as the panel
    recommendations = []
    if req.radiant or req.dire:
        if req.my_team == "dire":
            ally_ids = req.dire
            enemy_ids = req.radiant
        else:
            ally_ids = req.radiant
            enemy_ids = req.dire

        hero_pool = []
        if user_profile:
            hero_pool = user_profile.get("hero_pool", [])

        all_hero_ids = [int(k) for k in _cache["heroes"].keys()]
        excluded = set(ally_ids + enemy_ids)
        candidates = [h for h in all_hero_ids if h not in excluded]

        bracket_enum = fetch_matchups.BRACKET_ENUM.get(req.mmr_bracket, "DIVINE_IMMORTAL")
        vs_for_bracket = _cache["matchups"]["vs"].get(bracket_enum, {})
        with_for_bracket = _cache["matchups"]["with"].get(bracket_enum, {})

        result = score_candidates(
            candidate_ids=candidates,
            enemy_pick_ids=enemy_ids,
            ally_pick_ids=ally_ids,
            all_matchups={"vs": vs_for_bracket, "with": with_for_bracket},
            hero_stats=_cache["hero_stats"],
            heroes=_cache["heroes"],
            mmr_bracket=req.mmr_bracket,
            weights=None,
            top_n=10,
            hero_pool=hero_pool,
        )
        recommendations = result.get("top", [])

    try:
        reply = assistant_answer(
            question=req.question,
            heroes=_cache["heroes"],
            hero_stats=_cache["hero_stats"],
            matchups=_cache["matchups"],
            role_map=_role_map,
            radiant_ids=req.radiant,
            dire_ids=req.dire,
            bracket=req.mmr_bracket,
            conversation_history=req.history,
            user_profile=user_profile,
            recommendations=recommendations,
            my_team=req.my_team,
        )
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(500, str(e))


class RefreshRequest(BaseModel):
    force: bool = False


@app.post("/api/refresh")
def refresh(req: RefreshRequest):
    if not _cache["ready"]:
        raise HTTPException(503, "Still loading initial cache")
    _cache["ready"] = False
    thread = threading.Thread(
        target=_load_cache_forced if req.force else _load_cache, daemon=True
    )
    thread.start()
    return {"message": "Refresh started in background"}


def _load_cache_forced() -> None:
    try:
        _cache["total"] = 1
        _cache["progress"] = 0
        global _role_map
        heroes, stats, role_map = fetch_hero_data.run(force=True)
        _cache["heroes"] = heroes
        _cache["hero_stats"] = stats
        _role_map = role_map
        _cache["total"] = len(heroes)
        _cache["progress"] = 0
        fetch_matchups.run(force=True, progress_callback=_progress_callback)
        _cache["matchups"] = fetch_matchups.load_all_matchups()
        _cache["ready"] = True
        print("Force refresh complete.", flush=True)
    except Exception as e:
        _cache["error"] = str(e)
        print(f"Force refresh error: {e}", flush=True)


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import webbrowser
    import time

    print("Starting Dota 2 Draft Assistant...")
    print("Opening http://127.0.0.1:8000 in your browser.")
    print("First run will cache hero data (~2-3 minutes). Subsequent runs are instant.\n")

    # Open browser after short delay to let server start
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8000")).start()

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
