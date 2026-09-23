// Tests for the Worker's /person endpoint (the director/actor profile page).
// See film-relations.test.mjs for why the source is loaded as a data URL.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SOURCE = fileURLToPath(new URL("../src/index.js", import.meta.url));
const worker = (await import(
  "data:text/javascript," + encodeURIComponent(readFileSync(SOURCE, "utf8"))
)).default;

const ENV = { TMDB_API_KEY: "k", TRIGGER_SECRET: "s", GITHUB_TOKEN: "g" };

const post = (body, headers = { "X-Trigger-Secret": "s" }) =>
  new Request("https://worker.test/person", {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

const credit = (id, date, extra = {}) => ({
  id,
  title: "F" + id,
  release_date: date,
  poster_path: "/p" + id + ".jpg",
  vote_average: 7,
  popularity: 10,
  vote_count: 500,
  ...extra,
});

function stubTmdb(routes) {
  const calls = [];
  globalThis.fetch = async (url) => {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/^\/3/, "");
    calls.push({ path, params: Object.fromEntries(parsed.searchParams) });
    if (!(path in routes)) return { ok: false, status: 404 };
    return { ok: true, json: async () => routes[path] };
  };
  return calls;
}

const PERSON = {
  id: 91,
  name: "Norman Jewison",
  biography: "A director.",
  birthday: "1926-07-21",
  deathday: "2024-01-20",
  place_of_birth: "Toronto, Canada",
  known_for_department: "Directing",
  profile_path: "/face.jpg",
  movie_credits: {
    crew: [
      { ...credit(11, "1987-01-01"), job: "Director" },
      { ...credit(12, "1999-01-01"), job: "Director" },
      { ...credit(12, "1999-01-01"), job: "Director" },   // TMDB double-credits
      { ...credit(13, "1975-01-01"), job: "Writer" },
    ],
    cast: [
      credit(20, "1990-01-01"),
      credit(21, "1991-01-01", { vote_count: 2 }),        // nobody has seen it
      credit(22, "1992-01-01", { poster_path: null }),
      credit(23, "", {}),                                  // unreleased
    ],
  },
};

test("a known id is one subrequest, with credits folded into it", async () => {
  const calls = stubTmdb({ "/person/91": PERSON });
  const body = await (await worker.fetch(post({ person_id: 91 }), ENV)).json();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].params.append_to_response, "movie_credits");
  assert.equal(body.person.name, "Norman Jewison");
  assert.equal(body.person.profile_url, "https://image.tmdb.org/t/p/w300/face.jpg");
  assert.equal(body.person.place_of_birth, "Toronto, Canada");
  assert.equal(body.person.known_for_department, "Directing");
});

test("directing credits are the Director ones, deduped, newest first", async () => {
  stubTmdb({ "/person/91": PERSON });
  const body = await (await worker.fetch(post({ person_id: 91 }), ENV)).json();

  assert.deepEqual(body.directed.map((f) => f.tmdb_id), [12, 11]);
});

test("acting credits drop the unseen, the posterless and the unreleased", async () => {
  stubTmdb({ "/person/91": PERSON });
  const body = await (await worker.fetch(post({ person_id: 91 }), ENV)).json();

  assert.deepEqual(body.acted.map((f) => f.tmdb_id), [20]);
});

test("a name is resolved to an id first, then read the same way", async () => {
  const calls = stubTmdb({
    "/search/person": { results: [{ id: 91, name: "Norman Jewison" }, { id: 92, name: "Someone Else" }] },
    "/person/91": PERSON,
  });
  const body = await (await worker.fetch(post({ name: "Norman Jewison" }), ENV)).json();

  assert.deepEqual(calls.map((c) => c.path), ["/search/person", "/person/91"]);
  assert.equal(body.person.tmdb_id, 91);
});

test("a name TMDB doesn't know is a 404, not an empty profile", async () => {
  stubTmdb({ "/search/person": { results: [] } });
  const res = await worker.fetch(post({ name: "Nobody At All" }), ENV);

  assert.equal(res.status, 404);
  assert.equal((await res.json()).ok, false);
});

test("neither an id nor a name is rejected before anything is fetched", async () => {
  const calls = stubTmdb({});
  for (const payload of [{}, { person_id: "abc" }, { person_id: -1 }, { name: "   " }]) {
    const res = await worker.fetch(post(payload), ENV);
    assert.equal(res.status, 400, JSON.stringify(payload));
  }
  assert.equal(calls.length, 0);
});

test("an over-long name is rejected rather than sent to TMDB", async () => {
  const calls = stubTmdb({});
  const res = await worker.fetch(post({ name: "x".repeat(101) }), ENV);

  assert.equal(res.status, 400);
  assert.equal(calls.length, 0);
});

test("a TMDB failure is a 502, and the key is never in the message", async () => {
  stubTmdb({});
  const res = await worker.fetch(post({ person_id: 91 }), ENV);
  const body = await res.json();

  assert.equal(res.status, 502);
  assert.ok(!body.error.includes("api_key"));
});

test("a missing TMDB key is said plainly", async () => {
  stubTmdb({});
  assert.equal((await worker.fetch(post({ person_id: 1 }), { ...ENV, TMDB_API_KEY: "" })).status, 503);
});

test("the endpoint is behind the trigger secret", async () => {
  stubTmdb({});
  assert.equal((await worker.fetch(post({ person_id: 1 }, {}), ENV)).status, 403);
});
