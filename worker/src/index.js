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
// Poster *tiles* are a different job: measured, a grid tile is 345-387
// device pixels wide on a phone, so w154 arrives at well under half the
// resolution it's drawn at and looks soft next to the Letterboxd posters
// beside it. w342 is the nearest size that covers it (+24KB per poster,
// and only for the untracked films in a relation grid).
const TMDB_TILE_POSTER_BASE = "https://image.tmdb.org/t/p/w342";
const JUSTWATCH_GRAPHQL_URL = "https://apis.justwatch.com/graphql";
const JUSTWATCH_RETRY_DELAY_MS = 600;

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

// ---------- Film relations (the film detail page's live layer) ----------
//
// The dashboard already builds "more by this director" and "similar films"
// from the ~500 films it ships, which only ever answers "of the ones you
// track". This is TMDB's own answer to the same questions.
//
// Budget: one credits call, then the rest in parallel — 2 for similar +
// recommendations, at most 2 director filmographies and 3 cast ones. Eight
// subrequests worst case, against Cloudflare's limit of 50.
const RELATIONS_DIRECTOR_CAP = 2;
// Matches ACTOR_SECTIONS_CAP on the page: more than three actor sections
// and the page stops being about the film.
const RELATIONS_CAST_CAP = 3;
const RELATIONS_FILMS_PER_PERSON = 40;
const RELATIONS_SIMILAR_CAP = 24;
// An actor's movie_credits runs to hundreds of entries, most of them one-
// scene parts and voice work. "order" is TMDB's own billing position, so
// this keeps the roles the film was actually sold on. A director's crew
// credits need no such filter — their filmography is the point.
const RELATIONS_MAX_BILLING_ORDER = 10;
// Enough to drop unreleased stubs and things with no audience at all,
// low enough to keep genuinely obscure films.
const RELATIONS_MIN_VOTES = 20;

// ---------- Person profile ----------
//
// Everything about one person in a single TMDB call: append_to_response
// folds movie_credits into the details response, so the whole page is one
// subrequest when the id is already known and two when it has to be found
// by name (a director credited on a Letterboxd page the relations call
// hasn't covered).
const PERSON_PROFILE_IMAGE_BASE = "https://image.tmdb.org/t/p/w300";
// A full filmography, not the per-section cut a film page shows — this
// page is the place to see all of it. Still bounded: a prolific character
// actor runs to several hundred credits, most of them uncredited walk-ons.
const PERSON_FILMS_CAP = 150;

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
    // Letterboxd's own title for the film that TMDB id actually resolved to
    // — the caller supplied a title too, and the two disagreeing means they
    // aren't talking about the same film (see the cross-check in
    // /film-lookup).
    title: data.name || null,
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

// One retry, for the same reason justwatch_client._with_retry exists: this
// API rate-limits (a burst of lookups earns a 429) and times out
// occasionally, and here a failed call would otherwise surface as a film
// with no availability — indistinguishable to a reader from one that
// genuinely isn't streaming anywhere. Just the one, and a short wait:
// someone is watching a spinner, so a Python-style five-attempt backoff
// would be worse than admitting defeat. Waiting is I/O, not CPU, so it
// doesn't count against the Workers CPU budget.
async function justWatchGraphql(operationName, query, variables) {
  let lastError;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt) await new Promise((resolve) => setTimeout(resolve, JUSTWATCH_RETRY_DELAY_MS));
    try {
      const resp = await fetch(JUSTWATCH_GRAPHQL_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operationName, query, variables }),
      });
      if (!resp.ok) throw new Error(`JustWatch GraphQL returned HTTP ${resp.status}`);
      const body = await resp.json();
      if (body.errors) throw new Error(`JustWatch GraphQL error: ${JSON.stringify(body.errors).slice(0, 200)}`);
      return body.data;
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError;
}

const JUSTWATCH_SEARCH_QUERY = `
query SearchTitles($filter: TitleFilter!, $country: Country!, $language: Language!, $first: Int!) {
  popularTitles(country: $country, filter: $filter, first: $first) {
    edges { node { id content(country: $country, language: $language) {
      title originalReleaseYear externalIds { tmdbId }
    } } }
  }
}`;

function normalizeTitle(title) {
  return (title || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

// justwatch_client.search_film picks a match by year alone (exact, then ±1,
// then nearest year, then whatever came back first) because that's all its
// search response carries. This one can do better, and has to.
//
// Better: JustWatch exposes each title's own TMDB id and the film was picked
// from TMDB in the first place, so the two match outright.
//
// Has to: search_film's last two rungs answer with SOME film whenever the
// API answers at all, which is a reasonable bet for a watchlist film (it
// genuinely exists and JustWatch almost certainly has it) and a bad one
// here. A search for a film JustWatch doesn't carry was matched to "PAW
// Patrol: The Movie" in testing and would have shown its offers on the
// card. Claiming a film is on Paramount+ when it isn't is worse than
// saying it isn't available anywhere tracked, so an unconvincing match is
// no match: title and year both have to agree when the TMDB ids don't.
function pickJustWatchMatch(nodes, tmdbId, title, year) {
  const wantedId = String(tmdbId);
  const byTmdbId = nodes.find((n) => n.content.externalIds && n.content.externalIds.tmdbId === wantedId);
  if (byTmdbId) return { node: byTmdbId, confidence: "tmdb_exact" };

  // Only reached for a title JustWatch hasn't mapped to a TMDB id.
  const wantedTitle = normalizeTitle(title);
  const sameTitle = nodes.filter((n) => normalizeTitle(n.content.title) === wantedTitle);
  if (!sameTitle.length) return { node: null, confidence: "unmatched" };

  if (year == null) {
    // No year to check against (TMDB had no release date) — an exact title
    // match on its own is as much confidence as is available.
    return { node: sameTitle[0], confidence: "title_only" };
  }
  const exact = sameTitle.find((n) => n.content.originalReleaseYear === year);
  if (exact) return { node: exact, confidence: "title_year_exact" };
  // ±1 covers festival-vs-release-year disagreements, the same tolerance
  // justwatch_client.search_film allows.
  const tolerant = sameTitle.find(
    (n) => n.content.originalReleaseYear != null && Math.abs(n.content.originalReleaseYear - year) <= 1
  );
  if (tolerant) return { node: tolerant, confidence: "title_year_tolerant" };

  return { node: null, confidence: "unmatched" };
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

// Returns { matched, confidence, offers, error? }. The error field is the
// difference between "JustWatch has nothing for this film" and "JustWatch
// couldn't be asked" — an empty offers list means the former only when no
// error rides along, and the page has to say so differently in each case.
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
    match = pickJustWatchMatch(nodes, tmdbId, title, year);
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

// One TMDB movie, trimmed to what a poster tile needs. The page renders
// these itself, so anything it doesn't draw is weight on every response.
function relationRow(movie) {
  return {
    tmdb_id: movie.id,
    title: movie.title || movie.original_title || "",
    year: releaseYear(movie.release_date),
    poster_url: movie.poster_path ? TMDB_TILE_POSTER_BASE + movie.poster_path : null,
    tmdb_rating: typeof movie.vote_average === "number" ? movie.vote_average : null,
    popularity: typeof movie.popularity === "number" ? movie.popularity : 0,
  };
}

// A filmography reads as a career, so it's ordered by date; undated entries
// are already filtered out by usableRelation before this sees them.
function byNewestFirst(movies, cap) {
  return movies
    .slice()
    .sort((a, b) => String(b.release_date).localeCompare(String(a.release_date)))
    .slice(0, cap)
    .map(relationRow);
}

// Most popular first, so the per-person cap keeps the films worth showing
// rather than an arbitrary forty. The page re-sorts for display; this only
// decides what survives the cut.
function sortedRelationRows(movies, cap) {
  return movies
    .map(relationRow)
    .sort((a, b) => b.popularity - a.popularity)
    .slice(0, cap);
}

function usableRelation(movie, sourceTmdbId) {
  return (
    movie &&
    movie.id !== sourceTmdbId &&
    Boolean(movie.release_date) &&
    Boolean(movie.poster_path)
  );
}

// A person's filmography, as either the films they directed or the films
// they were billed in. A failure here costs that one section, not the
// whole response — the same best-effort stance the search picker's
// per-row credits call takes.
async function personFilms(env, personId, role, sourceTmdbId) {
  let credits;
  try {
    credits = await tmdbGet(env, `/person/${personId}/movie_credits`, { language: "en-US" });
  } catch {
    return null;
  }
  const entries =
    role === "director"
      ? (credits.crew || []).filter((c) => c.job === "Director")
      : (credits.cast || []).filter(
          (c) =>
            typeof c.order === "number" &&
            c.order <= RELATIONS_MAX_BILLING_ORDER &&
            (c.vote_count || 0) >= RELATIONS_MIN_VOTES
        );

  // A director credited twice on one film (TMDB does this) would otherwise
  // appear twice in their own filmography.
  const seen = new Set();
  const unique = [];
  entries.forEach((movie) => {
    if (!usableRelation(movie, sourceTmdbId) || seen.has(movie.id)) return;
    seen.add(movie.id);
    unique.push(movie);
  });
  return sortedRelationRows(unique, RELATIONS_FILMS_PER_PERSON);
}

// TMDB's own id for a name, when the caller only has the name — the
// relations payload carries ids, but a director read off a Letterboxd page
// before (or without) that call has only what Letterboxd printed.
async function findPersonId(env, name) {
  const search = await tmdbGet(env, "/search/person", { query: name, include_adult: "false" });
  const results = search.results || [];
  if (!results.length) return null;
  // TMDB orders by its own popularity, which is the right tie-break for two
  // people sharing a name: the one a film page means is almost always the
  // one with the credits.
  return results[0].id;
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

      // Running those two in parallel means the JustWatch half is searched
      // by the title the caller sent rather than the one the TMDB id really
      // resolves to, and a caller that sends a title and an id belonging to
      // different films would get one film's details next to another film's
      // offers. A tmdb_exact match can't drift that way — it is anchored to
      // the same id the Letterboxd page came from — but a title-based match
      // is only ever as good as the title it was handed, so it has to agree
      // with what Letterboxd resolved. Dropping the offers (rather than
      // trusting them) keeps the response about one film, which is the same
      // call pickJustWatchMatch makes about weak matches.
      let offers = justwatch;
      if (
        letterboxd.ok && letterboxd.title &&
        justwatch.matched && justwatch.confidence !== "tmdb_exact" &&
        normalizeTitle(letterboxd.title) !== normalizeTitle(title)
      ) {
        offers = {
          matched: false,
          confidence: "unmatched",
          offers: [],
          error: `title mismatch: TMDB id ${tmdbId} is "${letterboxd.title}" on Letterboxd, not "${title}"`,
        };
      }

      return jsonResponse({ ok: true, tmdb_id: tmdbId, letterboxd, justwatch: offers }, 200, cors);
    }

    // ---------- /film-relations (the film detail page's live layer) ----------
    //
    // Deliberately no JustWatch: the page already knows the availability of
    // every film it tracks, and asking for the rest would be a hundred
    // lookups for posters most of which are never tapped. A film the page
    // doesn't have goes through /film-lookup when it's actually opened,
    // which is the same one-film-at-a-time path quick search uses.
    if (url.pathname === "/film-relations") {
      if (!env.TMDB_API_KEY) {
        return jsonResponse(
          { ok: false, error: "TMDB_API_KEY is not configured on this Worker" }, 503, cors);
      }

      let payload;
      try {
        payload = await request.json();
      } catch {
        return jsonResponse({ ok: false, error: "invalid JSON body" }, 400, cors);
      }

      const sourceTmdbId = Number(payload.tmdb_id);
      if (!Number.isInteger(sourceTmdbId) || sourceTmdbId <= 0) {
        return jsonResponse({ ok: false, error: 'missing or invalid "tmdb_id"' }, 400, cors);
      }

      // The people have to be known before their filmographies can be
      // asked for, so this one call is the only sequential step.
      let credits;
      try {
        credits = await tmdbGet(env, `/movie/${sourceTmdbId}/credits`, { language: "en-US" });
      } catch (err) {
        return jsonResponse({ ok: false, error: String(err).slice(0, 300) }, 502, cors);
      }

      const directors = (credits.crew || [])
        .filter((c) => c.job === "Director")
        .slice(0, RELATIONS_DIRECTOR_CAP);
      const cast = (credits.cast || [])
        .slice()
        .sort((a, b) => (a.order ?? 999) - (b.order ?? 999))
        .slice(0, RELATIONS_CAST_CAP);

      // Everything below depends only on the ids above, so it all goes out
      // at once — the whole endpoint is two round trips deep, not eight.
      const [similarResult, recommendedResult, ...peopleFilms] = await Promise.all([
        tmdbGet(env, `/movie/${sourceTmdbId}/similar`, { language: "en-US" }).catch(() => null),
        tmdbGet(env, `/movie/${sourceTmdbId}/recommendations`, { language: "en-US" }).catch(() => null),
        ...directors.map((d) => personFilms(env, d.id, "director", sourceTmdbId)),
        ...cast.map((c) => personFilms(env, c.id, "cast", sourceTmdbId)),
      ]);

      // Interleaved for the same reason tmdb_client.similar_and_recommended
      // interleaves them: recommendations are behaviour-based and similar is
      // content-based, and letting either dominate the cap loses half the
      // point of asking both.
      const similarSeen = new Set();
      const similar = [];
      const similarRows = (similarResult && similarResult.results) || [];
      const recommendedRows = (recommendedResult && recommendedResult.results) || [];
      for (let i = 0; i < Math.max(similarRows.length, recommendedRows.length); i++) {
        for (const movie of [recommendedRows[i], similarRows[i]]) {
          if (!usableRelation(movie, sourceTmdbId) || similarSeen.has(movie.id)) continue;
          similarSeen.add(movie.id);
          similar.push(relationRow(movie));
          if (similar.length >= RELATIONS_SIMILAR_CAP) break;
        }
        if (similar.length >= RELATIONS_SIMILAR_CAP) break;
      }

      // A person whose filmography call failed is dropped rather than sent
      // as an empty section — "nothing else by this director" and "couldn't
      // ask" must not read the same on the page.
      const people = [...directors, ...cast].map((person, i) => ({
        tmdb_id: person.id,
        name: person.name,
        role: i < directors.length ? "director" : "cast",
        films: peopleFilms[i],
      })).filter((p) => p.films !== null);

      return jsonResponse({
        ok: true,
        tmdb_id: sourceTmdbId,
        people,
        similar,
        // Distinguishes "TMDB has no similar films" from "both calls
        // failed", which the page has to be able to say differently.
        similar_unavailable: similarResult === null && recommendedResult === null,
      }, 200, cors);
    }

    // ---------- /person (the director/actor profile page) ----------
    //
    // Takes an id when the page has one (the relations payload carries
    // them) and a name when it doesn't. Returns the person plus their whole
    // filmography, split by what they did on each film — the page renders
    // directing and acting as separate sections, and merges each against
    // what it already tracks the same way the film page does.
    if (url.pathname === "/person") {
      if (!env.TMDB_API_KEY) {
        return jsonResponse(
          { ok: false, error: "TMDB_API_KEY is not configured on this Worker" }, 503, cors);
      }

      let payload;
      try {
        payload = await request.json();
      } catch {
        return jsonResponse({ ok: false, error: "invalid JSON body" }, 400, cors);
      }

      const name = typeof payload.name === "string" ? payload.name.trim() : "";
      let personId = payload.person_id === undefined || payload.person_id === null
        ? null : Number(payload.person_id);
      if (personId !== null && (!Number.isInteger(personId) || personId <= 0)) {
        return jsonResponse({ ok: false, error: 'invalid "person_id"' }, 400, cors);
      }
      if (personId === null && !name) {
        return jsonResponse({ ok: false, error: 'missing "person_id" or "name"' }, 400, cors);
      }
      if (name.length > MAX_QUERY_LENGTH) {
        return jsonResponse(
          { ok: false, error: `name too long (max ${MAX_QUERY_LENGTH} characters)` }, 400, cors);
      }

      try {
        if (personId === null) {
          personId = await findPersonId(env, name);
          if (personId === null) {
            return jsonResponse(
              { ok: false, error: `TMDB has nobody called ${JSON.stringify(name)}` }, 404, cors);
          }
        }

        const details = await tmdbGet(env, `/person/${personId}`, {
          language: "en-US", append_to_response: "movie_credits",
        });
        const credits = details.movie_credits || {};

        const seenDirected = new Set();
        const directed = [];
        (credits.crew || []).forEach((movie) => {
          // A person credited twice on one film (TMDB does this) would
          // otherwise appear twice in their own filmography.
          if (movie.job !== "Director" || !usableRelation(movie, null) || seenDirected.has(movie.id)) return;
          seenDirected.add(movie.id);
          directed.push(movie);
        });

        const seenActed = new Set();
        const acted = [];
        (credits.cast || []).forEach((movie) => {
          if (!usableRelation(movie, null) || seenActed.has(movie.id)) return;
          if ((movie.vote_count || 0) < RELATIONS_MIN_VOTES) return;
          seenActed.add(movie.id);
          acted.push(movie);
        });

        return jsonResponse({
          ok: true,
          person: {
            tmdb_id: personId,
            name: details.name || name,
            biography: details.biography || null,
            birthday: details.birthday || null,
            deathday: details.deathday || null,
            place_of_birth: details.place_of_birth || null,
            known_for_department: details.known_for_department || null,
            profile_url: details.profile_path ? PERSON_PROFILE_IMAGE_BASE + details.profile_path : null,
          },
          // Newest first here rather than most popular: a filmography reads
          // as a career, and the cap is high enough that nothing worth
          // seeing falls off the end of it.
          directed: byNewestFirst(directed, PERSON_FILMS_CAP),
          acted: byNewestFirst(acted, PERSON_FILMS_CAP),
        }, 200, cors);
      } catch (err) {
        return jsonResponse({ ok: false, error: String(err).slice(0, 300) }, 502, cors);
      }
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

    // Anything else is a mistake, and must not fall through to the daily
    // run below — a typo'd path, or a request sent to an endpoint this
    // Worker hasn't been redeployed with yet, used to silently kick off a
    // full scrape-and-deploy pipeline and answer as if it had done what was
    // asked. Unknown paths say so instead.
    if (url.pathname !== "/") {
      return jsonResponse({ ok: false, error: `unknown endpoint: ${url.pathname}` }, 404, cors);
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
