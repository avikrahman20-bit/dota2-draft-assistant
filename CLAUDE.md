# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Response Rules (MANDATORY)
- No preamble. No "I'll now...", "Sure!", "Great question!". Lead with action.
- No trailing summaries. User reads the diff.
- Caveman mode: short phrases. "Fix X in Y" not "The user wants me to fix X in Y so I will open the file and..."
- Targeted edits only — never rewrite whole files for small changes.
- Read each file once. No re-reads of unchanged files.
- **COMMUNICATION MODE: Minimal mode ON.**
- Max 1–2 sentences unless necessary.
- No fluff, no politeness, no filler.
- No step-by-step unless asked.
- Output only what is needed to proceed.
- Code > explanation.
- If explanation needed: 1–2 lines max.
- `/compact` when context grows large.

## What This Is
Real-time Dota 2 draft tool. Counter-pick recs, synergy analysis, win probability, AI draft advice. Backed by live Stratz data.

## Run
```bash
python app.py   # → http://127.0.0.1:8000
```
Python path: `C:\Users\Khan Gadget\AppData\Local\Programs\Python\Python312\python.exe`

First run caches hero data (~2-3 min). Subsequent runs instant (served from `.tmp/`).

Kill server: `powershell -Command "Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force"`

Backend change → restart server. Frontend change → Ctrl+Shift+R.

## Architecture

```
Browser (SPA)              FastAPI (app.py)                External
─────────────              ───────────────                 ────────
index.html                 /api/status                     Stratz GraphQL  (matchups)
app.js        ←──────────→ /api/heroes                     Stratz REST     (hero list, stats, player)
style.css                  /api/recommend          ──────→ Anthropic Claude (chat)
                           /api/draft_analysis             Dota2 Datafeed  (patch notes)
                           /api/chat
                           /api/refresh
                           /api/register, /api/login
                           /api/profile (GET/PUT)
                           /api/link_account, /api/player_stats
```

## Files
- `app.py` — FastAPI server, all endpoints, cache loading, `compute_threats()`
- `auth.py` — JWT (HS256, 30-day expiry) + bcrypt password hashing; `JWT_SECRET` auto-generated into `.env`
- `database.py` — SQLite (`users.db`); tables: `users`, `user_profiles`, `chat_feedback`; auto-migrates schema
- `tools/fetch_hero_data.py` — Stratz REST: hero list + bracket win rates → `.tmp/heroes.json`, `.tmp/hero_stats.json`, `.tmp/role_map.json`
- `tools/fetch_matchups.py` — Stratz GraphQL: vs + with matchup data per hero → `.tmp/matchups_stratz/<hero_id>.json`
- `tools/scoring_engine.py` — Pure scoring logic (no I/O); `score_candidates()` + `analyze_draft()`
- `tools/assistant.py` — Claude chat; injects live meta + patch notes + draft state as context
- `tools/patch_notes.py` — Fetches dota2.com datafeed patch notes (1-hour TTL)
- `tools/fetch_player.py` — Stratz player summary by Dota2 Friend ID
- `.env` — `STRATZ_API_KEY`, `ANTHROPIC_API_KEY`, `JWT_SECRET` (auto-appended)
- `.tmp/` — Disposable cache. Delete to force re-fetch from Stratz.

## Key Data Flows

### Recommendation request (`/api/recommend`)
1. Resolve user's hero pool from DB (if logged in)
2. Filter candidates by role_filter + exclude picked/banned heroes
3. Slice `_cache["matchups"]` to the requested bracket enum
4. `score_candidates()` → top 20 for your team
5. `compute_threats()` → per-enemy hero, avg win rate vs all your picks (sorted desc)
6. `score_candidates()` again (swapped ally/enemy) → top 15 enemy predictions
7. Return `{top, all_scores, threats, enemy_predictions}`

### Cache structure
```python
_cache["matchups"] = {
    "vs":   {bracket_enum: {hero_id: {opp_id: {"win_rate": float, "games": int}}}},
    "with": {bracket_enum: {hero_id: {ally_id: {"win_rate": float, "games": int}}}}
}
```
`vs[bracket][hero_id][opp_id]["win_rate"]` = hero_id's win rate AGAINST opp_id (>0.5 = counters)
`with[bracket][hero_id][ally_id]["win_rate"]` = hero_id's win rate ON SAME TEAM as ally_id

## Scoring Engine
- Weights: `counter=0.55, win_rate=0.15, synergy=0.20, hero_pool=0.05, meta=0.05` (user-adjustable)
- All components normalized to [0,1]; inactive components (no allies = no synergy) auto-redistributed
- Bayesian shrinkage k=400: low-sample matchups regress toward 50%
- Synergy = Stratz co-pick win rate, NOT thematic/mechanical combos — LC+Sky won't appear if co-pick WR is mediocre
- Win probability: sigmoid `1/(1+exp(-raw*10))*100`, components: matchup 65%, win rate 10%, synergy 25%

## Bracket Enum Mismatch (Common Pitfall)
`fetch_hero_data.py` uses `"IMMORTAL"` but `fetch_matchups.py` uses `"DIVINE_IMMORTAL"` for the same bracket. Each module has its own `BRACKET_ENUM` map. Always check which module you're touching.

## Frontend State (`app.js`)
- `state.add_target` — `"my-pick"` or `"enemy-pick"`. All hero-add paths (hero grid, rec cards, enemy prediction cards) MUST check this before deciding which team array to push to.
- `state.threats` — array from backend, one entry per enemy hero (not per pair). Fields: `enemy_id, enemy_name, enemy_img, enemy_roles, avg_win_rate, matchups[]`.
- `state.recommendations` — top 20 scored heroes for your team
- `state.enemy_predictions` — top 15 scored heroes for the enemy team

## Auth Flow
- Register/login → JWT returned → stored in `localStorage` as `authToken`
- All protected endpoints read `Authorization: Bearer <token>` header
- Profile stores: `preferred_roles[]`, `hero_pool[]`, `playstyle_tags[]`, `playstyle_notes`, `custom_weights{}`, `dota_account_id`, `player_stats{}`

## Chat Assistant
- Model: `claude-haiku-4-5-20251001`
- Context injected per query: top heroes per role (win rate + pick count) + latest patch notes + current draft state
- Meta score = `win_rate * (picks/max_picks)^0.3` to suppress inflated low-sample win rates

## Common Pitfalls
- Browser caching: Ctrl+Shift+R after any frontend change
- Patch notes API uses `hero_id` (int) not names — must resolve via heroes.json cache
- `compute_threats` returns one entry per enemy hero (grouped), not per (enemy, ally) pair
- Adding heroes from rec/prediction cards must respect `state.add_target` (was a bug, now fixed)
