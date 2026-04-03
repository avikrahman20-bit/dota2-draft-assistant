# Workflow: Dota 2 Draft Assistant

## Objective
Provide real-time hero pick recommendations during a Dota 2 ranked draft by analyzing counter-pick strength, patch win rates, and role synergy against the live draft state.

## Prerequisites

**Install Python dependencies (one-time):**
```bash
pip install fastapi uvicorn httpx
```

**Optional:** Add your free OpenDota API key to `.env` to increase rate limits:
```
OPENDOTA_API_KEY=your_key_here
```
Get a free key at https://www.opendota.com/api-keys

## How to Run

```bash
python app.py
```

The browser opens automatically at `http://127.0.0.1:8000`.

- **Cold start (first run):** ~2–3 minutes while matchup data is cached for all heroes (~124 heroes × 1.1s delay). A progress bar shows status.
- **Warm start (subsequent runs):** Instant — all data loads from `.tmp/` cache.

## How to Use

1. **Set your team** — Select Radiant or Dire in the top-right dropdown.
2. **Input picks/bans** — As the draft progresses, search for heroes in the picker and click them. Use the "Add as" buttons to specify whether it's your pick, enemy pick, or ban.
3. **Read recommendations** — The panel shows the top 15 hero suggestions ranked by score, with counter details and win rate.
4. **Remove a hero** — Hover over any slot on the draft board and click the ×.
5. **Reset** — Click "Reset Draft" to clear the board for a new game.

## Scoring Algorithm

Each candidate hero (not yet picked/banned) is scored by three components:

| Component | Default Weight | Description |
|---|---|---|
| Counter Score | 55% | Average win-rate advantage vs each enemy pick |
| Win Rate | 25% | Hero's win rate at Divine/Immortal bracket |
| Role Synergy | 20% | Fills role gaps not covered by allies |

**Counter Score** is derived from OpenDota's matchup data: for each enemy hero, how often does this candidate win when facing them? Advantage = win_rate_vs_enemy − 0.50.

Weights are adjustable via the "⚙ Adjust Scoring Weights" panel and are saved locally between sessions.

## Data Sources

All data comes from the [OpenDota Public API](https://api.opendota.com):
- `GET /heroes` — Hero list, roles, attributes
- `GET /heroStats` — Win rates by MMR bracket (uses bracket 8 = Divine/Immortal)
- `GET /heroes/{id}/matchups` — Head-to-head matchup win rates

## Cache Management

Cache files live in `.tmp/`:
```
.tmp/
  heroes.json          # Hero list + image URLs (refreshed every 24h)
  hero_stats.json      # Win rates by bracket (refreshed every 24h)
  matchups/
    {hero_id}_matchups.json   # Per-hero matchup data (~124 files)
```

**Force a full refresh** (e.g., after a major patch):
```bash
python tools/fetch_matchups.py --force
```
Or click **"↻ Refresh Data"** in the app header.

## Troubleshooting

**Server starts but takes longer than expected:**
- Normal on first run. OpenDota API requests are throttled at 1.1s between calls.
- If you have an API key in `.env`, this delay is reduced significantly.

**HTTP 429 rate-limit errors in the console:**
- The fetch scripts already handle this with 1.1s delays.
- If errors persist, add an API key to `.env` or wait and re-run.

**Hero images not loading:**
- Images are served from `cdn.cloudflare.steamstatic.com`. Requires internet access.
- If Valve updates hero names, re-run with `--force` to refresh the cache.

**Recommendations don't change:**
- Make sure you've clicked the correct "Add as" target (My Pick / Enemy Pick / Ban) before clicking a hero.

## Known Limitations

- **No with-teammate synergy data:** OpenDota's public API provides head-to-head (adversarial) matchup data only. Ally synergy is approximated via role gap-filling, not actual "hero A wins more with hero B" data.
- **Role detection is tag-based:** Roles are from OpenDota's hero role tags (Carry, Support, etc.), not actual positional data. A hero tagged "Carry" and "Support" may count for both.
- **No MMR bracket filter for matchups:** Matchup data is aggregated across all brackets. Win rates use Divine/Immortal (bracket 8) but matchup files don't separate by bracket.

## Self-Improvement Loop

When you encounter issues or find better approaches:
1. Fix the relevant tool script in `tools/`
2. Verify the fix works
3. Update this workflow with the new approach and any discovered constraints
