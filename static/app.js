/* =============================================================
   Dota 2 Draft Assistant — Frontend
   ============================================================= */

// ── State ────────────────────────────────────────────────────
const DEFAULT_WEIGHTS = { counter: 0.55, win_rate: 0.15, synergy: 0.20, hero_pool: 0.05, meta: 0.05 };
const WEIGHTS_KEY = 'draft_weights_v2';

const state = {
  radiant_picks: [],   // [hero_id, ...]  max 5
  dire_picks: [],      // [hero_id, ...]  max 5
  bans: [],            // [hero_id, ...]  max 12
  my_team: 'radiant',
  add_target: 'my-pick',  // 'my-pick' | 'enemy-pick' | 'ban'
  heroes: {},          // hero_id (int) -> hero object
  heroList: [],        // sorted array of heroes for grid
  recommendations: [],
  allScores: {},       // hero_id (str) -> total_score, for grid coloring
  threats: [],         // [{enemy_id, enemy_name, vs_ally_id, vs_ally_name, win_rate}]
  enemy_predictions: [],
  enemy_role_filter: '',
  weights: loadWeights(),
  mmr_bracket: loadMmrBracket(),
  role_filter: loadRoleFilter(),
  can_refresh: false,   // /api/refresh only works from localhost
  chat_enabled: true,   // false when server has no ANTHROPIC_API_KEY
  data_updated_at: 0,    // unix seconds of the last real Stratz fetch (from /api/status)
  patch_name: '',
  show_all_recs: true,   // stacked layout shows the full list
  active_tab: localStorage.getItem('insight_tab') || 'recs',
  draft_analysis: null,
};

const ROLE_LABEL = { carry: 'Carry', mid: 'Mid', offlane: 'Offlane', support: 'Support', hard_support: 'Hard Support' };
const ROLE_SHORT = { carry: '1', mid: '2', offlane: '3', support: '4', hard_support: '5' };
const LOW_DATA_GAMES = 200;

function scoreTier(total) { return total >= 0.7 ? 'score-high' : total >= 0.5 ? 'score-mid' : 'score-low'; }
function tierColor(total) { return total >= 0.7 ? 'var(--tier-a)' : total >= 0.5 ? 'var(--tier-b)' : 'var(--tier-c)'; }
function pct1(v) { return v != null ? (v * 100).toFixed(1) : '?'; }

function rolePills(heroId) {
  const pos = state.heroes[heroId]?.positions || [];
  return pos.map(r => `<span class="role-pill" title="${ROLE_LABEL[r]}">${ROLE_SHORT[r]}</span>`).join('');
}

/** Human-readable reasons for a scored hero. counters_detail is sorted best-first. */
function buildReasons(rec, opts = {}) {
  const bd = rec.breakdown || {};
  const detail = bd.counters_detail || [];
  const good = detail.filter(c => c.advantage > 0.005).slice(0, 2);
  const bad  = detail.filter(c => c.advantage < -0.005).slice(0, 2);
  const parts = [];
  for (const c of good) parts.push(`<span class="good">Counters <b>${_esc(c.vs_hero)}</b> ${pct1(c.win_rate)}%</span>`);
  for (const c of bad)  parts.push(`<span class="bad">Weak vs <b>${_esc(c.vs_hero)}</b> ${pct1(c.win_rate)}%</span>`);
  if (bd.win_rate_pct != null) parts.push(`${bd.win_rate_pct}% win rate`);
  if (opts.allies && bd.synergy_score > 0.6) parts.push(`<span class="good">Synergy with ${opts.enemyPerspective ? 'their' : 'your'} picks</span>`);
  if (opts.allies && bd.synergy_score < 0.4) parts.push(`<span class="bad">Poor synergy with ${opts.enemyPerspective ? 'their' : 'your'} picks</span>`);
  return parts.join(' · ');
}

function lowDataPill(rec) {
  const detail = (rec.breakdown || {}).counters_detail || [];
  const thin = detail.filter(c => (c.games || 0) < LOW_DATA_GAMES);
  if (!detail.length || !thin.length) return '';
  const names = thin.map(c => `${c.vs_hero}: ${c.games || 0} games`).join(', ');
  return `<span class="role-pill pill-lowdata" title="Small sample vs ${_esc(names)}. Scores are shrunk toward 50% when data is thin.">low data</span>`;
}

function breakdownHtml(rec, opts = {}) {
  const bd = rec.breakdown || {};
  const w = state.weights;
  const rows = [
    ['Counter',  Math.max(0, Math.min(1, 0.5 + (bd.counter_score || 0) * 10)), w.counter,   opts.enemies],
    ['Synergy',  bd.synergy_score ?? 0.5,   w.synergy,   opts.allies],
    ['Win rate', bd.win_rate_score ?? 0.5,  w.win_rate,  true],
    ['Meta',     bd.meta_score ?? 0.5,      w.meta,      true],
    ['Pool',     bd.hero_pool_score ?? 0,   w.hero_pool, opts.pool],
  ];
  return `<div class="rec-breakdown">` + rows.map(([label, v, weight, active]) => `
    <span class="${active ? '' : 'bd-off'}">${label} <span style="opacity:.6">×${weight.toFixed(2)}</span></span>
    <div class="bd-bar ${active ? '' : 'bd-off'}"><div style="width:${Math.round(v * 100)}%"></div></div>
    <span class="bd-val ${active ? '' : 'bd-off'}">${active ? Math.round(v * 100) : '—'}</span>`).join('') + `</div>`;
}

function attachInfoToggle(card) {
  const btn = card.querySelector('.rec-info-btn');
  const bd  = card.querySelector('.rec-breakdown');
  if (!btn || !bd) return;
  bd.hidden = true;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    bd.hidden = !bd.hidden;
    btn.setAttribute('aria-expanded', String(!bd.hidden));
  });
  btn.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') e.stopPropagation(); });
}

// ── Insight tabs ─────────────────────────────────────────────
function switchTab(name) {
  state.active_tab = name;
  localStorage.setItem('insight_tab', name);
  document.querySelectorAll('#insight-tabs .tab').forEach(t => {
    const on = t.dataset.tab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  document.querySelectorAll('.col-right .tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'lookup') document.getElementById('hero-lookup-input')?.focus();
}
function setupTabs() {
  document.querySelectorAll('#insight-tabs .tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
  document.getElementById('insight-tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const tabs = [...document.querySelectorAll('#insight-tabs .tab')];
    const i = tabs.findIndex(t => t.classList.contains('active'));
    const n = (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    switchTab(tabs[n].dataset.tab); tabs[n].focus();
  });
  switchTab(state.active_tab);
}
// Show/hide empty-state text + badges after each render
function updateTabState() {
  const hidden = id => document.getElementById(id)?.classList.contains('hidden');
  document.getElementById('threats-empty')?.classList.toggle('hidden', !hidden('threat-panel'));
  document.getElementById('enemy-empty')?.classList.toggle('hidden', !hidden('enemy-predictions-panel'));
  document.getElementById('winprob-empty')?.classList.toggle('hidden', !hidden('winprob-panel'));
  const badge = document.getElementById('badge-threats');
  const crit = (state.threats || []).filter(t => t.avg_win_rate >= 0.53).length;
  if (badge) { badge.textContent = crit; badge.classList.toggle('hidden', crit === 0 || hidden('threat-panel')); }
}

// ── Team needs (roles no ally covers) ────────────────────────
function renderTeamNeeds() {
  const el = document.getElementById('team-needs');
  if (!el) return;
  const allies = state.my_team === 'radiant' ? state.radiant_picks : state.dire_picks;
  if (!allies.length || allies.length >= 5) { el.classList.add('hidden'); return; }
  const covered = new Set();
  for (const id of allies) for (const r of (state.heroes[id]?.positions || [])) covered.add(r);
  const needs = Object.keys(ROLE_LABEL).filter(r => !covered.has(r));
  el.classList.remove('hidden');
  if (!needs.length) { el.innerHTML = `<span class="need-ok">Every position is covered by your picks.</span>`; return; }
  el.innerHTML = `<span>Team still needs:</span>` + needs.map(r =>
    `<button type="button" class="need-pill ${state.role_filter === r ? 'active' : ''}" data-role="${r}" title="Filter recommendations to ${ROLE_LABEL[r]}">${ROLE_LABEL[r]}</button>`).join('');
  el.querySelectorAll('.need-pill').forEach(b => b.addEventListener('click', () => {
    const r = state.role_filter === b.dataset.role ? '' : b.dataset.role;
    document.querySelectorAll('.role-filter-btn').forEach(x => x.classList.toggle('active', x.dataset.role === r));
    state.role_filter = r; saveRoleFilter(); fetchRecommendations();
  }));
}

// ── Live sync with the Dota 2 client (Game State Integration) ─
const gsi = { enabled: localStorage.getItem('gsi_enabled') === '1', installed: false, connected: false, lastSig: '', timer: null };

function draftSig(r, d, b, team) { return JSON.stringify([r, d, b, team]); }

function setLivePill(mode, text) {
  const pill = document.getElementById('live-pill');
  if (!pill) return;
  pill.classList.remove('hidden', 'on', 'waiting', 'off');
  pill.classList.add(mode);
  pill.textContent = text;
}

async function gsiRefreshSetup() {
  try {
    const r = await fetch('/api/gsi/setup');
    if (!r.ok) return;
    const d = await r.json();
    gsi.installed = d.installed.length > 0;
    const paths = document.getElementById('gsi-paths');
    if (paths) {
      paths.classList.toggle('hidden', !gsi.installed);
      paths.textContent = gsi.installed ? `Config: ${d.installed.join(', ')}` : '';
    }
    const btn = document.getElementById('gsi-install-btn');
    if (btn) btn.textContent = gsi.installed ? 'Reinstall game config' : 'Install game config';
    if (!d.candidates.length && btn) btn.title = 'Dota 2 install not found automatically';
  } catch (_) {}
  renderGsiStatus();
}

function renderGsiStatus() {
  const el = document.getElementById('gsi-status');
  if (!el) return;
  if (!gsi.installed) el.innerHTML = `<span class="warn">Not installed.</span> Install the config, then restart Dota 2.`;
  else if (!gsi.enabled) el.innerHTML = `Installed. Auto-fill is <b>off</b>.`;
  else if (gsi.connected) el.innerHTML = `<span class="ok">Connected</span> — your side and your locked hero come from the game. Enter the other picks and bans yourself: Dota doesn't share them with tools while you play.`;
  else el.innerHTML = `<span class="warn">Waiting for Dota 2…</span> Launch the game (after a restart) and the board will fill during hero selection.`;
}

async function gsiPoll() {
  if (!gsi.enabled) return;
  try {
    const r = await fetch('/api/gsi/state');
    if (!r.ok) throw new Error();
    const s = await r.json();
    const was = gsi.connected;
    gsi.connected = !!s.connected;
    if (gsi.connected) setLivePill('on', s.in_draft ? '● Live · drafting' : '● Live');
    else setLivePill('waiting', '○ Live · waiting for Dota');
    if (was !== gsi.connected) renderGsiStatus();
    if (!gsi.connected) return;

    const valid = id => state.heroes[id] != null;
    const r_ = (s.radiant || []).filter(valid), d_ = (s.dire || []).filter(valid), b_ = (s.bans || []).filter(valid);
    const sig = draftSig(r_, d_, b_, s.my_team) + '|' + (s.own_hero || '') + '|' + (s.partial ? 'p' : 'f');
    if (sig === gsi.lastSig) return;          // nothing new from the game; keep any manual edits
    gsi.lastSig = sig;

    let changed = false;
    if (s.my_team && s.my_team !== state.my_team) {
      state.my_team = s.my_team;
      localStorage.setItem('my_team', s.my_team);
      document.getElementById('my-team-select').value = s.my_team;
      updateAddTargetLabels(); updateYouBadge();
      changed = true;
    }

    if (s.partial) {
      // Valve only tells players their own hero + side. Merge: lock our hero, keep manual picks.
      const own = s.own_hero;
      if (own && valid(own) && !getUsedSet().has(own)) {
        const mine = state.my_team === 'radiant' ? state.radiant_picks : state.dire_picks;
        if (mine.length < 5) { pushUndo(); mine.push(own); changed = true; showToast(`Locked ${state.heroes[own].localized_name} from the game.`, 'success'); }
      }
    } else if (r_.length || d_.length || b_.length) {
      // Full draft (spectating / Captains Mode feed): replace the board
      pushUndo();
      state.radiant_picks = r_; state.dire_picks = d_; state.bans = b_.slice(0, MAX_BANS);
      if (s.picking != null && s.active_team) {
        setAddTarget(s.picking ? (s.active_team === state.my_team ? 'my-pick' : 'enemy-pick') : 'ban');
      }
      changed = true;
    }
    if (changed) onStateChange();
  } catch (_) {
    if (gsi.connected) { gsi.connected = false; renderGsiStatus(); }
    setLivePill('off', '○ Live');
  }
}

function setGsiEnabled(on) {
  gsi.enabled = on;
  localStorage.setItem('gsi_enabled', on ? '1' : '0');
  const cb = document.getElementById('gsi-enable'); if (cb) cb.checked = on;
  clearInterval(gsi.timer); gsi.timer = null;
  if (on) { gsi.lastSig = ''; gsiPoll(); gsi.timer = setInterval(gsiPoll, 1000); }
  else { gsi.connected = false; setLivePill('off', '○ Live off'); }
  renderGsiStatus();
}

function setupLiveSync() {
  document.getElementById('gsi-section')?.classList.remove('hidden');
  document.getElementById('gsi-enable').addEventListener('change', (e) => setGsiEnabled(e.target.checked));
  document.getElementById('gsi-install-btn').addEventListener('click', async () => {
    const btn = document.getElementById('gsi-install-btn');
    btn.disabled = true;
    try {
      const r = await fetch('/api/gsi/install', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `Error ${r.status}`);
      showToast(`Game config installed. Restart Dota 2 to activate.`, 'success', 6000);
      await gsiRefreshSetup();
      if (!gsi.enabled) setGsiEnabled(true);
    } catch (e) {
      showToast(e.message, 'error', 7000);
    } finally { btn.disabled = false; }
  });
  document.getElementById('live-pill').addEventListener('click', () => { openSettings(); document.getElementById('gsi-section').scrollIntoView({ block: 'nearest' }); });
  gsiRefreshSetup().then(() => {
    if (gsi.installed && gsi.enabled) setGsiEnabled(true);
    else if (gsi.installed) setLivePill('off', '○ Live off');
    else setLivePill('off', '○ Live setup');
  });
}

// ── Settings modal (weights) ─────────────────────────────────
function openSettings() {
  document.getElementById('settings-modal').classList.remove('hidden');
  document.getElementById('w-counter').focus();
}
function closeSettings() { document.getElementById('settings-modal').classList.add('hidden'); }

const MAX_BANS = 14;

// Stratz matchup data is aggregated into 4 cohorts; win rates are per exact bracket.
const MATCHUP_COHORT = {
  '7': 'Divine + Immortal', '6': 'Divine + Immortal',
  '5': 'Legend + Ancient',  '4': 'Legend + Ancient',
  '3': 'Crusader + Archon', '2': 'Crusader + Archon',
  '1': 'Herald + Guardian',
};

// Community shorthand → hero name. Keys are matched exactly against the search box.
const HERO_ALIASES = {
  aa: 'Ancient Apparition', abba: 'Abaddon', alch: 'Alchemist', am: 'Anti-Mage', bat: 'Batrider',
  bara: 'Spirit Breaker', bb: 'Bristleback', bh: 'Bounty Hunter', bm: 'Beastmaster', brew: 'Brewmaster',
  bs: 'Bloodseeker', cent: 'Centaur Warrunner', centaur: 'Centaur Warrunner', ck: 'Chaos Knight',
  clock: 'Clockwerk', cm: 'Crystal Maiden', dawn: 'Dawnbreaker', dk: 'Dragon Knight', dp: 'Death Prophet',
  drow: 'Drow Ranger', ds: 'Dark Seer', dusa: 'Medusa', ember: 'Ember Spirit', ench: 'Enchantress',
  es: 'Earthshaker', et: 'Elder Titan', furion: "Nature's Prophet", grim: 'Grimstroke', gyro: 'Gyrocopter',
  hood: 'Hoodwink', invo: 'Invoker', io: 'Io', jugg: 'Juggernaut', kotl: 'Keeper of the Light',
  lc: 'Legion Commander', ld: 'Lone Druid', lesh: 'Leshrac', ls: 'Lifestealer', mag: 'Magnus',
  mk: 'Monkey King', morph: 'Morphling', naga: 'Naga Siren', necro: 'Necrophos', np: "Nature's Prophet",
  ns: 'Night Stalker', od: 'Outworld Destroyer', ogre: 'Ogre Magi', omni: 'Omniknight', pa: 'Phantom Assassin',
  pango: 'Pangolier', pb: 'Primal Beast', pit: 'Underlord', pl: 'Phantom Lancer', potm: 'Mirana',
  primal: 'Primal Beast', qop: 'Queen of Pain', sand: 'Sand King', sb: 'Spirit Breaker', sd: 'Shadow Demon',
  sf: 'Shadow Fiend', sk: 'Sand King', sky: 'Skywrath Mage', snap: 'Snapfire', spec: 'Spectre',
  ss: 'Shadow Shaman', storm: 'Storm Spirit', ta: 'Templar Assassin', tb: 'Terrorblade', terror: 'Terrorblade',
  tide: 'Tidehunter', timber: 'Timbersaw', treant: 'Treant Protector', troll: 'Troll Warlord',
  venge: 'Vengeful Spirit', veno: 'Venomancer', void: 'Faceless Void', vs: 'Vengeful Spirit',
  wd: 'Witch Doctor', wisp: 'Io', wk: 'Wraith King', wl: 'Warlock', wr: 'Windranger', ww: 'Winter Wyvern',
  wyvern: 'Winter Wyvern',
};

const ATTR_GROUPS = [
  ['str', 'Strength'], ['agi', 'Agility'], ['int', 'Intelligence'], ['all', 'Universal'],
];

/**
 * Rank a hero against a search query. Lower is better; null = no match.
 * 0 exact name/alias, 1 name starts with, 2 a word starts with, 3 initials, 4 substring.
 */
function heroMatchRank(hero, q) {
  const name = hero.localized_name.toLowerCase();
  if (!q) return 4;
  if (name === q) return 0;
  const alias = HERO_ALIASES[q];
  if (alias && alias.toLowerCase() === name) return 0;
  if (name.startsWith(q)) return 1;
  const words = name.split(/[\s'-]+/);
  const qWords = q.split(/\s+/).filter(Boolean);
  // every query token must prefix some name word, in order ("sky m" → Skywrath Mage)
  let wi = 0, ok = true;
  for (const qw of qWords) {
    while (wi < words.length && !words[wi].startsWith(qw)) wi++;
    if (wi >= words.length) { ok = false; break; }
    wi++;
  }
  if (ok) return 2;
  const initials = words.map(w => w[0]).join('');
  if (initials === q.replace(/\s+/g, '')) return 3;
  if (name.includes(q)) return 4;
  return null;
}

function searchHeroes(q) {
  q = (q || '').trim().toLowerCase();
  if (!q) return state.heroList.slice();
  return state.heroList
    .map(h => ({ h, r: heroMatchRank(h, q) }))
    .filter(x => x.r !== null)
    .sort((a, b) => a.r - b.r || a.h.localized_name.localeCompare(b.h.localized_name))
    .map(x => x.h);
}

// ── Undo stack ───────────────────────────────────────────────
const undoStack = [];
function _draftSnapshot() {
  return {
    radiant: [...state.radiant_picks], dire: [...state.dire_picks], bans: [...state.bans],
    add_target: state.add_target,
  };
}
function pushUndo() {
  undoStack.push(_draftSnapshot());
  if (undoStack.length > 60) undoStack.shift();
  updateUndoBtn();
}
function undo() {
  const s = undoStack.pop();
  if (!s) return false;
  state.radiant_picks = s.radiant;
  state.dire_picks    = s.dire;
  state.bans          = s.bans;
  setAddTarget(s.add_target);
  onStateChange();
  updateUndoBtn();
  return true;
}
function updateUndoBtn() {
  const b = document.getElementById('undo-btn');
  if (b) b.disabled = undoStack.length === 0;
}

// ── Draft persistence ────────────────────────────────────────
const DRAFT_KEY = 'draft_state_v1';
const DRAFT_RESUME_MAX_AGE_MS = 6 * 3600 * 1000;
function saveDraft() {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({ ..._draftSnapshot(), ts: Date.now() }));
  } catch (_) {}
}
function offerSavedDraft() {
  let s = null;
  try { s = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null'); } catch (_) {}
  if (!s) return;
  const valid = id => state.heroes[id] != null;
  s.radiant = (s.radiant || []).filter(valid); s.dire = (s.dire || []).filter(valid); s.bans = (s.bans || []).filter(valid);
  const n = s.radiant.length + s.dire.length + s.bans.length;
  if (!n || Date.now() - (s.ts || 0) > DRAFT_RESUME_MAX_AGE_MS) { localStorage.removeItem(DRAFT_KEY); return; }
  const bar = document.getElementById('resume-bar');
  const picks = s.radiant.length + s.dire.length;
  document.getElementById('resume-text').textContent =
    `Unfinished draft from ${relativeTime(s.ts / 1000)}: ${picks} pick${picks === 1 ? '' : 's'}, ${s.bans.length} ban${s.bans.length === 1 ? '' : 's'}.`;
  bar.classList.remove('hidden');
  document.getElementById('resume-yes').onclick = () => {
    pushUndo();
    state.radiant_picks = s.radiant; state.dire_picks = s.dire; state.bans = s.bans;
    setAddTarget(s.add_target || 'my-pick');
    onStateChange();
    bar.classList.add('hidden');
    document.getElementById('hero-search').focus();
  };
  document.getElementById('resume-no').onclick = () => {
    localStorage.removeItem(DRAFT_KEY);
    bar.classList.add('hidden');
  };
}

function relativeTime(unixSecs) {
  if (!unixSecs) return 'unknown';
  const s = Math.max(0, Date.now() / 1000 - unixSecs);
  if (s < 90) return 'just now';
  const m = s / 60;   if (m < 90) return `${Math.round(m)} min ago`;
  const h = m / 60;   if (h < 36) return `${Math.round(h)} hour${Math.round(h) === 1 ? '' : 's'} ago`;
  const d = h / 24;   return `${Math.round(d)} day${Math.round(d) === 1 ? '' : 's'} ago`;
}

function updateYouBadge() {
  document.getElementById('team-radiant')?.classList.toggle('mine', state.my_team === 'radiant');
  document.getElementById('team-dire')?.classList.toggle('mine', state.my_team === 'dire');
}

function updateBracketNote() {
  const note = document.getElementById('bracket-note');
  if (!note) return;
  const sel = document.getElementById('mmr-bracket-select');
  const label = sel?.options[sel.selectedIndex]?.textContent || '';
  const cohort = MATCHUP_COHORT[state.mmr_bracket] || '';
  note.textContent = `Win rates: ${label} · Counters & synergy: ${cohort} matchup data`;
  note.title = 'Stratz aggregates matchup data into four cohorts, so neighbouring brackets share counter data.';
}

function renderFooter() {
  const el = document.getElementById('app-footer');
  if (!el) return;
  const ts = state.data_updated_at;
  const ageH = ts ? (Date.now() / 1000 - ts) / 3600 : Infinity;
  const stale = ageH > 48;
  const parts = [];
  parts.push(`<span class="${stale ? 'stale' : ''}">Stratz data updated ${ts ? relativeTime(ts) : 'unknown'}${stale ? ' — refresh pending' : ''}</span>`);
  if (state.patch_name) parts.push(`<span>Patch ${_esc(state.patch_name)}</span>`);
  parts.push(`<span>Refreshes automatically after 24h</span>`);
  el.innerHTML = parts.join('<span aria-hidden="true">·</span>');
}

function loadWeights() {
  try {
    const w = JSON.parse(localStorage.getItem(WEIGHTS_KEY) || 'null');
    if (w && ['counter', 'win_rate', 'synergy', 'hero_pool', 'meta'].every(k => typeof w[k] === 'number')) return w;
  } catch (_) {}
  return { ...DEFAULT_WEIGHTS };
}
function saveWeights() {
  localStorage.setItem(WEIGHTS_KEY, JSON.stringify(state.weights));
}

function loadMmrBracket() { return localStorage.getItem('draft_mmr_bracket') || '7'; }
function saveMmrBracket() { localStorage.setItem('draft_mmr_bracket', state.mmr_bracket); }
function loadRoleFilter() { return localStorage.getItem('draft_role_filter') || ''; }
function saveRoleFilter() { localStorage.setItem('draft_role_filter', state.role_filter); }

// ── Auth State ───────────────────────────────────────────────
const authState = {
  token: localStorage.getItem('auth_token') || null,
  user: (() => { try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch (_) { return null; } })(),
  profile: null,
};

function authHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (authState.token) h['Authorization'] = `Bearer ${authState.token}`;
  return h;
}

function setAuthState(token, user) {
  authState.token = token;
  authState.user = user;
  if (token) {
    localStorage.setItem('auth_token', token);
    localStorage.setItem('auth_user', JSON.stringify(user));
  } else {
    localStorage.removeItem('auth_token');
    localStorage.removeItem('auth_user');
    authState.profile = null;
  }
  updateAuthUI();
}

function updateAuthUI() {
  const authControls = document.getElementById('auth-controls');
  const userControls = document.getElementById('user-controls');
  if (!authControls || !userControls) return;

  if (authState.user) {
    authControls.classList.add('hidden');
    userControls.classList.remove('hidden');
    document.getElementById('username-display').textContent = authState.user.username;
  } else {
    authControls.classList.remove('hidden');
    userControls.classList.add('hidden');
  }
}

async function loadProfile() {
  if (!authState.token) return;
  try {
    const res = await fetch('/api/profile', { headers: authHeaders() });
    if (res.ok) {
      authState.profile = await res.json();
      applySyncedSettings(authState.profile);
    } else if (res.status === 401) {
      setAuthState(null, null);
      showToast('Your session expired — please log in again.', 'error');
    }
  } catch (_) {}
}

// Weights + bracket follow the account across devices
function applySyncedSettings(p) {
  if (!p) return;
  let changed = false;
  const w = p.custom_weights;
  if (w && ['counter', 'win_rate', 'synergy', 'hero_pool', 'meta'].every(k => typeof w[k] === 'number')) {
    state.weights = { ...w }; saveWeights(); applyWeightsToUI(); changed = true;
  }
  if (p.mmr_bracket && MATCHUP_COHORT[p.mmr_bracket]) {
    state.mmr_bracket = p.mmr_bracket; saveMmrBracket();
    const sel = document.getElementById('mmr-bracket-select'); if (sel) sel.value = p.mmr_bracket;
    updateBracketNote(); changed = true;
  }
  if (changed) fetchRecommendations();
}
let settingsSyncTimer = null;
function syncSettingsToProfile() {
  if (!authState.token) return;
  clearTimeout(settingsSyncTimer);
  settingsSyncTimer = setTimeout(() => saveProfile({ custom_weights: state.weights, mmr_bracket: state.mmr_bracket }), 600);
}

// Auto-save profile edits (roles, pool, playstyle, notes)
let profileSaveTimer = null;
function scheduleProfileSave() {
  if (!authState.token) return;
  clearTimeout(profileSaveTimer);
  profileSaveTimer = setTimeout(() => handleProfileSubmit(null), 700);
}

async function saveProfile(data) {
  if (!authState.token) return;
  try {
    const res = await fetch('/api/profile', {
      method: 'PUT',
      headers: authHeaders(),
      body: JSON.stringify(data),
    });
    if (res.ok) {
      authState.profile = await res.json();
      return true;
    }
  } catch (_) {}
  return false;
}

// ── Auth Modal Logic ─────────────────────────────────────────
let authMode = 'login'; // 'login' | 'register'

function openAuthModal(mode = 'login') {
  authMode = mode;
  const modal = document.getElementById('auth-modal');
  document.getElementById('auth-modal-title').textContent = mode === 'login' ? 'Log In' : 'Register';
  document.getElementById('auth-submit-btn').textContent = mode === 'login' ? 'Log In' : 'Register';
  document.getElementById('auth-switch-text').textContent = mode === 'login' ? "Don't have an account?" : 'Already have an account?';
  document.getElementById('auth-switch-btn').textContent = mode === 'login' ? 'Register' : 'Log In';
  document.getElementById('auth-error').classList.add('hidden');
  document.getElementById('auth-form').reset();
  modal.classList.remove('hidden');
  document.getElementById('auth-username').focus();
}

function closeAuthModal() {
  document.getElementById('auth-modal').classList.add('hidden');
}

async function handleAuthSubmit(e) {
  e.preventDefault();
  const username = document.getElementById('auth-username').value.trim();
  const password = document.getElementById('auth-password').value;
  const errorEl = document.getElementById('auth-error');
  const submitBtn = document.getElementById('auth-submit-btn');

  submitBtn.disabled = true;
  errorEl.classList.add('hidden');

  const endpoint = authMode === 'login' ? '/api/login' : '/api/register';
  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.detail || 'Something went wrong';
      errorEl.classList.remove('hidden');
      return;
    }
    setAuthState(data.token, data.user);
    closeAuthModal();
    chatGateShown = false;
    await loadProfile();
    fetchRecommendations();
  } catch (err) {
    errorEl.textContent = 'Network error';
    errorEl.classList.remove('hidden');
  } finally {
    submitBtn.disabled = false;
  }
}

// ── Profile Panel Logic ──────────────────────────────────────
function openProfilePanel() {
  document.getElementById('profile-panel').classList.remove('hidden');
  populateProfileForm();
  document.getElementById('profile-dota-id').focus();
}

function closeProfilePanel() {
  document.getElementById('profile-panel').classList.add('hidden');
  closeHeroDropdown();
}

function populateProfileForm() {
  const p = authState.profile || {};

  // Role tags
  const roles = p.preferred_roles || [];
  document.querySelectorAll('#profile-roles .role-tag').forEach(btn => {
    btn.classList.toggle('active', roles.includes(btn.dataset.role));
  });

  // Hero pool
  renderHeroPoolDisplay(p.hero_pool || []);
  document.getElementById('profile-hero-search').value = '';

  // Playstyle tags
  const styles = p.playstyle_tags || [];
  document.querySelectorAll('#playstyle-tags .style-tag').forEach(btn => {
    btn.classList.toggle('active', styles.includes(btn.dataset.style));
  });

  // Notes
  document.getElementById('profile-notes').value = p.playstyle_notes || '';

  // Dota account
  document.getElementById('profile-dota-id').value = p.dota_account_id || '';
  if (p.player_stats && p.player_stats.name) {
    showAccountStatus(p.player_stats);
    offerBracketFromRank(p.player_stats);
  } else {
    document.getElementById('account-status').classList.add('hidden');
    document.getElementById('bracket-suggest')?.classList.add('hidden');
  }

  // Player stats
  if (p.player_stats && p.player_stats.top_heroes && p.player_stats.top_heroes.length) {
    renderPlayerStats(p.player_stats);
  } else {
    document.getElementById('player-stats-section').classList.add('hidden');
  }
}

// ── Hero Pool: Autocomplete Search ───────────────────────────
let heroDropdownOpen = false;

function setupHeroSearch() {
  const input = document.getElementById('profile-hero-search');
  const dropdown = document.getElementById('hero-search-dropdown');
  if (!input || !dropdown) return;

  input.addEventListener('input', () => {
    const query = input.value.trim().toLowerCase();
    if (query.length < 1) { closeHeroDropdown(); return; }

    const currentPool = authState.profile?.hero_pool || [];
    const matches = state.heroList.filter(h => {
      if (currentPool.includes(h.id)) return false;
      const name = h.localized_name.toLowerCase();
      const initials = h.localized_name.split(' ').map(w => w[0]).join('').toLowerCase();
      return name.includes(query) || initials === query;
    }).slice(0, 8);

    if (matches.length === 0) { closeHeroDropdown(); return; }

    dropdown.innerHTML = '';
    matches.forEach(hero => {
      const item = document.createElement('div');
      item.className = 'hero-dropdown-item';
      item.innerHTML = `<img src="${_esc(hero.img_url)}" alt="" onerror="this.style.display='none'" /> <span>${_esc(hero.localized_name)}</span>`;
      item.addEventListener('click', () => {
        addHeroToPool(hero.id);
        input.value = '';
        closeHeroDropdown();
        input.focus();
      });
      dropdown.appendChild(item);
    });
    dropdown.classList.remove('hidden');
    heroDropdownOpen = true;
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const firstItem = dropdown.querySelector('.hero-dropdown-item');
      if (firstItem) firstItem.click();
    } else if (e.key === 'Escape') {
      closeHeroDropdown();
      input.value = '';
    }
  });

  // Close dropdown on outside click
  document.addEventListener('click', (e) => {
    if (heroDropdownOpen && !e.target.closest('.hero-search-wrap')) {
      closeHeroDropdown();
    }
  });
}

function closeHeroDropdown() {
  const dropdown = document.getElementById('hero-search-dropdown');
  if (dropdown) { dropdown.classList.add('hidden'); dropdown.innerHTML = ''; }
  heroDropdownOpen = false;
}

// ── Hero Lookup ───────────────────────────────────────────────
let lookupDropdownOpen = false;
let lookupPerspective = 'my-team'; // 'my-team' | 'enemy-team'

function closeLookupDropdown() {
  const d = document.getElementById('hero-lookup-dropdown');
  if (d) { d.classList.add('hidden'); d.innerHTML = ''; }
  lookupDropdownOpen = false;
}

function setupHeroLookup() {
  const input    = document.getElementById('hero-lookup-input');
  const dropdown = document.getElementById('hero-lookup-dropdown');
  if (!input || !dropdown) return;

  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    if (q.length < 1) { closeLookupDropdown(); return; }

    const matches = state.heroList.filter(h => {
      const name = h.localized_name.toLowerCase();
      const initials = h.localized_name.split(' ').map(w => w[0]).join('').toLowerCase();
      return name.includes(q) || initials === q;
    }).slice(0, 8);

    if (matches.length === 0) { closeLookupDropdown(); return; }

    dropdown.innerHTML = '';
    matches.forEach(hero => {
      const item = document.createElement('div');
      item.className = 'hero-dropdown-item';
      item.innerHTML = `<img src="${_esc(hero.img_url)}" alt="" onerror="this.style.display='none'" /> <span>${_esc(hero.localized_name)}</span>`;
      item.addEventListener('click', () => {
        input.value = hero.localized_name;
        closeLookupDropdown();
        lookupHeroScore(hero.id);
      });
      dropdown.appendChild(item);
    });
    dropdown.classList.remove('hidden');
    lookupDropdownOpen = true;
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const first = dropdown.querySelector('.hero-dropdown-item');
      if (first) first.click();
    } else if (e.key === 'Escape') {
      closeLookupDropdown();
      input.value = '';
      document.getElementById('hero-lookup-result').innerHTML = '';
    }
  });

  document.addEventListener('click', (e) => {
    if (lookupDropdownOpen && !e.target.closest('.hero-lookup-wrap')) {
      closeLookupDropdown();
    }
  });

  document.querySelectorAll('.lookup-persp-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      lookupPerspective = btn.dataset.persp;
      document.querySelectorAll('.lookup-persp-btn').forEach(b => b.classList.toggle('active', b === btn));
      // Re-run lookup if a hero is already shown
      const input = document.getElementById('hero-lookup-input');
      const heroName = input?.value.trim();
      if (heroName) {
        const hero = state.heroList.find(h => h.localized_name.toLowerCase() === heroName.toLowerCase());
        if (hero) lookupHeroScore(hero.id);
      }
    });
  });
}

let lookupSeq = 0;
async function lookupHeroScore(heroId) {
  const seq = ++lookupSeq;
  const resultEl = document.getElementById('hero-lookup-result');
  resultEl.innerHTML = '<div style="color:var(--text-muted);font-style:italic;padding:6px 0;font-size:12px">Loading…</div>';

  const myTeam     = state.my_team;
  const myPicks    = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;
  const theirPicks = myTeam === 'radiant' ? state.dire_picks    : state.radiant_picks;

  // For enemy-team perspective, swap: their picks are allies, my picks are enemies
  const allyPicks  = lookupPerspective === 'enemy-team' ? theirPicks : myPicks;
  const enemyPicks = lookupPerspective === 'enemy-team' ? myPicks    : theirPicks;

  try {
    const res = await fetch('/api/hero_score', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({
        hero_id:     heroId,
        ally_picks:  allyPicks,
        enemy_picks: enemyPicks,
        bans:        state.bans,
        mmr_bracket: state.mmr_bracket,
        weights:     state.weights,
        role_filter: state.role_filter,
      }),
    });
    if (seq !== lookupSeq) return;  // a newer lookup superseded this one
    if (!res.ok) {
      let detail = res.status;
      try { detail = (await res.json()).detail || res.status; } catch (_) {}
      resultEl.innerHTML = `<div style="color:var(--score-low);font-size:12px">Error: ${_esc(detail)}</div>`;
      return;
    }
    const rec = await res.json();
    if (seq !== lookupSeq) return;
    renderLookupResult(rec, enemyPicks, resultEl);
  } catch (err) {
    if (seq !== lookupSeq) return;
    resultEl.innerHTML = `<div style="color:var(--score-low);font-size:12px">Request failed</div>`;
  }
}

function renderLookupResult(rec, enemyPicks, container) {
  const score = Math.round(rec.total_score * 100);
  const allyPicks = lookupPerspective === 'enemy-team'
    ? (state.my_team === 'radiant' ? state.dire_picks : state.radiant_picks)
    : (state.my_team === 'radiant' ? state.radiant_picks : state.dire_picks);
  const opts = { allies: allyPicks.length > 0, enemies: enemyPicks.length > 0, pool: !!(authState.profile?.hero_pool?.length), enemyPerspective: lookupPerspective === 'enemy-team' };
  const reasons = enemyPicks.length === 0 && allyPicks.length === 0
    ? 'No picks yet — showing overall win rate only'
    : (buildReasons(rec, opts) || 'Ranked by overall win rate');
  container.innerHTML = `
    <div class="rec-card top" style="cursor:default">
      <img class="rec-img" src="${_esc(rec.img_url)}" alt="" onerror="this.style.display='none'" />
      <div class="rec-info">
        <div class="rec-name-row">
          <span class="rec-name">${_esc(rec.localized_name)}</span>
          ${rolePills(rec.hero_id)}
          ${rec.in_hero_pool ? '<span class="role-pill pill-pool" title="In your hero pool">pool</span>' : ''}
          ${lowDataPill(rec)}
        </div>
        <div class="rec-score-bar-wrap">
          <div class="rec-score-bar" style="width:${score}%;background:${tierColor(rec.total_score)}"></div>
        </div>
        <div class="rec-reasons">${reasons}</div>
      </div>
      <div class="rec-score-num ${scoreTier(rec.total_score)}" title="Pick score out of 100">${score}<small>SCORE</small></div>
      <button type="button" class="rec-info-btn" aria-label="Show score breakdown" aria-expanded="false" title="Why this score?">i</button>
      ${breakdownHtml(rec, opts)}
    </div>
  `;
  attachInfoToggle(container.querySelector('.rec-card'));
}

function renderHeroPoolDisplay(heroIds) {
  const container = document.getElementById('hero-pool-display');
  if (!container) return;
  container.innerHTML = '';
  if (!heroIds || heroIds.length === 0) {
    container.innerHTML = '<span class="text-muted">Search and add heroes below</span>';
    return;
  }
  heroIds.forEach(id => {
    const hero = state.heroes[id];
    if (!hero) return;
    const chip = document.createElement('span');
    chip.className = 'hero-chip';
    chip.innerHTML = `<img src="${_esc(hero.img_url)}" alt="" onerror="this.style.display='none'" />${_esc(hero.localized_name)}<button type="button" class="chip-remove" data-id="${id}" aria-label="Remove ${_esc(hero.localized_name)} from pool">×</button>`;
    container.appendChild(chip);
  });

  container.querySelectorAll('.chip-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      const removeId = parseInt(btn.dataset.id);
      if (authState.profile && authState.profile.hero_pool) {
        authState.profile.hero_pool = authState.profile.hero_pool.filter(h => h !== removeId);
        renderHeroPoolDisplay(authState.profile.hero_pool);
        scheduleProfileSave();
      }
    });
  });
}

function addHeroToPool(heroId) {
  if (!authState.profile) authState.profile = { hero_pool: [] };
  if (!authState.profile.hero_pool) authState.profile.hero_pool = [];
  if (!authState.profile.hero_pool.includes(heroId)) {
    authState.profile.hero_pool.push(heroId);
    renderHeroPoolDisplay(authState.profile.hero_pool);
    scheduleProfileSave();
  }
}

// ── Role Tags ────────────────────────────────────────────────
function setupRoleTags() {
  document.querySelectorAll('#profile-roles .role-tag').forEach(btn => {
    btn.addEventListener('click', () => { btn.classList.toggle('active'); scheduleProfileSave(); });
  });
  document.getElementById('profile-notes')?.addEventListener('input', scheduleProfileSave);
}

// ── Playstyle Tags (max 3) ───────────────────────────────────
function setupPlaystyleTags() {
  document.querySelectorAll('#playstyle-tags .style-tag').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.classList.contains('active')) {
        btn.classList.remove('active');
      } else {
        const activeCount = document.querySelectorAll('#playstyle-tags .style-tag.active').length;
        if (activeCount >= 3) {
          // Flash the tags to indicate max reached
          document.getElementById('playstyle-tags').classList.add('shake');
          setTimeout(() => document.getElementById('playstyle-tags').classList.remove('shake'), 300);
          return;
        }
        btn.classList.add('active');
      }
      scheduleProfileSave();
    });
  });
}

// ── Dota Account Linking ─────────────────────────────────────
function setupAccountLink() {
  const btn = document.getElementById('link-account-btn');
  if (btn) btn.addEventListener('click', linkDotaAccount);
  const unlinkBtn = document.getElementById('unlink-account-btn');
  if (unlinkBtn) unlinkBtn.addEventListener('click', unlinkDotaAccount);
}

const STEAM64_BASE = 76561197960265728n;
function normalizeFriendId(raw) {
  let s = (raw || '').trim();
  const m = s.match(/steamcommunity\.com\/profiles\/(\d{17})/i);
  if (m) s = m[1];
  if (/^\d{17}$/.test(s)) s = (BigInt(s) - STEAM64_BASE).toString();
  return /^\d{1,12}$/.test(s) ? s : '';
}
// Stratz seasonRank: tens digit = medal (1 Herald … 8 Immortal) → our bracket value
function bracketFromRank(rankNum) {
  const medal = Math.floor((rankNum || 0) / 10);
  return { 8: '7', 7: '6', 6: '5', 5: '4', 4: '3', 3: '2', 2: '2', 1: '1' }[medal] || '';
}
function offerBracketFromRank(stats) {
  const el = document.getElementById('bracket-suggest');
  if (!el) return;
  const b = bracketFromRank(stats?.rank_num);
  if (!b || b === state.mmr_bracket) { el.classList.add('hidden'); return; }
  const sel = document.getElementById('mmr-bracket-select');
  const label = [...sel.options].find(o => o.value === b)?.textContent || b;
  el.innerHTML = `Your Stratz rank is <b>${_esc(stats.rank || '')}</b>. Set bracket to <b>${_esc(label)}</b>?
    <button type="button" class="btn-accent btn-sm bracket-suggest-btn" id="bracket-suggest-yes">Use ${_esc(label)}</button>`;
  el.classList.remove('hidden');
  el.querySelector('#bracket-suggest-yes').addEventListener('click', () => {
    state.mmr_bracket = b; saveMmrBracket(); sel.value = b; updateBracketNote(); syncSettingsToProfile(); fetchRecommendations();
    el.classList.add('hidden');
    showToast(`Bracket set to ${label}.`, 'success');
  });
}

async function linkDotaAccount() {
  const input = document.getElementById('profile-dota-id');
  const btn = document.getElementById('link-account-btn');
  const accountId = normalizeFriendId(input.value);
  const errEl0 = document.getElementById('link-account-error');
  if (!accountId) {
    if (errEl0) { errEl0.textContent = 'Enter a numeric Friend ID, a 17-digit Steam64 ID, or a steamcommunity.com/profiles/… URL.'; errEl0.classList.remove('hidden'); }
    return;
  }
  input.value = accountId;

  btn.disabled = true;
  btn.textContent = 'Linking...';

  const errEl = document.getElementById('link-account-error');
  if (errEl) errEl.classList.add('hidden');

  try {
    const res = await fetch('/api/link_account', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ dota_account_id: accountId }),
    });
    const data = await res.json();
    if (!res.ok) {
      if (errEl) { errEl.textContent = data.detail || 'Could not link account'; errEl.classList.remove('hidden'); }
      return;
    }
    // Update local profile
    if (!authState.profile) authState.profile = {};
    authState.profile.dota_account_id = accountId;
    authState.profile.player_stats = data;
    showAccountStatus(data);
    renderPlayerStats(data);
    offerBracketFromRank(data);
    showToast(`Linked ${data.name || 'account'}.`, 'success');
  } catch (err) {
    if (errEl) { errEl.textContent = 'Network error — try again'; errEl.classList.remove('hidden'); }
  } finally {
    btn.disabled = false;
    btn.textContent = 'Link';
  }
}

async function unlinkDotaAccount() {
  if (!confirm('Unlink your Dota 2 account?')) return;
  const errEl = document.getElementById('link-account-error');
  try {
    const res = await fetch('/api/unlink_account', {
      method: 'POST',
      headers: authHeaders(),
    });
    if (!res.ok) {
      if (errEl) { errEl.textContent = 'Could not unlink account'; errEl.classList.remove('hidden'); }
      return;
    }
    // Clear local state
    if (authState.profile) {
      authState.profile.dota_account_id = '';
      authState.profile.player_stats = {};
    }
    document.getElementById('profile-dota-id').value = '';
    document.getElementById('account-status').classList.add('hidden');
    document.getElementById('player-stats-section').classList.add('hidden');
  } catch (err) {
    if (errEl) { errEl.textContent = 'Network error — try again'; errEl.classList.remove('hidden'); }
  }
}

function showAccountStatus(playerData) {
  const statusEl = document.getElementById('account-status');
  const infoEl = document.getElementById('account-info');
  statusEl.classList.remove('hidden');
  infoEl.innerHTML = `
    <span class="account-name">${_esc(playerData.name || 'Unknown')}</span>
    <span class="account-rank">${_esc(playerData.rank || '')}</span>
    <span class="account-wr">${_esc(playerData.overall_wr || 0)}% WR</span>
    <span class="account-matches">${_esc((playerData.total_matches || 0).toLocaleString())} matches</span>
  `;
}

function renderPlayerStats(data) {
  const section = document.getElementById('player-stats-section');
  const content = document.getElementById('player-stats-content');
  if (!data || !data.top_heroes || !data.top_heroes.length) {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');

  const heroes = data.top_heroes.slice(0, 10);
  content.innerHTML = `
    <div class="player-heroes-grid">
      ${heroes.map(h => {
        const heroData = state.heroes[h.hero_id] || {};
        const wrClass = h.win_rate >= 55 ? 'stat-high' : h.win_rate >= 48 ? 'stat-mid' : 'stat-low';
        return `
          <div class="player-hero-row">
            <img src="${_esc(heroData.img_url || '')}" alt="" onerror="this.style.display='none'" />
            <span class="ph-name">${_esc(h.hero_name)}</span>
            <span class="ph-matches">${_esc(h.matches)} games</span>
            <span class="ph-wr ${wrClass}">${_esc(h.win_rate)}%</span>
          </div>`;
      }).join('')}
    </div>
    <button type="button" class="btn-ghost btn-sm" id="import-heroes-btn">Import top heroes to pool</button>
  `;

  // Import button
  document.getElementById('import-heroes-btn')?.addEventListener('click', () => {
    const topIds = heroes.filter(h => h.matches >= 5).map(h => h.hero_id);
    topIds.forEach(id => addHeroToPool(id));
  });
}

// ── Profile Save ─────────────────────────────────────────────
async function handleProfileSubmit(e) {
  if (e) e.preventDefault();
  clearTimeout(profileSaveTimer);

  // Roles
  const roles = [];
  document.querySelectorAll('#profile-roles .role-tag.active').forEach(btn => roles.push(btn.dataset.role));

  // Hero pool
  const heroPool = authState.profile?.hero_pool ? [...authState.profile.hero_pool] : [];

  // Playstyle tags
  const styles = [];
  document.querySelectorAll('#playstyle-tags .style-tag.active').forEach(btn => styles.push(btn.dataset.style));

  // Notes
  const notes = document.getElementById('profile-notes').value.trim();

  const ok = await saveProfile({
    preferred_roles: roles,
    hero_pool: heroPool,
    playstyle_tags: styles,
    playstyle_notes: notes,
  });

  const statusEl = document.getElementById('profile-status');
  if (ok) {
    statusEl.textContent = 'Saved';
    statusEl.classList.remove('hidden');
    setTimeout(() => statusEl.classList.add('hidden'), 1500);
    fetchRecommendations();   // pool changes affect scores
  } else {
    statusEl.textContent = 'Save failed';
    statusEl.classList.remove('hidden');
    showToast('Could not save your profile.', 'error');
  }
}

// ── Toasts ───────────────────────────────────────────────────
function showToast(msg, kind = 'info', ms = 4000) {
  const host = document.getElementById('toast-host');
  if (!host) return;
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ── Boot: poll until backend is ready ────────────────────────
let pollInterval = null;

async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    if (data.error) {
      clearInterval(pollInterval);
      const friendly = data.can_refresh
        ? _esc(data.error)
        : 'The server could not load hero data. This usually clears itself in a minute — try again shortly.';
      document.getElementById('splash-status').innerHTML =
        `<strong>⚠ Startup Error</strong><br>${friendly}<br><br>` +
        `<button onclick="location.reload()" style="padding:8px 16px; font-size:14px; cursor:pointer;">Retry</button>`;
      return;
    }

    const pct = data.total > 0 ? Math.round((data.progress / data.total) * 100) : 0;
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('splash-status').textContent =
      data.total > 0
        ? `Fetching matchup data from Stratz… ${data.progress}/${data.total} heroes` +
          (data.total - data.progress > 3 ? ` (about ${Math.max(1, Math.round((data.total - data.progress) * 0.6 / 60))} min left)` : '')
        : 'Loading hero data...';

    if (data.ready) {
      clearInterval(pollInterval);
      state.can_refresh  = !!data.can_refresh;
      state.chat_enabled = data.chat_enabled !== false;
      state.data_updated_at = data.data_updated_at || 0;
      state.patch_name = data.patch_name || '';
      await initApp();
    }
  } catch (_) {
    document.getElementById('splash-status').textContent = 'Waiting for server...';
  }
}

async function initApp() {
  // Fetch hero list
  let heroes;
  try {
    const res = await fetch('/api/heroes');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    heroes = await res.json();
  } catch (err) {
    document.getElementById('splash-status').textContent = 'Failed to load heroes — refresh or restart the server.';
    return;
  }

  for (const [id, hero] of Object.entries(heroes)) {
    state.heroes[parseInt(id)] = hero;
  }

  state.heroList = Object.values(heroes).sort((a, b) =>
    a.localized_name.localeCompare(b.localized_name)
  );

  // Restore team selection
  const savedTeam = localStorage.getItem('my_team');
  if (savedTeam) state.my_team = savedTeam;
  document.getElementById('my-team-select').value = state.my_team;
  document.getElementById('mmr-bracket-select').value = state.mmr_bracket;
  document.querySelectorAll('.role-filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.role === state.role_filter);
  });

  buildBanSlots();
  setupSlotTargeting();
  setupTabs();
  renderHeroGrid();
  updateAddTargetLabels();
  updateYouBadge();
  updateBracketNote();
  renderFooter();
  applyWeightsToUI();
  renderDraftBoard();
  fetchRecommendations();
  offerSavedDraft();

  // Server capability gating
  if (!state.can_refresh) document.getElementById('refresh-btn').classList.add('hidden');
  if (state.can_refresh) setupLiveSync();   // GSI only makes sense on the machine running Dota
  if (!state.chat_enabled) {
    const fab = document.getElementById('chat-fab');
    fab.title = 'AI chat is not configured on this server';
    fab.classList.add('chat-fab-disabled');
  }

  // Auth: restore login state + load profile
  updateAuthUI();
  if (authState.token) await loadProfile();

  // Wire up profile panel interactions
  setupHeroSearch();
  setupHeroLookup();
  setupRoleTags();
  setupPlaystyleTags();
  setupAccountLink();

  // Show app
  document.getElementById('progress-fill').style.width = '100%';
  const splash = document.getElementById('splash');
  splash.classList.add('fade-out');
  setTimeout(() => {
    splash.style.display = 'none';
    document.getElementById('app').classList.remove('hidden');
    updateSearchModeStyle();
    document.getElementById('hero-search').focus();
  }, 420);
}

// ── Hero Grid ─────────────────────────────────────────────────
function renderHeroGrid(filter = '') {
  const grid = document.getElementById('hero-grid');
  const used = getUsedSet();
  const query = (filter || '').trim().toLowerCase();
  const filtered = searchHeroes(query);

  grid.innerHTML = '';
  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'hero-grid-empty';
    empty.textContent = `No hero matches "${filter.trim()}"`;
    grid.appendChild(empty);
    return;
  }

  // Grouped by primary attribute when browsing; flat ranked list when searching
  const sections = query
    ? [[null, filtered]]
    : ATTR_GROUPS.map(([key, label]) => [[key, label], filtered.filter(h => (h.primary_attr || 'all') === key)])
                 .filter(([, list]) => list.length);

  for (const [group, heroes] of sections) {
    if (group) {
      const hdr = document.createElement('div');
      hdr.className = `hero-grid-group attr-${group[0]}`;
      hdr.textContent = group[1].toUpperCase();
      grid.appendChild(hdr);
    }
    for (const hero of heroes) appendHeroCard(grid, hero, used);
  }
  applyGridScoreOverlays();
}

function appendHeroCard(grid, hero, used) {
  {
    const card = document.createElement('div');
    const isUsed = used.has(hero.id);
    card.className = 'hero-card' + (isUsed ? ' used' : '');
    card.dataset.heroId = hero.id;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', hero.localized_name);
    card.tabIndex = isUsed ? -1 : 0;
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); card.click(); }
    });

    const img = document.createElement('img');
    img.src = hero.img_url;
    img.alt = hero.localized_name;
    img.loading = 'lazy';
    img.onerror = () => { img.style.display = 'none'; };

    const name = document.createElement('div');
    name.className = 'hero-card-name';
    name.textContent = hero.localized_name;

    card.appendChild(img);
    card.appendChild(name);
    card.addEventListener('click', () => handleHeroCardClick(hero.id));
    grid.appendChild(card);
  }
}

// Update only used/unused state on existing grid cards (no DOM rebuild)
function updateHeroGridUsed() {
  const used = getUsedSet();
  document.querySelectorAll('#hero-grid .hero-card').forEach(card => {
    const id = parseInt(card.dataset.heroId);
    const isUsed = used.has(id);
    card.classList.toggle('used', isUsed);
    card.tabIndex = isUsed ? -1 : 0;
  });
}

function getUsedSet() {
  return new Set([...state.radiant_picks, ...state.dire_picks, ...state.bans]);
}

// ── Add-target helper ─────────────────────────────────────────
function setAddTarget(target) {
  state.add_target = target;
  document.querySelectorAll('.add-target-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.target === target);
  });
  updateSearchModeStyle();
  markTargetSlot();
}

// Highlight the slot that will receive the next hero
function markTargetSlot() {
  document.querySelectorAll('.target-next').forEach(el => el.classList.remove('target-next'));
  let container;
  if (state.add_target === 'ban') {
    container = document.getElementById('ban-slots');
  } else {
    const team = state.add_target === 'my-pick'
      ? state.my_team
      : (state.my_team === 'radiant' ? 'dire' : 'radiant');
    container = document.getElementById(team + '-picks');
  }
  container?.querySelector('.empty')?.classList.add('target-next');
}

// Clicking an empty slot makes it the target (delegated: slots are re-rendered often)
function setupSlotTargeting() {
  const onSlot = (e) => {
    const slot = e.target.closest('.pick-slot.empty, .ban-slot.empty');
    if (!slot) return;
    if (slot.classList.contains('ban-slot')) {
      setAddTarget('ban');
    } else {
      setAddTarget(slot.dataset.team === state.my_team ? 'my-pick' : 'enemy-pick');
    }
    document.getElementById('hero-search').focus();
  };
  for (const id of ['radiant-picks', 'dire-picks', 'ban-slots']) {
    const el = document.getElementById(id);
    el.addEventListener('click', onSlot);
    el.addEventListener('keydown', (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('.pick-slot.empty, .ban-slot.empty')) {
        e.preventDefault(); onSlot(e);
      }
    });
  }
}

function buildBanSlots() {
  const c = document.getElementById('ban-slots');
  if (!c || c.children.length) return;
  for (let i = 0; i < MAX_BANS; i++) {
    const s = document.createElement('div');
    s.className = 'ban-slot empty';
    s.dataset.index = i;
    c.appendChild(s);
  }
}

function updateSearchModeStyle() {
  const el = document.getElementById('hero-search');
  if (!el) return;
  const myTeam = state.my_team;
  el.classList.remove('mode-my-pick', 'mode-enemy-pick', 'mode-ban');
  el.classList.add('mode-' + state.add_target);
  const labels = {
    'my-pick':    myTeam === 'radiant' ? 'Radiant (Me)' : 'Dire (Me)',
    'enemy-pick': myTeam === 'radiant' ? 'Dire (Enemy)' : 'Radiant (Enemy)',
    'ban':        'Ban',
  };
  el.placeholder = `Search — ${labels[state.add_target] || ''} — Tab to switch`;
}

// ── Hero selection logic ──────────────────────────────────────
function handleHeroCardClick(heroId) {
  if (getUsedSet().has(heroId)) return;
  const myTeam = state.my_team;
  let arr;
  if (state.add_target === 'my-pick')         arr = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;
  else if (state.add_target === 'enemy-pick') arr = myTeam === 'radiant' ? state.dire_picks : state.radiant_picks;
  else if (state.add_target === 'ban')        arr = state.bans;
  else return;
  const max = state.add_target === 'ban' ? MAX_BANS : 5;
  if (arr.length >= max) return;

  pushUndo();
  arr.push(heroId);
  const searchEl = document.getElementById('hero-search');
  searchEl.value = '';
  onStateChange();
  flipTargetIfFull();
  searchEl.focus();
}

// When the current target side is full, move to the other side (no draft-order guessing)
function flipTargetIfFull() {
  const myArr    = state.my_team === 'radiant' ? state.radiant_picks : state.dire_picks;
  const enemyArr = state.my_team === 'radiant' ? state.dire_picks : state.radiant_picks;
  if (state.add_target === 'my-pick'    && myArr.length >= 5 && enemyArr.length < 5) setAddTarget('enemy-pick');
  else if (state.add_target === 'enemy-pick' && enemyArr.length >= 5 && myArr.length < 5) setAddTarget('my-pick');
  else if (state.add_target === 'ban' && state.bans.length >= MAX_BANS) setAddTarget('my-pick');
}

function handleSlotClick(type, index) {
  // Clicking an existing slot removes the hero
  if (type === 'radiant') {
    if (state.radiant_picks[index] != null) {
      state.radiant_picks.splice(index, 1);
      onStateChange();
    }
  } else if (type === 'dire') {
    if (state.dire_picks[index] != null) {
      state.dire_picks.splice(index, 1);
      onStateChange();
    }
  }
}

function onStateChange() {
  renderDraftBoard();
  updateHeroGridUsed();
  applyGridScoreOverlays();
  saveDraft();
  fetchRecommendations();
  renderTeamNeeds();
  if (state.radiant_picks.length && state.dire_picks.length) {
    fetchDraftAnalysis();
  } else {
    document.getElementById('winprob-panel').classList.add('hidden');
    updateTabState();
  }
}

// ── Draft Board Render ────────────────────────────────────────
function renderDraftBoard() {
  renderPickSlots('radiant', state.radiant_picks);
  renderPickSlots('dire', state.dire_picks);
  renderBanSlots();
  markTargetSlot();
}

function renderBanSlots() {
  const slots = document.querySelectorAll('#ban-slots .ban-slot');
  const count = document.getElementById('ban-count');
  if (count) count.textContent = state.bans.length ? `${state.bans.length}/${MAX_BANS}` : '';
  slots.forEach((slot, i) => {
    const heroId = state.bans[i];
    slot.innerHTML = '';
    if (heroId != null && state.heroes[heroId]) {
      const hero = state.heroes[heroId];
      slot.className = 'ban-slot filled';
      slot.removeAttribute('role'); slot.removeAttribute('tabindex'); slot.removeAttribute('aria-label');
      const img = document.createElement('img');
      img.src = hero.img_url; img.alt = hero.localized_name;
      img.onerror = () => { img.style.display = 'none'; };
      const label = document.createElement('div');
      label.className = 'slot-label'; label.textContent = hero.localized_name;
      const rm = document.createElement('button');
      rm.type = 'button'; rm.className = 'slot-remove'; rm.textContent = '×';
      rm.setAttribute('aria-label', `Unban ${hero.localized_name}`); rm.title = `Unban ${hero.localized_name}`;
      rm.addEventListener('click', (e) => {
        e.stopPropagation();
        pushUndo();
        state.bans.splice(i, 1);
        onStateChange();
      });
      slot.append(img, label, rm);
    } else {
      slot.className = 'ban-slot empty';
      slot.setAttribute('role', 'button'); slot.tabIndex = 0;
      slot.setAttribute('aria-label', 'Empty ban slot — select to ban the next hero here');
    }
  });
}

function renderPickSlots(team, picks) {
  const container = document.getElementById(team + '-picks');
  const slots = container.querySelectorAll('.pick-slot');
  slots.forEach((slot, i) => {
    const heroId = picks[i];
    if (heroId != null) {
      const hero = state.heroes[heroId];
      slot.className = 'pick-slot filled';
      slot.innerHTML = '';

      const img = document.createElement('img');
      img.src = hero.img_url;
      img.alt = hero.localized_name;
      img.onerror = () => { img.style.display = 'none'; };

      const label = document.createElement('div');
      label.className = 'slot-label';
      label.textContent = hero.localized_name;

      const rmBtn = document.createElement('button');
      rmBtn.type = 'button';
      rmBtn.className = 'slot-remove';
      rmBtn.textContent = '×';
      rmBtn.setAttribute('aria-label', `Remove ${hero.localized_name}`);
      rmBtn.title = `Remove ${hero.localized_name}`;
      rmBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        pushUndo();
        picks.splice(i, 1);
        onStateChange();
      });

      slot.removeAttribute('role'); slot.removeAttribute('tabindex'); slot.removeAttribute('aria-label');
      slot.appendChild(img);
      slot.appendChild(label);
      slot.appendChild(rmBtn);
    } else {
      slot.className = 'pick-slot empty';
      slot.innerHTML = '';
      slot.setAttribute('role', 'button'); slot.tabIndex = 0;
      slot.setAttribute('aria-label', `Empty ${team} slot — select to add the next hero here`);
    }
  });
}


// ── Grid Score Overlays ───────────────────────────────────────
function applyGridScoreOverlays() {
  // Remove existing badges
  document.querySelectorAll('.hero-score-badge').forEach(el => el.remove());
}

// ── Threat Panel ──────────────────────────────────────────────

function _esc(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderThreatPanel() {
  const panel = document.getElementById('threat-panel');
  const list  = document.getElementById('threat-list');
  const myTeam     = state.my_team;
  const allyPicks  = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;
  const enemyPicks = myTeam === 'radiant' ? state.dire_picks    : state.radiant_picks;

  if (allyPicks.length === 0 || enemyPicks.length === 0 || !state.threats?.length) {
    panel.classList.add('hidden');
    updateTabState();
    return;
  }

  panel.classList.remove('hidden');

  const threats  = state.threats; // sorted by avg_win_rate desc
  const critical = threats.filter(t => t.avg_win_rate >= 0.53);
  const minor    = threats.filter(t => t.avg_win_rate <  0.53);

  let html = '';

  // ── Critical Threats ──────────────────────────────────────
  if (critical.length) html += `<div class="threat-tier-label critical">CRITICAL THREATS</div>`;

  critical.forEach(t => {
    const avgPct = (t.avg_win_rate * 100).toFixed(1);
    const severity = 'CRITICAL';
    const sevCls   = 'sev-critical';

    // Worst matchup tags (max 3, only show if win_rate > 0.5)
    const badMatchups = t.matchups
      .filter(m => m.win_rate > 0.5)
      .slice(0, 3)
      .map(m => `<span class="threat-tag">${_esc(m.ally_name)} <span class="threat-tag-pct">${(m.win_rate*100).toFixed(0)}%</span></span>`)
      .join('');

    html += `
      <div class="threat-card">
        <img class="threat-card-img" src="${_esc(t.enemy_img)}" onerror="this.style.display='none'" alt="${_esc(t.enemy_name)}">
        <div class="threat-card-body">
          <div class="threat-card-top">
            <span class="threat-card-name">${_esc(t.enemy_name)}</span>
            <span class="threat-sev ${sevCls}">${severity}</span>
            <span class="threat-avg-pct">${avgPct}% vs your team</span>
          </div>
          ${badMatchups ? `<div class="threat-matchup-tags">Counters: ${badMatchups}</div>` : ''}
        </div>
      </div>`;
  });

  // ── Minor Threats ─────────────────────────────────────────
  if (minor.length) {
    html += `<div class="threat-tier-label minor">${critical.length ? 'MINOR THREATS' : 'THREATS'}</div>`;
    minor.forEach(t => {
      const worstAlly = t.matchups[0];
      const avgPct    = (t.avg_win_rate * 100).toFixed(1);
      const severity  = t.avg_win_rate >= 0.50 ? 'MODERATE' : 'LOW';
      const sevCls    = severity === 'MODERATE' ? 'sev-moderate' : 'sev-low';
      html += `
        <div class="threat-card minor">
          <img class="threat-card-img" src="${_esc(t.enemy_img)}" onerror="this.style.display='none'" alt="${_esc(t.enemy_name)}">
          <div class="threat-card-body">
            <div class="threat-card-top">
              <span class="threat-card-name">${_esc(t.enemy_name)}</span>
              <span class="threat-sev ${sevCls}">${severity}</span>
              <span class="threat-avg-pct">${avgPct}% vs your team</span>
            </div>
            ${worstAlly ? `<div class="threat-matchup-tags">Counters: <span class="threat-tag">${_esc(worstAlly?.ally_name ?? '—')} <span class="threat-tag-pct">${(worstAlly.win_rate*100).toFixed(0)}%</span></span></div>` : ''}
          </div>
        </div>`;
    });
  }

  list.innerHTML = html;
  updateTabState();
}

// ── Win Probability ───────────────────────────────────────
let analysisSeq = 0;
async function fetchDraftAnalysis() {
  const seq = ++analysisSeq;
  try {
    const res = await fetch('/api/draft_analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        radiant: state.radiant_picks,
        dire: state.dire_picks,
        mmr_bracket: state.mmr_bracket,
      }),
    });
    if (!res.ok) return;
    const data = await res.json();
    if (seq !== analysisSeq) return;  // draft changed while this was in flight
    if (!state.radiant_picks.length || !state.dire_picks.length) return;
    const wasComplete = state.draft_analysis?.complete;
    state.draft_analysis = data;
    renderWinProb(data);
    if (data.complete && !wasComplete) document.getElementById('winprob-panel')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (_) {}
}

function renderWinProb(data) {
  const panel   = document.getElementById('winprob-panel');
  const content = document.getElementById('winprob-content');
  if (!data) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');

  const rProb = data.radiant_win_prob;
  const dProb = data.dire_win_prob;
  const comp  = data.components || {};
  const complete = data.complete !== false;
  const title = document.getElementById('winprob-title');
  if (title) title.textContent = complete ? 'DRAFT COMPLETE — WIN PROBABILITY' : `DRAFT EDGE SO FAR — ${data.picks ?? ''}/10 PICKED`;
  const diff = rProb - 50;
  const mag  = Math.abs(diff);
  const who  = diff >= 0 ? 'Radiant' : 'Dire';
  const edgeWord = mag < 2 ? 'Even draft' : mag < 5 ? `Slight ${who} edge` : mag < 10 ? `Clear ${who} edge` : `Strong ${who} edge`;
  const edgeCls  = mag < 2 ? 'edge-even' : diff >= 0 ? 'edge-radiant' : 'edge-dire';
  const conf = complete ? '' : `<span class="wp-conf">low confidence until 5v5 — lane matchups aren't known yet</span>`;
  const edgeSection = `<div class="wp-edge ${edgeCls}">${edgeWord}${conf}</div>`;

  function fmtAdv(val) {
    return (val >= 0 ? '+' : '') + val.toFixed(1) + '%';
  }
  function advCls(val) { return val >= 0 ? 'adv-radiant' : 'adv-dire'; }
  function favored(val) { return val >= 0 ? 'Radiant' : 'Dire'; }

  function matchupRow(pair, radiantFavored) {
    // pair.win_rate is Radiant hero's WR vs Dire hero; flip for Dire-favored rows
    const wr          = radiantFavored ? pair.win_rate : 1 - pair.win_rate;
    const winPct      = (wr * 100).toFixed(1);
    const winnerImg   = radiantFavored ? pair.radiant_img  : pair.dire_img;
    const winnerName  = radiantFavored ? pair.radiant_name : pair.dire_name;
    const loserImg    = radiantFavored ? pair.dire_img     : pair.radiant_img;
    const loserName   = radiantFavored ? pair.dire_name    : pair.radiant_name;
    const cls         = radiantFavored ? 'matchup-radiant' : 'matchup-dire';
    const games       = pair.games ? ` <span class="wp-games">${_esc(pair.games.toLocaleString())}g</span>` : '';
    return `
      <div class="wp-matchup-row ${cls}">
        <img src="${_esc(winnerImg)}" alt="" onerror="this.style.display='none'" />
        <span class="wp-mname">${_esc(winnerName)}</span>
        <span class="wp-arrow">beats</span>
        <img src="${_esc(loserImg)}" alt="" onerror="this.style.display='none'" />
        <span class="wp-mname">${_esc(loserName)}</span>
        <span class="wp-pct">${winPct}%</span>${games}
      </div>`;
  }

  function synRow(pair, team) {
    const winPct = (pair.win_rate * 100).toFixed(1);
    const cls    = team === 'radiant' ? 'syn-radiant' : 'syn-dire';
    const games  = pair.games ? ` <span class="wp-games">${_esc(pair.games.toLocaleString())}g</span>` : '';
    return `
      <div class="wp-syn-row ${cls}">
        <img src="${_esc(pair.hero1_img)}" alt="" onerror="this.style.display='none'" />
        <span class="wp-mname">${_esc(pair.hero1_name)}</span>
        <span class="wp-arrow">+</span>
        <img src="${_esc(pair.hero2_img)}" alt="" onerror="this.style.display='none'" />
        <span class="wp-mname">${_esc(pair.hero2_name)}</span>
        <span class="wp-pct">${winPct}%</span>${games}
      </div>`;
  }

  function factorRow(label, val) {
    // val is +ve when Radiant is favoured, in percentage points
    const cls = val >= 0 ? 'adv-radiant' : 'adv-dire';
    const who = val >= 0 ? 'Radiant' : 'Dire';
    const width = Math.min(100, Math.abs(val) * 8);  // ±12.5pp fills the bar
    return `
      <div class="wp-factor-row ${cls}">
        <span class="wp-factor-name">${label}</span>
        <div class="wp-factor-bar-wrap"><div class="wp-factor-bar" style="width:${width}%"></div></div>
        <span class="wp-factor-val">${who} ${(val >= 0 ? '+' : '') + val.toFixed(1)}pp</span>
      </div>`;
  }

  const radiantBest = (data.key_matchups?.radiant_best || []);
  const direBest    = (data.key_matchups?.dire_best    || []);
  const radiantSyn  = (data.synergies?.radiant_best    || []);
  const direSyn     = (data.synergies?.dire_best       || []);

  const matchupSection = (radiantBest.length || direBest.length) ? `
    <div class="wp-details-grid">
      ${radiantBest.length ? `
        <div class="wp-col">
          <div class="wp-section-label radiant-label">RADIANT'S BEST MATCHUPS</div>
          ${radiantBest.map(p => matchupRow(p, true)).join('')}
        </div>` : ''}
      ${direBest.length ? `
        <div class="wp-col">
          <div class="wp-section-label dire-label">DIRE'S BEST MATCHUPS</div>
          ${direBest.map(p => matchupRow(p, false)).join('')}
        </div>` : ''}
    </div>` : '';

  const synSection = (radiantSyn.length || direSyn.length) ? `
    <div class="wp-details-grid">
      ${radiantSyn.length ? `
        <div class="wp-col">
          <div class="wp-section-label radiant-label">RADIANT SYNERGIES</div>
          ${radiantSyn.map(p => synRow(p, 'radiant')).join('')}
        </div>` : ''}
      ${direSyn.length ? `
        <div class="wp-col">
          <div class="wp-section-label dire-label">DIRE SYNERGIES</div>
          ${direSyn.map(p => synRow(p, 'dire')).join('')}
        </div>` : ''}
    </div>` : '';

  const factorsSection = `
    <div class="wp-factors">
      <div class="wp-section-label">WHY</div>
      ${factorRow('Matchups', comp.matchup_adv ?? 0)}
      ${factorRow('Synergy',  comp.synergy_adv ?? 0)}
      ${factorRow('Win rates', comp.wr_adv ?? 0)}
    </div>`;

  content.innerHTML = `
    <div class="wp-bar-section">
      <div class="wp-bar-labels">
        <span class="wp-team-name radiant-label">RADIANT ${rProb > dProb ? '▲' : ''}</span>
        <span class="wp-team-name dire-label">${dProb > rProb ? '▲' : ''} DIRE</span>
      </div>
      <div class="wp-bar-track">
        <div class="wp-bar-fill-radiant" style="width:${rProb}%"></div>
      </div>
      <div class="wp-bar-nums">
        <span class="wp-prob ${rProb > dProb ? 'wp-winner' : ''}" style="color:var(--radiant)">${rProb}%</span>
        <span class="wp-prob dire-num ${dProb > rProb ? 'wp-winner' : ''}">${dProb}%</span>
      </div>
    </div>
    ${factorsSection}
    ${complete ? matchupSection + synSection : ''}
  `;
  content.insertAdjacentHTML('afterbegin', edgeSection);
  updateTabState();
}

// ── Recommendations ───────────────────────────────────────────
let recDebounceTimer = null;
let recSeq = 0;

async function fetchRecommendations() {
  clearTimeout(recDebounceTimer);
  recDebounceTimer = setTimeout(async () => {
    const seq = ++recSeq;
    const myTeam = state.my_team;
    const allyPicks = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;
    const enemyPicks = myTeam === 'radiant' ? state.dire_picks : state.radiant_picks;

    try {
      const res = await fetch('/api/recommend', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
          ally_picks: allyPicks,
          enemy_picks: enemyPicks,
          bans: state.bans,
          my_team: myTeam,
          weights: state.weights,
          mmr_bracket: state.mmr_bracket,
          role_filter: state.role_filter,
          enemy_role_filter: state.enemy_role_filter,
        }),
      });

      if (seq !== recSeq) return;  // a newer request superseded this one
      if (!res.ok) {
        let errorMsg = '';
        if (res.status === 503) {
          errorMsg = 'Recommendations unavailable — cache still loading. Retry in a moment.';
        } else if (res.status === 400) {
          try {
            const err = await res.json();
            errorMsg = err.detail || 'Invalid request';
          } catch (_) {
            errorMsg = `Error ${res.status}: Check your hero selections`;
          }
        } else {
          errorMsg = `Error ${res.status} fetching recommendations`;
        }
        document.getElementById('rec-hint').textContent = errorMsg;
        document.getElementById('rec-list').innerHTML = '';
        return;
      }

      const data = await res.json();
      if (seq !== recSeq) return;
      // Handle both old list format and new {top, all_scores, threats} format
      if (Array.isArray(data)) {
        state.recommendations = data;
        state.allScores = {};
        state.threats = [];
      } else {
        state.recommendations    = data.top || [];
        state.allScores          = data.all_scores || {};
        state.threats            = data.threats    || [];
        state.enemy_predictions  = data.enemy_predictions || [];
      }
      renderRecommendations();
      applyGridScoreOverlays();
      renderThreatPanel();
      renderEnemyPredictions();
    } catch (err) {
      if (seq !== recSeq) return;
      document.getElementById('rec-hint').textContent = 'Network error fetching recommendations';
      showToast('Lost connection to the server — retrying on your next change.', 'error');
    }
  }, 150);
}

function renderRecommendations() {
  const list = document.getElementById('rec-list');
  const hint = document.getElementById('rec-hint');
  const recs = state.recommendations;

  const myTeam = state.my_team;
  const enemyPicks = myTeam === 'radiant' ? state.dire_picks : state.radiant_picks;
  const allyPicks = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;

  if (enemyPicks.length === 0 && allyPicks.length === 0) {
    hint.textContent = 'Select heroes to see suggestions';
  } else if (enemyPicks.length === 0) {
    hint.textContent = 'Showing by win rate — add enemy picks for counter suggestions';
  } else {
    hint.textContent = `Countering ${enemyPicks.length} enemy pick${enemyPicks.length > 1 ? 's' : ''}`;
  }

  list.innerHTML = '';
  const moreBtn = document.getElementById('rec-more-btn');

  if (!recs || recs.length === 0) {
    const noPicks = enemyPicks.length === 0 && allyPicks.length === 0;
    list.innerHTML = noPicks
      ? `<div class="rec-onboard">
           <ol>
             <li>Set <b>My Team</b> and your <b>Bracket</b> at the top.</li>
             <li>Type a hero in the search box and press <kbd>Enter</kbd>, or click the hero grid. <kbd>Tab</kbd> flips between your pick and the enemy's.</li>
             <li>Click a recommendation to add it. <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes.</li>
           </ol>
           ${authState.user ? '' : 'Log in to save a hero pool and use the AI assistant.'}
         </div>`
      : '<div style="color:var(--text-muted);font-style:italic;padding:8px">No recommendations yet.</div>';
    moreBtn?.classList.add('hidden');
    return;
  }

  const TOP_N = 5;
  const opts = { allies: allyPicks.length > 0, enemies: enemyPicks.length > 0, pool: !!(authState.profile?.hero_pool?.length) };

  recs.forEach((rec, i) => {
    const card = document.createElement('div');
    const isTop = i < TOP_N;
    card.className = 'rec-card' + (isTop ? ' top' : '') + (!isTop && !state.show_all_recs ? ' rec-more-hidden' : '');

    const score = Math.round(rec.total_score * 100);            // absolute 0-100
    const scoreClass = scoreTier(rec.total_score);
    const barColor = tierColor(rec.total_score);

    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.setAttribute('aria-label', `Add ${rec.localized_name}`);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); card.click(); }
    });
    card.innerHTML = `
      <div class="rec-rank">${i + 1}</div>
      <img class="rec-img" src="${_esc(rec.img_url)}" alt="" onerror="this.style.display='none'" />
      <div class="rec-info">
        <div class="rec-name-row">
          <span class="rec-name">${_esc(rec.localized_name)}</span>
          ${rolePills(rec.hero_id)}
          ${rec.in_hero_pool ? '<span class="role-pill pill-pool" title="In your hero pool">pool</span>' : ''}
          ${lowDataPill(rec)}
        </div>
        <div class="rec-score-bar-wrap">
          <div class="rec-score-bar" style="width:${score}%;background:${barColor}"></div>
        </div>
        <div class="rec-reasons">${buildReasons(rec, opts) || 'Ranked by overall win rate'}</div>
      </div>
      <div class="rec-score-num ${scoreClass}" title="Pick score out of 100">${score}<small>SCORE</small></div>
      <button type="button" class="rec-info-btn" aria-label="Show score breakdown" aria-expanded="false" title="Why this score?">i</button>
      ${breakdownHtml(rec, opts)}
    `;
    attachInfoToggle(card);

    // Every add path follows the Add-as toggle — one rule, no surprises.
    card.addEventListener('click', () => handleHeroCardClick(rec.hero_id));

    list.appendChild(card);
  });

  if (moreBtn) {
    const extra = recs.length - TOP_N;
    moreBtn.classList.toggle('hidden', extra <= 0);
    moreBtn.textContent = state.show_all_recs ? 'Show top 5 only' : `Show ${extra} more`;
  }
}

function renderEnemyPredictions() {
  const panel = document.getElementById('enemy-predictions-panel');
  const list = document.getElementById('enemy-predictions-list');
  if (!panel || !list) return;

  const myTeam = state.my_team;
  const allyPicks = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;
  const enemyPicks = myTeam === 'radiant' ? state.dire_picks : state.radiant_picks;
  const preds = state.enemy_predictions;

  // Hide when no picks exist or no predictions
  if ((allyPicks.length === 0 && enemyPicks.length === 0) || !preds || preds.length === 0) {
    panel.classList.add('hidden');
    updateTabState();
    return;
  }

  panel.classList.remove('hidden');
  updateTabState();
  list.innerHTML = '';

  const opts = { allies: enemyPicks.length > 0, enemies: allyPicks.length > 0, pool: false, enemyPerspective: true };
  preds.forEach((pred, i) => {
    const card = document.createElement('div');
    card.className = 'enemy-pred-card' + (i < 5 ? ' top' : '');
    const score = Math.round(pred.total_score * 100);

    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.setAttribute('aria-label', `Add ${pred.localized_name}`);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); card.click(); }
    });
    card.innerHTML = `
      <div class="rec-rank">${i + 1}</div>
      <img class="rec-img" src="${_esc(pred.img_url)}" alt="" onerror="this.style.display='none'" />
      <div class="rec-info">
        <div class="rec-name-row">
          <span class="rec-name">${_esc(pred.localized_name)}</span>
          ${rolePills(pred.hero_id)}
          ${lowDataPill(pred)}
        </div>
        <div class="rec-score-bar-wrap">
          <div class="rec-score-bar" style="width:${score}%;background:var(--dire)"></div>
        </div>
        <div class="rec-reasons">${buildReasons(pred, opts) || 'Ranked by overall win rate'}</div>
      </div>
      <div class="rec-score-num" title="How much the enemy wants this hero, out of 100">${score}<small>SCORE</small></div>
      <button type="button" class="rec-info-btn" aria-label="Show score breakdown" aria-expanded="false" title="Why this score?">i</button>
      ${breakdownHtml(pred, opts)}
    `;
    attachInfoToggle(card);

    // Every add path follows the Add-as toggle — one rule, no surprises.
    card.addEventListener('click', () => handleHeroCardClick(pred.hero_id));

    list.appendChild(card);
  });
}

function shortName(name) {
  // Return first word or abbreviation for short display
  if (!name) return '';
  const words = name.split(' ');
  if (words.length === 1) return name;
  if (name.length <= 10) return name;
  return words[0];
}

// ── Add target buttons ────────────────────────────────────────
function updateAddTargetLabels() {
  const myTeam = state.my_team;
  document.getElementById('btn-my-pick').textContent =
    myTeam === 'radiant' ? 'Radiant Pick (Me)' : 'Dire Pick (Me)';
  document.getElementById('btn-enemy-pick').textContent =
    myTeam === 'radiant' ? 'Dire Pick (Enemy)' : 'Radiant Pick (Enemy)';
  updateSearchModeStyle();
}

// ── Weights UI ────────────────────────────────────────────────
function applyWeightsToUI() {
  document.getElementById('w-counter').value = state.weights.counter;
  document.getElementById('w-winrate').value = state.weights.win_rate;
  document.getElementById('w-role').value = state.weights.synergy;
  document.getElementById('w-heropool').value = state.weights.hero_pool;
  document.getElementById('w-meta').value = state.weights.meta;
  document.getElementById('w-counter-val').textContent = state.weights.counter.toFixed(2);
  document.getElementById('w-winrate-val').textContent = state.weights.win_rate.toFixed(2);
  document.getElementById('w-role-val').textContent = state.weights.synergy.toFixed(2);
  document.getElementById('w-heropool-val').textContent = state.weights.hero_pool.toFixed(2);
  document.getElementById('w-meta-val').textContent = state.weights.meta.toFixed(2);
}

// ── Event listeners ───────────────────────────────────────────
document.getElementById('hero-search').addEventListener('input', (e) => {
  renderHeroGrid(e.target.value);
  if (e.target.value.trim()) document.getElementById('hero-grid-toggle').open = true;
});

document.getElementById('hero-search').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    const firstCard = document.querySelector('#hero-grid .hero-card:not(.used)');
    if (firstCard) handleHeroCardClick(parseInt(firstCard.dataset.heroId));
  } else if (e.key === 'Tab' || (e.key === ' ' && e.target.value === '')) {
    // Tab flips my pick ↔ enemy pick (Ban is the button or a ban slot). Shift+Tab leaves the field.
    if (e.key === 'Tab' && e.shiftKey) return;
    e.preventDefault();
    setAddTarget(state.add_target === 'my-pick' ? 'enemy-pick' : 'my-pick');
  } else if (/^[1-5]$/.test(e.key) && e.target.value === '') {
    // 1–5 takes the Nth recommendation
    e.preventDefault();
    document.querySelectorAll('#rec-list .rec-card')[parseInt(e.key) - 1]?.click();
  } else if (e.key === 'Backspace' && e.target.value === '') {
    e.preventDefault();
    undo();
  } else if (e.key === 'Escape') {
    e.target.value = '';
    renderHeroGrid('');
  }
});

document.getElementById('my-team-select').addEventListener('change', (e) => {
  state.my_team = e.target.value;
  localStorage.setItem('my_team', state.my_team);
  updateAddTargetLabels();
  updateYouBadge();
  renderDraftBoard();
  renderTeamNeeds();
  fetchRecommendations();
  renderRecommendations();
  renderThreatPanel();
  renderEnemyPredictions();
});

document.querySelectorAll('.add-target-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    setAddTarget(btn.dataset.target);
    document.getElementById('hero-search').focus();
  });
});

document.getElementById('undo-btn').addEventListener('click', () => {
  undo();
  document.getElementById('hero-search').focus();
});

document.getElementById('reset-btn').addEventListener('click', () => {
  if (state.radiant_picks.length + state.dire_picks.length + state.bans.length) pushUndo();
  state.radiant_picks = [];
  state.dire_picks = [];
  state.bans = [];
  state.recommendations = [];
  state.allScores = {};
  state.threats = [];
  state.enemy_predictions = [];
  state.show_all_recs = false;
  state.draft_analysis = null;
  const searchEl = document.getElementById('hero-search');
  searchEl.value = '';
  setAddTarget('my-pick');
  onStateChange();
  searchEl.focus();
  document.getElementById('rec-list').innerHTML = '';
  document.getElementById('rec-hint').textContent = 'Select heroes to see suggestions';
  document.getElementById('winprob-panel').classList.add('hidden');
  document.getElementById('hero-lookup-result').innerHTML = '';
  document.getElementById('hero-lookup-input').value = '';
  lookupPerspective = 'my-team';
  document.querySelectorAll('.lookup-persp-btn').forEach(b => b.classList.toggle('active', b.dataset.persp === 'my-team'));
  renderThreatPanel();
  renderEnemyPredictions();
  applyGridScoreOverlays();
});

document.getElementById('refresh-btn').addEventListener('click', async () => {
  const btn = document.getElementById('refresh-btn');
  btn.disabled = true;
  btn.textContent = 'Refreshing...';
  try {
    const res = await fetch('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ force: true }) });
    if (!res.ok) {
      let detail = `Error ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      btn.textContent = '↻ ' + detail;
      setTimeout(() => { btn.disabled = false; btn.textContent = '↻ Refresh Data'; }, 4000);
      return;
    }
    btn.textContent = '↻ Refreshing in background';
    setTimeout(() => { btn.disabled = false; btn.textContent = '↻ Refresh Data'; }, 5000);
  } catch (_) {
    btn.disabled = false;
    btn.textContent = '↻ Refresh Data';
  }
});

// Weight sliders
['counter', 'winrate', 'role', 'heropool', 'meta'].forEach(key => {
  const slider = document.getElementById(`w-${key}`);
  const label = document.getElementById(`w-${key}-val`);
  const stateKey = key === 'winrate' ? 'win_rate' : key === 'role' ? 'synergy' : key === 'heropool' ? 'hero_pool' : key === 'meta' ? 'meta' : 'counter';
  slider.addEventListener('input', () => {
    state.weights[stateKey] = parseFloat(slider.value);
    label.textContent = parseFloat(slider.value).toFixed(2);
    saveWeights();
    syncSettingsToProfile();
    fetchRecommendations();
  });
});

// MMR bracket dropdown
document.getElementById('mmr-bracket-select').addEventListener('change', (e) => {
  state.mmr_bracket = e.target.value;
  saveMmrBracket();
  updateBracketNote();
  syncSettingsToProfile();
  fetchRecommendations();
});

// Role filter buttons
document.querySelectorAll('.role-filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.role-filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.role_filter = btn.dataset.role;
    saveRoleFilter();
    renderTeamNeeds();
    fetchRecommendations();
  });
});

// Show more / fewer recommendations
document.getElementById('rec-more-btn').addEventListener('click', () => {
  state.show_all_recs = !state.show_all_recs;
  renderRecommendations();
});

// Settings modal
document.getElementById('settings-btn').addEventListener('click', openSettings);
document.getElementById('settings-close').addEventListener('click', closeSettings);
document.getElementById('settings-modal').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeSettings(); });
document.getElementById('weights-reset-btn').addEventListener('click', () => {
  state.weights = { ...DEFAULT_WEIGHTS };
  saveWeights();
  syncSettingsToProfile();
  applyWeightsToUI();
  fetchRecommendations();
  const st = document.getElementById('weights-status');
  st.classList.remove('hidden');
  setTimeout(() => st.classList.add('hidden'), 1500);
});

// Enemy role filter buttons
document.querySelectorAll('.enemy-role-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.enemy-role-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.enemy_role_filter = btn.dataset.erole;
    fetchRecommendations();
  });
});

// ── Chat Assistant ────────────────────────────────────────────
const chatHistory = [];  // [{role, content}] kept for multi-turn context

let chatGateShown = false;
function chatOpen() {
  document.getElementById('chat-panel').classList.remove('hidden');
  refreshChatQuota();
  const input = document.getElementById('chat-input');
  const send  = document.getElementById('chat-send-btn');
  if (!state.chat_enabled) {
    input.disabled = true; send.disabled = true;
    input.placeholder = 'AI chat is not configured on this server';
    if (!chatGateShown) { appendChatMsg('assistant', 'AI chat is unavailable: the server has no Anthropic API key. Drafting still works.'); chatGateShown = true; }
    return;
  }
  if (!authState.user) {
    input.disabled = true; send.disabled = true;
    input.placeholder = 'Log in to use AI chat';
    if (!chatGateShown) {
      const el = appendChatMsg('assistant', '');
      el.innerHTML = 'Log in to use the AI assistant. <button type="button" class="btn-link" id="chat-login-link">Log in</button>';
      el.querySelector('#chat-login-link').addEventListener('click', () => { chatClose(); openAuthModal('login'); });
      chatGateShown = true;
    }
    return;
  }
  input.disabled = false; send.disabled = false;
  input.placeholder = 'e.g. best offlane right now?';
  input.focus();
}
function chatClose() {
  document.getElementById('chat-panel').classList.add('hidden');
}

document.getElementById('chat-fab').addEventListener('click', () => {
  const panel = document.getElementById('chat-panel');
  panel.classList.contains('hidden') ? chatOpen() : chatClose();
});
document.getElementById('chat-close-btn').addEventListener('click', chatClose);

document.getElementById('chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendChatMessage();
});
document.getElementById('chat-send-btn').addEventListener('click', sendChatMessage);

/** Tiny markdown → HTML: headings, bold, italics, code, bullet/numbered lists, paragraphs. Escapes first. */
function renderMarkdown(text) {
  const lines = _esc(text || '').split(/\r?\n/);
  const out = []; let list = null;
  const inline = s => s
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<i>$2</i>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^#{1,3}\s+(.*)/))) { closeList(); out.push(`<h3>${inline(m[1])}</h3>`); }
    else if (!line.trim()) { closeList(); }
    else { closeList(); out.push(`<p>${inline(line)}</p>`); }
  }
  closeList();
  return out.join('');
}

function appendChatMsg(role, text, { markdown = false } = {}) {
  const el = document.createElement('div');
  el.className = `chat-msg ${role}`;
  if (markdown) el.innerHTML = renderMarkdown(text); else el.textContent = text;
  const msgs = document.getElementById('chat-messages');
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}

function setChatQuota(remaining) {
  const el = document.getElementById('chat-quota');
  if (!el || remaining == null) return;
  el.textContent = `${remaining} left today`;
  el.classList.toggle('low', remaining <= 10);
}
async function refreshChatQuota() {
  if (!authState.token || !state.chat_enabled) return;
  try {
    const r = await fetch('/api/chat/quota', { headers: authHeaders() });
    if (r.ok) setChatQuota((await r.json()).remaining);
  } catch (_) {}
}

// Persist the visible conversation for this browser tab
const CHAT_KEY = 'chat_history_v1';
function persistChat() { try { sessionStorage.setItem(CHAT_KEY, JSON.stringify(chatHistory.slice(-20))); } catch (_) {} }
function restoreChat() {
  try {
    const h = JSON.parse(sessionStorage.getItem(CHAT_KEY) || '[]');
    for (const m of h) { chatHistory.push(m); appendChatMsg(m.role, m.content, { markdown: m.role === 'assistant' }); }
  } catch (_) {}
}

async function sendChatMessage(preset) {
  const input = document.getElementById('chat-input');
  const btn   = document.getElementById('chat-send-btn');
  const question = (preset || input.value).trim();
  if (!question || input.disabled) return;

  input.value = '';
  btn.disabled = true;
  document.getElementById('chat-chips')?.classList.add('hidden');
  appendChatMsg('user', question);
  const bubble = appendChatMsg('assistant', '');
  bubble.classList.add('streaming', 'thinking');
  bubble.textContent = '…';

  const body = JSON.stringify({
    question,
    radiant: state.radiant_picks,
    dire:    state.dire_picks,
    my_team: state.my_team,
    mmr_bracket: state.mmr_bracket,
    weights: state.weights,
    history: chatHistory.slice(-10),
  });
  const msgs = document.getElementById('chat-messages');
  let reply = '';
  let remaining = null;

  try {
    const res = await fetch('/api/chat/stream', { method: 'POST', headers: authHeaders(), body });
    if (!res.ok) {
      let msg = `Error ${res.status}`;
      try { const e = await res.json(); msg = e.detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    bubble.classList.remove('thinking');
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let first = true;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = frame.split('\n').find(l => l.startsWith('data: '));
        if (!line) continue;
        let ev; try { ev = JSON.parse(line.slice(6)); } catch (_) { continue; }
        if (ev.error) throw new Error(ev.error);
        if (ev.delta) {
          if (first) { bubble.textContent = ''; first = false; }
          reply += ev.delta;
          bubble.innerHTML = renderMarkdown(reply);
          msgs.scrollTop = msgs.scrollHeight;
        }
        if (ev.done) remaining = ev.chats_remaining;
      }
    }
    bubble.classList.remove('streaming');
    bubble.innerHTML = renderMarkdown(reply);
    chatHistory.push({ role: 'user', content: question });
    chatHistory.push({ role: 'assistant', content: reply });
    persistChat();
    setChatQuota(remaining);
    if (remaining != null && remaining <= 5) showToast(`${remaining} AI messages left today.`, 'info');
  } catch (err) {
    bubble.classList.remove('streaming', 'thinking');
    bubble.textContent = 'Error: ' + err.message;
    bubble.classList.add('error');
  } finally {
    btn.disabled = false;
    input.focus();
  }
}

document.querySelectorAll('.chat-chip').forEach(c => c.addEventListener('click', () => sendChatMessage(c.dataset.q)));
restoreChat();

// ── Auth & Profile Event Listeners ───────────────────────────
document.getElementById('login-btn').addEventListener('click', () => openAuthModal('login'));
document.getElementById('logout-btn').addEventListener('click', () => {
  setAuthState(null, null);
  chatGateShown = false;
  fetchRecommendations();
});
document.getElementById('profile-btn').addEventListener('click', openProfilePanel);
document.getElementById('auth-modal-close').addEventListener('click', closeAuthModal);
document.getElementById('auth-form').addEventListener('submit', handleAuthSubmit);
document.getElementById('auth-switch-btn').addEventListener('click', () => {
  openAuthModal(authMode === 'login' ? 'register' : 'login');
});

// Close auth modal on overlay click
document.getElementById('auth-modal').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeAuthModal();
});

document.getElementById('profile-close').addEventListener('click', closeProfilePanel);
document.getElementById('profile-form').addEventListener('submit', handleProfileSubmit);
document.getElementById('profile-panel').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeProfilePanel();
});

// ── Global keyboard: Escape closes modals, Tab is trapped inside them ──
function _openModal() {
  return ['auth-modal', 'profile-panel', 'settings-modal']
    .map(id => document.getElementById(id))
    .find(el => el && !el.classList.contains('hidden')) || null;
}
document.addEventListener('keydown', (e) => {
  const modal = _openModal();
  // Ctrl+Z / Cmd+Z undoes the last draft change unless the user is editing text
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'z' && !modal) {
    const t = e.target;
    const editingText = t && (t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && t.id !== 'hero-search' && t.value !== ''));
    if (!editingText) { e.preventDefault(); undo(); return; }
  }
  if (e.key === 'Escape') {
    if (modal) {
      e.preventDefault();
      if (modal.id === 'auth-modal') closeAuthModal();
      else if (modal.id === 'settings-modal') closeSettings();
      else closeProfilePanel();
      return;
    }
    const chat = document.getElementById('chat-panel');
    if (!chat.classList.contains('hidden') && chat.contains(document.activeElement)) {
      chatClose();
      document.getElementById('chat-fab').focus();
    }
    return;
  }
  if (e.key === 'Tab' && modal) {
    const focusables = modal.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])');
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});
// Make the chat FAB keyboard-operable
(() => {
  const fab = document.getElementById('chat-fab');
  fab.setAttribute('role', 'button');
  fab.setAttribute('aria-label', 'Open draft assistant chat');
  fab.tabIndex = 0;
  fab.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fab.click(); }
  });
})();

// ── Start polling ─────────────────────────────────────────────
pollInterval = setInterval(pollStatus, 600);
pollStatus();
