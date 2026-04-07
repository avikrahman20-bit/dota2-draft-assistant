# Dota 2 Draft Assistant

Real-time Dota 2 drafting tool. Recommends counter-picks and synergy picks using live Stratz matchup data, scores candidates by bracket, and answers draft questions via Claude AI.

---

## Features

- Counter-pick recommendations using live Stratz win-rate data
- Synergy scoring (co-pick win rates, not guessed combos)
- Enemy threat analysis — which of their picks hurts you most
- Enemy pick prediction — what they're most likely to draft next
- Win probability for completed 5v5 drafts
- Claude AI chat assistant with full draft context and patch notes
- User accounts with hero pool, role preferences, and playstyle profile
- Stratz account linking — AI uses your real match history

---

## Requirements

- Python 3.12+
- [Stratz API key](https://stratz.com/api) (free)
- [Anthropic API key](https://console.anthropic.com/) (for AI chat)

---

## Setup

```bash
# 1. Clone
git clone <repo-url>
cd "Dota 2 Draft Assistant"

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env and fill in STRATZ_API_KEY and ANTHROPIC_API_KEY

# 4. Run
python app.py
```

Opens at `http://127.0.0.1:8000` automatically.

**First run:** caches hero + matchup data from Stratz (~2-3 minutes). Progress shown in-app.  
**Subsequent runs:** instant (served from `.tmp/`).

---

## Configuration

All config lives in `.env`:

| Variable | Required | Description |
|---|---|---|
| `STRATZ_API_KEY` | Yes | Stratz GraphQL API key |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for AI chat |
| `JWT_SECRET` | Auto | Generated on first run — do not change unless migrating |

---

## Development

```bash
# Run server
python app.py

# Run tests
python -m pytest tests/ -v

# Force-refresh Stratz data cache
# POST http://127.0.0.1:8000/api/refresh   (localhost only)

# Kill server (Windows)
powershell -Command "Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force"
```

**Backend changes** → restart server.  
**Frontend changes** → Ctrl+Shift+R in browser.

---

## Architecture

```
Browser (SPA)          FastAPI (app.py)             External
─────────────          ───────────────              ────────
index.html             /api/status                  Stratz GraphQL  (matchups)
app.js    ←──────────→ /api/heroes                  Stratz REST     (heroes, stats)
style.css              /api/recommend      ────────→ Anthropic Claude (chat)
                       /api/draft_analysis           Dota2 Datafeed  (patch notes)
                       /api/chat
                       /api/register, /api/login
                       /api/profile (GET/PUT)
                       /api/link_account
```

**Key files:**
- `app.py` — FastAPI server, all endpoints, cache loading
- `tools/scoring_engine.py` — pure scoring logic, no I/O
- `tools/assistant.py` — Claude chat with live meta context
- `database.py` — SQLite user accounts and profiles
- `auth.py` — JWT + bcrypt
- `.tmp/` — disposable Stratz data cache (delete to re-fetch)

---

## Scoring Weights

Default weights (user-adjustable in UI):

| Component | Weight | Description |
|---|---|---|
| counter | 0.55 | Win rate vs enemy picks |
| synergy | 0.20 | Co-pick win rate with allies |
| win_rate | 0.15 | Overall bracket win rate |
| hero_pool | 0.05 | Boost for heroes in your saved pool |
| meta | 0.05 | Boost for high-pickrate meta heroes |

Inactive components (e.g. synergy with no allies) have their weight redistributed automatically.
