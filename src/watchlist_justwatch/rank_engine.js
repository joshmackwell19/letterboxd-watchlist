// The ranking game's logic, with no DOM in it: dashboard.py inlines this file
// into the page, and worker/test/rank-engine.test.mjs runs it under node.
//
// The ranked list is built by binary insertion. `placed` is the films ranked
// so far, best first; every other film on the roster is waiting. Placing a
// film is a run of "which ranks higher?" answers against films already
// placed, each one halving where it can go — about log2(n) taps, which is as
// few as any method can ask for an exact order.
//
// The star rating only decides where a film's search starts: among the
// placed films with the same stars. A film that comes out best (or worst)
// of that stretch is then asked against the film just outside it, and if it
// wins there too the search carries on across the boundary — so a 4★ film
// can end up above a 4½★ one, but only by actually beating it.
const RankEngine = (() => {
  // Where a film with `stars` would sit among `placed` going by stars alone:
  // [lo, hi] is the range of insertion positions. No rating means anywhere.
  function starWindow(stars, placed, starsOf) {
    const n = placed.length;
    if (stars == null) return [0, n];
    let lo = 0;
    while (lo < n && starsOf(placed[lo]) != null && starsOf(placed[lo]) > stars) lo++;
    let hi = n;
    while (hi > 0 && starsOf(placed[hi - 1]) != null && starsOf(placed[hi - 1]) < stars) hi--;
    return lo <= hi ? [lo, hi] : [hi, lo];
  }

  function startSearch(slug, stars, placed, starsOf) {
    const [lo, hi] = starWindow(stars, placed, starsOf);
    return { slug, lo, hi, lo0: lo, hi0: hi, upDone: false, downDone: false, taps: 0 };
  }

  // The index in `placed` of the film to ask about next, or null once the
  // position is known (it is then search.lo).
  function nextOpponent(search, placed) {
    const n = placed.length;
    if (search.lo < search.hi) return Math.floor((search.lo + search.hi) / 2);
    const p = search.lo;
    if (p === search.lo0 && p > 0 && !search.upDone) return p - 1;
    if (p === search.hi0 && p < n && !search.downDone) return p;
    return null;
  }

  // Records the answer against nextOpponent's film; returns the new search.
  function answer(search, placed, won) {
    const n = placed.length;
    const s = { ...search, taps: search.taps + 1 };
    if (s.lo < s.hi) {
      const mid = Math.floor((s.lo + s.hi) / 2);
      if (won) s.hi = mid; else s.lo = mid + 1;
      return s;
    }
    const p = s.lo;
    if (p === s.lo0 && p > 0 && !s.upDone) {
      s.upDone = true;
      if (won) {
        // Better than the film above its star tier: anywhere above that.
        s.lo = 0; s.hi = p - 1; s.lo0 = 0; s.hi0 = -1; s.downDone = true;
      }
      return s;
    }
    if (p === s.hi0 && p < n && !s.downDone) {
      s.downDone = true;
      if (!won) {
        s.lo = p + 1; s.hi = n; s.hi0 = n; s.lo0 = -1; s.upDone = true;
      }
      return s;
    }
    return s;
  }

  function insertAt(placed, slug, index) {
    const next = placed.filter((x) => x !== slug);
    next.splice(Math.min(index, next.length), 0, slug);
    return next;
  }

  // Films still to place, in the order the game offers them: anything newly
  // logged or rewatched first, then the highest rated (so the top of the
  // list is exact long before the rest), unrated ones after the 3½s.
  function queue(roster, placed) {
    const done = new Set(placed);
    const key = (f) => (f.stars == null ? 3.25 : f.stars);
    return roster.filter((f) => !done.has(f.slug)).sort((a, b) =>
      (b.queue_first ? 1 : 0) - (a.queue_first ? 1 : 0)
      || key(b) - key(a)
      || String(a.watched_date || '').localeCompare(String(b.watched_date || ''))
      || a.slug.localeCompare(b.slug));
  }

  // The whole year in order, whether or not every film has been placed: a
  // waiting film goes at the bottom of its star rating, i.e. just above the
  // first placed film rated lower. This is what the export writes.
  function fullOrder(roster, placed) {
    const bySlug = new Map(roster.map((f) => [f.slug, f]));
    const order = placed.filter((slug) => bySlug.has(slug));
    const starsOf = (slug) => bySlug.get(slug).stars;
    const waiting = queue(roster.map((f) => ({ ...f, queue_first: false })), order);
    for (const film of waiting) {
      const stars = film.stars == null ? 3.25 : film.stars;
      let at = order.length;
      for (let i = 0; i < order.length; i++) {
        const s = starsOf(order[i]) == null ? 3.25 : starsOf(order[i]);
        if (s < stars) { at = i; break; }
      }
      order.splice(at, 0, film.slug);
    }
    return order;
  }

  function csvCell(text) {
    const s = text == null ? '' : String(text);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  // Letterboxd's list importer: file order is list order, and the film's own
  // URL matches it exactly (a title and year alone can pick a remake).
  function toCsv(roster, placed) {
    const bySlug = new Map(roster.map((f) => [f.slug, f]));
    const lines = ['Title,Year,LetterboxdURI'];
    for (const slug of fullOrder(roster, placed)) {
      const f = bySlug.get(slug);
      lines.push([csvCell(f.title), f.film_year == null ? '' : f.film_year,
        'https://letterboxd.com/film/' + slug + '/'].join(','));
    }
    return lines.join('\n') + '\n';
  }

  return { starWindow, startSearch, nextOpponent, answer, insertAt, queue, fullOrder, toCsv };
})();

if (typeof module !== 'undefined') module.exports = RankEngine;
