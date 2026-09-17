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

// ---------- Quick search helpers ----------

const TMDB_BASE_URL = "https://api.themoviedb.org/3";
// w154 is the smallest TMDB size that still looks right in the picker list;
// the full-size poster on the card comes from Letterboxd instead.
const TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w154";
const JUSTWATCH_GRAPHQL_URL = "https://apis.justwatch.com/graphql";

// Enough rows to cover the same-title collisions this exists for (TMDB
// knows four films called "Parasite") without turning the picker into a
// list to scroll — and each row costs its own TMDB credits call.
const SEARCH_RESULT_CAP = 6;
const MAX_QUERY_LENGTH = 100;
// A sanity bound on the country list the page sends, not a real limit —
// countries.py currently tracks 124.
const MAX_COUNTRIES = 200;
// Keep in step with countries.py's QUALIFYING_MONETIZATION_TYPES: the
// watchlist only ever counts subscription and free/ad-supported offers, so
// rentals and purchases are filtered out by JustWatch itself rather than
// fetched and discarded here (it cuts the response from ~190KB to ~60KB).
const QUALIFYING_MONETIZATION_TYPES = ["FLATRATE", "ADS", "FREE"];
// Mirrors letterboxd.py's MAX_STARRING.
const MAX_STARRING = 5;

const JSON_LD_OPEN_TAG = '<script type="application/ld+json">';
const ISO_DURATION_RE = /^PT(?:(\d+)H)?(?:(\d+)M)?$/;

function jsonResponse(body, status, cors) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, "Content-Type": "application/json" },
  });
}

function releaseYear(releaseDate) {
  if (!releaseDate || releaseDate.length < 4) return null;
  const year = Number(releaseDate.slice(0, 4));
  return Number.isInteger(year) ? year : null;
}

async function tmdbGet(env, path, params) {
  const target = new URL(TMDB_BASE_URL + path);
  target.searchParams.set("api_key", env.TMDB_API_KEY);
  Object.entries(params || {}).forEach(([key, value]) => target.searchParams.set(key, value));
  const resp = await fetch(target.toString(), { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    // Never include the URL in the message — it carries the API key.
    throw new Error(`TMDB ${path} failed (HTTP ${resp.status})`);
  }
  return resp.json();
}

// schema.org's ISO-8601 duration, e.g. "PT2H13M" -> 133. Mirrors
// letterboxd.py's _parse_duration_minutes.
function parseDurationMinutes(duration) {
  if (!duration) return null;
  const match = ISO_DURATION_RE.exec(duration);
  if (!match) return null;
  const total = Number(match[1] || 0) * 60 + Number(match[2] || 0);
  return total || null;
}

// Sliced out with indexOf rather than matched with a regex: the film page is
// ~330KB and the Workers free plan allows 10ms CPU per invocation, so it's
// worth not scanning the whole document. Mirrors letterboxd.py's
// _film_details_from_json_ld field for field, so a searched film's card data
// is shaped exactly like a watchlist film's.
function parseFilmJsonLd(html) {
  const open = html.indexOf(JSON_LD_OPEN_TAG);
  if (open === -1) return null;
  const start = open + JSON_LD_OPEN_TAG.length;
  const end = html.indexOf("</script>", start);
  if (end === -1) return null;

  let data;
  try {
    data = JSON.parse(
      html.slice(start, end).replace("/* <![CDATA[ */", "").replace("/* ]]> */", "").trim()
    );
  } catch {
    return null;
  }

  const aggregate = data.aggregateRating || {};
  // schema.org allows a single string for genre; Letterboxd emits a list for
  // every film seen so far, but normalize rather than trust that.
  const genre = typeof data.genre === "string" ? [data.genre] : data.genre || [];
  const directors = (data.director || []).map((p) => p.name).filter(Boolean);

  return {
    rating: aggregate.ratingValue != null ? Number(aggregate.ratingValue) : null,
    rating_count: aggregate.ratingCount != null ? Number(aggregate.ratingCount) : null,
    poster_url: data.image || null,
    // Joined, because that's the shape films_by_slug uses and the card reads.
    director: directors.length ? directors.join(", ") : null,
    starring: (data.actor || []).slice(0, MAX_STARRING).map((p) => p.name).filter(Boolean),
    synopsis: data.description || null,
    genre,
    runtime_minutes: parseDurationMinutes(data.duration),
  };
}

// Letterboxd redirects /tmdb/<id>/ straight to the matching /film/<slug>/,
// so one request resolves both the slug and every detail the card needs —
// the same trick letterboxd.py's get_film_details_by_tmdb_id uses.
//
// Fails soft: the card can still be rendered from the TMDB row the picker
// already has, minus the Letterboxd rating. Letterboxd serves its /search/
// paths behind a JS challenge, so it's worth reporting a challenge
// distinctly from an ordinary failure if that ever spreads to film pages.
async function fetchLetterboxdFilm(tmdbId) {
  try {
    const resp = await fetch(`https://letterboxd.com/tmdb/${tmdbId}/`, { redirect: "follow" });
    if (!resp.ok) {
      return { ok: false, status: resp.status, error: `Letterboxd returned HTTP ${resp.status}` };
    }
    const html = await resp.text();
    if (html.includes("Just a moment...") || html.includes("cf-browser-verification")) {
      return { ok: false, status: resp.status, challenged: true, error: "Letterboxd served a bot challenge" };
    }
    const slug = (resp.url.match(/\/film\/([^/]+)\//) || [])[1] || null;
    if (!slug) {
      return { ok: false, status: resp.status, error: "no Letterboxd film page for this TMDB id" };
    }
    const details = parseFilmJsonLd(html);
    if (!details) {
      return { ok: false, status: resp.status, slug, error: "could not read Letterboxd film details" };
    }
    return { ok: true, slug, url: resp.url, ...details };
  } catch (err) {
    return { ok: false, error: String(err).slice(0, 300) };
  }
}

async function justWatchGraphql(operationName, query, variables) {
  const resp = await fetch(JUSTWATCH_GRAPHQL_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operationName, query, variables }),
  });
  if (!resp.ok) throw new Error(`JustWatch GraphQL returned HTTP ${resp.status}`);
  const body = await resp.json();
  if (body.errors) throw new Error(`JustWatch GraphQL error: ${JSON.stringify(body.errors).slice(0, 200)}`);
  return body.data;
}

const JUSTWATCH_SEARCH_QUERY = `
query SearchTitles($filter: TitleFilter!, $country: Country!, $language: Language!, $first: Int!) {
  popularTitles(country: $country, filter: $filter, first: $first) {
    edges { node { id content(country: $country, language: $language) {
      title originalReleaseYear externalIds { tmdbId }
    } } }
  }
}`;

// justwatch_client.search_film picks a match by year alone (exact, then ±1,
// then nearest) because that's all its search response carries. This one can
// do better: JustWatch exposes each title's own TMDB id, and the film was
// picked from TMDB in the first place, so the two can be matched outright
// and the year ladder is only a fallback for titles JustWatch hasn't mapped.
function pickJustWatchMatch(nodes, tmdbId, year) {
  const wanted = String(tmdbId);
  const byTmdbId = nodes.find((n) => n.content.externalIds && n.content.externalIds.tmdbId === wanted);
  if (byTmdbId) return { node: byTmdbId, confidence: "tmdb_exact" };

  if (year != null) {
    const exact = nodes.find((n) => n.content.originalReleaseYear === year);
    if (exact) return { node: exact, confidence: "exact" };
    const tolerant = nodes.find(
      (n) => n.content.originalReleaseYear != null && Math.abs(n.content.originalReleaseYear - year) <= 1
    );
    if (tolerant) return { node: tolerant, confidence: "year_tolerant" };
    const withYear = nodes.filter((n) => n.content.originalReleaseYear != null);
    if (withYear.length) {
      const closest = withYear.reduce((best, n) =>
        Math.abs(n.content.originalReleaseYear - year) < Math.abs(best.content.originalReleaseYear - year) ? n : best
      );
      return { node: closest, confidence: "low_confidence" };
    }
  }

  return nodes.length ? { node: nodes[0], confidence: "low_confidence" } : { node: null, confidence: "unmatched" };
}

function buildOffersQuery(countries) {
  const entries = countries
    .map((code) => `${code}: offers(country: ${code}, platform: WEB, filter: $filter) { ...O }`)
    .join("\n");
  return `query TitleOffers($nodeId: ID!, $filter: OfferFilter!) {
  node(id: $nodeId) { ... on MovieOrShowOrSeason { ${entries} } }
}
fragment O on Offer {
  monetizationType availableToTime standardWebURL package { clearName technicalName }
}`;
}

async function fetchJustWatchOffers(title, year, tmdbId, countries) {
  if (!title) return { matched: false, confidence: "unmatched", offers: [] };

  let match;
  try {
    const data = await justWatchGraphql("SearchTitles", JUSTWATCH_SEARCH_QUERY, {
      filter: { searchQuery: title, objectTypes: ["MOVIE"] },
      country: "GB",
      language: "en",
      first: 10,
    });
    const nodes = ((data.popularTitles || {}).edges || []).map((edge) => edge.node);
    match = pickJustWatchMatch(nodes, tmdbId, year);
  } catch (err) {
    return { matched: false, confidence: "unmatched", offers: [], error: String(err).slice(0, 300) };
  }
  if (!match.node) return { matched: false, confidence: "unmatched", offers: [] };

  let node;
  try {
    const data = await justWatchGraphql("TitleOffers", buildOffersQuery(countries), {
      nodeId: match.node.id,
      filter: { monetizationTypes: QUALIFYING_MONETIZATION_TYPES, bestOnly: false },
    });
    node = data.node || {};
  } catch (err) {
    return {
      matched: true, entry_id: match.node.id, confidence: match.confidence, offers: [],
      error: String(err).slice(0, 300),
    };
  }

  // Deduplicated on (country, service, monetization type) exactly as
  // justwatch_client.fetch_offers does — JustWatch lists the same offer once
  // per presentation type (SD/HD/4K), which collapsed ~300 rows to ~155 for
  // a well-distributed film in testing.
  const seen = new Set();
  const offers = [];
  countries.forEach((country) => {
    const countryOffers = node[country];
    if (!Array.isArray(countryOffers)) return;
    countryOffers.forEach((offer) => {
      const technicalName = offer.package ? offer.package.technicalName : null;
      if (!technicalName) return;
      const key = `${country}|${technicalName}|${offer.monetizationType}`;
      if (seen.has(key)) return;
      seen.add(key);
      offers.push({
        country,
        monetization_type: offer.monetizationType,
        clear_name: offer.package.clearName,
        technical_name: technicalName,
        url: offer.standardWebURL || null,
        // Date only: the dashboard's own daysUntil() appends "T00:00:00",
        // matching how offers are stored for watchlist films.
        available_to: offer.availableToTime ? offer.availableToTime.slice(0, 10) : null,
      });
    });
  });

  return { matched: true, entry_id: match.node.id, confidence: match.confidence, offers };
}

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

    // ---------- Quick search (the dashboard's search box) ----------
    //
    // Two endpoints, deliberately split so the user picks the right film
    // before anything expensive happens: /search-films is a cheap TMDB
    // lookup that fills the picker list, /film-lookup does the real work
    // for the one film chosen. Several films genuinely share a title
    // (TMDB knows four called "Parasite"), so guessing on the user's
    // behalf would be wrong often enough to matter.
    //
    // Neither endpoint classifies anything. They return raw JustWatch
    // offers and let the page turn those into have/free/could_get_again/
    // subscription badges using the taxonomy table dashboard.py emits —
    // brands.py's canonicalization and config.py's service matching stay
    // the single source of truth in Python rather than being reimplemented
    // here, where nothing tests them.
    if (url.pathname === "/search-films" || url.pathname === "/film-lookup") {
      let payload;
      try {
        payload = await request.json();
      } catch {
        return jsonResponse({ ok: false, error: "invalid JSON body" }, 400, cors);
      }

      if (url.pathname === "/search-films") {
        // Only this half needs TMDB — /film-lookup talks to Letterboxd and
        // JustWatch, both of which need no key at all.
        if (!env.TMDB_API_KEY) {
          return jsonResponse(
            { ok: false, error: "TMDB_API_KEY is not configured on this Worker" }, 503, cors);
        }

        const query = typeof payload.query === "string" ? payload.query.trim() : "";
        if (!query) {
          return jsonResponse({ ok: false, error: 'missing "query"' }, 400, cors);
        }
        if (query.length > MAX_QUERY_LENGTH) {
          return jsonResponse(
            { ok: false, error: `query too long (max ${MAX_QUERY_LENGTH} characters)` }, 400, cors);
        }

        let search;
        try {
          search = await tmdbGet(env, "/search/movie", { query, include_adult: "false", language: "en-US" });
        } catch (err) {
          return jsonResponse({ ok: false, error: String(err).slice(0, 300) }, 502, cors);
        }

        const rows = (search.results || []).slice(0, SEARCH_RESULT_CAP);
        // TMDB's search response carries no crew at all, and the director is
        // exactly what tells two same-titled films apart in the picker — so
        // each row gets its own credits call, all in flight at once rather
        // than one after another. A row whose credits call fails still shows
        // (just without a director) — it's a disambiguation aid, not data
        // the card depends on.
        const results = await Promise.all(rows.map(async (movie) => {
          let director = null;
          try {
            const credits = await tmdbGet(env, `/movie/${movie.id}/credits`, {});
            const names = (credits.crew || []).filter((c) => c.job === "Director").map((c) => c.name);
            if (names.length) director = names.join(", ");
          } catch {
            // Leave director null — see above.
          }
          return {
            tmdb_id: movie.id,
            title: movie.title,
            year: releaseYear(movie.release_date),
            director,
            // TMDB's poster, for the picker only. The card itself uses
            // Letterboxd's, so a picked film looks identical to every other
            // card on the dashboard.
            poster_url: movie.poster_path ? TMDB_POSTER_BASE + movie.poster_path : null,
            original_language: movie.original_language || null,
            overview: movie.overview || null,
          };
        }));

        return jsonResponse({ ok: true, results }, 200, cors);
      }

      // ---- /film-lookup ----
      const tmdbId = Number(payload.tmdb_id);
      if (!Number.isInteger(tmdbId) || tmdbId <= 0) {
        return jsonResponse({ ok: false, error: 'missing or invalid "tmdb_id"' }, 400, cors);
      }
      const title = typeof payload.title === "string" ? payload.title.trim() : "";
      const year = Number.isInteger(payload.year) ? payload.year : null;

      // The country list comes from the page (dashboard.py emits it from
      // countries.py's ALL_JUSTWATCH_COUNTRIES) rather than being a second
      // copy maintained here. That list was verified empirically against
      // this same API and is expected to be re-verified in one place when
      // JustWatch's coverage changes.
      const countries = Array.isArray(payload.countries) ? payload.countries : null;
      if (!countries || !countries.length) {
        return jsonResponse({ ok: false, error: 'missing "countries"' }, 400, cors);
      }
      if (countries.length > MAX_COUNTRIES) {
        return jsonResponse({ ok: false, error: `too many countries (max ${MAX_COUNTRIES})` }, 400, cors);
      }
      const badCountry = countries.find((c) => typeof c !== "string" || !/^[A-Z]{2}$/.test(c));
      if (badCountry !== undefined) {
        return jsonResponse(
          { ok: false, error: `invalid country code: ${JSON.stringify(badCountry)}` }, 400, cors);
      }

      // Independent of each other, so both are in flight at once — this is
      // what keeps a lookup at roughly one round trip rather than two.
      const [letterboxd, justwatch] = await Promise.all([
        fetchLetterboxdFilm(tmdbId),
        fetchJustWatchOffers(title, year, tmdbId, countries),
      ]);

      return jsonResponse({ ok: true, tmdb_id: tmdbId, letterboxd, justwatch }, 200, cors);
    }

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
