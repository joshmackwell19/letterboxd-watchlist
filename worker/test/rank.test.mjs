// Tests for the Worker's /rank-state and /rank-save endpoints (the Rank
// tab's ranked list), against a stubbed Neon HTTP endpoint. See
// film-relations.test.mjs for why the source is loaded as a data URL.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SOURCE = fileURLToPath(new URL("../src/index.js", import.meta.url));
const worker = (await import(
  "data:text/javascript," + encodeURIComponent(readFileSync(SOURCE, "utf8"))
)).default;

const DATABASE_URL = "postgresql://u:p@ep-test-123.eu-west-2.aws.neon.tech/db?sslmode=require";
const ENV = { TRIGGER_SECRET: "s", GITHUB_TOKEN: "g", DATABASE_URL };

const post = (path, body) =>
  new Request("https://worker.test" + path, {
    method: "POST",
    headers: { "X-Trigger-Secret": "s", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// answers: one per query, in order (the last repeats).
function stubNeon(answers) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, body });
    const pick = answers.find((a) => body.query.includes(a.match)) || { answer: { rows: [], rowCount: 0 } };
    if (pick.fail) return { ok: false, status: 400, json: async () => ({ message: pick.fail }) };
    return { ok: true, status: 200, json: async () => pick.answer };
  };
  return calls;
}

const ROSTER_ROW = { slug: "blow-out", title: "Blow Out", film_year: 1981, poster_url: null, stars: 5,
  watched_date: "2026-03-01", is_rewatch: false, queue_first: false };

test("the state is the roster plus the newest saved order", async () => {
  const calls = stubNeon([
    { match: "FROM ranking_roster", answer: { rows: [ROSTER_ROW] } },
    { match: "FROM ranking_state", answer: { rows: [{ id: "17", placed: ["blow-out"] }] } },
  ]);
  const body = await (await worker.fetch(post("/rank-state", { year: 2026 }), ENV)).json();

  assert.equal(body.ok, true);
  assert.deepEqual(body.roster, [ROSTER_ROW]);
  assert.equal(body.state_id, 17);
  assert.deepEqual(body.placed, ["blow-out"]);
  assert.equal(calls.length, 2);
  assert.ok(calls.every((c) => c.body.params[0] === 2026));
});

test("before any save the state is empty with id 0", async () => {
  stubNeon([{ match: "FROM ranking_roster", answer: { rows: [ROSTER_ROW] } }]);
  const body = await (await worker.fetch(post("/rank-state", { year: 2026 }), ENV)).json();
  assert.equal(body.state_id, 0);
  assert.deepEqual(body.placed, []);
});

test("a save names the state it started from, and is stored as JSON", async () => {
  const calls = stubNeon([{ match: "INSERT INTO ranking_state", answer: { rows: [{ id: "18" }], rowCount: 1 } }]);
  const response = await worker.fetch(post("/rank-save", { year: 2026, base_id: 17, placed: ["blow-out", "thief"] }), ENV);
  const body = await response.json();

  assert.equal(body.ok, true);
  assert.equal(body.state_id, 18);
  assert.deepEqual(calls[0].body.params, [2026, '["blow-out","thief"]', 17]);
  assert.match(calls[0].body.query, /max\(id\)/);
  // The second statement only trims old saves.
  assert.match(calls[1].body.query, /^DELETE FROM ranking_state/);
});

test("a save over a newer state is refused as a conflict", async () => {
  const calls = stubNeon([{ match: "INSERT INTO ranking_state", answer: { rows: [], rowCount: 0 } }]);
  const response = await worker.fetch(post("/rank-save", { year: 2026, base_id: 3, placed: ["blow-out"] }), ENV);
  assert.equal(response.status, 409);
  assert.equal((await response.json()).conflict, true);
  assert.equal(calls.length, 1);
});

test("anything that isn't a list of distinct film slugs is refused before the database", async () => {
  const calls = stubNeon([]);
  const bad = [
    { year: 2026, base_id: 0, placed: "blow-out" },
    { year: 2026, base_id: 0, placed: ["blow-out", "blow-out"] },
    { year: 2026, base_id: 0, placed: ["Blow Out"] },
    { year: 2026, base_id: 0, placed: ["a'; DROP TABLE films;--"] },
    { year: 2026, base_id: 0, placed: [42] },
    { year: 2026, base_id: -1, placed: [] },
    { year: 2026, placed: [] },
    { year: "2026", base_id: 0, placed: [] },
    { base_id: 0, placed: [] },
  ];
  for (const payload of bad) {
    const response = await worker.fetch(post("/rank-save", payload), ENV);
    assert.equal(response.status, 400, JSON.stringify(payload));
  }
  assert.equal(calls.length, 0);
});

test("a database failure is reported, not swallowed", async () => {
  stubNeon([{ match: "INSERT INTO ranking_state", fail: "relation does not exist" }]);
  const response = await worker.fetch(post("/rank-save", { year: 2026, base_id: 0, placed: [] }), ENV);
  assert.equal(response.status, 502);
  assert.match((await response.json()).error, /relation does not exist/);
});

test("a failed trim doesn't fail the save", async () => {
  stubNeon([
    { match: "INSERT INTO ranking_state", answer: { rows: [{ id: "5" }], rowCount: 1 } },
    { match: "DELETE FROM ranking_state", fail: "timeout" },
  ]);
  const response = await worker.fetch(post("/rank-save", { year: 2026, base_id: 4, placed: ["blow-out"] }), ENV);
  assert.equal(response.status, 200);
});
