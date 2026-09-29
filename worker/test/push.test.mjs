// Tests for the Worker's /push-subscribe and /push-unsubscribe endpoints,
// against a stubbed Neon HTTP endpoint. See film-relations.test.mjs for why
// the source is loaded as a data URL.

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

const SUBSCRIPTION = {
  endpoint: "https://web.push.apple.com/QGuQyavXutnMs",
  keys: { p256dh: "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM", auth: "tBHItJI5svbpez7KI4CCXg" },
};

function stubNeon(answer = { rowCount: 1 }, ok = true) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    return { ok, status: ok ? 200 : 400, json: async () => answer };
  };
  return calls;
}

test("a subscription is stored with one parameterised statement", async () => {
  const calls = stubNeon();
  const body = await (await worker.fetch(post("/push-subscribe", SUBSCRIPTION), ENV)).json();

  assert.equal(body.ok, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://ep-test-123.eu-west-2.aws.neon.tech/sql");
  assert.equal(calls[0].headers["Neon-Connection-String"], DATABASE_URL);
  assert.deepEqual(calls[0].body.params.slice(0, 3),
    [SUBSCRIPTION.endpoint, SUBSCRIPTION.keys.p256dh, SUBSCRIPTION.keys.auth]);
});

test("an endpoint that isn't a push service is refused before the database", async () => {
  const calls = stubNeon();
  for (const endpoint of ["https://evil.test/x", "http://web.push.apple.com/x", "https://web.push.apple.com.evil.test/x"]) {
    const response = await worker.fetch(post("/push-subscribe", { ...SUBSCRIPTION, endpoint }), ENV);
    assert.equal(response.status, 400, endpoint);
  }
  assert.equal(calls.length, 0);
});

test("malformed keys are refused", async () => {
  stubNeon();
  const response = await worker.fetch(
    post("/push-subscribe", { ...SUBSCRIPTION, keys: { p256dh: "not base64!", auth: "x" } }), ENV);
  assert.equal(response.status, 400);
});

test("nothing inserted means the cap was hit", async () => {
  stubNeon({ rowCount: 0 });
  const response = await worker.fetch(post("/push-subscribe", SUBSCRIPTION), ENV);
  assert.equal(response.status, 409);
});

test("a database error is reported, not swallowed", async () => {
  stubNeon({ message: 'relation "push_subscriptions" does not exist' }, false);
  const response = await worker.fetch(post("/push-subscribe", SUBSCRIPTION), ENV);
  const body = await response.json();
  assert.equal(response.status, 502);
  assert.match(body.error, /does not exist/);
});

test("unsubscribing deletes by endpoint", async () => {
  const calls = stubNeon();
  const body = await (await worker.fetch(post("/push-unsubscribe", { endpoint: SUBSCRIPTION.endpoint }), ENV)).json();
  assert.equal(body.ok, true);
  assert.match(calls[0].body.query, /^DELETE FROM push_subscriptions/);
  assert.deepEqual(calls[0].body.params, [SUBSCRIPTION.endpoint]);
});
