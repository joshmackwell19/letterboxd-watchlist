// Tests for the ranking game's logic (src/watchlist_justwatch/rank_engine.js,
// the file dashboard.py inlines into the page). It lives with the Worker's
// tests because this is where the repo's node tests run from.

import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const E = require("../../src/watchlist_justwatch/rank_engine.js");

// Places every film on the roster by answering from a hidden true order
// (lower `truth` is better). Returns the placed list and the taps it took.
function playOut(roster, placed = []) {
  const bySlug = new Map(roster.map((f) => [f.slug, f]));
  const starsOf = (slug) => bySlug.get(slug).stars;
  let taps = 0;
  for (;;) {
    const next = E.queue(roster, placed)[0];
    if (!next) break;
    let search = E.startSearch(next.slug, next.stars, placed, starsOf);
    for (let i = E.nextOpponent(search, placed); i !== null; i = E.nextOpponent(search, placed)) {
      search = E.answer(search, placed, next.truth < bySlug.get(placed[i]).truth);
      taps++;
    }
    placed = E.insertAt(placed, next.slug, search.lo);
  }
  return { placed, taps };
}

function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
}

function makeRoster(count, seed, { crossTier }) {
  const r = rng(seed);
  const tiers = [5, 4.5, 4, 3.5, 3, 2.5, 2];
  return Array.from({ length: count }, (_, i) => {
    const stars = tiers[Math.floor(r() * tiers.length)];
    // truth: lower is better. Without crossTier it never contradicts stars.
    const truth = -stars + (crossTier ? (r() - 0.5) * 0.9 : (r() - 0.5) * 0.4);
    return { slug: "film-" + i, title: "Film " + i, film_year: 2000, stars, truth, watched_date: "2026-01-01" };
  });
}

test("a year whose order agrees with its stars comes out exactly right", () => {
  const roster = makeRoster(120, 7, { crossTier: false });
  const { placed } = playOut(roster);
  const expected = [...roster].sort((a, b) => a.truth - b.truth).map((f) => f.slug);
  assert.deepEqual(placed, expected);
});

test("a film can cross into another star tier, and the order is still exact", () => {
  for (const seed of [1, 2, 3, 4, 5]) {
    const roster = makeRoster(150, seed, { crossTier: true });
    const { placed } = playOut(roster);
    const expected = [...roster].sort((a, b) => a.truth - b.truth).map((f) => f.slug);
    assert.deepEqual(placed, expected, "seed " + seed);
  }
});

test("placing takes about log2(n) taps a film, not a pass over the list", () => {
  const roster = makeRoster(311, 11, { crossTier: false });
  const { taps } = playOut(roster);
  // 311 films in seven tiers: an exact order needs ~1,500 answers at the very
  // least; a comparison against every neighbour would be tens of thousands.
  assert.ok(taps < 2200, "took " + taps);
});

test("an unrated film is searched for across the whole list", () => {
  const placed = ["a", "b", "c", "d"];
  const stars = { a: 5, b: 4, c: 4, d: 3 };
  const search = E.startSearch("x", null, placed, (s) => stars[s]);
  assert.deepEqual([search.lo, search.hi], [0, 4]);
});

test("the queue puts new and rewatched films first, then the highest rated", () => {
  const roster = [
    { slug: "three", stars: 3, watched_date: "2026-01-01" },
    { slug: "five", stars: 5, watched_date: "2026-02-01" },
    { slug: "new", stars: 2, watched_date: "2026-11-09", queue_first: true },
    { slug: "placed", stars: 5, watched_date: "2026-01-01" },
  ];
  assert.deepEqual(E.queue(roster, ["placed"]).map((f) => f.slug), ["new", "five", "three"]);
});

test("the full order slots waiting films in at the bottom of their stars", () => {
  const roster = [
    { slug: "a", title: "A", film_year: 2001, stars: 5 },
    { slug: "b", title: "B", film_year: 2002, stars: 4 },
    { slug: "c", title: "C", film_year: 2003, stars: 3 },
    { slug: "wait-45", title: "W", film_year: 2004, stars: 4.5 },
    { slug: "wait-3", title: "V", film_year: 2005, stars: 3 },
  ];
  assert.deepEqual(E.fullOrder(roster, ["a", "b", "c"]), ["a", "wait-45", "b", "c", "wait-3"]);
});

test("a slug no longer on the roster is left out of the order", () => {
  const roster = [{ slug: "a", title: "A", film_year: 2001, stars: 5 }];
  assert.deepEqual(E.fullOrder(roster, ["gone", "a"]), ["a"]);
});

test("the CSV is in list order, quoted where a title needs it", () => {
  const roster = [
    { slug: "when-harry-met-sally", title: "When Harry Met Sally...", film_year: 1989, stars: 5 },
    { slug: "me-myself-irene", title: 'Me, Myself & "Irene"', film_year: 2000, stars: 4 },
  ];
  assert.equal(E.toCsv(roster, ["when-harry-met-sally", "me-myself-irene"]),
    "Title,Year,LetterboxdURI\n" +
    "When Harry Met Sally...,1989,https://letterboxd.com/film/when-harry-met-sally/\n" +
    '"Me, Myself & ""Irene""",2000,https://letterboxd.com/film/me-myself-irene/\n');
});
