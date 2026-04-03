/* =============================================================
   Dota 2 Draft Assistant — Frontend
   ============================================================= */

// ── State ────────────────────────────────────────────────────
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
  weights: loadWeights(),
  mmr_bracket: loadMmrBracket(),
  role_filter: loadRoleFilter(),
};

function loadWeights() {
  try {
    const stored = localStorage.getItem('draft_weights');
    if (stored) {
      const w = JSON.parse(stored);
      // Discard known old defaults so users get the new defaults
      const isOldDefault =
        (w.counter === 0.55 && w.win_rate === 0.25 && w.role_synergy === 0.20) ||
        (w.counter === 0.75 && w.win_rate === 0.20 && w.role_synergy === 0.05) ||
        (w.counter === 0.65 && w.win_rate === 0.15 && w.role_synergy === 0.20) ||
        (w.counter === 0.65 && w.win_rate === 0.15 && w.synergy === 0.20 && !w.hero_pool) ||
        (w.counter === 0.55 && w.win_rate === 0.15 && w.synergy === 0.20 && w.hero_pool === 0.10 && !w.meta);
      if (isOldDefault) {
        localStorage.removeItem('draft_weights');
      } else if (w.meta != null && w.hero_pool != null && w.synergy != null) {
        return w;  // current format with all keys
      } else if (w.synergy != null && w.hero_pool != null) {
        return { ...w, meta: 0.05 };  // add meta to existing weights
      } else if (w.synergy != null) {
        return { ...w, hero_pool: 0.05, meta: 0.05 };
      } else if (w.role_synergy != null) {
        return { counter: w.counter, win_rate: w.win_rate, synergy: w.role_synergy, hero_pool: 0.05, meta: 0.05 };
      }
    }
  } catch (_) {}
  return { counter: 0.55, win_rate: 0.15, synergy: 0.20, hero_pool: 0.05, meta: 0.05 };
}
function saveWeights() {
  localStorage.setItem('draft_weights', JSON.stringify(state.weights));
}

function loadMmrBracket() { return localStorage.getItem('draft_mmr_bracket') || '7'; }
function saveMmrBracket() { localStorage.setItem('draft_mmr_bracket', state.mmr_bracket); }
function loadRoleFilter() { return localStorage.getItem('draft_role_filter') || ''; }
function saveRoleFilter() { localStorage.setItem('draft_role_filter', state.role_filter); }

// ── Auth State ───────────────────────────────────────────────
const authState = {
  token: localStorage.getItem('auth_token') || null,
  user: JSON.parse(localStorage.getItem('auth_user') || 'null'),
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
    } else if (res.status === 401) {
      setAuthState(null, null);
    }
  } catch (_) {}
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
    await loadProfile();
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
  } else {
    document.getElementById('account-status').classList.add('hidden');
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
      item.innerHTML = `<img src="${hero.img_url}" onerror="this.style.display='none'" /> <span>${hero.localized_name}</span>`;
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
    chip.innerHTML = `<img src="${hero.img_url}" onerror="this.style.display='none'" />${hero.localized_name}<button class="chip-remove" data-id="${id}">×</button>`;
    container.appendChild(chip);
  });

  container.querySelectorAll('.chip-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      const removeId = parseInt(btn.dataset.id);
      if (authState.profile && authState.profile.hero_pool) {
        authState.profile.hero_pool = authState.profile.hero_pool.filter(h => h !== removeId);
        renderHeroPoolDisplay(authState.profile.hero_pool);
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
  }
}

// ── Role Tags ────────────────────────────────────────────────
function setupRoleTags() {
  document.querySelectorAll('#profile-roles .role-tag').forEach(btn => {
    btn.addEventListener('click', () => btn.classList.toggle('active'));
  });
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

async function linkDotaAccount() {
  const input = document.getElementById('profile-dota-id');
  const btn = document.getElementById('link-account-btn');
  const accountId = input.value.trim();
  if (!accountId) return;

  btn.disabled = true;
  btn.textContent = 'Linking...';

  try {
    const res = await fetch('/api/link_account', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ dota_account_id: accountId }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.detail || 'Could not link account');
      return;
    }
    // Update local profile
    if (!authState.profile) authState.profile = {};
    authState.profile.dota_account_id = accountId;
    authState.profile.player_stats = data;
    showAccountStatus(data);
    renderPlayerStats(data);
  } catch (err) {
    alert('Network error linking account');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Link';
  }
}

async function unlinkDotaAccount() {
  if (!confirm('Unlink your Dota 2 account?')) return;
  try {
    const res = await fetch('/api/unlink_account', {
      method: 'POST',
      headers: authHeaders(),
    });
    if (!res.ok) {
      alert('Could not unlink account');
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
    alert('Network error unlinking account');
  }
}

function showAccountStatus(playerData) {
  const statusEl = document.getElementById('account-status');
  const infoEl = document.getElementById('account-info');
  statusEl.classList.remove('hidden');
  infoEl.innerHTML = `
    <span class="account-name">${playerData.name || 'Unknown'}</span>
    <span class="account-rank">${playerData.rank || ''}</span>
    <span class="account-wr">${playerData.overall_wr || 0}% WR</span>
    <span class="account-matches">${(playerData.total_matches || 0).toLocaleString()} matches</span>
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
            <img src="${heroData.img_url || ''}" onerror="this.style.display='none'" />
            <span class="ph-name">${h.hero_name}</span>
            <span class="ph-matches">${h.matches} games</span>
            <span class="ph-wr ${wrClass}">${h.win_rate}%</span>
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
  e.preventDefault();

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
    statusEl.textContent = 'Saved!';
    statusEl.classList.remove('hidden');
    setTimeout(() => statusEl.classList.add('hidden'), 2000);
  } else {
    statusEl.textContent = 'Save failed';
    statusEl.classList.remove('hidden');
  }
}

// ── Boot: poll until backend is ready ────────────────────────
let pollInterval = null;

async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    if (data.error) {
      document.getElementById('splash-status').textContent = 'Error: ' + data.error;
      return;
    }

    const pct = data.total > 0 ? Math.round((data.progress / data.total) * 100) : 0;
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('splash-status').textContent =
      data.total > 0
        ? `Caching matchup data... ${data.progress}/${data.total} heroes`
        : 'Loading hero data...';

    if (data.ready) {
      clearInterval(pollInterval);
      await initApp();
    }
  } catch (_) {
    document.getElementById('splash-status').textContent = 'Waiting for server...';
  }
}

async function initApp() {
  // Fetch hero list
  const res = await fetch('/api/heroes');
  const heroes = await res.json();

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

  renderHeroGrid();
  updateAddTargetLabels();
  applyWeightsToUI();
  renderDraftBoard();
  fetchRecommendations();

  // Auth: restore login state + load profile
  updateAuthUI();
  if (authState.token) await loadProfile();

  // Wire up profile panel interactions
  setupHeroSearch();
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
  const query = filter.toLowerCase();

  const filtered = query
    ? state.heroList.filter(h => {
        const name = h.localized_name.toLowerCase();
        const initials = h.localized_name.split(' ').map(w => w[0]).join('').toLowerCase();
        return name.includes(query) || initials === query;
      })
    : state.heroList;

  grid.innerHTML = '';
  for (const hero of filtered) {
    const card = document.createElement('div');
    card.className = 'hero-card' + (used.has(hero.id) ? ' used' : '');
    card.dataset.heroId = hero.id;

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
  applyGridScoreOverlays();
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
  };
  el.placeholder = `Search — ${labels[state.add_target] || ''} — Tab to switch`;
}

// ── Hero selection logic ──────────────────────────────────────
function handleHeroCardClick(heroId) {
  const myTeam = state.my_team;
  let added = false;

  if (state.add_target === 'my-pick') {
    const arr = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;
    if (arr.length >= 5) return;
    arr.push(heroId);
    added = true;
    if (arr.length >= 5) setAddTarget('enemy-pick');
  } else if (state.add_target === 'enemy-pick') {
    const arr = myTeam === 'radiant' ? state.dire_picks : state.radiant_picks;
    if (arr.length >= 5) return;
    arr.push(heroId);
    added = true;
    if (arr.length >= 5) setAddTarget('my-pick');
  }

  if (added) {
    const searchEl = document.getElementById('hero-search');
    searchEl.value = '';
    onStateChange();
    searchEl.focus();
  }
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
  renderHeroGrid(document.getElementById('hero-search').value);
  fetchRecommendations();
  if (state.radiant_picks.length === 5 && state.dire_picks.length === 5) {
    fetchDraftAnalysis();
  } else {
    document.getElementById('winprob-panel').classList.add('hidden');
  }
}

// ── Draft Board Render ────────────────────────────────────────
function renderDraftBoard() {
  renderPickSlots('radiant', state.radiant_picks);
  renderPickSlots('dire', state.dire_picks);
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

      const rmBtn = document.createElement('div');
      rmBtn.className = 'slot-remove';
      rmBtn.textContent = '×';
      rmBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        picks.splice(i, 1);
        onStateChange();
      });

      slot.appendChild(img);
      slot.appendChild(label);
      slot.appendChild(rmBtn);
    } else {
      slot.className = 'pick-slot empty';
      slot.innerHTML = '';
    }
  });
}


// ── Grid Score Overlays ───────────────────────────────────────
function applyGridScoreOverlays() {
  // Remove existing badges
  document.querySelectorAll('.hero-score-badge').forEach(el => el.remove());
}

// ── Threat Panel ──────────────────────────────────────────────
function renderThreatPanel() {
  const panel = document.getElementById('threat-panel');
  const list  = document.getElementById('threat-list');
  const myTeam     = state.my_team;
  const allyPicks  = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;
  const enemyPicks = myTeam === 'radiant' ? state.dire_picks    : state.radiant_picks;

  if (allyPicks.length === 0 || enemyPicks.length === 0 || !state.threats?.length) {
    panel.classList.add('hidden');
    return;
  }

  panel.classList.remove('hidden');
  list.innerHTML = '';

  state.threats.forEach(t => {
    const winPct = (t.win_rate * 100).toFixed(1);
    const adv = t.win_rate - 0.5;
    const cls = adv >= 0.05 ? 'threat-high' : adv >= 0.02 ? 'threat-mid' : 'threat-low';
    const enemy = state.heroes[t.enemy_id]    || {};
    const ally  = state.heroes[t.vs_ally_id]  || {};

    const row = document.createElement('div');
    row.className = `threat-row ${cls}`;
    row.innerHTML = `
      <img class="threat-img" src="${enemy.img_url || ''}" alt="${t.enemy_name}"
           onerror="this.style.display='none'">
      <span class="threat-name">${t.enemy_name}</span>
      <span class="threat-arrow">counters</span>
      <img class="threat-img" src="${ally.img_url || ''}" alt="${t.vs_ally_name}"
           onerror="this.style.display='none'">
      <span class="threat-name">${t.vs_ally_name}</span>
      <span class="threat-winrate ${cls}">${winPct}%</span>
    `;
    list.appendChild(row);
  });
}

// ── Win Probability ───────────────────────────────────────
async function fetchDraftAnalysis() {
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
    renderWinProb(await res.json());
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

  function fmtAdv(val) {
    return (val >= 0 ? '+' : '') + val.toFixed(1) + '%';
  }
  function advCls(val) { return val >= 0 ? 'adv-radiant' : 'adv-dire'; }
  function favored(val) { return val >= 0 ? 'Radiant' : 'Dire'; }

  function matchupRow(pair, radiantFavored) {
    const winPct      = (pair.win_rate * 100).toFixed(1);
    const winnerImg   = radiantFavored ? pair.radiant_img  : pair.dire_img;
    const winnerName  = radiantFavored ? pair.radiant_name : pair.dire_name;
    const loserImg    = radiantFavored ? pair.dire_img     : pair.radiant_img;
    const loserName   = radiantFavored ? pair.dire_name    : pair.radiant_name;
    const cls         = radiantFavored ? 'matchup-radiant' : 'matchup-dire';
    return `
      <div class="wp-matchup-row ${cls}">
        <img src="${winnerImg}"  onerror="this.style.display='none'" />
        <span class="wp-mname">${winnerName}</span>
        <span class="wp-arrow">beats</span>
        <img src="${loserImg}"   onerror="this.style.display='none'" />
        <span class="wp-mname">${loserName}</span>
        <span class="wp-pct">${winPct}%</span>
      </div>`;
  }

  function synRow(pair, team) {
    const winPct = (pair.win_rate * 100).toFixed(1);
    const cls    = team === 'radiant' ? 'syn-radiant' : 'syn-dire';
    return `
      <div class="wp-syn-row ${cls}">
        <img src="${pair.hero1_img}" onerror="this.style.display='none'" />
        <span class="wp-mname">${pair.hero1_name}</span>
        <span class="wp-arrow">+</span>
        <img src="${pair.hero2_img}" onerror="this.style.display='none'" />
        <span class="wp-mname">${pair.hero2_name}</span>
        <span class="wp-pct">${winPct}%</span>
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
        <span class="wp-prob ${dProb > rProb ? 'wp-winner' : ''}" style="color:var(--dire)">${dProb}%</span>
      </div>
    </div>

    <div class="wp-factors">
      <div class="wp-section-label">WHY</div>
      <div class="wp-factor-row ${advCls(comp.matchup_adv)}">
        <span class="wp-factor-name">Matchup edge</span>
        <span class="wp-factor-bar-wrap"><div class="wp-factor-bar" style="width:${Math.min(100, Math.abs(comp.matchup_adv) * 400)}%"></div></span>
        <span class="wp-factor-val">${fmtAdv(comp.matchup_adv)} ${favored(comp.matchup_adv)}</span>
      </div>
      <div class="wp-factor-row ${advCls(comp.wr_adv)}">
        <span class="wp-factor-name">Win rate edge</span>
        <span class="wp-factor-bar-wrap"><div class="wp-factor-bar" style="width:${Math.min(100, Math.abs(comp.wr_adv) * 400)}%"></div></span>
        <span class="wp-factor-val">${fmtAdv(comp.wr_adv)} ${favored(comp.wr_adv)} (R:${data.radiant_avg_wr}% / D:${data.dire_avg_wr}%)</span>
      </div>
      <div class="wp-factor-row ${advCls(comp.synergy_adv)}">
        <span class="wp-factor-name">Synergy edge</span>
        <span class="wp-factor-bar-wrap"><div class="wp-factor-bar" style="width:${Math.min(100, Math.abs(comp.synergy_adv) * 400)}%"></div></span>
        <span class="wp-factor-val">${fmtAdv(comp.synergy_adv)} ${favored(comp.synergy_adv)}</span>
      </div>
    </div>

    ${matchupSection}
    ${synSection}
  `;
}

// ── Recommendations ───────────────────────────────────────────
let recDebounceTimer = null;

async function fetchRecommendations() {
  clearTimeout(recDebounceTimer);
  recDebounceTimer = setTimeout(async () => {
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
        }),
      });
      if (!res.ok) return;
      const data = await res.json();
      // Handle both old list format and new {top, all_scores, threats} format
      if (Array.isArray(data)) {
        state.recommendations = data;
        state.allScores = {};
        state.threats = [];
      } else {
        state.recommendations = data.top || [];
        state.allScores       = data.all_scores || {};
        state.threats         = data.threats    || [];
      }
      renderRecommendations();
      applyGridScoreOverlays();
      renderThreatPanel();
    } catch (_) {}
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

  if (!recs || recs.length === 0) {
    list.innerHTML = '<div style="color:var(--text-muted);font-style:italic;padding:8px">No recommendations yet.</div>';
    return;
  }

  // Find score range for color coding
  const maxScore = recs[0]?.total_score ?? 0.1;
  const minScore = recs[recs.length - 1]?.total_score ?? -0.1;
  const range = Math.max(maxScore - minScore, 0.01);

  recs.forEach((rec, i) => {
    const card = document.createElement('div');
    card.className = 'rec-card';

    // Bar width: relative to score range, clamped 5%-100%
    const barPct = Math.max(5, Math.min(100, ((rec.total_score - minScore) / range) * 100));
    const barColor = rec.total_score > 0.05
      ? 'var(--score-high)' : rec.total_score > 0
      ? 'var(--score-mid)' : 'var(--score-low)';
    const scoreClass = rec.total_score > 0.05
      ? 'score-high' : rec.total_score > 0
      ? 'score-mid' : 'score-low';

    // Build counter tags — show both good and bad matchups
    // Display raw win rate + game count from Stratz (consistent with chat assistant)
    // Threshold lowered to 0.005 so even small-sample matchups show (with game count for context)
    const counterDetail = rec.breakdown.counters_detail || [];
    const goodCounters = counterDetail.filter(c => c.advantage > 0.005);
    const badCounters = counterDetail.filter(c => c.advantage < -0.005);

    const tags = [];
    if (goodCounters.length > 0) {
      const counterLabels = goodCounters.map(c => {
        const winPct = c.win_rate != null ? (c.win_rate * 100).toFixed(1) : '?';
        const games = c.games ? ` (${c.games}g)` : '';
        return `${shortName(c.vs_hero)} ${winPct}%${games}`;
      });
      tags.push(`<span class="tag tag-counter">vs ${counterLabels.join(' · ')}</span>`);
    }
    for (const c of badCounters) {
      const winPct = c.win_rate != null ? (c.win_rate * 100).toFixed(1) : '?';
      const games = c.games ? ` (${c.games}g)` : '';
      tags.push(`<span class="tag tag-weak">weak vs ${shortName(c.vs_hero)} ${winPct}%${games}</span>`);
    }
    tags.push(`<span class="tag tag-wr">WR ${rec.breakdown.win_rate_pct}%</span>`);
    if (rec.breakdown.synergy_score > 0.6) {
      tags.push(`<span class="tag tag-role">Synergy</span>`);
    }
    if (rec.in_hero_pool) {
      tags.push(`<span class="tag tag-pool">Pool</span>`);
    }

    card.innerHTML = `
      <div class="rec-rank">${i + 1}</div>
      <img class="rec-img" src="${rec.img_url}" alt="${rec.localized_name}"
           onerror="this.style.display='none'" />
      <div class="rec-info">
        <div class="rec-name">${rec.localized_name}</div>
        <div class="rec-score-bar-wrap">
          <div class="rec-score-bar" style="width:${barPct}%;background:${barColor}"></div>
        </div>
        <div class="rec-tags">${tags.join('')}</div>
      </div>
      <div class="rec-score-num ${scoreClass}">${rec.total_score > 0 ? '+' : ''}${rec.total_score.toFixed(3)}</div>
    `;

    // Click a recommendation to add to your team
    card.addEventListener('click', () => {
      const allyArr = myTeam === 'radiant' ? state.radiant_picks : state.dire_picks;
      if (allyArr.length < 5 && !getUsedSet().has(rec.hero_id)) {
        allyArr.push(rec.hero_id);
        onStateChange();
      }
    });

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
});

document.getElementById('hero-search').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    const firstCard = document.querySelector('#hero-grid .hero-card:not(.used)');
    if (firstCard) handleHeroCardClick(parseInt(firstCard.dataset.heroId));
  } else if (e.key === 'Tab') {
    e.preventDefault();
    const modes = ['my-pick', 'enemy-pick'];
    const idx = modes.indexOf(state.add_target);
    setAddTarget(modes[(idx + 1) % modes.length]);
  } else if (e.key === 'Escape') {
    e.target.value = '';
    renderHeroGrid('');
  }
});

document.getElementById('my-team-select').addEventListener('change', (e) => {
  state.my_team = e.target.value;
  localStorage.setItem('my_team', state.my_team);
  updateAddTargetLabels();
  fetchRecommendations();
  renderRecommendations();
  renderThreatPanel();
});

document.querySelectorAll('.add-target-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    setAddTarget(btn.dataset.target);
    document.getElementById('hero-search').focus();
  });
});

document.getElementById('reset-btn').addEventListener('click', () => {
  state.radiant_picks = [];
  state.dire_picks = [];
  state.bans = [];
  state.recommendations = [];
  state.allScores = {};
  state.threats = [];
  const searchEl = document.getElementById('hero-search');
  searchEl.value = '';
  setAddTarget('my-pick');
  onStateChange();
  searchEl.focus();
  document.getElementById('rec-list').innerHTML = '';
  document.getElementById('rec-hint').textContent = 'Select heroes to see suggestions';
  document.getElementById('winprob-panel').classList.add('hidden');
  renderThreatPanel();
  applyGridScoreOverlays();
});

document.getElementById('refresh-btn').addEventListener('click', async () => {
  const btn = document.getElementById('refresh-btn');
  btn.disabled = true;
  btn.textContent = 'Refreshing...';
  try {
    await fetch('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ force: true }) });
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
    fetchRecommendations();
  });
});

// MMR bracket dropdown
document.getElementById('mmr-bracket-select').addEventListener('change', (e) => {
  state.mmr_bracket = e.target.value;
  saveMmrBracket();
  fetchRecommendations();
});

// Role filter buttons
document.querySelectorAll('.role-filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.role-filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.role_filter = btn.dataset.role;
    saveRoleFilter();
    fetchRecommendations();
  });
});

// ── Chat Assistant ────────────────────────────────────────────
const chatHistory = [];  // [{role, content}] kept for multi-turn context

function chatOpen() {
  document.getElementById('chat-panel').classList.remove('hidden');
  document.getElementById('chat-input').focus();
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

function appendChatMsg(role, text) {
  const el = document.createElement('div');
  el.className = `chat-msg ${role}`;
  el.textContent = text;
  const msgs = document.getElementById('chat-messages');
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}

async function sendChatMessage() {
  const input = document.getElementById('chat-input');
  const btn   = document.getElementById('chat-send-btn');
  const question = input.value.trim();
  if (!question) return;

  input.value = '';
  btn.disabled = true;
  appendChatMsg('user', question);
  const thinking = appendChatMsg('thinking', '...');

  const myTeam     = state.my_team;
  const radiantIds = state.radiant_picks;
  const direIds    = state.dire_picks;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({
        question,
        radiant: radiantIds,
        dire:    direIds,
        my_team: myTeam,
        mmr_bracket: state.mmr_bracket,
        history: chatHistory.slice(-10),  // last 10 turns for context
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    thinking.remove();
    appendChatMsg('assistant', data.reply);
    chatHistory.push({ role: 'user',      content: question    });
    chatHistory.push({ role: 'assistant', content: data.reply  });
  } catch (err) {
    thinking.remove();
    appendChatMsg('assistant', '⚠ Error: ' + err.message);
  } finally {
    btn.disabled = false;
    input.focus();
  }
}

// ── Auth & Profile Event Listeners ───────────────────────────
document.getElementById('login-btn').addEventListener('click', () => openAuthModal('login'));
document.getElementById('logout-btn').addEventListener('click', () => {
  setAuthState(null, null);
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

// ── Start polling ─────────────────────────────────────────────
pollInterval = setInterval(pollStatus, 600);
pollStatus();
