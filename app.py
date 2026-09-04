"""
Dota 2 Draft Assistant — FastAPI server.
Run: python app.py
Opens at http://127.0.0.1:8000
"""

import collections
import json
import logging
import os
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
from fastapi.responses import FileResponse, StreamingResponse
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

DAILY_CHAT_LIMIT = 50

# Validate required environment configuration at startup
def _validate_env():
    """Check required API keys and configuration. Fail fast with clear error."""
    errors = []

    stratz_key = os.environ.get("STRATZ_API_KEY", "").strip()
    if not stratz_key:
        errors.append("STRATZ_API_KEY not set in .env")

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        logger.warning("ANTHROPIC_API_KEY not set — AI chat will be disabled. Drafting still works.")

    if errors:
        msg = "Startup configuration errors:\n  • " + "\n  • ".join(errors)
        msg += "\n\nFix: Copy .env.example to .env and fill in your API key from:"
        msg += "\n  • Stratz: https://stratz.com/api-token"
        raise RuntimeError(msg)

_validate_env()

CHAT_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

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
    threading.Thread(target=_auto_refresh_loop, daemon=True).start()
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
    "load_start_time": 0,  # unix timestamp when cache load began
    "data_updated_at": 0,  # unix timestamp of the last real Stratz fetch
}

_AUTO_REFRESH_AGE_SECS = 24 * 3600   # refetch Stratz data in the background once it's a day old
_AUTO_REFRESH_POLL_SECS = 30 * 60
_refresh_lock = threading.Lock()

TMP_DIR = Path(__file__).parent / ".tmp"
_cache_lock = threading.Lock()
_CACHE_LOAD_TIMEOUT_SECS = 300  # 5 minutes max for initial load


# ---------------------------------------------------------------------------
# Startup cache loading
# ---------------------------------------------------------------------------

def _progress_callback(done: int, total: int) -> None:
    _cache["progress"] = done
    _cache["total"] = total


def _load_cache(force: bool = False) -> None:
    with _cache_lock:
        _cache["load_start_time"] = time.time()
        _cache["error"] = None

    try:
        _cache["total"] = 1
        _cache["progress"] = 0
        heroes, stats, role_map = fetch_hero_data.run(force=force)
        _cache["total"] = len(heroes)
        _cache["progress"] = 0
        fetched = fetch_matchups.run(force=force, progress_callback=_progress_callback)
        if force and fetched == 0:
            fetch_matchups.write_fetch_stamp()  # hero list/stats were refetched even if matchups all errored
        matchups = fetch_matchups.load_all_matchups()
        with _cache_lock:
            global _role_map
            _cache["heroes"]     = heroes
            _cache["hero_stats"] = stats
            _role_map            = role_map
            _cache["matchups"]   = matchups
            _cache["ready"]      = True
            _cache["error"]      = None
            _cache["data_updated_at"] = fetch_matchups.read_fetch_stamp()
        vs_count = sum(len(v) for v in matchups["vs"].values())
        logger.info(
            "%s ready. %d heroes, %d vs matchup entries loaded.",
            "Force refresh" if force else "Server", len(heroes), vs_count,
        )
    except Exception as e:
        _cache["error"] = str(e)
        logger.error("%s error: %s", "Force refresh" if force else "Startup", e)


def _background_refresh() -> None:
    """Refetch Stratz data without taking the app offline: users keep the old data until the swap."""
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        logger.info("Auto-refresh: Stratz data is older than %dh, refetching in background", _AUTO_REFRESH_AGE_SECS // 3600)
        heroes, stats, role_map = fetch_hero_data.run(force=True)
        fetch_matchups.run(force=True)
        fetch_matchups.write_fetch_stamp()
        matchups = fetch_matchups.load_all_matchups()
        with _cache_lock:
            global _role_map
            _cache["heroes"]     = heroes
            _cache["hero_stats"] = stats
            _role_map            = role_map
            _cache["matchups"]   = matchups
            _cache["data_updated_at"] = fetch_matchups.read_fetch_stamp()
        logger.info("Auto-refresh complete")
    except Exception as e:
        logger.warning("Auto-refresh failed, keeping existing data: %s", e)
    finally:
        _refresh_lock.release()


def _auto_refresh_loop() -> None:
    # First check shortly after boot so stale committed data gets refreshed without waiting 30 min
    delay = 90
    while True:
        time.sleep(delay)
        delay = _AUTO_REFRESH_POLL_SECS
        if _cache["ready"] and time.time() - _cache.get("data_updated_at", 0) > _AUTO_REFRESH_AGE_SECS:
            _background_refresh()


def _current_patch_name() -> str:
    """Patch label from the cached patch-notes file, if any (no network)."""
    try:
        p = TMP_DIR / "patch_notes_cache.json"
        if p.exists():
            content = json.loads(p.read_text(encoding="utf-8")).get("content", "")
            first = content.split("\n", 1)[0]
            if "Patch" in first:
                return first.replace("=", "").replace("Dota 2 Patch", "").strip()
    except Exception:
        pass
    return ""



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

def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1")


@app.get("/api/status")
def get_status(request: Request):
    # Check for timeout: if load started but not finished after 5 min, mark error
    if (not _cache["ready"] and _cache["load_start_time"] > 0 and
        time.time() - _cache["load_start_time"] > _CACHE_LOAD_TIMEOUT_SECS):
        _cache["error"] = (
            "Cache loading timed out after 5 minutes. "
            "Check that Stratz API is reachable and your API key is valid."
        )
        _cache["progress"] = 0

    return {
        "ready": _cache["ready"],
        "progress": _cache["progress"],
        "total": _cache["total"],
        "error": _cache["error"],
        "can_refresh": _is_local(request),
        "chat_enabled": CHAT_ENABLED,
        "data_updated_at": _cache.get("data_updated_at", 0),
        "patch_name": _current_patch_name(),
    }


@app.get("/api/heroes")
def get_heroes():
    if not _cache["heroes"]:
        raise HTTPException(503, "Hero data not loaded yet")
    # Attach played positions (from Stratz position data) so the UI can show role pills / team needs
    role_sets = {role: set(ids) for role, ids in _role_map.items()}
    out = {}
    for hid, hero in _cache["heroes"].items():
        h = dict(hero)
        h["positions"] = [role for role, ids in role_sets.items() if int(hid) in ids]
        out[hid] = h
    return out


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


class HeroScoreRequest(BaseModel):
    hero_id:     int
    ally_picks:  list[int]        = Field(default=[], max_length=5)
    enemy_picks: list[int]        = Field(default=[], max_length=5)
    bans:        list[int]        = Field(default=[], max_length=14)
    mmr_bracket: str              = "7"
    weights:     dict[str, float] = Field(default={})
    role_filter: str              = Field(default="", max_length=20)


@app.post("/api/hero_score")
def hero_score(req: HeroScoreRequest, request: Request, authorization: Optional[str] = Header(None)):
    _check_rate_limit(request.client.host, "hero_score", max_per_minute=120)
    if not _cache["ready"]:
        raise HTTPException(503, "Cache not ready yet")
    if str(req.hero_id) not in _cache["heroes"]:
        raise HTTPException(400, f"Unknown hero ID: {req.hero_id}")

    # Resolve hero pool same as /api/recommend
    hero_pool = []
    user = _get_current_user(authorization)
    if user:
        profile = db.get_profile(user["id"])
        hero_pool = profile.get("hero_pool", []) if profile else []

    weights = {**DEFAULT_WEIGHTS, **req.weights} if req.weights else None
    matchups_for_bracket = _matchups_for_bracket(req.mmr_bracket)

    # Build candidate pool identically to /api/recommend
    all_hero_ids = [int(k) for k in _cache["heroes"].keys()]
    excluded = set(req.ally_picks + req.enemy_picks + req.bans)
    candidates = [h for h in all_hero_ids if h not in excluded]
    if req.role_filter and req.role_filter in _role_map:
        role_set = set(_role_map[req.role_filter])
        candidates = [h for h in candidates if h in role_set]
    if req.hero_id not in candidates:
        candidates.append(req.hero_id)  # include target even if outside role filter

    result = score_candidates(
        candidate_ids=candidates,
        enemy_pick_ids=req.enemy_picks,
        ally_pick_ids=req.ally_picks,
        all_matchups=matchups_for_bracket,
        hero_stats=_cache["hero_stats"],
        heroes=_cache["heroes"],
        mmr_bracket=req.mmr_bracket,
        weights=weights,
        top_n=len(candidates),
        hero_pool=hero_pool,
    )
    target = next((r for r in result["top"] if r["hero_id"] == req.hero_id), None)
    if not target:
        raise HTTPException(404, "No score data for this hero")
    return target


@app.post("/api/recommend")
def recommend(req: RecommendRequest, request: Request, authorization: Optional[str] = Header(None)):
    _check_rate_limit(request.client.host, "recommend", max_per_minute=60)
    if not _cache["ready"]:
        raise HTTPException(503, "Cache not ready yet")

    # Validate all requested heroes exist
    all_hero_ids = set(int(k) for k in _cache["heroes"].keys())
    requested_ids = set(req.ally_picks + req.enemy_picks + req.bans)
    missing = requested_ids - all_hero_ids
    if missing:
        raise HTTPException(400, f"Unknown hero IDs: {sorted(missing)}")

    # Resolve hero pool from logged-in user's profile
    hero_pool = []
    user = _get_current_user(authorization)
    if user:
        profile = db.get_profile(user["id"])
        hero_pool = profile.get("hero_pool", []) if profile else []

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
    logger.info(f"Enemy predictions before filter: {len(enemy_top)}, enemy_role_filter={repr(req.enemy_role_filter)}")
    if req.enemy_role_filter and req.enemy_role_filter in _role_map:
        enemy_role_set = set(_role_map[req.enemy_role_filter])
        enemy_top = [h for h in enemy_top if h["hero_id"] in enemy_role_set]
        logger.info(f"After role filter: {len(enemy_top)} heroes in role '{req.enemy_role_filter}'")
    enemy_top = enemy_top[:15]
    logger.info(f"Final enemy predictions: {len(enemy_top)}")

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
    if not (1 <= len(req.radiant) <= 5 and 1 <= len(req.dire) <= 5):
        raise HTTPException(400, "Need 1-5 heroes per team")

    m = _matchups_for_bracket(req.mmr_bracket)
    result = analyze_draft(
        radiant_ids=req.radiant,
        dire_ids=req.dire,
        vs_matchups=m["vs"],
        with_matchups=m["with"],
        hero_stats=_cache["hero_stats"],
        heroes=_cache["heroes"],
        bracket=req.mmr_bracket,
        role_map=_role_map,
    )
    result["complete"] = len(req.radiant) == 5 and len(req.dire) == 5
    result["picks"] = len(req.radiant) + len(req.dire)
    return result


class ChatRequest(BaseModel):
    question:    str                  = Field(..., max_length=2000)
    radiant:     list[int]            = Field(default=[], max_length=5)
    dire:        list[int]            = Field(default=[], max_length=5)
    my_team:     str                  = "radiant"
    mmr_bracket: str                  = "7"
    history:     list[dict[str, str]] = Field(default=[], max_length=20)
    weights:     dict[str, float]     = Field(default={})


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, authorization: Optional[str] = Header(None), _stream: bool = False):
    _check_rate_limit(request.client.host, "chat", max_per_minute=20)
    if not CHAT_ENABLED:
        raise HTTPException(503, "AI chat is not configured on this server (ANTHROPIC_API_KEY missing)")
    if not _cache.get("ready"):
        raise HTTPException(503, "Cache not ready yet")
    if not req.question.strip():
        raise HTTPException(400, "Empty question")

    user = _get_current_user(authorization)
    if not user:
        raise HTTPException(401, "Login required to use AI chat")

    chat_weights = {**DEFAULT_WEIGHTS, **req.weights} if req.weights else dict(DEFAULT_WEIGHTS)

    used = db.get_chat_count_today(user["id"])
    if used >= DAILY_CHAT_LIMIT:
        raise HTTPException(429, f"Daily chat limit of {DAILY_CHAT_LIMIT} messages reached — resets at midnight UTC")

    # Load user profile, refresh Stratz stats if linked
    user_profile = None
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
            weights=chat_weights,
            top_n=10,
            hero_pool=hero_pool,
        )
        recommendations = result.get("top", [])

    return _run_chat(req, user, used, user_profile, recommendations, chat_weights, stream=_stream)


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest, request: Request, authorization: Optional[str] = Header(None)):
    """Same as /api/chat but streams SSE: data:{"delta":...} lines, then data:{"done":true,...}."""
    return chat(req, request, authorization, _stream=True)


def _run_chat(req, user, used, user_profile, recommendations, chat_weights, stream):
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
            weights=chat_weights,
            stream=stream,
        )
        remaining = DAILY_CHAT_LIMIT - used - 1
        if not stream:
            db.increment_chat_count(user["id"])
            return {"reply": reply, "chats_remaining": remaining}

        def _sse():
            try:
                for delta in reply:
                    yield "data: " + json.dumps({"delta": delta}) + "\n\n"
                db.increment_chat_count(user["id"])
                yield "data: " + json.dumps({"done": True, "chats_remaining": remaining}) + "\n\n"
            except Exception as e:  # mid-stream failure: tell the client instead of dropping
                logger.exception("Chat stream error")
                yield "data: " + json.dumps({"error": _chat_error_message(e)}) + "\n\n"
        return StreamingResponse(_sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as e:
        logger.exception("Chat endpoint error")
        raise HTTPException(_chat_error_status(e), _chat_error_message(e))


def _chat_error_status(e: Exception) -> int:
    if isinstance(e, _anthropic.RateLimitError): return 429
    if isinstance(e, _anthropic.APIConnectionError): return 502
    return 500


def _chat_error_message(e: Exception) -> str:
    if isinstance(e, _anthropic.AuthenticationError): return "Anthropic API key is invalid or missing"
    if isinstance(e, _anthropic.RateLimitError): return "AI rate limit reached — try again in a moment"
    if isinstance(e, _anthropic.APIConnectionError): return "Could not connect to Anthropic API"
    return "An error occurred processing your request"


@app.get("/api/chat/quota")
def chat_quota(authorization: Optional[str] = Header(None)):
    user = _get_current_user(authorization)
    if not user:
        raise HTTPException(401, "Not authenticated")
    used = db.get_chat_count_today(user["id"])
    return {"used": used, "limit": DAILY_CHAT_LIMIT, "remaining": max(0, DAILY_CHAT_LIMIT - used)}


# ---------------------------------------------------------------------------
# Dota 2 Game State Integration (local only)
# The Dota client POSTs its state to /api/gsi; the UI polls /api/gsi/state.
# ---------------------------------------------------------------------------
import re as _re
import secrets as _secrets

_GSI_CFG_NAME = "gamestate_integration_draft_assistant.cfg"
_GSI_TOKEN_FILE = TMP_DIR / "gsi_token.txt"
_GSI_STALE_SECS = 15.0
_gsi_lock = threading.Lock()
_gsi_state: dict = {
    "updated_at": 0.0, "match_id": None, "game_state": "",
    "radiant": [], "dire": [], "bans": [], "my_team": None, "active_team": None, "picking": None,
    "partial": True, "own_hero": None,   # partial: the game only told us our own hero + side
}


def _gsi_token() -> str:
    try:
        if _GSI_TOKEN_FILE.exists():
            t = _GSI_TOKEN_FILE.read_text().strip()
            if t:
                return t
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        t = _secrets.token_hex(16)
        _GSI_TOKEN_FILE.write_text(t)
        return t
    except Exception:
        return "draft-assistant"


def _gsi_candidate_dirs() -> list[Path]:
    """Likely Dota 2 cfg/gamestate_integration folders on this machine."""
    roots = []
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Steam")
    for drive in "CDEF":
        roots += [Path(f"{drive}:/Steam"), Path(f"{drive}:/SteamLibrary"), Path(f"{drive}:/Games/Steam")]
    # Steam library folders listed in libraryfolders.vdf
    for r in list(roots):
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            try:
                for m in _re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore")):
                    roots.append(Path(m.group(1).replace("\\\\", "/")))
            except Exception:
                pass
    out, seen = [], set()
    for r in roots:
        d = r / "steamapps" / "common" / "dota 2 beta" / "game" / "dota" / "cfg" / "gamestate_integration"
        key = str(d).lower()
        if key not in seen and d.parent.exists():
            seen.add(key)
            out.append(d)
    return out


def _gsi_cfg_text(port: int) -> str:
    return f'''"Dota 2 Draft Assistant"
{{
    "uri"           "http://127.0.0.1:{port}/api/gsi"
    "timeout"       "5.0"
    "buffer"        "0.1"
    "throttle"      "0.2"
    "heartbeat"     "10.0"
    "data"
    {{
        "provider"  "1"
        "map"       "1"
        "player"    "1"
        "hero"      "1"
        "draft"     "1"
    }}
    "auth"
    {{
        "token"     "{_gsi_token()}"
    }}
}}
'''


def _parse_gsi_payload(p: dict) -> dict:
    """Pull picks/bans/team/phase out of a raw GSI payload. Returns a partial update dict."""
    upd: dict = {}
    m = p.get("map") or {}
    if m:
        upd["match_id"] = m.get("matchid") or None
        upd["game_state"] = m.get("game_state") or ""
    pl = p.get("player") or {}
    team_name = (pl.get("team_name") or "").lower()
    if team_name in ("radiant", "dire"):
        upd["my_team"] = team_name

    draft = p.get("draft") or {}
    if draft:
        picks = {"radiant": [], "dire": []}
        bans: list[int] = []
        for key, team in (("team2", "radiant"), ("team3", "dire")):
            block = draft.get(key) or {}
            slots = {}
            for k, v in block.items():
                mm = _re.match(r"^(pick|ban)(\d+)_id$", k)
                if not mm:
                    continue
                try:
                    hid = int(v)
                except (TypeError, ValueError):
                    continue
                if hid > 0:
                    slots[(mm.group(1), int(mm.group(2)))] = hid
            for (kind, idx) in sorted(slots):
                (picks[team] if kind == "pick" else bans).append(slots[(kind, idx)])
        upd["radiant"], upd["dire"], upd["bans"] = picks["radiant"], picks["dire"], bans
        at = draft.get("activeteam")
        upd["active_team"] = {2: "radiant", 3: "dire"}.get(at) if isinstance(at, int) else None
        upd["picking"] = draft.get("pick")
    else:
        hero = p.get("hero") or {}
        # Spectator / observer shape: hero.team2.player0.id … hero.team3.player4.id
        spect = {"radiant": [], "dire": []}
        for key, team in (("team2", "radiant"), ("team3", "dire")):
            block = hero.get(key) or {}
            for pk in sorted(k for k in block if k.startswith("player")):
                hid = (block.get(pk) or {}).get("id")
                if isinstance(hid, int) and hid > 0:
                    spect[team].append(hid)
        if spect["radiant"] or spect["dire"]:
            upd["radiant"], upd["dire"] = spect["radiant"], spect["dire"]
        else:
            # Player shape in-game: only the local hero is known
            hid = hero.get("id")
            if isinstance(hid, int) and hid > 0 and team_name in ("radiant", "dire"):
                upd.setdefault("_own_hero", (team_name, hid))
    return upd


import collections as _collections
_gsi_raw: _collections.deque = _collections.deque(maxlen=60)
_GSI_RAW_LOG = TMP_DIR / "gsi_raw.jsonl"


def _gsi_record_raw(payload: dict) -> None:
    """Keep recent raw payloads (minus auth) so the draft block shape can be inspected."""
    p = {k: v for k, v in payload.items() if k != "auth"}
    entry = {"ts": time.time(), "keys": sorted(p.keys()),
             "game_state": ((p.get("map") or {}).get("game_state")), "payload": p}
    _gsi_raw.append(entry)
    # Persist draft-phase payloads only (they're the ones we need to study)
    if p.get("draft"):   # Valve sends draft:{} to players; only keep the rare non-empty ones
        try:
            with _GSI_RAW_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


@app.get("/api/gsi/raw")
def gsi_raw(request: Request, n: int = 5):
    if not _is_local(request):
        raise HTTPException(403, "GSI is local only")
    items = list(_gsi_raw)[-max(1, min(n, 60)):]
    return {"count": len(_gsi_raw), "recent": items}


@app.post("/api/gsi")
async def gsi_ingest(request: Request):
    if not _is_local(request):
        raise HTTPException(403, "GSI is local only")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    token = ((payload.get("auth") or {}).get("token") or "")
    if token != _gsi_token():
        raise HTTPException(403, "Bad GSI token")
    upd = _parse_gsi_payload(payload)
    _gsi_record_raw(payload)
    with _gsi_lock:
        if upd.get("match_id") and upd["match_id"] != _gsi_state.get("match_id"):
            # New match: forget the previous draft
            _gsi_state.update(radiant=[], dire=[], bans=[], active_team=None, picking=None)
        own = upd.pop("_own_hero", None)
        full = "radiant" in upd   # a real draft/spectator block was present
        _gsi_state.update(upd)
        _gsi_state["partial"] = not full
        _gsi_state["own_hero"] = own[1] if own else (_gsi_state.get("own_hero") if not full else None)
        if own:
            team, hid = own
            if hid not in _gsi_state[team] and hid not in _gsi_state["radiant"] + _gsi_state["dire"]:
                _gsi_state[team] = list(_gsi_state[team]) + [hid]
        _gsi_state["updated_at"] = time.time()
    return {"ok": True}


@app.get("/api/gsi/state")
def gsi_state(request: Request):
    if not _is_local(request):
        raise HTTPException(403, "GSI is local only")
    with _gsi_lock:
        s = dict(_gsi_state)
    age = time.time() - s["updated_at"] if s["updated_at"] else None
    s["connected"] = age is not None and age < _GSI_STALE_SECS
    s["age"] = round(age, 1) if age is not None else None
    s["in_draft"] = s["game_state"] == "DOTA_GAMERULES_STATE_HERO_SELECTION"
    return s


@app.get("/api/gsi/setup")
def gsi_setup(request: Request):
    """Where the cfg would go / already is."""
    if not _is_local(request):
        raise HTTPException(403, "GSI is local only")
    dirs = _gsi_candidate_dirs()
    installed = [str(d / _GSI_CFG_NAME) for d in dirs if (d / _GSI_CFG_NAME).exists()]
    return {"candidates": [str(d) for d in dirs], "installed": installed, "cfg_name": _GSI_CFG_NAME}


@app.post("/api/gsi/install")
def gsi_install(request: Request):
    """Write the GSI cfg into every detected Dota 2 install. Dota reads it on next launch."""
    if not _is_local(request):
        raise HTTPException(403, "GSI is local only")
    dirs = _gsi_candidate_dirs()
    if not dirs:
        raise HTTPException(404, "Could not find a Dota 2 install (…/dota 2 beta/game/dota/cfg). Install the cfg manually.")
    port = int(os.environ.get("PORT", 8000))
    written = []
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / _GSI_CFG_NAME).write_text(_gsi_cfg_text(port), encoding="utf-8")
            written.append(str(d / _GSI_CFG_NAME))
        except Exception as e:
            logger.warning("GSI cfg write failed for %s: %s", d, e)
    if not written:
        raise HTTPException(500, "Found Dota 2 but could not write the cfg (permissions?)")
    return {"written": written}


@app.get("/api/gsi/cfg")
def gsi_cfg(request: Request):
    """Raw cfg text for manual installation."""
    if not _is_local(request):
        raise HTTPException(403, "GSI is local only")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_gsi_cfg_text(int(os.environ.get("PORT", 8000))))


class RefreshRequest(BaseModel):
    force: bool = False


@app.post("/api/refresh")
def refresh(req: RefreshRequest, request: Request):
    if not _is_local(request):
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

    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    if host == "127.0.0.1":
        print("Starting Dota 2 Draft Assistant...")
        print(f"Opening http://127.0.0.1:{port} in your browser.")
        print("First run will cache hero data (~2-3 minutes). Subsequent runs are instant.\n")
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
