import html
import json
from collections import defaultdict
from datetime import date

from .brands import canonical_brand_name, group_offers_by_brand_and_country, is_major_brand
from .config import CountryConfig, is_have_anywhere
from .countries import country_name
from .state import StateDoc

FREE_MONETIZATION_TYPES = {"ADS", "FREE"}
FREE_TIER_COUNTRIES = {"AU", "GB", "US"}
ALWAYS_MAIN_BRANDS = {"Netflix", "HBO Max"}
RECOMMENDED_COUNT = 10
CARD_DIRECTOR_CAP = 2
LETTERBOXD_USERNAME = "Jmackwell"
# Cloudflare Worker proxy for the settings-page "Refresh now" button — holds
# the real GitHub PAT server-side so the browser never sees it. TRIGGER_SECRET
# just deters casual/bot hits on the endpoint; it's not a real security
# boundary since it's necessarily embedded in this public page anyway.
REFRESH_WORKER_URL = "https://letterboxd-refresh-trigger.joshmackwell19.workers.dev"
REFRESH_TRIGGER_SECRET = "c873bf14292aecf07b61b66c61a6d540"


def _truncate_joined(value: str | None, max_shown: int = CARD_DIRECTOR_CAP) -> str | None:
    """Shortens an already-comma-joined "A, B, C, D" string to "A, B +2 more"
    for card contexts — an anthology film's full 13-director credit list is
    fine in quick-look/service-detail (films_by_slug/discovery_films keep the
    untruncated string; quick-look reads from there directly, not from this),
    but blows out card height and breaks the grid when every card is
    supposed to be roughly the same size."""
    if not value:
        return value
    parts = value.split(", ")
    if len(parts) <= max_shown:
        return value
    return ", ".join(parts[:max_shown]) + f" +{len(parts) - max_shown} more"


def _classify(brand: str, country: str, monetization_types: set[str], config: dict[str, CountryConfig],
              global_subscriptions: list[str], revisitable: set[str]) -> str:
    if is_have_anywhere(brand, country, config, global_subscriptions):
        return "have"
    if brand in revisitable:
        return "could_get_again"
    if "FLATRATE" in monetization_types:
        return "subscription"
    return "free"


_MONETIZATION_PRIORITY = {"FLATRATE": 0, "FREE": 1, "ADS": 2}


def _all_offers_for_film(
    film, config: dict[str, CountryConfig], global_subscriptions: list[str], revisitable: set[str]
) -> list[dict]:
    """Every (brand, country) this film has a qualifying offer for, each
    classified — the single source of truth other views bucket/filter.

    available_to (soonest expiry seen for that brand/country, if any) and
    url (a deep link to actually watch it there) both ride along here
    rather than needing a second pass over film.offers later —
    group_offers_by_brand_and_country only tracks monetization types, so
    this is the one place with access to the raw per-offer dates/urls. When
    a (brand, country) has multiple qualifying offers (e.g. a free ad tier
    and a full subscription), the url from the most-watchable one wins."""
    soonest_expiry: dict[tuple[str, str], str] = {}
    best_url: dict[tuple[str, str], tuple[int, str]] = {}
    for offer in film.offers:
        key = (canonical_brand_name(offer.package_clear_name), offer.country)
        if offer.available_to and (key not in soonest_expiry or offer.available_to < soonest_expiry[key]):
            soonest_expiry[key] = offer.available_to
        rank = _MONETIZATION_PRIORITY.get(offer.monetization_type, 9)
        if offer.url and (key not in best_url or rank < best_url[key][0]):
            best_url[key] = (rank, offer.url)

    result = []
    for brand, by_country in group_offers_by_brand_and_country(film.offers).items():
        for country, monetization_types in by_country.items():
            classification = _classify(brand, country, monetization_types, config, global_subscriptions, revisitable)
            key = (brand, country)
            url_entry = best_url.get(key)
            result.append({
                "brand": brand, "country": country, "classification": classification,
                "available_to": soonest_expiry.get(key),
                "url": url_entry[1] if url_entry else None,
            })
    return result


def compute_offer_snapshot(
    state: StateDoc, config: dict[str, CountryConfig], global_subscriptions: list[str], revisitable: set[str]
) -> dict[str, dict[tuple[str, str], str]]:
    """slug -> {(brand, country): classification}, using the same have/
    could_get_again/free/subscription taxonomy as the rest of the dashboard —
    lets the daily run diff today's snapshot against yesterday's to detect
    newly-added have/free offers without a second, differently-classified
    audit system."""
    return {
        slug: {(o["brand"], o["country"]): o["classification"]
               for o in _all_offers_for_film(film, config, global_subscriptions, revisitable)}
        for slug, film in state.films.items()
    }


def _select_main_brands(
    state: StateDoc, config: dict[str, CountryConfig], global_subscriptions: list[str]
) -> list[str]:
    """Main columns on the films tab: services you actually have (real
    subscriptions), Netflix/HBO Max explicitly (had before, worth seeing),
    and any free/ad-supported service in AU/GB/US. Everything else rolls
    into "Other services" — keeps the wide table down to a page-able size.
    """
    have_brands: set[str] = set()
    free_brands: set[str] = set()

    for film in state.films.values():
        for brand, by_country in group_offers_by_brand_and_country(film.offers).items():
            for country, monetization_types in by_country.items():
                if is_have_anywhere(brand, country, config, global_subscriptions):
                    have_brands.add(brand)
                if country in FREE_TIER_COUNTRIES and monetization_types & FREE_MONETIZATION_TYPES:
                    free_brands.add(brand)

    main = {b for b in (have_brands | ALWAYS_MAIN_BRANDS | free_brands) if is_major_brand(b)}
    priority = {**{b: 0 for b in have_brands}, **{b: 0 for b in ALWAYS_MAIN_BRANDS}, **{b: 1 for b in free_brands}}
    return sorted(main, key=lambda b: (priority.get(b, 1), b))


def _film_row(film, main_brands: set[str], all_offers: list[dict]) -> dict:
    main_availability: dict[str, list[dict]] = {}
    other_services: list[dict] = []
    any_have = False
    all_countries: set[str] = set()

    for offer in all_offers:
        all_countries.add(offer["country"])
        if offer["classification"] == "have":
            any_have = True
        if offer["brand"] in main_brands:
            main_availability.setdefault(offer["brand"], []).append(
                {"country": offer["country"], "classification": offer["classification"]}
            )
        else:
            other_services.append({"brand": offer["brand"], "country": offer["country"],
                                    "classification": offer["classification"]})

    for entries in main_availability.values():
        entries.sort(key=lambda e: (_CLASSIFICATION_PRIORITY[e["classification"]], e["country"]))
    other_services.sort(key=lambda o: (_CLASSIFICATION_PRIORITY[o["classification"]], o["brand"], o["country"]))

    return {
        "title": film.title,
        "year": film.year,
        "slug": film.slug,
        "rating": film.rating,
        "poster_url": film.poster_url,
        "director": _truncate_joined(", ".join(film.director) if film.director else None),
        "starring": ", ".join(film.starring) if film.starring else None,
        "genre": film.genre,
        "any_service": bool(all_offers),
        "have_service": any_have,
        "coverage_countries": len(all_countries),
        "main": main_availability,
        "other_services": other_services,
    }


# have > free > could_get_again > subscription, always — must match the JS
# CLASSIFICATION_PRIORITY constant exactly, since this same order needs to
# be consistent whether a badge list was pre-sorted here (server-side) or
# sorted client-side (e.g. buildFilmDetailCard's "other services" section).
_CLASSIFICATION_PRIORITY = {"have": 0, "free": 1, "could_get_again": 2, "subscription": 3}


def _service_rows(state: StateDoc, films_all_offers: dict[str, list[dict]]) -> list[dict]:
    """One row per (brand, country). "titles"/"unique_titles" are slug lists
    — the detail page resolves full film info from films_by_slug so poster/
    synopsis/etc. text isn't duplicated across every service row it appears in.

    A single "classification" represents the whole group (same have/
    could_get_again/free/subscription taxonomy as the film and country
    views) rather than separate have/paid booleans. "have"/"could_get_again"
    are structural per (brand, country) so never actually vary within a
    group; only free-vs-subscription can, when the service isn't one you
    have, and there the best (most favorable) classification wins.
    """
    by_brand_country: dict[tuple[str, str], dict] = {}

    for slug, all_offers in films_all_offers.items():
        film_has_have = any(o["classification"] == "have" for o in all_offers)
        for offer in all_offers:
            key = (offer["brand"], offer["country"])
            entry = by_brand_country.setdefault(key, {"slugs": [], "has_have_flags": {}, "classifications": set()})
            entry["slugs"].append(slug)
            entry["has_have_flags"][slug] = film_has_have
            entry["classifications"].add(offer["classification"])

    rows = []
    for (brand, country), entry in by_brand_country.items():
        slugs = sorted(entry["slugs"], key=lambda s: state.films[s].title.lower())
        unique_slugs = [s for s in slugs if not entry["has_have_flags"][s]]
        classification = min(entry["classifications"], key=lambda c: _CLASSIFICATION_PRIORITY[c])
        rows.append({
            "brand": brand,
            "country": country,
            "country_name": country_name(country),
            "classification": classification,
            "film_count": len(slugs),
            "slugs": slugs,
            "unique_film_count": len(unique_slugs),
            "unique_slugs": unique_slugs,
        })
    rows.sort(key=lambda r: (-r["film_count"], r["brand"], r["country"]))
    return rows


def _country_rows(state: StateDoc, films_all_offers: dict[str, list[dict]]) -> list[dict]:
    by_country: dict[str, list[dict]] = defaultdict(list)

    for slug, all_offers in films_all_offers.items():
        film = state.films[slug]
        country_services: dict[str, list[dict]] = defaultdict(list)
        for offer in all_offers:
            country_services[offer["country"]].append({"brand": offer["brand"], "classification": offer["classification"]})

        for country, services in country_services.items():
            services.sort(key=lambda s: (_CLASSIFICATION_PRIORITY[s["classification"]], s["brand"]))
            by_country[country].append({
                "title": film.title, "year": film.year, "slug": film.slug, "rating": film.rating,
                "poster_url": film.poster_url,
                "director": _truncate_joined(", ".join(film.director) if film.director else None),
                "starring": ", ".join(film.starring) if film.starring else None,
                "genre": film.genre,
                "services": services,
                "has_have": any(s["classification"] == "have" for s in services),
            })

    countries = []
    for code, films in by_country.items():
        films.sort(key=lambda f: f["title"].lower())
        countries.append({"code": code, "name": country_name(code), "films": films})
    countries.sort(key=lambda c: c["name"])
    return countries


def _films_by_slug(state: StateDoc, films_all_offers: dict[str, list[dict]]) -> dict[str, dict]:
    lookup = {}
    for slug, all_offers in films_all_offers.items():
        film = state.films[slug]
        lookup[slug] = {
            "slug": slug,
            "title": film.title,
            "year": film.year,
            "rating": film.rating,
            "poster_url": film.poster_url,
            "director": ", ".join(film.director) if film.director else None,
            "starring": film.starring,
            "synopsis": film.synopsis,
            "genre": film.genre,
            "all_offers": all_offers,
        }
    return lookup


def _mini_card(film) -> dict:
    """Minimal shape for a home-page tile: no services shown there (that's
    what the quick-look modal is for, resolved client-side from
    films_by_slug), so only enough to render the card itself."""
    return {
        "slug": film.slug,
        "title": film.title,
        "year": film.year,
        "rating": film.rating,
        "poster_url": film.poster_url,
        "director": _truncate_joined(", ".join(film.director) if film.director else None),
        "genre": film.genre,
    }


def _top_rated_section(state: StateDoc, films_all_offers: dict[str, list[dict]], exclude: set[str],
                        limit: int = RECOMMENDED_COUNT) -> dict:
    """Placeholder recommendation methodology (no watch-history data exists
    yet, only watchlist + availability + Letterboxd's crowd rating): the
    highest-rated films you can actually watch right now on a service you
    have, falling back to highest-rated overall if fewer than `limit`
    qualify. Revisit once there's a richer signal to rank on."""
    def has_have(slug: str) -> bool:
        return any(o["classification"] == "have" for o in films_all_offers.get(slug, []))

    rated = [(slug, film.rating) for slug, film in state.films.items()
             if film.rating is not None and slug not in exclude]
    watchable_now = sorted((s for s, r in rated if has_have(s)), key=lambda s: (-state.films[s].rating, state.films[s].title))
    chosen = watchable_now[:limit]
    if len(chosen) < limit:
        fallback = sorted((s for s, r in rated if s not in chosen), key=lambda s: (-state.films[s].rating, state.films[s].title))
        chosen += fallback[: limit - len(chosen)]

    return {
        "key": "top_rated", "header": "Top rated, ready to watch",
        "films": [_mini_card(state.films[s]) for s in chosen],
    }


def _mini_card_from_lookup(entry: dict) -> dict:
    """Same shape as _mini_card, but from a films_by_slug-shaped dict (either
    a watchlist film or a discovered one — see _build_home_sections). The
    full director string stays intact on films_by_slug/discovery_films
    itself (quick-look reads from there directly) — only this card-shaped
    copy gets truncated."""
    return {
        "slug": entry["slug"], "title": entry["title"], "year": entry["year"],
        "rating": entry["rating"], "poster_url": entry["poster_url"],
        "director": _truncate_joined(entry["director"]),
        "genre": entry.get("genre") or [],
    }


def _section_from_cached(cached: dict, lookup: dict[str, dict], exclude: set[str],
                          limit: int = RECOMMENDED_COUNT) -> dict:
    chosen = [s for s in cached["slugs"] if s in lookup and s not in exclude][:limit]
    return {"key": cached["key"], "header": cached["header"],
            "films": [_mini_card_from_lookup(lookup[s]) for s in chosen]}


def _cached_section(state: StateDoc, lookup: dict[str, dict], key: str, exclude: set[str],
                     limit: int = RECOMMENDED_COUNT) -> dict:
    """because_you_watched/by_genre/hidden_gems/popular_now/rewatch are
    correlated across all of TMDB (not just the watchlist), which needs
    network calls — resolved once during the real daily run and cached on
    state.recommendation_sections (+ state.discovery_films for anything not
    already on the watchlist), since this function itself must stay
    network-free to regenerate."""
    cached = next((s for s in state.recommendation_sections if s["key"] == key), None)
    if cached is None:
        return {"key": key, "header": "", "films": []}
    return _section_from_cached(cached, lookup, exclude, limit)


def _recently_added_section(state: StateDoc, exclude: set[str], limit: int = 12) -> dict:
    seen: set[str] = set()
    chosen: list[str] = []
    added_service_by_slug: dict[str, str] = {}
    for entry in state.recent_additions:  # already newest-first, capped rolling log
        slug = entry["slug"]
        if slug in seen or slug in exclude or slug not in state.films:
            continue
        seen.add(slug)
        chosen.append(slug)
        added_service_by_slug[slug] = f'{entry["brand"]} ({country_name(entry["country"])})'
        if len(chosen) >= limit:
            break

    films = []
    for s in chosen:
        card = _mini_card(state.films[s])
        # Which service/country actually triggered this addition — the
        # whole point of the section is "this just became watchable", so
        # naming where saves a click into quick-look to find out.
        card["added_service"] = added_service_by_slug[s]
        films.append(card)

    return {
        "key": "recently_added", "header": "Recently added to your services",
        "films": films,
    }


LEAVING_SOON_WINDOW_DAYS = 30


def _leaving_soon_section(state: StateDoc, films_all_offers: dict[str, list[dict]], exclude: set[str],
                           limit: int = RECOMMENDED_COUNT) -> dict:
    """Films with a have/free offer that actually expires within the window
    — most offers have no available_to at all (open-ended subscription
    flatrate), so this is inherently small/occasional, not a guaranteed
    everyday section. Only have/free count: losing a could_get_again offer
    isn't "you're about to lose access", since you don't currently have it
    via that route anyway."""
    today = date.today()
    candidates = []  # (days_left, slug, brand, country)

    for slug, offers in films_all_offers.items():
        if slug in exclude:
            continue
        soonest = None
        for offer in offers:
            if offer["classification"] not in ("have", "free") or not offer["available_to"]:
                continue
            try:
                days_left = (date.fromisoformat(offer["available_to"]) - today).days
            except ValueError:
                continue
            if days_left < 0 or days_left > LEAVING_SOON_WINDOW_DAYS:
                continue
            if soonest is None or days_left < soonest[0]:
                soonest = (days_left, offer["brand"], offer["country"])
        if soonest is not None:
            candidates.append((soonest[0], slug, soonest[1], soonest[2]))

    candidates.sort(key=lambda c: c[0])

    films = []
    for days_left, slug, brand, country in candidates[:limit]:
        when = "today" if days_left == 0 else "tomorrow" if days_left == 1 else f"in {days_left} days"
        card = _mini_card(state.films[slug])
        card["leaving_note"] = f"Leaving {brand} ({country_name(country)}) {when}"
        films.append(card)

    return {"key": "leaving_soon", "header": "Leaving soon", "films": films}


def _build_home_sections(state: StateDoc, films_all_offers: dict[str, list[dict]],
                          films_by_slug: dict[str, dict], dismissed_recommendations: set[str]) -> list[dict]:
    lookup = {**films_by_slug, **state.discovery_films}
    # Seeded with dismissed slugs so every section below skips them for
    # free — "not interested" only ever applies to a discovery pick (not
    # already on the watchlist), so this can't accidentally hide a real
    # watchlist film from leaving_soon/recently_added/top_rated too.
    used: set[str] = set(dismissed_recommendations)
    sections: list[dict] = []

    def add(section: dict) -> None:
        if section["films"]:
            sections.append(section)
            used.update(f["slug"] for f in section["films"])

    # Leaving soon leads — losing access to something you already know you
    # want is a bigger deal than a delayed discovery, so it outranks even
    # recently-added. Recent service additions next — the most immediately
    # actionable ("this is now watchable") signal after that.
    add(_leaving_soon_section(state, films_all_offers, used))
    add(_recently_added_section(state, used))

    # Recommended-from-recent-watches and top-rated next — general
    # discovery, not tied to a specific person — so they're not buried
    # under however many per-director/per-cast sections exist this run.
    add(_cached_section(state, lookup, "because_you_watched", used))
    add(_top_rated_section(state, films_all_offers, used))

    # One section per unique director/cast member from your last few
    # watches — however many that turns out to be (see main.py) — the most
    # personalized picks, but narrower-appeal than the two above.
    for prefix in ("director:", "cast:"):
        for cached in state.recommendation_sections:
            if cached["key"].startswith(prefix):
                add(_section_from_cached(cached, lookup, used))

    # Popular right now moved below the director/cast sections — general
    # trending picks are lower priority than either the sections above or
    # the personalized ones just above it.
    add(_cached_section(state, lookup, "popular_now", used))
    add(_cached_section(state, lookup, "rewatch", used))

    # Longer-tail exploration at the bottom, on purpose — genre/hidden-gem
    # picks are lower-confidence than the sections above.
    add(_cached_section(state, lookup, "by_genre", used))
    add(_cached_section(state, lookup, "hidden_gems", used))

    return sections


def _settings_data(config: dict[str, CountryConfig], global_subscriptions: list[str]) -> dict:
    """Read-only view of config/services.yaml for the settings page: the
    global (VPN-portable) "have" list, plus each country's own subscriptions
    and free-tier apps with the merged-in globals subtracted back out so
    they don't show up duplicated under every country."""
    countries = []
    for code, country_config in config.items():
        own_subscriptions = [s for s in country_config.subscriptions if s not in global_subscriptions]
        if not own_subscriptions and not country_config.free_tier:
            continue
        countries.append({
            "code": code, "name": country_name(code),
            "subscriptions": own_subscriptions, "free_tier": country_config.free_tier,
        })
    countries.sort(key=lambda c: c["name"])

    return {
        "letterboxd_username": LETTERBOXD_USERNAME,
        "global_subscriptions": global_subscriptions,
        "countries": countries,
        "refresh_worker_url": REFRESH_WORKER_URL,
        "refresh_trigger_secret": REFRESH_TRIGGER_SECRET,
    }


def build_dashboard_data(
    state: StateDoc,
    favorites: set[tuple[str, str]],
    config: dict[str, CountryConfig],
    global_subscriptions: list[str],
    revisitable: set[str],
    dismissed_recommendations: set[str] = frozenset(),
) -> dict:
    main_brands = _select_main_brands(state, config, global_subscriptions)
    main_brand_set = set(main_brands)

    films_all_offers = {
        slug: _all_offers_for_film(film, config, global_subscriptions, revisitable)
        for slug, film in state.films.items()
    }

    rows = [_film_row(film, main_brand_set, films_all_offers[slug]) for slug, film in state.films.items()]
    rows.sort(key=lambda r: r["title"].lower())

    films_by_slug = _films_by_slug(state, films_all_offers)

    return {
        "last_run_at": state.last_run_at,
        "letterboxd_watchlist_url": f"https://letterboxd.com/{LETTERBOXD_USERNAME}/watchlist/",
        "main_brands": main_brands,
        "home_sections": _build_home_sections(state, films_all_offers, films_by_slug, dismissed_recommendations),
        "films": rows,
        "services": _service_rows(state, films_all_offers),
        "countries": _country_rows(state, films_all_offers),
        "films_by_slug": {**films_by_slug, **state.discovery_films},
        "settings": _settings_data(config, global_subscriptions),
    }


def render_dashboard_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return _TEMPLATE.replace("__DATA__", payload).replace("__TITLE__", html.escape(f"{len(data['films'])} films"))


_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Watchlist streaming dashboard</title>
<link rel="manifest" href="manifest.json">
<link rel="icon" href="icons/favicon-32.png" sizes="32x32">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="theme-color" content="#141210">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Watchlist">
<style>
  :root {
    color-scheme: dark;
    --bg: #0e1013;
    --surface: #171a1f;
    --surface-2: #1e222a;
    --text: #edf0f2;
    --text-muted: #98a1ab;
    --text-faint: #5f6770;
    --hairline: rgba(255, 255, 255, 0.07);
    --hairline-strong: rgba(255, 255, 255, 0.14);
    --accent: #4fd1c5;
    --accent-soft: rgba(79, 209, 197, 0.14);
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.4), 0 12px 28px rgba(0, 0, 0, 0.35);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
    margin: 0; padding: calc(28px + env(safe-area-inset-top)) 32px 60px;
    background: var(--bg); color: var(--text);
    -webkit-font-smoothing: antialiased;
    display: flex; flex-direction: column; min-height: 100dvh;
  }
  .status-bar-fill {
    position: fixed; top: 0; left: 0; right: 0; height: env(safe-area-inset-top);
    background: var(--accent); z-index: 30;
  }
  .ptr-indicator {
    position: fixed; top: env(safe-area-inset-top); left: 50%; transform: translate(-50%, -60px);
    background: var(--surface); border: 1px solid var(--hairline-strong); color: var(--accent);
    font-size: 12px; font-weight: 600; padding: 6px 14px; border-radius: 999px;
    box-shadow: var(--shadow); z-index: 40; pointer-events: none; white-space: nowrap;
    /* The transform above is the primary hide mechanism, but on devices with
       a large safe-area-inset-top (Dynamic Island/notch phones) "-60px" isn't
       always enough headroom to clear the indicator's own height, and in
       standalone/home-screen mode there's no browser chrome to mask a stray
       peeking edge the way Safari's own UI does in a normal tab. Opacity is
       the real hide mechanism; the transform is just where it un-hides to. */
    opacity: 0; transition: opacity 0.15s ease;
  }
  .ptr-indicator.visible { opacity: 1; }
  .header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 18px; flex-wrap: wrap; }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 3px; letter-spacing: -0.01em; }
  .meta { color: var(--text-muted); font-size: 12.5px; }
  .watchlist-link {
    color: var(--accent); text-decoration: none; font-size: 13px; font-weight: 500;
    padding: 7px 14px; border: 1px solid var(--hairline-strong); border-radius: 999px; white-space: nowrap;
  }
  .watchlist-link:hover { background: var(--accent-soft); }
  .header-actions { display: flex; align-items: center; gap: 8px; }
  .icon-btn {
    background: none; border: 1px solid var(--hairline-strong); color: var(--text-muted);
    width: 34px; height: 34px; border-radius: 50%; cursor: pointer; font-size: 15px;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .icon-btn:hover { color: var(--text); border-color: var(--text-muted); }
  .settings-block { margin-bottom: 22px; }
  .settings-service-group { margin-bottom: 18px; }
  .settings-service-group > h4 {
    font-size: 12.5px; font-weight: 600; margin: 0 0 8px; color: var(--text-muted);
  }
  .settings-country-group {
    margin-bottom: 20px; padding: 14px 16px; background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 12px;
  }
  .settings-country-name { font-size: 13.5px; font-weight: 600; margin: 0 0 12px; color: var(--text); }
  .settings-subgroup { margin-bottom: 14px; }
  .settings-subgroup:last-child { margin-bottom: 0; }
  .settings-subgroup-label {
    display: block; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-faint); margin-bottom: 7px;
  }
  .service-pills { display: flex; flex-wrap: wrap; gap: 6px; min-height: 26px; margin-bottom: 8px; }
  .service-pill {
    display: inline-flex; align-items: center; gap: 5px; padding: 4px 6px 4px 12px; border-radius: 999px;
    font-size: 12px; font-weight: 500; white-space: nowrap; animation: pill-in 0.12s ease-out;
  }
  @keyframes pill-in { from { opacity: 0; transform: scale(0.85); } to { opacity: 1; transform: scale(1); } }
  .service-pill.pill-have { background: rgba(74, 222, 128, 0.14); color: #4ade80; }
  .service-pill.pill-free { background: rgba(96, 165, 250, 0.14); color: #60a5fa; }
  .service-pill-remove {
    display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px;
    border-radius: 50%; cursor: pointer; opacity: 0.65; font-size: 11px; line-height: 1; flex-shrink: 0;
  }
  .service-pill-remove:hover { opacity: 1; background: rgba(255, 255, 255, 0.18); }
  .service-add-wrap { position: relative; max-width: 300px; }
  .service-add-input {
    width: 100%; padding: 7px 12px; border: 1px dashed var(--hairline-strong); border-radius: 999px;
    font-size: 12.5px; background: transparent; color: var(--text); outline: none; transition: border-color 0.15s;
  }
  .service-add-input::placeholder { color: var(--text-faint); }
  .service-add-input:focus { border-color: var(--accent); border-style: solid; }
  .service-suggestions {
    position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 20;
    background: var(--surface-2); border: 1px solid var(--hairline-strong); border-radius: 10px;
    box-shadow: var(--shadow); max-height: 210px; overflow-y: auto; padding: 4px;
  }
  .service-suggestions.hidden { display: none; }
  .service-suggestion { padding: 7px 10px; font-size: 12.5px; cursor: pointer; border-radius: 7px; }
  .service-suggestion:hover, .service-suggestion.active { background: var(--hairline); }
  .service-suggestion.add-new { color: var(--accent); }
  #saveServices { transition: opacity 0.15s, background 0.15s, color 0.15s, border-color 0.15s; }
  #saveServices:disabled { opacity: 0.45; cursor: not-allowed; }
  #saveServices.has-changes {
    background: var(--accent); color: #06201d; border-color: var(--accent); font-weight: 600;
  }
  .info-icon {
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    color: var(--text-faint); cursor: pointer; font-size: 12.5px; margin-left: 4px;
  }
  .info-icon:hover { color: var(--text-muted); }
  .info-tooltip {
    display: none; position: absolute; bottom: 135%; left: 50%; transform: translateX(-50%);
    background: var(--surface-2); color: var(--text); font-size: 11px; padding: 6px 10px; border-radius: 8px;
    white-space: nowrap; box-shadow: var(--shadow); border: 1px solid var(--hairline-strong); z-index: 10;
  }
  .info-icon:hover .info-tooltip, .info-icon.open .info-tooltip { display: block; }
  .tabs {
    display: flex; gap: 6px; margin-bottom: 16px;
    position: sticky; top: 0; z-index: 15; background: var(--bg);
    padding: 10px 0; border-bottom: 1px solid var(--hairline);
  }
  .tab-btn {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 7px 15px 7px 12px; border: none; border-radius: 999px; background: transparent; color: var(--text-muted);
    cursor: pointer; font-size: 12.5px; font-weight: 500; transition: background 0.15s, color 0.15s;
  }
  .tab-btn svg { width: 16px; height: 16px; stroke: currentColor; flex-shrink: 0; }
  .tab-btn:hover { background: var(--hairline); }
  .tab-btn.active { background: var(--text); color: var(--bg); }
  .controls { display: flex; gap: 9px; align-items: center; margin-bottom: 11px; flex-wrap: wrap; font-size: 12.5px; }
  .quick-filters { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
  .quick-filters .hint { color: var(--text-faint); font-size: 11.5px; margin-right: 2px; }
  input[type=text] {
    padding: 8px 28px 8px 12px; border: 1px solid var(--hairline-strong); border-radius: 10px; font-size: 12.5px; width: 190px;
    background: var(--surface); color: var(--text); outline: none; transition: border-color 0.15s;
  }
  input[type=text]::placeholder { color: var(--text-faint); }
  input[type=text]:focus { border-color: var(--accent); }
  .search-wrap { position: relative; display: inline-flex; align-items: center; }
  .search-clear {
    position: absolute; right: 7px; top: 50%; transform: translateY(-50%);
    width: 16px; height: 16px; display: flex; align-items: center; justify-content: center;
    border-radius: 50%; cursor: pointer; color: var(--text-faint); font-size: 12px; line-height: 1;
    transition: background 0.15s, color 0.15s;
  }
  .search-clear:hover { color: var(--text); background: var(--hairline); }
  .search-clear.hidden { display: none; }
  label { color: var(--text-muted); display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 12.5px; }
  select {
    padding: 8px 11px; border: 1px solid var(--hairline-strong); border-radius: 10px; font-size: 12.5px;
    background: var(--surface); color: var(--text);
  }
  a.film-link { color: inherit; text-decoration: none; }
  a.film-link:hover { color: var(--accent); }
  section.view { display: none; }
  section.view.active { display: block; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 10.5px; font-weight: 500;
    margin: 1px 4px 1px 0; white-space: nowrap; cursor: pointer;
  }
  .badge-have { background: rgba(74, 222, 128, 0.14); color: #4ade80; }
  .badge-could_get_again { background: rgba(192, 132, 252, 0.14); color: #c084fc; }
  .badge-free { background: rgba(96, 165, 250, 0.14); color: #60a5fa; }
  .badge-subscription { background: rgba(255, 255, 255, 0.07); color: var(--text-muted); }
  .badge-more-btn {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 10.5px; font-weight: 500;
    margin: 1px 4px 1px 0; white-space: nowrap; cursor: pointer;
    background: none; border: 1px solid var(--hairline-strong); color: var(--text-muted);
  }
  .badge-more-btn:hover { color: var(--text); border-color: var(--text-muted); }
  a.badge-link { text-decoration: none; transition: filter 0.15s; }
  a.badge-link:hover { filter: brightness(1.35); }
  .watch-now-btn {
    font-size: 13px; font-weight: 600; padding: 7px 16px; margin: 6px 0 2px;
  }
  .filter-toggle { cursor: pointer; border: 1.5px solid transparent; transition: opacity 0.15s; }
  .filter-toggle.off { opacity: 0.3; }
  .quick-country {
    padding: 5px 12px; border-radius: 999px; font-size: 11.5px; font-weight: 500; cursor: pointer;
    background: var(--surface); border: 1px solid var(--hairline-strong); color: var(--text-muted);
  }
  .quick-country:hover { border-color: var(--accent); color: var(--accent); }
  .quick-country.active { background: var(--accent); border-color: var(--accent); color: #06201d; }
  .quick-country .count { opacity: 0.65; margin-left: 4px; }
  .poster-thumb {
    width: 32px; height: 47px; object-fit: cover; border-radius: 4px; flex-shrink: 0;
    background: var(--hairline); box-shadow: 0 1px 3px rgba(0,0,0,0.35);
  }
  .poster-placeholder { width: 32px; height: 47px; border-radius: 4px; flex-shrink: 0; background: var(--hairline); }
  .active-filters { display: flex; gap: 6px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; font-size: 11.5px; color: var(--text-muted); }
  .filter-chip {
    display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; border-radius: 999px;
    background: var(--accent-soft); color: var(--accent); cursor: pointer; font-weight: 500;
  }
  .clear-all-chip { background: rgba(255, 255, 255, 0.07); color: var(--text-muted); }
  .clear-all-chip:hover { background: rgba(255, 255, 255, 0.12); color: var(--text); }
  .back-btn {
    background: none; border: 1px solid var(--hairline-strong); color: var(--text-muted); padding: 7px 14px;
    border-radius: 999px; cursor: pointer; font-size: 12.5px; margin-bottom: 16px;
  }
  .back-btn:hover { color: var(--text); border-color: var(--text-muted); }
  .surprise-bar { margin-bottom: 18px; }
  .surprise-btn {
    background: none; border: 1.5px solid var(--accent); color: var(--accent); padding: 9px 18px;
    border-radius: 999px; cursor: pointer; font-size: 13.5px; font-weight: 600;
  }
  .surprise-btn:hover { background: var(--accent); color: var(--bg); }
  .detail-title { font-size: 17px; font-weight: 600; margin: 0 0 16px; }
  .detail-title i { color: var(--text-faint); font-style: italic; font-weight: 400; }
  .detail-card {
    display: flex; gap: 16px; padding: 16px 0; border-bottom: 1px solid var(--hairline);
  }
  .detail-poster { width: 76px; height: 112px; object-fit: cover; border-radius: 6px; flex-shrink: 0; background: var(--hairline); }
  .detail-poster-placeholder { width: 76px; height: 112px; border-radius: 6px; flex-shrink: 0; background: var(--hairline); }
  .detail-body h3 { margin: 0 0 4px; font-size: 15px; font-weight: 600; }
  .detail-rating { font-size: 12.5px; color: #4ade80; font-weight: 600; margin: 0 0 6px; }
  .detail-meta { font-size: 12px; color: var(--text-muted); margin: 0 0 5px; }
  .detail-meta strong { color: var(--text); font-weight: 600; }
  .detail-synopsis { font-size: 12.5px; color: var(--text-muted); line-height: 1.5; margin: 4px 0 8px; }
  .badge-wrap { display: flex; flex-wrap: wrap; gap: 2px; }
  .expiring-notes { margin-top: 8px; }
  .expiring-note { font-size: 11.5px; color: #fbbf24; font-weight: 500; margin: 0 0 3px; }
  .expiring-note i { color: #fbbf24; font-style: italic; opacity: 0.85; }
  .muted { color: var(--text-faint); font-size: 12px; }
  .detail-card.collapsible { cursor: pointer; }
  .detail-card.collapsible .other-services-section { display: none; }
  .detail-card.collapsible.expanded .other-services-section { display: block; }
  .modal-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0, 0, 0, 0.6); z-index: 50;
    align-items: center; justify-content: center; padding: 20px;
  }
  .modal-overlay.active { display: flex; }
  .modal-card {
    position: relative; background: var(--surface); border: 1px solid var(--hairline); border-radius: 16px;
    max-width: 560px; width: 100%; max-height: 85vh; overflow-y: auto; padding: 20px; box-shadow: var(--shadow);
  }
  .modal-card .detail-card { border-bottom: none; padding: 0; }
  .modal-card .detail-poster, .modal-card .detail-poster-placeholder { width: 120px; height: 176px; }
  .modal-close {
    position: absolute; top: 10px; right: 10px; background: var(--hairline); border: none; color: var(--text);
    width: 28px; height: 28px; border-radius: 50%; cursor: pointer; font-size: 14px; z-index: 1;
  }
  .modal-close:hover { background: var(--hairline-strong); }
  .home-section { margin-bottom: 24px; }
  .home-section-header { font-size: 14px; font-weight: 600; margin: 0 0 10px; }
  .film-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .film-card {
    background: var(--surface); border: 1px solid var(--hairline); border-radius: 14px; padding: 14px;
    box-shadow: var(--shadow); display: flex; gap: 12px; align-items: flex-start;
  }
  .film-card-end { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .dismiss-btn {
    width: 17px; height: 17px; line-height: 15px; flex-shrink: 0;
    border-radius: 50%; border: 1px solid var(--hairline-strong); background: none;
    color: var(--text-faint); font-size: 10px; cursor: pointer; padding: 0; text-align: center;
  }
  .dismiss-btn:hover { color: var(--text); border-color: var(--text-muted); background: var(--hairline); }
  .film-card.dismissing { opacity: 0; transform: scale(0.96); transition: opacity 0.2s, transform 0.2s; }
  .toast {
    position: fixed; bottom: calc(20px + env(safe-area-inset-bottom)); left: 50%; transform: translateX(-50%);
    background: var(--surface); border: 1px solid var(--hairline-strong); color: var(--text);
    padding: 10px 18px; border-radius: 10px; font-size: 12.5px; box-shadow: var(--shadow); z-index: 40;
    max-width: 90vw; text-align: center;
  }
  .toast.hidden { display: none; }
  .new-badge {
    display: inline-flex; align-items: center; justify-content: center;
    min-width: 15px; height: 15px; padding: 0 4px; border-radius: 999px;
    background: var(--accent); color: var(--bg); font-size: 9.5px; font-weight: 700;
    margin-left: 5px; vertical-align: middle;
  }
  .new-badge.hidden { display: none; }
  .film-card .poster-thumb, .film-card .poster-placeholder { width: 56px; height: 82px; }
  .skeleton-card {
    background: var(--surface); border: 1px solid var(--hairline); border-radius: 14px; height: 110px;
    animation: skeleton-pulse 1.4s ease-in-out infinite;
  }
  @keyframes skeleton-pulse { 0%, 100% { opacity: 0.5; } 50% { opacity: 0.9; } }
  .film-card-body { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 3px; }
  .film-card-title-row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .film-card-title { font-weight: 600; font-size: 13.5px; }
  .film-card-rating { font-size: 12px; color: #4ade80; font-weight: 600; white-space: nowrap; }
  .film-card-director { font-size: 11.5px; color: var(--text-muted); }
  .film-card-genre { font-size: 11px; color: var(--text-faint); }
  .film-card-added-service { font-size: 11px; color: var(--accent); font-weight: 500; margin-top: 2px; }
  .film-card-leaving-note { font-size: 11px; color: #fbbf24; font-weight: 500; margin-top: 2px; }
  .service-group { margin-top: 7px; }
  .service-group-name {
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-muted); cursor: pointer; margin-right: 6px;
  }
  .service-group-name:hover, .service-group-name.active { color: var(--accent); }
  .service-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
  .service-card {
    background: var(--surface); border: 1px solid var(--hairline); border-radius: 14px; padding: 14px;
    box-shadow: var(--shadow); cursor: pointer; display: flex; flex-direction: column; gap: 8px;
  }
  .service-card:hover { border-color: var(--accent); }
  .service-card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
  .service-card-name { font-weight: 600; font-size: 13.5px; }
  .service-card-name i { color: var(--text-faint); font-style: italic; font-weight: 400; display: block; font-size: 11.5px; }
  .service-card-stats { color: var(--text-muted); font-size: 11.5px; }
  .bottom-nav { display: none; }
  @media (max-width: 700px) {
    body { padding: calc(16px + env(safe-area-inset-top)) 12px 16px; }
    h1 { font-size: 17px; }
    .tabs { display: none; }
    .controls { gap: 7px; }
    /* iOS Safari zooms the whole page in on focus of any input/select whose
       computed font-size is under 16px, and doesn't reliably zoom back out
       on blur — 16px here is what stops the zoom from happening at all. */
    input[type=text], input[type=password], select { font-size: 16px; }
    .controls input[type=text], .controls select, .controls .search-wrap { width: auto; flex: 1 1 120px; }
    .controls .search-wrap { flex-basis: 100%; }
    .controls .search-wrap input[type=text] { width: 100%; }
    .film-cards, .service-cards { grid-template-columns: 1fr; }
    .bottom-nav {
      display: flex; position: sticky; bottom: 0; left: 0; right: 0; z-index: 20;
      margin: auto -12px -16px;
      background: var(--surface); border-top: 1px solid var(--hairline-strong);
      padding: 6px 4px calc(6px + env(safe-area-inset-bottom));
      box-shadow: 0 -2px 16px rgba(0, 0, 0, 0.4);
    }
    .bottom-nav-btn {
      flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px;
      padding: 6px 2px; background: none; border: none; color: var(--text-faint);
      font-size: 10px; font-weight: 500; cursor: pointer;
    }
    .bottom-nav-btn.active { color: var(--accent); }
    .bottom-nav-btn svg { width: 21px; height: 21px; stroke: currentColor; }
  }
</style>
</head>
<body>
<div class="status-bar-fill"></div>
<div class="header">
  <div>
    <h1>Watchlist streaming dashboard</h1>
    <div class="meta" id="meta"></div>
  </div>
  <div class="header-actions">
    <a class="watchlist-link" id="watchlistLink" target="_blank">View watchlist on Letterboxd ↗</a>
    <button class="icon-btn" id="settingsBtn" aria-label="Settings" title="Settings">⚙</button>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" id="tab-home">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 11l9-7 9 7"></path><path d="M5 10v10h14V10"></path>
    </svg>
    Home<span class="new-badge home-new-badge hidden"></span>
  </button>
  <button class="tab-btn" id="tab-country">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="9"></circle>
      <path d="M3 12h18M12 3c2.5 2.5 4 6 4 9s-1.5 6.5-4 9c-2.5-2.5-4-6-4-9s1.5-6.5 4-9z"></path>
    </svg>
    By VPN country
  </button>
  <button class="tab-btn" id="tab-services">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <ellipse cx="12" cy="5" rx="8" ry="3"></ellipse>
      <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"></path>
      <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"></path>
    </svg>
    By service
  </button>
  <button class="tab-btn" id="tab-films">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1.5"></rect>
      <rect x="14" y="3" width="7" height="7" rx="1.5"></rect>
      <rect x="3" y="14" width="7" height="7" rx="1.5"></rect>
      <rect x="14" y="14" width="7" height="7" rx="1.5"></rect>
    </svg>
    By film
  </button>
</div>

<section class="view active" id="view-home">
  <div class="surprise-bar">
    <button class="surprise-btn" id="surpriseMeBtn">🎲 Surprise me</button>
  </div>
  <div id="homeSections">
    <div class="film-cards">
      <div class="skeleton-card"></div>
      <div class="skeleton-card"></div>
      <div class="skeleton-card"></div>
      <div class="skeleton-card"></div>
    </div>
  </div>
</section>

<section class="view" id="view-country">
  <div class="controls">
    <select id="countrySelect"></select>
    <select id="countryServiceSelect"></select>
    <div class="search-wrap">
      <input type="text" id="countryFilmSearch" placeholder="Search title, year, director, cast...">
      <span class="search-clear hidden" id="countryFilmSearchClear">✕</span>
    </div>
    <select id="countrySortSelect">
      <option value="rating">Sort: Rating (highest)</option>
      <option value="title">Sort: Title (A–Z)</option>
      <option value="year">Sort: Year (newest)</option>
    </select>
    <span id="countryFilterToggles"></span>
  </div>
  <div class="active-filters" id="activeCountryFilters"></div>
  <div id="countryGrid" class="film-cards"></div>
</section>

<section class="view" id="view-services">
  <div class="controls">
    <select id="serviceSelect"></select>
    <select id="serviceCountrySelect"></select>
    <div class="search-wrap">
      <input type="text" id="serviceFilmSearch" placeholder="Search title, year, director, cast...">
      <span class="search-clear hidden" id="serviceFilmSearchClear">✕</span>
    </div>
    <select id="servicesSortSelect">
      <option value="film_count">Sort: # films (most)</option>
      <option value="brand">Sort: Service (A–Z)</option>
      <option value="unique_film_count">Sort: # unique (most)</option>
    </select>
    <span id="serviceFilterToggles"></span>
  </div>
  <div class="active-filters" id="activeServiceFilters"></div>
  <div id="servicesGrid" class="service-cards"></div>
</section>

<section class="view" id="view-service-detail">
  <button class="back-btn" id="backToServices">← Back to services</button>
  <h2 class="detail-title" id="serviceDetailTitle"></h2>
  <div id="serviceDetailCards"></div>
</section>

<section class="view" id="view-settings">
  <button class="back-btn" id="backFromSettings">← Back</button>
  <h2 class="detail-title">Settings</h2>
  <div class="settings-block">
    <h3 class="home-section-header">Letterboxd account</h3>
    <p id="settingsAccount" class="detail-meta"></p>
  </div>
  <div class="settings-block">
    <h3 class="home-section-header">Services marked "have"</h3>
    <p class="muted">Global services count as "have" everywhere (via VPN); a country's own list only counts there.</p>
    <div id="settingsServices"></div>
    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin: 4px 0 10px;">
      <button class="back-btn" id="saveServices" disabled style="margin-bottom:0;">No changes to save</button>
    </div>
    <p class="muted" id="servicesSaveStatus"></p>
  </div>
  <div class="settings-block">
    <h3 class="home-section-header">Refresh dashboard data</h3>
    <p class="muted">
      A new Letterboxd log already triggers this automatically within about 15 minutes — this button is
      just for forcing it sooner. Re-runs the daily check (watchlist, streaming availability,
      recommendations) and redeploys; takes about 5 minutes, then pull to refresh this page.
    </p>
    <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom: 10px;">
      <button class="back-btn" id="triggerRefresh" style="margin-bottom:0; color: var(--accent); border-color: var(--accent);">Refresh now</button>
    </div>
    <p class="muted" id="refreshStatus"></p>
  </div>
</section>

<section class="view" id="view-films">
  <div class="controls">
    <div class="search-wrap">
      <input type="text" id="search" placeholder="Search title, year, director, cast...">
      <span class="search-clear hidden" id="searchClear">✕</span>
    </div>
    <select id="filmsCountrySelect"></select>
    <select id="filmsGenreSelect"></select>
    <select id="filmsSortSelect">
      <option value="title">Sort: Title (A–Z)</option>
      <option value="year">Sort: Year (newest)</option>
      <option value="rating">Sort: Rating (highest)</option>
      <option value="coverage_countries">Sort: Most countries</option>
    </select>
    <label><input type="checkbox" id="notHaveOnly"> Only films not on a service I have</label>
  </div>
  <div class="active-filters" id="activeFilmFilters"></div>
  <div id="filmsGrid" class="film-cards"></div>
</section>

<nav class="bottom-nav">
  <button class="bottom-nav-btn active" id="nav-home">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 11l9-7 9 7"></path><path d="M5 10v10h14V10"></path>
    </svg>
    Home<span class="new-badge home-new-badge hidden"></span>
  </button>
  <button class="bottom-nav-btn" id="nav-country">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="9"></circle>
      <path d="M3 12h18M12 3c2.5 2.5 4 6 4 9s-1.5 6.5-4 9c-2.5-2.5-4-6-4-9s1.5-6.5 4-9z"></path>
    </svg>
    Country
  </button>
  <button class="bottom-nav-btn" id="nav-services">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <ellipse cx="12" cy="5" rx="8" ry="3"></ellipse>
      <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"></path>
      <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"></path>
    </svg>
    Services
  </button>
  <button class="bottom-nav-btn" id="nav-films">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1.5"></rect>
      <rect x="14" y="3" width="7" height="7" rx="1.5"></rect>
      <rect x="3" y="14" width="7" height="7" rx="1.5"></rect>
      <rect x="14" y="14" width="7" height="7" rx="1.5"></rect>
    </svg>
    Films
  </button>
</nav>

<div class="modal-overlay" id="quickLookOverlay">
  <div class="modal-card">
    <button class="modal-close" id="quickLookClose">✕</button>
    <div id="quickLookContent"></div>
  </div>
</div>

<div class="toast hidden" id="toast"></div>

<script>
const DATA = __DATA__;
const TABS = ['home', 'country', 'services', 'films'];

function esc(text) {
  if (text == null) return '';
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escAttr(text) {
  return esc(text).replace(/"/g, '&quot;');
}

function formatLastChecked(iso) {
  if (!iso) return 'never';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

document.getElementById('meta').innerHTML =
  DATA.films.length + ' films, ' + DATA.main_brands.length + ' main services ' +
  '<span class="info-icon" id="lastCheckedInfo">ⓘ<span class="info-tooltip">Last checked ' +
  esc(formatLastChecked(DATA.last_run_at)) + '</span></span>';
document.getElementById('lastCheckedInfo').addEventListener('click', event => {
  event.stopPropagation();
  event.currentTarget.classList.toggle('open');
});
document.addEventListener('click', () => {
  document.getElementById('lastCheckedInfo').classList.remove('open');
});
document.getElementById('watchlistLink').href = DATA.letterboxd_watchlist_url;

// have > free > could_get_again > subscription, always — used to order the
// "where to watch" badges on quick-look and service-detail cards.
const CLASSIFICATION_PRIORITY = { have: 0, free: 1, could_get_again: 2, subscription: 3 };
const CLASSIFICATIONS = ['have', 'free', 'could_get_again', 'subscription'];
const CLASSIFICATION_LABELS = { have: 'have', could_get_again: 'could get again', free: 'free', subscription: 'subscription needed' };

// Matches LEAVING_SOON_WINDOW_DAYS in dashboard.py — quick-look computes its
// own countdown against the viewer's clock rather than reusing the home
// section's server-baked one, so it stays accurate even days after the
// last refresh.
const LEAVING_SOON_WINDOW_DAYS = 30;
function daysUntil(dateStr) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const target = new Date(dateStr + 'T00:00:00');
  return Math.round((target - today) / 86400000);
}

// Shared pill-toggle filter row (Country and Services tabs) for the four
// have/could_get_again/free/subscription classifications.
function renderClassificationToggles(containerId, filterState, onChange) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  CLASSIFICATIONS.forEach(key => {
    const span = document.createElement('span');
    span.textContent = CLASSIFICATION_LABELS[key];
    span.classList.add('filter-toggle', 'badge', 'badge-' + key);
    span.addEventListener('click', () => {
      filterState[key] = !filterState[key];
      span.classList.toggle('off', !filterState[key]);
      onChange();
    });
    container.appendChild(span);
  });
}

function classificationBadgeLabel(key) {
  const label = CLASSIFICATION_LABELS[key];
  return label.charAt(0).toUpperCase() + label.slice(1);
}

// Searches title, year, director and starring cast so a query like "1994" or
// "Tarantino" or a lead actor's name all work from the same box.
function searchHaystack(row) {
  const starring = Array.isArray(row.starring) ? row.starring.join(' ') : (row.starring || '');
  return [row.title, row.year, row.director, starring].filter(Boolean).join(' ').toLowerCase();
}

// A filter/search combination with zero matches otherwise leaves a blank
// grid — indistinguishable from something being broken. Call after
// populating a grid container to fill that gap.
function ensureNotEmpty(container, message) {
  if (container.children.length) return;
  const p = document.createElement('p');
  p.className = 'muted';
  p.style.padding = '8px 0 24px';
  p.textContent = message;
  container.appendChild(p);
}

function wireSearchClear(inputId, clearId, rerender) {
  const input = document.getElementById(inputId);
  const btn = document.getElementById(clearId);
  const sync = () => btn.classList.toggle('hidden', !input.value);
  input.addEventListener('input', sync);
  btn.addEventListener('click', () => {
    input.value = '';
    sync();
    rerender();
    input.focus();
  });
  sync();
}

document.getElementById('tab-home').addEventListener('click', () => showView('home'));
document.getElementById('tab-country').addEventListener('click', () => showView('country'));
document.getElementById('tab-services').addEventListener('click', () => showView('services'));
document.getElementById('tab-films').addEventListener('click', () => showView('films'));
document.getElementById('nav-home').addEventListener('click', () => showView('home'));
document.getElementById('nav-country').addEventListener('click', () => showView('country'));
document.getElementById('nav-services').addEventListener('click', () => showView('services'));
document.getElementById('nav-films').addEventListener('click', () => showView('films'));
document.getElementById('backToServices').addEventListener('click', () => showView('services'));
document.getElementById('settingsBtn').addEventListener('click', () => { renderSettings(); showView('settings'); });
document.getElementById('backFromSettings').addEventListener('click', () => showView('home'));

// Every brand this watchlist has ever seen on JustWatch — the "entire
// domain" the settings-page autocomplete offers, since DATA.services
// (already loaded for the By-service tab) already has one row per
// (brand, country) pair.
const ALL_KNOWN_SERVICES = [...new Set(DATA.services.map(r => r.brand))].sort((a, b) => a.localeCompare(b));

let servicesBaseline = null;

function currentServicesState() {
  const global = [];
  const countries = {};
  document.querySelectorAll('.service-pills').forEach(el => {
    const key = el.dataset.key;
    const values = [...el.querySelectorAll('.service-pill')].map(p => p.dataset.value);
    if (key === 'global') {
      global.push(...values);
      return;
    }
    const [, code, field] = key.split(':');
    countries[code] = countries[code] || { subscriptions: [], free_tier: [] };
    countries[code][field] = values;
  });
  return { global, countries };
}

function updateSaveButtonState() {
  const btn = document.getElementById('saveServices');
  const changed = JSON.stringify(currentServicesState()) !== servicesBaseline;
  btn.disabled = !changed;
  btn.classList.toggle('has-changes', changed);
  btn.textContent = changed ? 'Save changes' : 'No changes to save';
}

function renderPills(container, values, pillClass) {
  container.innerHTML = '';
  values.forEach(value => {
    const pill = document.createElement('span');
    pill.className = 'service-pill pill-' + pillClass;
    pill.dataset.value = value;
    pill.innerHTML = esc(value) + ' <span class="service-pill-remove">✕</span>';
    pill.querySelector('.service-pill-remove').addEventListener('click', () => {
      pill.remove();
      updateSaveButtonState();
    });
    container.appendChild(pill);
  });
}

function addServicePill(container, value, pillClass) {
  const trimmed = value.trim();
  if (!trimmed) return;
  const values = [...container.querySelectorAll('.service-pill')].map(p => p.dataset.value);
  if (values.some(v => v.toLowerCase() === trimmed.toLowerCase())) return;
  values.push(trimmed);
  renderPills(container, values, pillClass);
  updateSaveButtonState();
}

function wireServiceAdd(wrap, pillsEl, pillClass) {
  const input = wrap.querySelector('.service-add-input');
  const suggestionsEl = wrap.querySelector('.service-suggestions');
  let activeIndex = -1;

  function selectValue(value) {
    addServicePill(pillsEl, value, pillClass);
    input.value = '';
    suggestionsEl.classList.add('hidden');
    input.focus();
  }

  function renderSuggestions() {
    const query = input.value.trim().toLowerCase();
    const existing = new Set([...pillsEl.querySelectorAll('.service-pill')].map(p => p.dataset.value.toLowerCase()));
    const matches = query
      ? ALL_KNOWN_SERVICES.filter(s => s.toLowerCase().includes(query) && !existing.has(s.toLowerCase())).slice(0, 8)
      : [];
    activeIndex = -1;
    suggestionsEl.innerHTML = '';
    matches.forEach(match => {
      const div = document.createElement('div');
      div.className = 'service-suggestion';
      div.textContent = match;
      div.dataset.value = match;
      div.addEventListener('mousedown', event => { event.preventDefault(); selectValue(match); });
      suggestionsEl.appendChild(div);
    });
    const trimmed = input.value.trim();
    if (trimmed && !ALL_KNOWN_SERVICES.some(s => s.toLowerCase() === trimmed.toLowerCase())) {
      const div = document.createElement('div');
      div.className = 'service-suggestion add-new';
      div.textContent = 'Add "' + trimmed + '"';
      div.dataset.value = trimmed;
      div.addEventListener('mousedown', event => { event.preventDefault(); selectValue(trimmed); });
      suggestionsEl.appendChild(div);
    }
    suggestionsEl.classList.toggle('hidden', suggestionsEl.children.length === 0);
  }

  input.addEventListener('input', renderSuggestions);
  input.addEventListener('focus', renderSuggestions);
  input.addEventListener('blur', () => setTimeout(() => suggestionsEl.classList.add('hidden'), 120));
  input.addEventListener('keydown', event => {
    const items = [...suggestionsEl.querySelectorAll('.service-suggestion')];
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      activeIndex = Math.min(activeIndex + 1, items.length - 1);
      items.forEach((item, i) => item.classList.toggle('active', i === activeIndex));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      items.forEach((item, i) => item.classList.toggle('active', i === activeIndex));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      if (activeIndex >= 0 && items[activeIndex]) {
        selectValue(items[activeIndex].dataset.value);
      } else if (input.value.trim()) {
        selectValue(input.value);
      }
    } else if (event.key === 'Escape') {
      suggestionsEl.classList.add('hidden');
    }
  });
}

function buildServiceEditor(label, key, values, pillClass) {
  const wrap = document.createElement('div');
  wrap.innerHTML =
    '<span class="settings-subgroup-label">' + esc(label) + '</span>' +
    '<div class="service-pills" data-key="' + esc(key) + '"></div>' +
    '<div class="service-add-wrap">' +
      '<input type="text" class="service-add-input" placeholder="Add a service..." autocomplete="off">' +
      '<div class="service-suggestions hidden"></div>' +
    '</div>';
  wrap.className = 'settings-subgroup';
  const pillsEl = wrap.querySelector('.service-pills');
  renderPills(pillsEl, values, pillClass);
  wireServiceAdd(wrap.querySelector('.service-add-wrap'), pillsEl, pillClass);
  return wrap;
}

function renderSettings() {
  document.getElementById('settingsAccount').innerHTML =
    '<a class="film-link" target="_blank" href="' + DATA.letterboxd_watchlist_url + '">' +
    esc(DATA.settings.letterboxd_username) + '</a>';

  const container = document.getElementById('settingsServices');
  container.innerHTML = '';
  document.getElementById('servicesSaveStatus').textContent = '';

  const globalGroup = document.createElement('div');
  globalGroup.className = 'settings-service-group';
  const globalHeading = document.createElement('h4');
  globalHeading.textContent = 'Global (any country via VPN)';
  globalGroup.appendChild(globalHeading);
  globalGroup.appendChild(buildServiceEditor('Subscriptions', 'global', DATA.settings.global_subscriptions, 'have'));
  container.appendChild(globalGroup);

  DATA.settings.countries.forEach(c => {
    const group = document.createElement('div');
    group.className = 'settings-country-group';
    const heading = document.createElement('h4');
    heading.className = 'settings-country-name';
    heading.textContent = c.name;
    group.appendChild(heading);
    group.appendChild(buildServiceEditor('Subscriptions', 'country:' + c.code + ':subscriptions', c.subscriptions, 'have'));
    group.appendChild(buildServiceEditor('Free tier', 'country:' + c.code + ':free_tier', c.free_tier, 'free'));
    container.appendChild(group);
  });

  servicesBaseline = JSON.stringify(currentServicesState());
  updateSaveButtonState();
}

document.getElementById('saveServices').addEventListener('click', async () => {
  const status = document.getElementById('servicesSaveStatus');
  const payload = currentServicesState();

  status.textContent = 'Saving...';
  try {
    const response = await fetch(DATA.settings.refresh_worker_url + '/update-services', {
      method: 'POST',
      headers: {
        'X-Trigger-Secret': DATA.settings.refresh_trigger_secret,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
    const body = await response.json().catch(() => null);
    if (response.ok && body && body.ok) {
      status.textContent = 'Saved — refreshing with the new config now, usually takes about 5 minutes.';
      servicesBaseline = JSON.stringify(currentServicesState());
      updateSaveButtonState();
    } else {
      const detail = body && body.error ? ': ' + body.error : '';
      status.textContent = 'Unexpected response (' + response.status + detail + ').';
    }
  } catch (error) {
    status.textContent = 'Request failed: ' + error.message;
  }
});

document.getElementById('triggerRefresh').addEventListener('click', async () => {
  const status = document.getElementById('refreshStatus');
  status.textContent = 'Triggering refresh...';
  try {
    const response = await fetch(DATA.settings.refresh_worker_url, {
      method: 'POST',
      headers: {
        'X-Trigger-Secret': DATA.settings.refresh_trigger_secret,
        'Content-Type': 'application/json',
      },
    });
    const body = await response.json().catch(() => null);
    if (response.ok && body && body.ok) {
      status.textContent = 'Refresh triggered — usually takes about 5 minutes. Pull to refresh this page once done.';
    } else {
      status.textContent = 'Unexpected response (' + response.status + ').';
    }
  } catch (error) {
    status.textContent = 'Request failed: ' + error.message;
  }
});

function showView(name) {
  document.querySelectorAll('section.view').forEach(el => el.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  TABS.forEach(n => {
    const isActive = n === name || (name === 'service-detail' && n === 'services');
    document.getElementById('tab-' + n).classList.toggle('active', isActive);
    document.getElementById('nav-' + n).classList.toggle('active', isActive);
  });
  // All tabs share one page-level scroll (only one section is visible at a
  // time), so switching tabs without resetting scroll left whatever the
  // previous tab was scrolled to still in effect on the new one.
  window.scrollTo(0, 0);
}

// Shared tile-card shell for the Films and Country tabs — poster + title/year/
// rating/director up top, with per-tab service badges passed in as HTML so a
// user never has to scroll a table sideways to see where a film streams.
// Clicking the card (outside the title link or a filter badge) opens a quick
// look with the film's synopsis and full cast.
function filmCardShell(row, servicesHtml) {
  const year = row.year ? ' (' + row.year + ')' : '';
  const rating = row.rating != null ? row.rating.toFixed(2) + '★' : '—';
  const poster = row.poster_url
    ? '<img class="poster-thumb" loading="lazy" src="' + row.poster_url + '" onerror="this.outerHTML=\\'<div class=&quot;poster-placeholder&quot;></div>\\'">'
    : '<div class="poster-placeholder"></div>';
  const director = row.director ? '<div class="film-card-director">' + esc(row.director) + '</div>' : '';
  const genre = (row.genre && row.genre.length)
    ? '<div class="film-card-genre">' + esc(row.genre.join(', ')) + '</div>' : '';
  const addedService = row.added_service
    ? '<div class="film-card-added-service">Added to ' + esc(row.added_service) + '</div>' : '';
  const leavingNote = row.leaving_note
    ? '<div class="film-card-leaving-note">' + esc(row.leaving_note) + '</div>' : '';
  const div = document.createElement('div');
  div.className = 'film-card';
  div.dataset.slug = row.slug;
  div.innerHTML = poster +
    '<div class="film-card-body">' +
      '<div class="film-card-title-row">' +
        '<a class="film-link film-card-title" target="_blank" href="https://letterboxd.com/film/' + row.slug + '/">' +
          esc(row.title) + year + '</a>' +
        '<span class="film-card-end"><span class="film-card-rating">' + rating + '</span></span>' +
      '</div>' +
      director + genre + addedService + leavingNote + servicesHtml +
    '</div>';
  return div;
}

function countryLabel(code) {
  return (DATA.countryNames && DATA.countryNames[code]) || code;
}

// A service on every VPN-reachable country (or a film with a dozen credited
// directors) turns one card into a wall of badges and breaks the grid's
// height rhythm — cap the inline list and let a "+N more" reveal the rest
// on demand instead of dropping the data entirely.
const BADGE_CAP = 8;

function capBadges(badgeParts, cap) {
  if (badgeParts.length <= cap) return badgeParts.join(' ');
  const id = 'more-' + Math.random().toString(36).slice(2, 9);
  return badgeParts.slice(0, cap).join(' ') +
    ' <span class="badge-more-wrap">' +
      '<span class="badges-hidden" id="' + id + '" hidden>' + badgeParts.slice(cap).join(' ') + '</span>' +
      '<button type="button" class="badge-more-btn" data-target="' + id + '">+' + (badgeParts.length - cap) + ' more</button>' +
    '</span>';
}

document.addEventListener('click', event => {
  const btn = event.target.closest('.badge-more-btn');
  if (!btn) return;
  event.stopPropagation();
  const target = document.getElementById(btn.getAttribute('data-target'));
  if (target) target.hidden = false;
  btn.remove();
});

function badgeHtml(entries, brandLabel) {
  const parts = entries.map(e => {
    const label = brandLabel ? brandLabel : esc(countryLabel(e.country));
    return '<span class="badge badge-' + e.classification + '" data-country="' + e.country + '">' + label + '</span>';
  });
  return capBadges(parts, BADGE_CAP);
}

// ---------- Home ----------

// "Not interested" only ever applies to a pure discovery pick — a film not
// already on the watchlist — since a real watchlist film's presence in
// leaving_soon/recently_added/top_rated reflects the watchlist itself, not
// a recommendation choice. DATA.films_by_slug is the *merged* watchlist +
// discovery lookup (quick-look needs both in one place), so it can't tell
// the two apart — DATA.films is watchlist-only, which can.
const WATCHLIST_SLUGS = new Set(DATA.films.map(f => f.slug));
function isDiscoveryOnly(slug) {
  return !WATCHLIST_SLUGS.has(slug);
}

let toastTimer = null;
function showToast(message) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add('hidden'), 4000);
}

function dismissRecommendation(slug, cardEl) {
  cardEl.classList.add('dismissing');
  setTimeout(() => cardEl.remove(), 200);
  fetch(DATA.settings.refresh_worker_url + '/dismiss-recommendation', {
    method: 'POST',
    headers: {
      'X-Trigger-Secret': DATA.settings.refresh_trigger_secret,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ slug }),
  })
    .then(response => response.json().catch(() => null))
    .then(body => {
      if (!body || !body.ok) showToast('Hidden for now, but saving that preference failed — it may reappear tomorrow.');
    })
    .catch(() => showToast('Hidden for now, but saving that preference failed — it may reappear tomorrow.'));
}

function addDismissButton(cardEl, slug) {
  if (!isDiscoveryOnly(slug)) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'dismiss-btn';
  btn.title = 'Not interested — hide from future recommendations';
  btn.textContent = '✕';
  btn.addEventListener('click', event => {
    event.stopPropagation();
    dismissRecommendation(slug, cardEl);
  });
  // Lives next to the rating (inside the flex title row) rather than
  // absolutely positioned over the card — that used to land directly on
  // top of the rating badge since both anchored to the same top-right
  // corner.
  const end = cardEl.querySelector('.film-card-end');
  (end || cardEl).appendChild(btn);
}

function renderHome() {
  const container = document.getElementById('homeSections');
  container.innerHTML = '';
  DATA.home_sections.forEach(section => {
    const wrap = document.createElement('div');
    wrap.className = 'home-section';
    const header = document.createElement('h2');
    header.className = 'home-section-header';
    header.textContent = section.header;
    wrap.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'film-cards';
    section.films.forEach(film => {
      const card = filmCardShell(film, '');
      addDismissButton(card, film.slug);
      grid.appendChild(card);
    });
    wrap.appendChild(grid);

    container.appendChild(wrap);
  });
}

document.getElementById('homeSections').addEventListener('click', event => {
  if (event.target.closest('a.film-link') || event.target.closest('.dismiss-btn')) return;
  const card = event.target.closest('.film-card');
  if (card) openQuickLook(card.dataset.slug);
});

// Decision paralysis, not lack of options, is the actual problem with 354
// films — pick one at random from a "good enough right now" pool rather
// than showing yet another ranked list. Prefers films you actually have on
// a service, rated 3.5+; widens the pool only if that's empty so it can
// never come up blank.
const SURPRISE_RATING_FLOOR = 3.5;

function surprisePool() {
  const rated = r => r.rating != null && r.rating >= SURPRISE_RATING_FLOOR;
  let pool = DATA.films.filter(r => r.have_service && rated(r));
  if (!pool.length) pool = DATA.films.filter(r => r.have_service);
  if (!pool.length) pool = DATA.films.filter(r => r.any_service && rated(r));
  if (!pool.length) pool = DATA.films.filter(r => r.any_service);
  if (!pool.length) pool = DATA.films;
  return pool;
}

document.getElementById('surpriseMeBtn').addEventListener('click', () => {
  const pool = surprisePool();
  if (!pool.length) return;
  const pick = pool[Math.floor(Math.random() * pool.length)];
  showView('home');
  openQuickLook(pick.slug);
});

// ---------- Film detail card (shared: quick look + service detail) ----------

function buildFilmDetailCard(film, excludeBrand, excludeCountry, collapsible) {
  const div = document.createElement('div');
  div.className = 'detail-card';
  const year = film.year ? ' (' + film.year + ')' : '';
  const rating = film.rating != null ? film.rating.toFixed(2) + '★' : '—';
  const poster = film.poster_url
    ? '<img class="detail-poster" loading="lazy" src="' + film.poster_url + '">'
    : '<div class="detail-poster-placeholder"></div>';
  const director = film.director ? '<p class="detail-meta"><strong>Director:</strong> ' + esc(film.director) + '</p>' : '';
  const starring = (film.starring && film.starring.length)
    ? '<p class="detail-meta"><strong>Starring:</strong> ' + esc(film.starring.join(', ')) + '</p>' : '';
  const genreLine = (film.genre && film.genre.length)
    ? '<p class="detail-meta"><strong>Genre:</strong> ' + esc(film.genre.join(', ')) + '</p>' : '';
  const synopsis = film.synopsis ? '<p class="detail-synopsis">' + esc(film.synopsis) + '</p>' : '';

  // Offers a real JustWatch url turn into an actual link (badge-link) that
  // opens straight into the streaming service — otherwise it's just a
  // static label, same as before.
  function offerBadgeHtml(o, extraClass) {
    const label = esc(o.brand) + ' <i>' + esc(countryLabel(o.country)) + '</i>';
    const cls = 'badge badge-' + o.classification + (extraClass ? ' ' + extraClass : '');
    if (!o.url) return '<span class="' + cls + '">' + label + '</span>';
    return '<a class="' + cls + ' badge-link" href="' + escAttr(o.url) +
      '" target="_blank" rel="noopener">' + label + ' ↗</a>';
  }

  const primaryOffer = excludeBrand
    ? film.all_offers.find(o => o.brand === excludeBrand && o.country === excludeCountry)
    : null;
  const primaryHtml = (primaryOffer && primaryOffer.url)
    ? '<p class="detail-meta">' + offerBadgeHtml(primaryOffer, 'watch-now-btn') + '</p>'
    : '';

  const others = film.all_offers
    .filter(o => !(o.brand === excludeBrand && o.country === excludeCountry))
    .slice()
    .sort((a, b) => CLASSIFICATION_PRIORITY[a.classification] - CLASSIFICATION_PRIORITY[b.classification]);
  const otherHtml = others.length
    ? capBadges(others.map(offerBadgeHtml), BADGE_CAP)
    : '<span class="muted">Not available anywhere else tracked</span>';

  // Computed against the viewer's own clock (not baked in at generation
  // time) so the countdown is still accurate days after the last refresh.
  // have/free only — losing a could_get_again offer isn't "you're about to
  // lose access", since you don't currently have it via that route anyway.
  const expiring = film.all_offers
    .filter(o => (o.classification === 'have' || o.classification === 'free') && o.available_to)
    .map(o => ({ ...o, daysLeft: daysUntil(o.available_to) }))
    .filter(o => o.daysLeft >= 0 && o.daysLeft <= LEAVING_SOON_WINDOW_DAYS)
    .sort((a, b) => a.daysLeft - b.daysLeft);
  const expiringHtml = expiring.length
    ? '<div class="expiring-notes">' + expiring.map(o => {
        const when = o.daysLeft === 0 ? 'today' : o.daysLeft === 1 ? 'tomorrow' : 'in ' + o.daysLeft + ' days';
        return '<p class="expiring-note">Leaving ' + esc(o.brand) + ' <i>' + esc(countryLabel(o.country)) + '</i> ' + when + '</p>';
      }).join('') + '</div>'
    : '';

  const otherLabel = excludeBrand ? 'Other services' : 'Where to watch';
  div.innerHTML =
    '<div style="flex-shrink:0;">' + poster + '</div>' +
    '<div class="detail-body">' +
      '<a class="film-link" target="_blank" href="https://letterboxd.com/film/' + film.slug + '/"><h3>' + esc(film.title) + year + '</h3></a>' +
      '<p class="detail-rating">' + rating + '</p>' +
      director + starring + genreLine + synopsis + primaryHtml +
      '<div class="other-services-section">' +
        '<p class="detail-meta"><strong>' + otherLabel + '</strong></p>' +
        '<div class="badge-wrap">' + otherHtml + '</div>' +
        expiringHtml +
      '</div>' +
    '</div>';

  if (collapsible) {
    div.classList.add('collapsible');
    div.addEventListener('click', event => {
      if (event.target.closest('a.film-link') || event.target.closest('a.badge-link')) return;
      div.classList.toggle('expanded');
    });
  }
  return div;
}

// ---------- Quick look modal (Films + Country card click) ----------

function openQuickLook(slug) {
  const film = DATA.films_by_slug[slug];
  if (!film) return;
  const content = document.getElementById('quickLookContent');
  content.innerHTML = '';
  content.appendChild(buildFilmDetailCard(film, null, null));
  document.getElementById('quickLookOverlay').classList.add('active');
}

function closeQuickLook() {
  document.getElementById('quickLookOverlay').classList.remove('active');
}

document.getElementById('quickLookClose').addEventListener('click', closeQuickLook);
document.getElementById('quickLookOverlay').addEventListener('click', event => {
  if (event.target.id === 'quickLookOverlay') closeQuickLook();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') closeQuickLook();
});

// ---------- Films cards ----------

const filmCols = [
  { key: 'title', sort: r => r.title.toLowerCase(), dir: 1 },
  { key: 'year', sort: r => r.year || 0, dir: -1 },
  { key: 'rating', sort: r => r.rating == null ? -1 : r.rating, dir: -1 },
  { key: 'coverage_countries', sort: r => r.coverage_countries, dir: -1 },
];

let filmSortKey = 'title', filmSortDir = 1;
let activeCountry = null;
let activeService = null;
let activeGenre = null;

function baseFilteredFilms() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  const notHaveOnly = document.getElementById('notHaveOnly').checked;
  return DATA.films.filter(row => {
    if (q && !searchHaystack(row).includes(q)) return false;
    if (notHaveOnly && row.have_service) return false;
    if (activeService && !row.main[activeService]) return false;
    return true;
  });
}

function genreCountsFromRows(rows) {
  const counts = {};
  rows.forEach(row => (row.genre || []).forEach(g => { counts[g] = (counts[g] || 0) + 1; }));
  return counts;
}

function updateFilmsGenreSelect(counts) {
  const select = document.getElementById('filmsGenreSelect');
  select.innerHTML = '';
  const allOpt = document.createElement('option');
  allOpt.value = '';
  allOpt.textContent = 'Focus on a genre...';
  select.appendChild(allOpt);

  const ranked = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  if (activeGenre && !counts[activeGenre]) ranked.push([activeGenre, 0]);
  ranked.forEach(([genre, count]) => {
    const opt = document.createElement('option');
    opt.value = genre;
    opt.textContent = genre + ' (' + count + ')';
    select.appendChild(opt);
  });
  select.value = activeGenre || '';
}

function countryCountsFromRows(rows) {
  const counts = {};
  rows.forEach(row => {
    const withHave = new Set();
    Object.values(row.main).forEach(entries => {
      entries.forEach(e => { if (e.classification === 'have') withHave.add(e.country); });
    });
    withHave.forEach(c => { counts[c] = (counts[c] || 0) + 1; });
  });
  return counts;
}

function updateFilmsCountrySelect(counts) {
  const select = document.getElementById('filmsCountrySelect');
  select.innerHTML = '';
  const allOpt = document.createElement('option');
  allOpt.value = '';
  allOpt.textContent = 'Focus on a country...';
  select.appendChild(allOpt);

  const ranked = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  // A badge click can set activeCountry to a country with zero "have" films
  // (e.g. a free/subscription-only market) — keep it selectable rather than
  // silently clearing the filter just because it isn't have-ranked.
  if (activeCountry && !counts[activeCountry]) {
    ranked.push([activeCountry, 0]);
  }
  ranked.forEach(([code, count]) => {
    const opt = document.createElement('option');
    opt.value = code;
    opt.textContent = (DATA.countryNames && DATA.countryNames[code] || code) + ' (' + count + ')';
    select.appendChild(opt);
  });
  select.value = activeCountry || '';
}

function renderActiveFilmFilters() {
  const container = document.getElementById('activeFilmFilters');
  container.innerHTML = '';
  const searchVal = document.getElementById('search').value.trim();
  const notHaveOnly = document.getElementById('notHaveOnly').checked;
  if (!activeCountry && !activeService && !activeGenre && !searchVal && !notHaveOnly) return;
  if (activeCountry) {
    const name = (DATA.countryNames && DATA.countryNames[activeCountry]) || activeCountry;
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = name + ' ✕';
    chip.addEventListener('click', () => { activeCountry = null; renderFilms(); });
    container.appendChild(chip);
  }
  if (activeGenre) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = activeGenre + ' ✕';
    chip.addEventListener('click', () => { activeGenre = null; renderFilms(); });
    container.appendChild(chip);
  }
  if (activeService) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = activeService + ' ✕';
    chip.addEventListener('click', () => { activeService = null; renderFilms(); });
    container.appendChild(chip);
  }
  if (searchVal) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = '"' + searchVal + '" ✕';
    chip.addEventListener('click', () => {
      document.getElementById('search').value = '';
      document.getElementById('searchClear').classList.add('hidden');
      renderFilms();
    });
    container.appendChild(chip);
  }
  if (notHaveOnly) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = 'Not on a service I have ✕';
    chip.addEventListener('click', () => { document.getElementById('notHaveOnly').checked = false; renderFilms(); });
    container.appendChild(chip);
  }
  const clearAll = document.createElement('span');
  clearAll.className = 'filter-chip clear-all-chip';
  clearAll.textContent = 'Clear all ✕';
  clearAll.addEventListener('click', () => {
    activeCountry = null;
    activeService = null;
    activeGenre = null;
    document.getElementById('search').value = '';
    document.getElementById('searchClear').classList.add('hidden');
    document.getElementById('notHaveOnly').checked = false;
    renderFilms();
  });
  container.appendChild(clearAll);
}

function renderFilms() {
  const preGenre = baseFilteredFilms();
  updateFilmsGenreSelect(genreCountsFromRows(preGenre));
  const base = activeGenre ? preGenre.filter(row => (row.genre || []).includes(activeGenre)) : preGenre;
  updateFilmsCountrySelect(countryCountsFromRows(base));
  renderActiveFilmFilters();

  // Clicking a service header narrows the column set to just that service
  // (symmetric with clicking a country badge narrowing to that country) —
  // "filter for that service only" means the field set narrows, not just
  // the row set.
  const candidateBrands = activeService ? [activeService] : DATA.main_brands;
  const showOtherServices = !activeService;

  const processed = [];
  const visibleBrands = new Set();

  base.forEach(row => {
    const visibleMain = {};
    candidateBrands.forEach(brand => {
      const entries = row.main[brand];
      if (!entries) return;
      const filtered = activeCountry ? entries.filter(e => e.country === activeCountry) : entries;
      if (filtered.length) visibleMain[brand] = filtered;
    });
    const visibleOther = showOtherServices
      ? (activeCountry ? row.other_services.filter(o => o.country === activeCountry) : row.other_services)
      : [];

    const include = Object.keys(visibleMain).length > 0 || visibleOther.length > 0;
    if (!include) return;
    Object.keys(visibleMain).forEach(b => visibleBrands.add(b));
    processed.push({ row, visibleMain, visibleOther });
  });

  const columnBrands = activeCountry
    ? candidateBrands.filter(b => visibleBrands.has(b))
    : candidateBrands;

  const col = filmCols.find(c => c.key === filmSortKey);
  if (col) {
    processed.sort((a, b) => {
      const av = col.sort(a.row), bv = col.sort(b.row);
      return av < bv ? -filmSortDir : av > bv ? filmSortDir : 0;
    });
  }

  renderFilmCards(processed, columnBrands, showOtherServices);
}

function onBadgeDelegateClick(event) {
  const brandEl = event.target.closest('[data-brand]');
  if (brandEl) {
    const brand = brandEl.getAttribute('data-brand');
    activeService = (activeService === brand) ? null : brand;
    renderFilms();
    return;
  }
  const badge = event.target.closest('[data-country]');
  if (badge) {
    const code = badge.getAttribute('data-country');
    activeCountry = (activeCountry === code) ? null : code;
    renderFilms();
    return;
  }
  if (event.target.closest('a.film-link')) return;
  const card = event.target.closest('.film-card');
  if (card) openQuickLook(card.dataset.slug);
}

function renderFilmCards(processed, columnBrands, showOtherServices) {
  const container = document.getElementById('filmsGrid');
  container.innerHTML = '';

  const frag = document.createDocumentFragment();
  processed.forEach(({ row, visibleMain, visibleOther }) => {
    let servicesHtml = '';

    columnBrands.forEach(brand => {
      const entries = visibleMain[brand];
      if (!entries || !entries.length) return;
      const activeCls = activeService === brand ? ' active' : '';
      servicesHtml += '<div class="service-group">' +
        '<span class="service-group-name' + activeCls + '" data-brand="' + esc(brand) + '">' + esc(brand) + '</span>' +
        badgeHtml(entries, null) +
      '</div>';
    });

    if (showOtherServices && visibleOther.length) {
      const otherBadges = visibleOther.map(o =>
        '<span class="badge badge-' + o.classification + '" data-country="' + o.country + '">' + esc(o.brand) + ' (' + esc(countryLabel(o.country)) + ')</span>'
      );
      servicesHtml += '<div class="service-group">' +
        '<span class="service-group-name">Other</span>' +
        capBadges(otherBadges, BADGE_CAP) +
      '</div>';
    }

    frag.appendChild(filmCardShell(row, servicesHtml));
  });
  container.appendChild(frag);
  ensureNotEmpty(container, 'No films match your search and filters.');
}

document.getElementById('search').addEventListener('input', renderFilms);
wireSearchClear('search', 'searchClear', renderFilms);
document.getElementById('notHaveOnly').addEventListener('change', renderFilms);
document.getElementById('filmsCountrySelect').addEventListener('change', e => {
  activeCountry = e.target.value || null;
  renderFilms();
});
document.getElementById('filmsGenreSelect').addEventListener('change', e => {
  activeGenre = e.target.value || null;
  renderFilms();
});
document.getElementById('filmsSortSelect').addEventListener('change', e => {
  filmSortKey = e.target.value;
  filmSortDir = filmCols.find(c => c.key === filmSortKey).dir;
  renderFilms();
});
document.getElementById('filmsGrid').addEventListener('click', onBadgeDelegateClick);

// ---------- Services cards ----------

const serviceCols = [
  { key: 'brand', sort: r => r.brand.toLowerCase(), dir: 1 },
  { key: 'film_count', sort: r => r.film_count, dir: -1 },
  { key: 'unique_film_count', sort: r => r.unique_film_count, dir: -1 },
];
const serviceFilterState = { have: true, could_get_again: true, free: true, subscription: true };

let serviceSortKey = 'film_count', serviceSortDir = -1;

function populateServiceSelects() {
  // Services you have/could get again, plus any free service in your three
  // home markets, are what you'd actually reach for — surfaced in their own
  // group above the long tail of everything else this film happens to be on.
  const topBrands = new Set(
    DATA.services
      .filter(r => r.classification === 'have' || r.classification === 'could_get_again' ||
                   (r.classification === 'free' && ['AU', 'GB', 'US'].includes(r.country)))
      .map(r => r.brand)
  );
  const serviceNames = [...new Set(DATA.services.map(r => r.brand))].sort((a, b) => a.localeCompare(b));
  const topNames = serviceNames.filter(n => topBrands.has(n));
  const restNames = serviceNames.filter(n => !topBrands.has(n));
  const countryNames = [...new Set(DATA.services.map(r => r.country_name))].sort((a, b) => a.localeCompare(b));

  const buildOptions = names => names.map(n => '<option value="' + esc(n) + '">' + esc(n) + '</option>').join('');
  const serviceSelect = document.getElementById('serviceSelect');
  serviceSelect.innerHTML = '<option value="">All services</option>';
  if (topNames.length) {
    const topGroup = document.createElement('optgroup');
    topGroup.label = 'Have or can get';
    topGroup.innerHTML = buildOptions(topNames);
    serviceSelect.appendChild(topGroup);
  }
  if (restNames.length) {
    const restGroup = document.createElement('optgroup');
    restGroup.label = 'Other services';
    restGroup.innerHTML = buildOptions(restNames);
    serviceSelect.appendChild(restGroup);
  }

  const countrySelect = document.getElementById('serviceCountrySelect');
  countrySelect.innerHTML = '<option value="">All countries</option>' +
    countryNames.map(n => '<option value="' + esc(n) + '">' + esc(n) + '</option>').join('');
}

function renderActiveServiceFilters() {
  const container = document.getElementById('activeServiceFilters');
  container.innerHTML = '';
  const serviceQ = document.getElementById('serviceSelect').value;
  const countryQ = document.getElementById('serviceCountrySelect').value;
  const filmQ = document.getElementById('serviceFilmSearch').value.trim();
  const anyToggleOff = CLASSIFICATIONS.some(k => !serviceFilterState[k]);
  if (!serviceQ && !countryQ && !filmQ && !anyToggleOff) return;

  if (serviceQ) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = serviceQ + ' ✕';
    chip.addEventListener('click', () => { document.getElementById('serviceSelect').value = ''; renderServicesRows(); });
    container.appendChild(chip);
  }
  if (countryQ) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = countryQ + ' ✕';
    chip.addEventListener('click', () => { document.getElementById('serviceCountrySelect').value = ''; renderServicesRows(); });
    container.appendChild(chip);
  }
  if (filmQ) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = '"' + filmQ + '" ✕';
    chip.addEventListener('click', () => {
      document.getElementById('serviceFilmSearch').value = '';
      document.getElementById('serviceFilmSearchClear').classList.add('hidden');
      renderServicesRows();
    });
    container.appendChild(chip);
  }
  if (anyToggleOff) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = 'Type filters ✕';
    chip.addEventListener('click', () => {
      CLASSIFICATIONS.forEach(k => { serviceFilterState[k] = true; });
      renderServiceFilterToggles();
      renderServicesRows();
    });
    container.appendChild(chip);
  }
  const clearAll = document.createElement('span');
  clearAll.className = 'filter-chip clear-all-chip';
  clearAll.textContent = 'Clear all ✕';
  clearAll.addEventListener('click', () => {
    document.getElementById('serviceSelect').value = '';
    document.getElementById('serviceCountrySelect').value = '';
    document.getElementById('serviceFilmSearch').value = '';
    document.getElementById('serviceFilmSearchClear').classList.add('hidden');
    CLASSIFICATIONS.forEach(k => { serviceFilterState[k] = true; });
    renderServiceFilterToggles();
    renderServicesRows();
  });
  container.appendChild(clearAll);
}

function renderServiceFilterToggles() {
  renderClassificationToggles('serviceFilterToggles', serviceFilterState, renderServicesRows);
}

function renderServicesRows() {
  const container = document.getElementById('servicesGrid');
  container.innerHTML = '';
  const serviceQ = document.getElementById('serviceSelect').value;
  const countryQ = document.getElementById('serviceCountrySelect').value;
  const filmQ = document.getElementById('serviceFilmSearch').value.trim().toLowerCase();
  renderActiveServiceFilters();

  let rows = DATA.services.slice();
  const col = serviceCols.find(c => c.key === serviceSortKey);
  // Services you have/can-get-again always lead, regardless of the chosen
  // sort column — otherwise a big subscription-needed catalog in a market
  // you don't use can outrank what you actually have, just on film count.
  rows.sort((a, b) => {
    const clsDiff = CLASSIFICATION_PRIORITY[a.classification] - CLASSIFICATION_PRIORITY[b.classification];
    if (clsDiff !== 0) return clsDiff;
    if (!col || !col.sort) return 0;
    const av = col.sort(a), bv = col.sort(b);
    return av < bv ? -serviceSortDir : av > bv ? serviceSortDir : 0;
  });

  const frag = document.createDocumentFragment();
  rows.forEach(row => {
    if (serviceQ && row.brand !== serviceQ) return;
    if (countryQ && row.country_name !== countryQ) return;
    if (filmQ && !row.slugs.some(s => searchHaystack(DATA.films_by_slug[s]).includes(filmQ))) return;
    if (!serviceFilterState[row.classification]) return;

    const card = document.createElement('div');
    card.className = 'service-card';
    card.innerHTML =
      '<div class="service-card-head">' +
        '<span class="service-card-name">' + esc(row.brand) + '<i>' + esc(row.country_name) + '</i></span>' +
        '<span class="badge badge-' + row.classification + '">' + classificationBadgeLabel(row.classification) + '</span>' +
      '</div>' +
      '<div class="service-card-stats">' + row.film_count + ' films tracked · ' + row.unique_film_count + ' unique</div>';
    card.addEventListener('click', () => openServiceDetail(row.brand, row.country, row.country_name));
    frag.appendChild(card);
  });
  container.appendChild(frag);
  ensureNotEmpty(container, 'No services match your search and filters.');
}

function openServiceDetail(brand, country, countryName) {
  const row = DATA.services.find(r => r.brand === brand && r.country === country);
  document.getElementById('serviceDetailTitle').innerHTML = esc(brand) + ' <i>' + esc(countryName) + '</i>';
  const container = document.getElementById('serviceDetailCards');
  container.innerHTML = '';
  row.slugs.forEach(slug => {
    const film = DATA.films_by_slug[slug];
    container.appendChild(buildFilmDetailCard(film, brand, country, true));
  });
  showView('service-detail');
}

document.getElementById('serviceSelect').addEventListener('change', renderServicesRows);
document.getElementById('serviceCountrySelect').addEventListener('change', renderServicesRows);
document.getElementById('serviceFilmSearch').addEventListener('input', renderServicesRows);
wireSearchClear('serviceFilmSearch', 'serviceFilmSearchClear', renderServicesRows);
document.getElementById('servicesSortSelect').addEventListener('change', e => {
  serviceSortKey = e.target.value;
  serviceSortDir = serviceCols.find(c => c.key === serviceSortKey).dir;
  renderServicesRows();
});

populateServiceSelects();
renderServiceFilterToggles();
renderServicesRows();

// ---------- By VPN country ----------

const countryCols = [
  { key: 'title', sort: r => r.title.toLowerCase(), dir: 1 },
  { key: 'year', sort: r => r.year || 0, dir: -1 },
  { key: 'rating', sort: r => r.rating == null ? -1 : r.rating, dir: -1 },
];

const countryFilterState = { have: true, could_get_again: true, free: true, subscription: true };

let countrySortKey = 'rating', countrySortDir = -1;

function populateCountrySelect() {
  const select = document.getElementById('countrySelect');

  const byFilmCount = DATA.countries.slice().sort((a, b) => b.films.length - a.films.length || a.name.localeCompare(b.name));
  const top = byFilmCount.slice(0, 10);
  const topCodes = new Set(top.map(c => c.code));
  const rest = DATA.countries.filter(c => !topCodes.has(c.code)); // DATA.countries is already name-sorted

  const addOptions = (label, countries) => {
    if (!countries.length) return;
    const group = document.createElement('optgroup');
    group.label = label;
    countries.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.code;
      opt.textContent = c.name + ' (' + c.films.length + ' films)';
      group.appendChild(opt);
    });
    select.appendChild(group);
  };
  addOptions('Most films on your watchlist', top);
  addOptions('All other countries', rest);

  if (top.length) select.value = top[0].code;
}

function populateCountryServiceSelect() {
  const country = currentCountry();
  const select = document.getElementById('countryServiceSelect');
  const previous = select.value;
  const services = country ? [...new Set(country.films.flatMap(f => f.services.map(s => s.brand)))].sort((a, b) => a.localeCompare(b)) : [];
  select.innerHTML = '<option value="">All services</option>' +
    services.map(s => '<option value="' + esc(s) + '">' + esc(s) + '</option>').join('');
  if (services.includes(previous)) select.value = previous;
}

function renderCountryFilterToggles() {
  renderClassificationToggles('countryFilterToggles', countryFilterState, renderCountryRows);
}

function currentCountry() {
  const code = document.getElementById('countrySelect').value;
  return DATA.countries.find(c => c.code === code);
}

function renderActiveCountryFilters() {
  const container = document.getElementById('activeCountryFilters');
  container.innerHTML = '';
  const q = document.getElementById('countryFilmSearch').value.trim();
  const serviceQ = document.getElementById('countryServiceSelect').value;
  const anyToggleOff = CLASSIFICATIONS.some(k => !countryFilterState[k]);
  if (!q && !serviceQ && !anyToggleOff) return;

  if (q) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = '"' + q + '" ✕';
    chip.addEventListener('click', () => {
      document.getElementById('countryFilmSearch').value = '';
      document.getElementById('countryFilmSearchClear').classList.add('hidden');
      renderCountryRows();
    });
    container.appendChild(chip);
  }
  if (serviceQ) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = serviceQ + ' ✕';
    chip.addEventListener('click', () => { document.getElementById('countryServiceSelect').value = ''; renderCountryRows(); });
    container.appendChild(chip);
  }
  if (anyToggleOff) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = 'Availability filters ✕';
    chip.addEventListener('click', () => {
      CLASSIFICATIONS.forEach(k => { countryFilterState[k] = true; });
      renderCountryFilterToggles();
      renderCountryRows();
    });
    container.appendChild(chip);
  }
  const clearAll = document.createElement('span');
  clearAll.className = 'filter-chip clear-all-chip';
  clearAll.textContent = 'Clear all ✕';
  clearAll.addEventListener('click', () => {
    document.getElementById('countryFilmSearch').value = '';
    document.getElementById('countryFilmSearchClear').classList.add('hidden');
    document.getElementById('countryServiceSelect').value = '';
    CLASSIFICATIONS.forEach(k => { countryFilterState[k] = true; });
    renderCountryFilterToggles();
    renderCountryRows();
  });
  container.appendChild(clearAll);
}

function renderCountryRows() {
  const container = document.getElementById('countryGrid');
  container.innerHTML = '';
  const country = currentCountry();
  renderActiveCountryFilters();
  if (!country) return;

  const q = document.getElementById('countryFilmSearch').value.trim().toLowerCase();
  const serviceQ = document.getElementById('countryServiceSelect').value;

  let rows = country.films.slice();
  const col = countryCols.find(c => c.key === countrySortKey);
  if (col && col.sort) {
    rows.sort((a, b) => {
      const av = col.sort(a), bv = col.sort(b);
      return av < bv ? -countrySortDir : av > bv ? countrySortDir : 0;
    });
  }

  const frag = document.createDocumentFragment();
  rows.forEach(row => {
    if (q && !searchHaystack(row).includes(q)) return;
    if (serviceQ && !row.services.some(s => s.brand === serviceQ)) return;
    const visibleServices = row.services.filter(s => countryFilterState[s.classification]);
    if (!visibleServices.length) return;

    const serviceBadges = visibleServices.map(s =>
      '<span class="badge badge-' + s.classification + '" data-brand="' + esc(s.brand) + '">' + esc(s.brand) + '</span>'
    );
    const servicesHtml = '<div class="service-group">' + capBadges(serviceBadges, BADGE_CAP) + '</div>';
    frag.appendChild(filmCardShell(row, servicesHtml));
  });
  container.appendChild(frag);
  ensureNotEmpty(container, 'No films match your search and filters.');
}

function onCountryCardClick(event) {
  const badge = event.target.closest('[data-brand]');
  if (badge) {
    const brand = badge.getAttribute('data-brand');
    const select = document.getElementById('countryServiceSelect');
    select.value = (select.value === brand) ? '' : brand;
    renderCountryRows();
    return;
  }
  if (event.target.closest('a.film-link')) return;
  const card = event.target.closest('.film-card');
  if (card) openQuickLook(card.dataset.slug);
}
document.getElementById('countryGrid').addEventListener('click', onCountryCardClick);

document.getElementById('countrySelect').addEventListener('change', () => { populateCountryServiceSelect(); renderCountryRows(); });
document.getElementById('countryServiceSelect').addEventListener('change', renderCountryRows);
document.getElementById('countryFilmSearch').addEventListener('input', renderCountryRows);
wireSearchClear('countryFilmSearch', 'countryFilmSearchClear', renderCountryRows);
document.getElementById('countrySortSelect').addEventListener('change', e => {
  countrySortKey = e.target.value;
  countrySortDir = countryCols.find(c => c.key === countrySortKey).dir;
  renderCountryRows();
});

populateCountrySelect();
populateCountryServiceSelect();
renderCountryFilterToggles();
renderCountryRows();

// ---------- Init ----------

DATA.countryNames = {};
DATA.countries.forEach(c => { DATA.countryNames[c.code] = c.name; });

renderHome();
renderFilms();

// ---------- New-since-last-viewed indicator ----------

// No server round-trip needed — leaving_soon/recently_added are already in
// DATA, so "new" is just "wasn't in that section's slug list last time this
// browser loaded the page", tracked in localStorage. Resets the baseline on
// every load (each open re-establishes what's "seen"), which matches how a
// PWA actually gets opened (periodically, not continuously).
const SEEN_SLUGS_KEY = 'watchlist_seen_slugs_v1';
const NEW_BADGE_SECTION_KEYS = ['leaving_soon', 'recently_added'];

function loadSeenSlugs() {
  try {
    return JSON.parse(localStorage.getItem(SEEN_SLUGS_KEY) || '{}');
  } catch {
    return {};
  }
}

function updateNewSinceLastViewed() {
  const seen = loadSeenSlugs();
  let totalNew = 0;
  const nextSeen = {};

  NEW_BADGE_SECTION_KEYS.forEach(key => {
    const section = DATA.home_sections.find(s => s.key === key);
    const slugs = section ? section.films.map(f => f.slug) : [];
    const previouslySeen = new Set(seen[key] || []);
    totalNew += slugs.filter(s => !previouslySeen.has(s)).length;
    nextSeen[key] = slugs;
  });

  try {
    localStorage.setItem(SEEN_SLUGS_KEY, JSON.stringify(nextSeen));
  } catch {
    // Private-browsing/storage-full — badge just won't persist across loads.
  }

  document.querySelectorAll('.home-new-badge').forEach(el => {
    if (totalNew > 0) {
      el.textContent = String(totalNew);
      el.classList.remove('hidden');
    } else {
      el.classList.add('hidden');
    }
  });
}

updateNewSinceLastViewed();

// ---------- Pull to refresh (mobile) ----------
// Reload picks up whatever dashboard.html the last daily run deployed —
// there's no live backend to re-fetch from, but this is still the fix for
// "my phone has a stale cached copy from earlier today."
(function initPullToRefresh() {
  const THRESHOLD = 70;
  const HIDDEN_TRANSFORM = 'translate(-50%, -60px)';
  let startY = null;
  let currentDelta = 0;

  const indicator = document.createElement('div');
  indicator.className = 'ptr-indicator';
  document.body.appendChild(indicator);

  function reset() {
    startY = null;
    currentDelta = 0;
    indicator.style.transition = 'none';
    indicator.style.transform = HIDDEN_TRANSFORM;
    indicator.classList.remove('visible');
    indicator.textContent = 'Pull to refresh ↓';
  }
  reset();

  // A reload can be served from the back/forward cache (bfcache) instead of
  // re-running this script from scratch, which would otherwise leave the
  // indicator stuck wherever the pull gesture left it — pageshow fires for
  // both a fresh load and a bfcache restore, unlike DOMContentLoaded.
  window.addEventListener('pageshow', reset);

  document.addEventListener('touchstart', event => {
    startY = window.scrollY === 0 ? event.touches[0].clientY : null;
    currentDelta = 0;
    indicator.style.transition = 'none';
  }, { passive: true });

  document.addEventListener('touchmove', event => {
    if (startY == null) return;
    currentDelta = event.touches[0].clientY - startY;
    if (currentDelta <= 0) {
      indicator.style.transform = HIDDEN_TRANSFORM;
      indicator.classList.remove('visible');
      return;
    }
    // Only take over the gesture once it's clearly a downward pull, so a
    // normal upward scroll right at the top of the page isn't hijacked —
    // and only then block the browser's own native pull-to-refresh, which
    // would otherwise show its own spinner alongside this one.
    if (event.cancelable) event.preventDefault();
    const clamped = Math.min(currentDelta, 120);
    indicator.style.transform = 'translate(-50%, ' + clamped + 'px)';
    indicator.classList.add('visible');
    indicator.textContent = clamped > THRESHOLD ? 'Release to refresh ↑' : 'Pull to refresh ↓';
  }, { passive: false });

  document.addEventListener('touchend', () => {
    if (startY == null) return;
    indicator.style.transition = 'transform 0.2s';
    if (currentDelta > THRESHOLD) {
      indicator.textContent = 'Refreshing…';
      indicator.style.transform = 'translate(-50%, 40px)';
      startY = null;
      window.location.reload();
    } else {
      reset();
    }
  });
})();
</script>
</body>
</html>
"""
