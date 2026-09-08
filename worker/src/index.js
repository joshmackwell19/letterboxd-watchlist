const REPO = "joshmackwell19/letterboxd-watchlist";

// Very small line-based parser/writer for config/dismissed_recommendations.yaml
// — reads the current committed list back out (rather than trusting client
// state, which only knows about slugs still visible on the page) so two
// dismisses in quick succession don't race and clobber each other's write.
function parseDismissedYaml(yamlText) {
  const slugs = [];
  yamlText.split("\n").forEach((line) => {
    const match = line.match(/^\s*-\s+["']?([\w-]+)["']?\s*$/);
    if (match) slugs.push(match[1]);
  });
  return slugs;
}

function buildDismissedYaml(slugs) {
  const unique = [...new Set(slugs)].sort();
  const lines = ["dismissed:"];
  if (!unique.length) {
    lines.push("  []");
  } else {
    unique.forEach((slug) => lines.push("  - " + JSON.stringify(slug)));
  }
  return lines.join("\n") + "\n";
}

const COUNTRY_CODE_RE = /^[A-Za-z]{2,3}$/;

// Subscription/free_tier *values* were already safe (JSON.stringify quotes
// anything), but a country code was spliced into the YAML document raw and
// unvalidated — a payload with a code containing a newline/colon could
// corrupt config/services.yaml badly enough to crash the daily pipeline's
// yaml.safe_load() until someone manually fixes it in git.
function invalidCountryCode(countries) {
  return Object.keys(countries).find((code) => !COUNTRY_CODE_RE.test(code));
}

function buildYaml(global, countries) {
  const lines = [];
  lines.push("# Managed from the dashboard's Settings page — hand-edits here get");
  lines.push("# overwritten next time someone saves from there.");
  lines.push("global:");
  lines.push("  subscriptions:");
  if (!global.length) {
    lines.push("    []");
  } else {
    global.forEach((s) => lines.push("    - " + JSON.stringify(s)));
  }
  lines.push("");
  lines.push("countries:");
  Object.keys(countries).sort().forEach((code) => {
    const c = countries[code];
    lines.push("  " + code + ":");
    lines.push("    subscriptions:");
    if (!c.subscriptions.length) {
      lines.push("      []");
    } else {
      c.subscriptions.forEach((s) => lines.push("      - " + JSON.stringify(s)));
    }
    lines.push("    free_tier:");
    if (!c.free_tier.length) {
      lines.push("      []");
    } else {
      c.free_tier.forEach((s) => lines.push("      - " + JSON.stringify(s)));
    }
  });
  return lines.join("\n") + "\n";
}

const WATCH_TOGETHER_STATUSES = new Set(["confirmed", "declined"]);
// Keep in step with regenerate-dashboard.yml's own cap-free reality, but
// bound it anyway — GitHub's workflow_dispatch API rejects an oversized
// inputs payload outright, and a review session is realistically never
// going to be this large in one sitting.
const MAX_BATCH_SIZE = 500;

export default {
  async fetch(request, env) {
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "X-Trigger-Secret, Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405, headers: cors });
    }
    if (request.headers.get("X-Trigger-Secret") !== env.TRIGGER_SECRET) {
      return new Response("Forbidden", { status: 403, headers: cors });
    }

    const ghHeaders = {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "letterboxd-watchlist-refresh-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    };

    // Generic workflow_dispatch trigger — `inputs` (if given) becomes the
    // workflow's own `inputs` context (see regenerate-dashboard.yml).
    async function triggerWorkflow(workflowFile, inputs) {
      const body = { ref: "main" };
      if (inputs) body.inputs = inputs;
      return fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${workflowFile}/dispatches`, {
        method: "POST",
        headers: { ...ghHeaders, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    }

    const url = new URL(request.url);

    if (url.pathname === "/update-services") {
      let payload;
      try {
        payload = await request.json();
      } catch {
        return new Response(JSON.stringify({ ok: false, error: "invalid JSON body" }), {
          status: 400,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }

      const badCode = invalidCountryCode(payload.countries || {});
      if (badCode !== undefined) {
        return new Response(JSON.stringify({ ok: false, error: `invalid country code: ${JSON.stringify(badCode)}` }), {
          status: 400,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }

      const getResp = await fetch(`https://api.github.com/repos/${REPO}/contents/config/services.yaml`, {
        headers: ghHeaders,
      });
      if (!getResp.ok) {
        return new Response(JSON.stringify({ ok: false, error: "could not read current services.yaml" }), {
          status: 502,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }
      const current = await getResp.json();

      const yamlText = buildYaml(payload.global || [], payload.countries || {});
      const contentB64 = btoa(unescape(encodeURIComponent(yamlText)));

      const putResp = await fetch(`https://api.github.com/repos/${REPO}/contents/config/services.yaml`, {
        method: "PUT",
        headers: { ...ghHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          message: "Update services.yaml via settings page",
          content: contentB64,
          sha: current.sha,
          branch: "main",
        }),
      });

      if (!putResp.ok) {
        const detail = (await putResp.text()).slice(0, 300);
        return new Response(JSON.stringify({ ok: false, error: "GitHub write failed", status: putResp.status, detail }), {
          status: 502,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }

      // Only ever changes how already-fetched offers are classified/rendered
      // — never needs a fresh scrape, so this regenerates the dashboard
      // instead of re-running the full daily pipeline (see
      // regenerate-dashboard.yml).
      const triggerResp = await triggerWorkflow("regenerate-dashboard.yml");
      return new Response(JSON.stringify({ ok: true, triggered: triggerResp.status === 204 }), {
        status: 200,
        headers: { ...cors, "Content-Type": "application/json" },
      });
    }

    if (url.pathname === "/dismiss-recommendation") {
      let payload;
      try {
        payload = await request.json();
      } catch {
        return new Response(JSON.stringify({ ok: false, error: "invalid JSON body" }), {
          status: 400,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }
      const slug = payload && payload.slug;
      if (!slug || typeof slug !== "string") {
        return new Response(JSON.stringify({ ok: false, error: 'missing "slug"' }), {
          status: 400,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }

      const filePath = "config/dismissed_recommendations.yaml";
      const getResp = await fetch(`https://api.github.com/repos/${REPO}/contents/${filePath}`, {
        headers: ghHeaders,
      });
      if (!getResp.ok) {
        return new Response(JSON.stringify({ ok: false, error: "could not read current dismissed_recommendations.yaml" }), {
          status: 502,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }
      const current = await getResp.json();
      const currentYaml = decodeURIComponent(escape(atob(current.content.replace(/\n/g, ""))));
      const slugs = parseDismissedYaml(currentYaml);

      if (slugs.includes(slug)) {
        return new Response(JSON.stringify({ ok: true, alreadyDismissed: true }), {
          status: 200,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }
      slugs.push(slug);

      const yamlText = buildDismissedYaml(slugs);
      const contentB64 = btoa(unescape(encodeURIComponent(yamlText)));

      const putResp = await fetch(`https://api.github.com/repos/${REPO}/contents/${filePath}`, {
        method: "PUT",
        headers: { ...ghHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          message: `Dismiss recommendation: ${slug}`,
          content: contentB64,
          sha: current.sha,
          branch: "main",
        }),
      });

      if (!putResp.ok) {
        const detail = (await putResp.text()).slice(0, 300);
        return new Response(JSON.stringify({ ok: false, error: "GitHub write failed", status: putResp.status, detail }), {
          status: 502,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }

      const triggerResp2 = await triggerWorkflow("regenerate-dashboard.yml");
      return new Response(JSON.stringify({ ok: true, triggered: triggerResp2.status === 204 }), {
        status: 200,
        headers: { ...cors, "Content-Type": "application/json" },
      });
    }

    if (url.pathname === "/tag-film") {
      // No GitHub Contents API round trip needed here at all — unlike the
      // two endpoints above, the watch-together status lives in Postgres
      // (see db.py's watch_together table), written by main.py, never by
      // this worker directly. This endpoint's only job is to hand the
      // batch off to the workflow that does that write.
      //
      // Takes a BATCH ({decisions: [{slug, status}, ...]}), not a single
      // {slug, status} — the Review tab debounce-batches taps client-side
      // so one review session costs one workflow run, not one per tap (see
      // dashboard.py's queueDecision/flushPendingDecisions). Dispatching a
      // separate run per tap was also silently *losing* decisions: GitHub
      // Actions concurrency only lets one run wait queued at a time, so
      // several rapid dispatches to the same group cancelled all but the
      // last one before their DB write ever executed.
      let payload;
      try {
        payload = await request.json();
      } catch {
        return new Response(JSON.stringify({ ok: false, error: "invalid JSON body" }), {
          status: 400,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }
      const decisions = Array.isArray(payload && payload.decisions) ? payload.decisions : null;
      if (!decisions || !decisions.length) {
        return new Response(JSON.stringify({ ok: false, error: 'missing "decisions" array' }), {
          status: 400,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }
      if (decisions.length > MAX_BATCH_SIZE) {
        return new Response(JSON.stringify({ ok: false, error: `too many decisions in one batch (max ${MAX_BATCH_SIZE})` }), {
          status: 400,
          headers: { ...cors, "Content-Type": "application/json" },
        });
      }
      const validated = [];
      for (const d of decisions) {
        if (!d || typeof d.slug !== "string" || !d.slug || !WATCH_TOGETHER_STATUSES.has(d.status)) {
          return new Response(JSON.stringify({ ok: false, error: `invalid entry: ${JSON.stringify(d)}` }), {
            status: 400,
            headers: { ...cors, "Content-Type": "application/json" },
          });
        }
        validated.push({ slug: d.slug, status: d.status });
      }

      const triggerResp = await triggerWorkflow("regenerate-dashboard.yml", { decisions: JSON.stringify(validated) });
      return new Response(JSON.stringify({ ok: true, triggered: triggerResp.status === 204, count: validated.length }), {
        status: 200,
        headers: { ...cors, "Content-Type": "application/json" },
      });
    }

    // Base route ("Refresh data" button) — this one genuinely does need a
    // fresh scrape, so it's the one case that still targets daily.yml.
    const ghResponse = await triggerWorkflow("daily.yml");
    return new Response(JSON.stringify({ ok: ghResponse.status === 204, status: ghResponse.status }), {
      status: 200,
      headers: { ...cors, "Content-Type": "application/json" },
    });
  },
};
