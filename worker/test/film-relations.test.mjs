// Tests for the Worker's /film-relations endpoint, run with `node --test`.
//
// TMDB is stubbed at global fetch, so these are about this file's own logic:
// which credits become sections, what survives the caps and filters, how the
// two "similar" endpoints interleave, and — the one that actually constrains
// the design — how many subrequests one request makes, against Cloudflare's
// limit of 50.
//
// The source is loaded as a data URL rather than imported by path because
// worker/ has no package.json, so Node would read a .js file as CommonJS and
// refuse its `export default`. Adding one purely for tests would change what
// wrangler sees when it deploys, which isn't a trade worth making.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SOURCE = fileURLToPath(new URL("../src/index.js", import.meta.url));
const worker = (await import(
  "data:text/javascript," + encodeURIComponent(readFileSync(SOURCE, "utf8"))
)).default;

const ENV = { TMDB_API_KEY: "k", TRIGGER_SECRET: "s", GITHUB_TOKEN: "g" };

const post = (path, body, headers = { "X-Trigger-Secret": "s" }) =>
  new Request("https://worker.test" + path, {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

const movie = (id, extra = {}) => ({
  id,
  title: "F" + id,
  release_date: "2000-01-01",
  poster_path: "/p" + id + ".jpg",
  vote_average: 7,
  popularity: 100 - id,
  ...extra,
});

// Returns the list of TMDB paths hit, so a test can assert the budget.
function stubTmdb(routes) {
  const calls = [];
  globalThis.fetch = async (url) => {
    const path = new URL(url).pathname.replace(/^\/3/, "");
    calls.push(path);
    if (!(path in routes)) return { ok: false, status: 404 };
    return { ok: true, json: async () => routes[path] };
  };
  return calls;
}

const relations = (tmdbId) => worker.fetch(post("/film-relations", { tmdb_id: tmdbId }), ENV);

test("one request is at most eight subrequests, two round trips deep", async () => {
  const calls = stubTmdb({
    "/movie/500/credits": {
      crew: [
        { id: 91, name: "Dir One", job: "Director" },
        { id: 92, name: "Dir Two", job: "Director" },
        { id: 93, name: "Dir Three", job: "Director" },
        { id: 94, name: "A Writer", job: "Writer" },
      ],
      cast: [
        { id: 81, name: "Second Billed", order: 1 },
        { id: 80, name: "Lead", order: 0 },
        { id: 82, name: "Third", order: 2 },
        { id: 83, name: "Fourth", order: 3 },
      ],
    },
    "/movie/500/similar": { results: [] },
    "/movie/500/recommendations": { results: [] },
    "/person/91/movie_credits": { crew: [], cast: [] },
    "/person/92/movie_credits": { crew: [], cast: [] },
    "/person/80/movie_credits": { crew: [], cast: [] },
    "/person/81/movie_credits": { crew: [], cast: [] },
    "/person/82/movie_credits": { crew: [], cast: [] },
  });
  const body = await (await relations(500)).json();

  assert.equal(calls.length, 8);
  assert.deepEqual(
    body.people.filter((p) => p.role === "director").map((p) => p.name),
    ["Dir One", "Dir Two"],
  );
  // Billing order, not the order TMDB happened to return them in.
  assert.deepEqual(
    body.people.filter((p) => p.role === "cast").map((p) => p.name),
    ["Lead", "Second Billed", "Third"],
  );
});

test("similar interleaves recommendations with similar and drops duplicates", async () => {
  stubTmdb({
    "/movie/500/credits": { crew: [], cast: [] },
    "/movie/500/similar": { results: [movie(1), movie(2), movie(3)] },
    "/movie/500/recommendations": { results: [movie(4), movie(2), movie(5)] },
  });
  const body = await (await relations(500)).json();

  assert.deepEqual(body.similar.map((r) => r.tmdb_id), [4, 1, 2, 5, 3]);
  assert.equal(body.similar_unavailable, false);
});

test("rows are trimmed to what a poster tile draws", async () => {
  stubTmdb({
    "/movie/500/credits": { crew: [], cast: [] },
    "/movie/500/similar": { results: [movie(1, { overview: "long text" })] },
    "/movie/500/recommendations": { results: [] },
  });
  const body = await (await relations(500)).json();

  assert.deepEqual(Object.keys(body.similar[0]).sort(), [
    "popularity", "poster_url", "title", "tmdb_id", "tmdb_rating", "year",
  ]);
});

test("the film never appears among its own relations", async () => {
  stubTmdb({
    "/movie/7/credits": { crew: [{ id: 91, name: "Dir", job: "Director" }], cast: [] },
    "/movie/7/similar": { results: [movie(7), movie(8)] },
    "/movie/7/recommendations": { results: [] },
    "/person/91/movie_credits": { crew: [{ ...movie(7), job: "Director" }], cast: [] },
  });
  const body = await (await relations(7)).json();

  assert.deepEqual(body.similar.map((r) => r.tmdb_id), [8]);
  assert.deepEqual(body.people[0].films, []);
});

test("a director's filmography is their directing credits, deduped", async () => {
  stubTmdb({
    "/movie/1/credits": { crew: [{ id: 91, name: "Dir", job: "Director" }], cast: [] },
    "/movie/1/similar": { results: [] },
    "/movie/1/recommendations": { results: [] },
    "/person/91/movie_credits": {
      crew: [
        { ...movie(11), job: "Director" },
        // TMDB credits the same person twice on one film often enough to matter.
        { ...movie(11), job: "Director" },
        { ...movie(12), job: "Writer" },
      ],
      cast: [{ ...movie(13), order: 0, vote_count: 900 }],
    },
  });
  const body = await (await relations(1)).json();

  assert.deepEqual(body.people[0].films.map((f) => f.tmdb_id), [11]);
});

test("an actor's filmography keeps billed roles with an audience", async () => {
  stubTmdb({
    "/movie/1/credits": { crew: [], cast: [{ id: 80, name: "Lead", order: 0 }] },
    "/movie/1/similar": { results: [] },
    "/movie/1/recommendations": { results: [] },
    "/person/80/movie_credits": {
      crew: [],
      cast: [
        { ...movie(20), order: 0, vote_count: 500 },
        { ...movie(21), order: 40, vote_count: 500 },   // a walk-on
        { ...movie(22), order: 1, vote_count: 3 },      // nobody has seen it
        { ...movie(23), order: 1, vote_count: 500, poster_path: null },
        { ...movie(24), order: 1, vote_count: 500, release_date: "" },  // unreleased
      ],
    },
  });
  const body = await (await relations(1)).json();

  assert.deepEqual(body.people[0].films.map((f) => f.tmdb_id), [20]);
});

test("a filmography is kept most popular first, so its cap keeps what's worth showing", async () => {
  stubTmdb({
    "/movie/1/credits": { crew: [{ id: 91, name: "Dir", job: "Director" }], cast: [] },
    "/movie/1/similar": { results: [] },
    "/movie/1/recommendations": { results: [] },
    "/person/91/movie_credits": {
      crew: [
        { ...movie(11, { popularity: 1 }), job: "Director" },
        { ...movie(12, { popularity: 99 }), job: "Director" },
        { ...movie(13, { popularity: 50 }), job: "Director" },
      ],
      cast: [],
    },
  });
  const body = await (await relations(1)).json();

  assert.deepEqual(body.people[0].films.map((f) => f.tmdb_id), [12, 13, 11]);
});

test("a person whose filmography failed is dropped, never sent as an empty section", async () => {
  stubTmdb({
    "/movie/10/credits": {
      crew: [{ id: 91, name: "Works", job: "Director" }, { id: 99, name: "Broken", job: "Director" }],
      cast: [],
    },
    "/movie/10/similar": { results: [] },
    "/movie/10/recommendations": { results: [] },
    "/person/91/movie_credits": { crew: [{ ...movie(11), job: "Director" }], cast: [] },
  });
  const body = await (await relations(10)).json();

  assert.deepEqual(body.people.map((p) => p.name), ["Works"]);
});

test("both similar calls failing is said, not shown as 'nothing similar'", async () => {
  stubTmdb({ "/movie/9/credits": { crew: [], cast: [] } });
  const body = await (await relations(9)).json();

  assert.equal(body.similar_unavailable, true);
  assert.deepEqual(body.similar, []);
});

test("a failed credits call is a 502, not a half-answer", async () => {
  stubTmdb({});
  const res = await relations(11);

  assert.equal(res.status, 502);
  assert.equal((await res.json()).ok, false);
});

test("a bad or missing tmdb_id is rejected before anything is fetched", async () => {
  const calls = stubTmdb({});
  for (const payload of [{}, { tmdb_id: "abc" }, { tmdb_id: -3 }, { tmdb_id: 1.5 }]) {
    const res = await worker.fetch(post("/film-relations", payload), ENV);
    assert.equal(res.status, 400, JSON.stringify(payload));
  }
  assert.equal(calls.length, 0);
});

test("a missing TMDB key is said plainly rather than failing as a lookup", async () => {
  stubTmdb({});
  const res = await worker.fetch(post("/film-relations", { tmdb_id: 1 }), { ...ENV, TMDB_API_KEY: "" });

  assert.equal(res.status, 503);
});

test("the endpoint is still behind the trigger secret, and near-miss paths still 404", async () => {
  stubTmdb({});
  assert.equal((await worker.fetch(post("/film-relations", { tmdb_id: 1 }, {}), ENV)).status, 403);
  assert.equal((await worker.fetch(post("/film-relation", { tmdb_id: 1 }), ENV)).status, 404);
});
