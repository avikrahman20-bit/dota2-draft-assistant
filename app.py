"""
Dota 2 Draft Assistant — FastAPI server.
Run: python app.py
Opens at http://127.0.0.1:8000
"""

import collections
import json
import logging
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import anthropic as _anthropic
import uvicorn
from dotenv import load_dotenv
load_dotenv()  # Load .env into os.environ before any tool modules are imported

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Simple per-IP rate limiter ────────────────────────────────────────────────
# Sliding-window: track timestamps of recent requests per (ip, endpoint).
_rate_data: dict[str, collections.deque] = {}
_rate_lock = threading.Lock()


def _check_rate_limit(ip: str, endpoint: str, max_per_minute: int = 30) -> None:
    key = f"{ip}:{endpoint}"
    now = time.monotonic()
    window = 60.0
    with _rate_lock:
        dq = _rate_data.setdefault(key, collections.deque())
        # Drop timestamps older than the window
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= max_per_minute:
            logger.warning("Rate limit hit: %s on %s", ip, endpoint)
            raise HTTPException(429, "Too many requests — slow down")
        dq.append(now)
        # Evict empty keys to prevent unbounded dict growth
        if len(_rate_data) > 5000:
            stale = [k for k, v in _rate_data.items() if not v]
            for k in stale:
                del _rate_data[k]

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=_load_cache, daemon=True).start()
    yield


app = FastAPI(title="Dota 2 Draft Assistant", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all API requests with method, path, status, and elapsed time."""
    start = time.monotonic()
    response = await call_next(request)
    elapsed = time.monotonic() - start
    if request.url.path.startswith("/api/"):
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(level, "%s %s → %d (%.3fs)", request.method, request.url.path, response.status_code, elapsed)
    return response


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
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Startup cache loading
# ---------------------------------------------------------------------------

def _progress_callback(done: int, total: int) -> None:
    _cache["progress"] = done
    _cache["total"] = total


def _load_cache(force: bool = False) -> None:
    try:
        _cache["total"] = 1
        _cache["progress"] = 0
        heroes, stats, role_map = fetch_hero_data.run(force=force)
        _cache["total"] = len(heroes)
        _cache["progress"] = 0
        fetch_matchups.run(force=force, progress_callback=_progress_callback)
        matchups = fetch_matchups.load_all_matchups()
        with _cache_lock:
            global _role_map
            _cache["heroes"]     = heroes
            _cache["hero_stats"] = stats
            _role_map            = role_map
            _cache["matchups"]   = matchups
            _cache["ready"]      = True
        vs_count = sum(len(v) for v in matchups["vs"].values())
        logger.info(
            "%s ready. %d heroes, %d vs matchup entries loaded.",
            "Force refresh" if force else "Server", len(heroes), vs_count,
        )
    except Exception as e:
        _cache["error"] = str(e)
        logger.error("%s error: %s", "Force refresh" if force else "Startup", e)



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _matchups_for_bracket(mmr_bracket: str) -> dict:
    """Return {"vs": ..., "with": ...} slice from cache for the given bracket UI value."""
    bracket_enum = fetch_matchups.BRACKET_ENUM.get(mmr_bracket, "DIVINE_IMMORTAL")
    return {
        "vs":   _cache["matchups"].get("vs",   {}).get(bracket_enum, {}),
        "with": _cache["matchups"].get("with", {}).get(bracket_enum, {}),
    }


def compute_threats(
    enemy_ids: list[int],
    ally_ids: list[int],
    vs_matchups: dict[int, dict[int, dict]],
    heroes: dict,
) -> list[dict]:
    """
    Per enemy hero: compute avg win rate across all ally picks (= threat score).
    Returns list sorted by avg_win_rate descending, one entry per enemy hero.
    """
    if not enemy_ids or not ally_ids:
        return []

    result = []
    for enemy_id in enemy_ids:
        enemy      = heroes.get(str(enemy_id), {})
        e_matchups = vs_matchups.get(enemy_id, {})

        matchups = []
        total_wr = 0.0
        for ally_id in ally_ids:
            m   = e_matchups.get(ally_id)
            wr  = m["win_rate"] if m and m.get("games", 0) > 0 else 0.5
            total_wr += wr
            matchups.append({
                "ally_id":   ally_id,
                "ally_name": heroes.get(str(ally_id), {}).get("localized_name", str(ally_id)),
                "ally_img":  heroes.get(str(ally_id), {}).get("img_url", ""),
                "win_rate":  round(wr, 4),
                "games":     m.get("games", 0) if m else 0,
            })

        matchups.sort(key=lambda x: x["win_rate"], reverse=True)
        avg_wr = total_wr / len(ally_ids)

        result.append({
            "enemy_id":       enemy_id,
            "enemy_name":     enemy.get("localized_name", str(enemy_id)),
            "enemy_img":      enemy.get("img_url", ""),
            "enemy_roles":    enemy.get("roles", []),
            "avg_win_rate":   round(avg_wr, 4),
            "matchups":       matchups,   # sorted worst-for-you first
        })

    result.sort(key=lambda x: x["avg_win_rate"], reverse=True)
    return result


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
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
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

    player_data["_fetched_at"] = time.time()
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
    player_data["_fetched_at"] = time.time()
    db.update_profile(user["id"], player_stats=player_data)
    return player_data


class RecommendRequest(BaseModel):
    ally_picks:        list[int]        = Field(default=[], max_length=5)
    enemy_picks:       list[int]        = Field(default=[], max_length=5)
    bans:              list[int]        = Field(default=[], max_length=14)
    my_team:           str              = "radiant"
    weights:           dict[str, float] = Field(default={})
    mmr_bracket:       str              = "7"   # "1"=Herald … "7"=Immortal
    role_filter:       str              = Field(default="", max_length=20)
    enemy_role_filter: str              = Field(default="", max_length=20)


@app.post("/api/recommend")
def recommend(req: RecommendRequest, request: Request, authorization: Optional[str] = Header(None)):
    _check_rate_limit(request.client.host, "recommend", max_per_minute=60)
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

    matchups_for_bracket = _matchups_for_bracket(req.mmr_bracket)
    vs_for_bracket   = matchups_for_bracket["vs"]

    # Enemy predictions first — scores are used as denial_scores for ally ranking
    enemy_candidates_all = [h for h in all_hero_ids if h not in excluded]
    enemy_result = score_candidates(
        candidate_ids=enemy_candidates_all,
        enemy_pick_ids=req.ally_picks,      # YOUR picks are their enemies
        ally_pick_ids=req.enemy_picks,       # THEIR picks are their allies
        all_matchups=matchups_for_bracket,
        hero_stats=_cache["hero_stats"],
        heroes=_cache["heroes"],
        mmr_bracket=req.mmr_bracket,
        weights=weights,
        top_n=40,                            # buffer for role-filter post-slice
        hero_pool=[],                        # unknown; engine auto-redistributes
    )
    # Apply enemy role filter for display (post-slice from oversized top)
    enemy_top = enemy_result["top"]
    if req.enemy_role_filter and req.enemy_role_filter in _role_map:
        enemy_role_set = set(_role_map[req.enemy_role_filter])
        enemy_top = [h for h in enemy_top if h["hero_id"] in enemy_role_set]
    enemy_top = enemy_top[:15]

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

    return {
        "top": result["top"],
        "all_scores": result["all_scores"],
        "threats": threats,
        "enemy_predictions": enemy_top,
    }


class DraftAnalysisRequest(BaseModel):
    radiant: list[int]
    dire: list[int]
    mmr_bracket: str = "7"


@app.post("/api/draft_analysis")
def draft_analysis(req: DraftAnalysisRequest, request: Request):
    _check_rate_limit(request.client.host, "draft_analysis", max_per_minute=20)
    if not _cache.get("ready"):
        raise HTTPException(503, "Cache not ready yet")
    if len(req.radiant) != 5 or len(req.dire) != 5:
        raise HTTPException(400, "Need exactly 5 heroes per team")

    m = _matchups_for_bracket(req.mmr_bracket)
    return analyze_draft(
        radiant_ids=req.radiant,
        dire_ids=req.dire,
        vs_matchups=m["vs"],
        with_matchups=m["with"],
        hero_stats=_cache["hero_stats"],
        heroes=_cache["heroes"],
        bracket=req.mmr_bracket,
        role_map=_role_map,
    )


class ChatRequest(BaseModel):
    question:    str                  = Field(..., max_length=2000)
    radiant:     list[int]            = Field(default=[], max_length=5)
    dire:        list[int]            = Field(default=[], max_length=5)
    my_team:     str                  = "radiant"
    mmr_bracket: str                  = "7"
    history:     list[dict[str, str]] = Field(default=[], max_length=20)


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, authorization: Optional[str] = Header(None)):
    _check_rate_limit(request.client.host, "chat", max_per_minute=20)
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
        # Auto-refresh player stats from Stratz — 10-minute TTL
        account_id = user_profile.get("dota_account_id", "")
        if account_id:
            fetched_at = user_profile.get("player_stats", {}).get("_fetched_at", 0)
            if time.time() - fetched_at > 600:
                try:
                    fresh_stats = fetch_player_summary(account_id, _cache.get("heroes", {}))
                    if fresh_stats:
                        fresh_stats["_fetched_at"] = time.time()
                        db.update_profile(user["id"], player_stats=fresh_stats)
                        user_profile["player_stats"] = fresh_stats
                except Exception as refresh_err:
                    logger.warning("Stratz stats refresh failed: %s", refresh_err)
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

        result = score_candidates(
            candidate_ids=candidates,
            enemy_pick_ids=enemy_ids,
            ally_pick_ids=ally_ids,
            all_matchups=_matchups_for_bracket(req.mmr_bracket),
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
        logger.exception("Chat endpoint error")
        if isinstance(e, _anthropic.AuthenticationError):
            raise HTTPException(500, "Anthropic API key is invalid or missing")
        if isinstance(e, _anthropic.RateLimitError):
            raise HTTPException(429, "AI rate limit reached — try again in a moment")
        if isinstance(e, _anthropic.APIConnectionError):
            raise HTTPException(502, "Could not connect to Anthropic API")
        raise HTTPException(500, "An error occurred processing your request")


class RefreshRequest(BaseModel):
    force: bool = False


@app.post("/api/refresh")
def refresh(req: RefreshRequest, request: Request):
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Refresh is only available from localhost")
    if not _cache["ready"]:
        raise HTTPException(503, "Still loading initial cache")
    _cache["ready"] = False
    threading.Thread(target=_load_cache, kwargs={"force": req.force}, daemon=True).start()
    return {"message": "Refresh started in background"}


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

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
