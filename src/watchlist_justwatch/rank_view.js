// ---------- Rank: the year's ranked list ----------
//
// (Inlined from rank_view.js; RankEngine is rank_engine.js, inlined above.)
//
// A game of "which ranks higher?" that places one film at a time into the
// year's list by binary insertion — see rank_engine.js for the method. The
// roster (which films the year holds) and the order placed so far live in
// Postgres and are read and written through the Worker's /rank-state and
// /rank-save, live rather than through the page's daily payload: the order
// changes every few taps and has to be the same on every device.
//
// What's kept on this device is only what hasn't reached the server yet: the
// order while a save is pending, and the half-finished search for the film
// currently being placed (answers given against it so far).
const RANK_YEAR = DATA.ranking.year;
const RANK_CACHE_KEY = 'watchlist_rank_v1_' + RANK_YEAR;
const RANK_LIST_PREVIEW = 30;

const rank = {
  status: 'idle',        // idle | loading | ready | error
  error: '',
  roster: [],
  bySlug: new Map(),
  placed: [],
  stateId: 0,            // the saved state this device's order is based on
  dirty: false,          // placed has changes the server doesn't have
  saving: false,
  saveFailed: false,
  search: null,
  flip: false,           // which side the film being placed is shown on
  skipped: new Set(),
  undo: [],
  justPlaced: null,      // {slug, position} — the line under the duel
  showAll: false,
  openRow: null,
};
let rankSaveTimer = null;

function rankApi(path, body, keepalive) {
  return fetch(DATA.settings.refresh_worker_url + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Trigger-Secret': DATA.settings.refresh_trigger_secret },
    body: JSON.stringify(body),
    keepalive: !!keepalive,
  });
}

function rankReadCache() {
  try {
    return JSON.parse(localStorage.getItem(RANK_CACHE_KEY) || 'null');
  } catch {
    return null;
  }
}

function rankWriteCache() {
  try {
    localStorage.setItem(RANK_CACHE_KEY, JSON.stringify({
      stateId: rank.stateId, placed: rank.placed, dirty: rank.dirty, search: rank.search,
    }));
  } catch {
    // Private browsing / storage full: an unsaved answer or two is lost on a
    // reload, nothing more — the saved order is the server's.
  }
}

const rankStarsOf = slug => (rank.bySlug.get(slug) || {}).stars;

function rankStars(stars) {
  if (stars == null) return 'not rated';
  return '★'.repeat(Math.floor(stars)) + (stars % 1 ? '½' : '');
}

async function rankLoad() {
  rank.status = 'loading';
  renderRank();
  try {
    const response = await rankApi('/rank-state', { year: RANK_YEAR });
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || ('the server answered ' + response.status));
    rank.roster = body.roster;
    rank.bySlug = new Map(body.roster.map(f => [f.slug, f]));
    const cached = rankReadCache();
    // Unsaved work from this device carries over only if nothing else has
    // saved since it was based — otherwise the server's order is the truth.
    const resume = cached && cached.stateId === body.state_id;
    rank.stateId = body.state_id;
    rank.placed = (resume && cached.dirty ? cached.placed : body.placed).filter(slug => rank.bySlug.has(slug));
    rank.dirty = !!(resume && cached.dirty);
    rank.search = resume ? rankValidSearch(cached.search) : null;
    rank.undo = [];
    rank.justPlaced = null;
    rank.status = 'ready';
    rankAdvance();
    if (rank.dirty) rankScheduleSave(0);
  } catch (err) {
    rank.status = 'error';
    rank.error = err.message || String(err);
  }
  renderRank();
}

// A search saved by an earlier visit is only good if it still describes this
// list: its film still waiting, its bounds inside the list as it is now.
function rankValidSearch(search) {
  if (!search || !rank.bySlug.has(search.slug) || rank.placed.includes(search.slug)) return null;
  const n = rank.placed.length;
  if (!(search.lo >= 0 && search.lo <= search.hi && search.hi <= n)) return null;
  return search;
}

function rankQueue() {
  const all = RankEngine.queue(rank.roster, rank.placed);
  const fresh = all.filter(f => !rank.skipped.has(f.slug));
  return fresh.length ? fresh : all;
}

// Moves on until there's a question to ask: starts the next film's search,
// and files any film whose position is already known (the first film of
// all needs no question).
function rankAdvance() {
  for (let guard = 0; guard < 5000; guard++) {
    if (!rank.search) {
      const next = rankQueue()[0];
      if (!next) return;
      rank.search = RankEngine.startSearch(next.slug, next.stars, rank.placed, rankStarsOf);
      rank.flip = Math.random() < 0.5;
    }
    if (RankEngine.nextOpponent(rank.search, rank.placed) !== null) return;
    const slug = rank.search.slug;
    rank.placed = RankEngine.insertAt(rank.placed, slug, rank.search.lo);
    rank.justPlaced = { slug, position: rank.placed.indexOf(slug) + 1, taps: rank.search.taps };
    rank.search = null;
    rank.skipped.delete(slug);
    rankChanged();
  }
}

function rankSnapshot() {
  rank.undo.push({ placed: rank.placed.slice(), search: rank.search && { ...rank.search }, flip: rank.flip,
                   justPlaced: rank.justPlaced });
  if (rank.undo.length > 80) rank.undo.shift();
}

function rankAnswer(pickedSlug, button) {
  if (!rank.search || rank.status !== 'ready') return;
  rankSnapshot();
  rank.justPlaced = null;
  rank.search = RankEngine.answer(rank.search, rank.placed, pickedSlug === rank.search.slug);
  rank.flip = Math.random() < 0.5;
  rankAdvance();
  rankWriteCache();
  // A beat of feedback on the poster that was picked before the next pair.
  if (button) {
    button.classList.add('picked');
    setTimeout(renderRank, 140);
  } else {
    renderRank();
  }
}

function rankUndo() {
  const last = rank.undo.pop();
  if (!last) return;
  const changed = last.placed.join('|') !== rank.placed.join('|');
  rank.placed = last.placed;
  rank.search = last.search;
  rank.flip = last.flip;
  rank.justPlaced = last.justPlaced;
  if (changed) rankChanged(); else rankWriteCache();
  renderRank();
}

function rankSkipFilm() {
  if (!rank.search) return;
  rankSnapshot();
  rank.skipped.add(rank.search.slug);
  rank.search = null;
  rank.justPlaced = null;
  rankAdvance();
  rankWriteCache();
  renderRank();
}

// Takes a film back out and places it again from scratch, right now.
function rankPlaceAgain(slug) {
  rankSnapshot();
  rank.placed = rank.placed.filter(s => s !== slug);
  const film = rank.bySlug.get(slug);
  rank.search = RankEngine.startSearch(slug, film.stars, rank.placed, rankStarsOf);
  rank.flip = Math.random() < 0.5;
  rank.justPlaced = null;
  rank.openRow = null;
  rankChanged();
  rankAdvance();
  renderRank();
  document.getElementById('rankScreen').scrollIntoView({ block: 'start' });
}

function rankNudge(slug, delta) {
  const from = rank.placed.indexOf(slug);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= rank.placed.length) return;
  rankSnapshot();
  const next = rank.placed.slice();
  next.splice(from, 1);
  next.splice(to, 0, slug);
  rank.placed = next;
  // The film being placed was mid-search against the old order.
  if (rank.search) {
    rank.search = null;
    rankAdvance();
  }
  rankChanged();
  renderRank();
}

function rankChanged() {
  rank.dirty = true;
  rankWriteCache();
  rankScheduleSave(1200);
}

// A save already waiting isn't pushed back by the next tap — otherwise a
// steady run of answers would keep deferring it and nothing would be saved
// until you stopped. `delay` 0 means now.
function rankScheduleSave(delay) {
  if (rankSaveTimer && delay > 0) return;
  clearTimeout(rankSaveTimer);
  rankSaveTimer = setTimeout(() => { rankSaveTimer = null; rankSave(); }, delay);
}

async function rankSave() {
  if (rank.saving || !rank.dirty || rank.status !== 'ready') return;
  rank.saving = true;
  const sent = rank.placed.slice();
  try {
    const response = await rankApi('/rank-save', { year: RANK_YEAR, base_id: rank.stateId, placed: sent });
    const body = await response.json().catch(() => ({}));
    if (response.status === 409) {
      // Saved from another device, or the daily run took a rewatched film
      // back out. Theirs is newer: start again from it.
      rank.saving = false;
      rank.dirty = false;
      rank.search = null;
      rankWriteCache();
      showToast('Your ranking changed somewhere else — reloaded it.');
      await rankLoad();
      return;
    }
    if (!response.ok || !body.ok) throw new Error(body.error || ('the server answered ' + response.status));
    rank.stateId = body.state_id;
    rank.saveFailed = false;
    rank.dirty = sent.join('|') !== rank.placed.join('|');
  } catch {
    rank.saveFailed = true;
  }
  rank.saving = false;
  rankWriteCache();
  rankUpdateSaveNote();
  if (rank.dirty) rankScheduleSave(rank.saveFailed ? 15000 : 600);
}

// Leaving with a save still pending: one last try that outlives the page.
window.addEventListener('pagehide', () => {
  if (rank.status === 'ready' && rank.dirty && !rank.saving) {
    rankApi('/rank-save', { year: RANK_YEAR, base_id: rank.stateId, placed: rank.placed }, true).catch(() => {});
  }
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && rank.dirty) rankScheduleSave(0);
});

function rankOpen() {
  if (rank.status === 'idle' || rank.status === 'error') rankLoad();
  else renderRank();
}

// ---------- Export ----------

function rankCsv() {
  return RankEngine.toCsv(rank.roster, rank.placed);
}

async function rankExport() {
  const name = 'letterboxd-ranked-' + RANK_YEAR + '.csv';
  const csv = rankCsv();
  // A phone's share sheet is the dependable way to get a file out of a
  // home-screen app (Save to Files, AirDrop); a desktop just downloads it.
  try {
    const file = new File([csv], name, { type: 'text/csv' });
    if (matchMedia('(pointer: coarse)').matches && navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: name });
      return;
    }
  } catch (err) {
    if (err && err.name === 'AbortError') return;
  }
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
}

async function rankCopyCsv() {
  try {
    await navigator.clipboard.writeText(rankCsv());
    showToast('Copied the list as CSV.');
  } catch {
    showToast("Couldn't copy — use Export instead.");
  }
}

// ---------- Rendering ----------

function rankPosterHtml(film) {
  const label = '<span>' + esc(film.title) + '</span>';
  if (!film.poster_url) return label;
  // A poster that fails to load falls back to the title, same as no poster.
  return '<img loading="lazy" alt="" src="' + escAttr(film.poster_url) + '" data-fallback="' + escAttr(film.title) + '">';
}

function rankChoiceHtml(film, badge) {
  const meta = [film.film_year, '<span class="rank-stars">' + rankStars(film.stars) + '</span>']
    .filter(part => part != null && part !== '').join(' · ');
  return '<button class="rank-choice" type="button" data-slug="' + escAttr(film.slug) + '">' +
    badge +
    '<div class="rank-poster">' + rankPosterHtml(film) + '</div>' +
    '<div><div class="rank-title">' + esc(film.title) + '</div><div class="rank-meta">' + meta + '</div></div>' +
    '</button>';
}

function rankSaveNoteText() {
  if (rank.saveFailed && rank.dirty) return 'Not saved yet — will retry';
  if (rank.dirty || rank.saving) return 'Saving…';
  return 'Saved';
}

function rankUpdateSaveNote() {
  const note = document.getElementById('rankSaveNote');
  if (note) note.textContent = rankSaveNoteText();
}

function rankDuelHtml() {
  const total = rank.roster.length;
  const waiting = total - rank.placed.length;
  const justPlaced = rank.justPlaced && rank.bySlug.get(rank.justPlaced.slug);
  const just = justPlaced
    ? '<div class="rank-note just">' + esc(justPlaced.title) + ' placed at #' + rank.justPlaced.position +
      (rank.justPlaced.taps ? ' in ' + rank.justPlaced.taps + (rank.justPlaced.taps === 1 ? ' tap' : ' taps') : '') + '.</div>'
    : '';
  const tail = '<div class="rank-actions">' +
    '<button class="rank-btn" id="rankUndo"' + (rank.undo.length ? '' : ' disabled') + '>Undo</button>' +
    (rank.search && waiting > 1 ? '<button class="rank-btn" id="rankSkip">Skip this film</button>' : '') +
    '<span class="rank-save" id="rankSaveNote">' + rankSaveNoteText() + '</span></div>';

  if (!rank.search) {
    return '<div class="rank-card"><div><div class="rank-q">Every film is placed.</div>' +
      '<div class="rank-sub">New watches and rewatches will turn up here to be placed. ' +
      'To move a film, tap it in the list below.</div></div>' + just + tail + '</div>';
  }

  const film = rank.bySlug.get(rank.search.slug);
  const opponentIndex = RankEngine.nextOpponent(rank.search, rank.placed);
  const opponent = rank.bySlug.get(rank.placed[opponentIndex]);
  const left = Math.max(1, Math.ceil(Math.log2(rank.search.hi - rank.search.lo + 1)));
  const label = film.queue_first ? (film.is_rewatch ? 'Rewatch' : 'New') : 'Placing';
  const cards = [
    rankChoiceHtml(film, '<span class="rank-badge">' + label + '</span>'),
    rankChoiceHtml(opponent, '<span class="rank-badge at">#' + (opponentIndex + 1) + '</span>'),
  ];
  if (rank.flip) cards.reverse();
  return '<div class="rank-card">' +
    '<div><div class="rank-q">Which ranks higher?</div>' +
    '<div class="rank-sub">Placing ' + esc(film.title) + ' · about ' + left + (left === 1 ? ' more tap' : ' more taps') + '</div></div>' +
    '<div class="rank-duel">' + cards.join('') + '</div>' + just + tail + '</div>';
}

function rankListHtml() {
  const order = RankEngine.fullOrder(rank.roster, rank.placed);
  const placed = new Set(rank.placed);
  const shown = rank.showAll ? order : order.slice(0, RANK_LIST_PREVIEW);
  const rows = shown.map((slug, index) => {
    const film = rank.bySlug.get(slug);
    const isPlaced = placed.has(slug);
    const thumb = film.poster_url
      ? '<img loading="lazy" alt="" src="' + escAttr(film.poster_url) + '">' : '<span class="rank-thumb"></span>';
    let html = '<div class="rank-row' + (isPlaced ? '' : ' waiting') + '" data-slug="' + escAttr(slug) + '">' +
      '<span class="rank-pos">' + (index + 1) + '</span>' + thumb +
      '<span class="rank-name">' + esc(film.title) + (film.film_year ? ' <small>' + film.film_year + '</small>' : '') +
      (isPlaced ? '' : '<em>to place</em>') + '</span>' +
      '<span class="rank-stars">' + rankStars(film.stars) + '</span></div>';
    if (rank.openRow === slug) {
      const at = rank.placed.indexOf(slug);
      html += '<div class="rank-row-actions">' +
        (isPlaced
          ? '<button class="rank-btn" data-act="up"' + (at === 0 ? ' disabled' : '') + '>Up one</button>' +
            '<button class="rank-btn" data-act="down"' + (at === rank.placed.length - 1 ? ' disabled' : '') + '>Down one</button>' +
            '<button class="rank-btn" data-act="again">Place again</button>'
          : '<button class="rank-btn" data-act="again">Place now</button>') +
        '<a class="rank-btn" target="_blank" rel="noopener" href="https://letterboxd.com/film/' + escAttr(slug) + '/">Letterboxd ↗</a>' +
        '</div>';
    }
    return html;
  }).join('');
  const more = order.length > RANK_LIST_PREVIEW
    ? '<button class="rank-btn" id="rankToggleAll">' + (rank.showAll ? 'Show top ' + RANK_LIST_PREVIEW : 'Show all ' + order.length) + '</button>'
    : '';
  return '<div class="rank-list-head"><h3>' + (rank.showAll ? 'All ' + order.length : 'Top ' + Math.min(RANK_LIST_PREVIEW, order.length)) + '</h3>' +
    '<div class="rank-actions">' + more +
    '<button class="rank-btn" id="rankCopy">Copy CSV</button>' +
    '<button class="rank-btn primary" id="rankExport">Export CSV</button></div></div>' +
    '<div class="rank-note">Films still to place sit at the bottom of their star rating until you get to them, so the list is complete whenever you export it.</div>' +
    '<div class="rank-list">' + rows + '</div>';
}

function renderRank() {
  const screen = document.getElementById('rankScreen');
  if (!screen) return;
  const head = '<div class="rank-head"><h2>Rank ' + RANK_YEAR + '</h2>';
  if (rank.status === 'idle' || rank.status === 'loading') {
    screen.innerHTML = '<div class="rank-wrap">' + head + '<div class="rank-note">Loading your ' + RANK_YEAR + ' films…</div></div></div>';
    return;
  }
  if (rank.status === 'error') {
    screen.innerHTML = '<div class="rank-wrap">' + head + '<div class="rank-note warn">Couldn’t load the ranking: ' + esc(rank.error) +
      '</div><div class="rank-actions"><button class="rank-btn" id="rankRetry">Try again</button></div></div></div>';
    document.getElementById('rankRetry').addEventListener('click', rankLoad);
    return;
  }
  const total = rank.roster.length;
  if (!total) {
    screen.innerHTML = '<div class="rank-wrap">' + head + '<div class="rank-note">No ' + RANK_YEAR +
      ' watches in your diary yet. They appear here after the next daily run.</div></div></div>';
    return;
  }
  const done = rank.placed.length;
  const waiting = total - done;
  // Each film costs about log2 of the list it's going into, plus one.
  let tapsLeft = 0;
  for (let k = done; k < total; k++) tapsLeft += Math.log2(Math.max(2, k * 0.35)) + 1;
  const newCount = rank.roster.filter(f => f.queue_first && !rank.placed.includes(f.slug)).length;
  screen.innerHTML = '<div class="rank-wrap">' + head +
    '<div class="rank-progress"><span>' + done + ' of ' + total + ' placed' +
    (newCount ? ' · ' + newCount + ' new' : '') + '</span>' +
    '<span>' + (waiting ? 'roughly ' + (Math.round(tapsLeft / 10) * 10 || 10) + ' taps to finish' : 'complete') + '</span></div>' +
    '<div class="rank-track"><i style="width:' + (done / total * 100).toFixed(1) + '%"></i></div></div>' +
    rankDuelHtml() + rankListHtml() + '</div>';

  screen.querySelectorAll('.rank-choice').forEach(button => {
    button.addEventListener('click', () => rankAnswer(button.dataset.slug, button));
  });
  screen.querySelectorAll('.rank-poster img').forEach(img => {
    img.addEventListener('error', () => {
      const label = document.createElement('span');
      label.textContent = img.dataset.fallback;
      img.replaceWith(label);
    });
  });
  const on = (id, handler) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', handler);
  };
  on('rankUndo', rankUndo);
  on('rankSkip', rankSkipFilm);
  on('rankExport', rankExport);
  on('rankCopy', rankCopyCsv);
  on('rankToggleAll', () => { rank.showAll = !rank.showAll; renderRank(); });
  screen.querySelectorAll('.rank-row').forEach(row => {
    row.addEventListener('click', () => {
      rank.openRow = rank.openRow === row.dataset.slug ? null : row.dataset.slug;
      renderRank();
    });
  });
  screen.querySelectorAll('.rank-row-actions button').forEach(button => {
    button.addEventListener('click', () => {
      const slug = rank.openRow;
      if (button.dataset.act === 'up') rankNudge(slug, -1);
      else if (button.dataset.act === 'down') rankNudge(slug, 1);
      else rankPlaceAgain(slug);
    });
  });
}
