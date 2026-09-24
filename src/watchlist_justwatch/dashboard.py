import dataclasses
import json
from collections import defaultdict
from datetime import date, datetime

from .brands import (
    JUNK_BRANDS,
    KNOWN_VARIANT_SUFFIXES,
    canonical_brand_name,
    group_offers_by_brand_and_country,
    is_major_brand,
)
from .cinemas import listing_match_key, match_watchlist_film
from .config import CountryConfig, is_have_anywhere, service_matches
from .countries import ALL_JUSTWATCH_COUNTRIES, country_name
from .custom_lists import CustomList, matches as custom_list_matches
from .languages import LANGUAGE_NAMES, is_subtitled, language_name
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
    film, config: dict[str, CountryConfig], global_subscriptions: list[str], revisitable: set[str],
    *, keep_monetization: bool = False,
) -> list[dict]:
    """Every (brand, country) this film has a qualifying offer for, each
    classified — the single source of truth other views bucket/filter.

    available_to (soonest expiry seen for that brand/country, if any) and
    url (a deep link to actually watch it there) both ride along here
    rather than needing a second pass over film.offers later —
    group_offers_by_brand_and_country only tracks monetization types, so
    this is the one place with access to the raw per-offer dates/urls. When
    a (brand, country) has multiple qualifying offers (e.g. a free ad tier
    and a full subscription), the url from the most-watchable one wins.

    keep_monetization carries the monetization types through onto each
    entry, which is what makes a classification recomputable later. Only
    discovery films need it — they're the ones stored classified (see
    _reclassified_discovery_films); a watchlist film is classified fresh on
    every build, so paying for the field in every page load would buy
    nothing."""
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
            entry = {
                "brand": brand, "country": country, "classification": classification,
                "available_to": soonest_expiry.get(key),
                "url": url_entry[1] if url_entry else None,
            }
            if keep_monetization:
                entry["monetization_types"] = sorted(monetization_types)
            result.append(entry)
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
        "runtime_minutes": film.runtime_minutes,
        # Not used by the small tile cards (filmCardShell), but the Review
        # screen's much bigger card reads DATA.films directly (not
        # films_by_slug, which is quick-look's own separate copy) and needs
        # the full plot, not just director/genre.
        "synopsis": film.synopsis,
        "language_name": language_name(film.original_language),
        "is_subtitled": is_subtitled(film.original_language),
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
    """One row per service, aggregated across every country it's in.

    The tab used to be a card per (brand, country) — Netflix appearing 118
    times — which answered "what's on Netflix in Chile" but never "what is
    Netflix actually worth to me". A service is one thing you pay for once,
    so it's one card; the countries become a filter inside it, carried here
    as `slugs_by_country`.

    `unique_slugs` is the point of the exercise: the films on this service
    that are on **no other service you have**. For a service you have that
    is what you'd lose by cancelling it. For one you don't, the same
    sentence reads as what you'd gain, since none of its films being on
    something you have is exactly the case where it would add something.
    Judged across all countries, so a film on Netflix here and Prime
    elsewhere is not unique to either.

    Note what "a service you have" means: every brand that classifies as
    `have`, which includes the free broadcasters (BBC iPlayer, ITVX, ABC
    iview) alongside the paid subscriptions. A film on iPlayer is one you
    don't need Netflix for, so counting it is what makes the number answer
    the question it's there to answer.
    """
    # The set of services you have that each film is on, which is all the
    # uniqueness test needs and is cheaper than rescanning offers per brand.
    have_brands_by_slug: dict[str, set[str]] = {
        slug: {o["brand"] for o in all_offers if o["classification"] == "have"}
        for slug, all_offers in films_all_offers.items()
    }

    by_brand: dict[str, dict] = {}
    for slug, all_offers in films_all_offers.items():
        for offer in all_offers:
            entry = by_brand.setdefault(offer["brand"], {
                "slugs": set(), "by_country": {}, "classifications": set(),
            })
            entry["slugs"].add(slug)
            entry["by_country"].setdefault(offer["country"], set()).add(slug)
            entry["classifications"].add(offer["classification"])

    def by_title(slug: str) -> str:
        return state.films[slug].title.lower()

    rows = []
    for brand, entry in by_brand.items():
        slugs = sorted(entry["slugs"], key=by_title)
        unique_slugs = [s for s in slugs if not (have_brands_by_slug[s] - {brand})]
        countries = sorted(
            ({"code": code, "name": country_name(code), "film_count": len(country_slugs)}
             for code, country_slugs in entry["by_country"].items()),
            key=lambda c: (-c["film_count"], c["name"]),
        )
        rows.append({
            "brand": brand,
            "classification": min(entry["classifications"], key=lambda c: _CLASSIFICATION_PRIORITY[c]),
            "film_count": len(slugs),
            "slugs": slugs,
            "unique_film_count": len(unique_slugs),
            "unique_slugs": unique_slugs,
            "country_count": len(countries),
            "countries": countries,
            "slugs_by_country": {
                code: sorted(country_slugs, key=by_title)
                for code, country_slugs in entry["by_country"].items()
            },
        })
    rows.sort(key=lambda r: (-r["film_count"], r["brand"]))
    return rows


def _country_index(state: StateDoc, films_all_offers: dict[str, list[dict]]) -> list[dict]:
    """Every country any watchlist film is available in, with how many.

    This used to be the By-country tab's whole dataset: a row per (country,
    film) carrying that film's title, poster, director, cast and genre
    again. 121 countries x 12,425 rows for 381 distinct films — every
    film's metadata repeated about thirty times, all of it already in
    films_by_slug — which came to 6MB of a 13MB payload, and 60% of what
    the page actually costs to download once gzipped.

    The tab it fed is gone (the Films tab's country filter answers the same
    question from data it already has), so what's left is the index the
    rest of the page needs: code to name for every badge on the dashboard,
    and the counts behind the Films tab's country dropdown.
    """
    counts: dict[str, int] = {}
    for all_offers in films_all_offers.values():
        for country in {offer["country"] for offer in all_offers}:
            counts[country] = counts.get(country, 0) + 1
    return sorted(
        ({"code": code, "name": country_name(code), "film_count": n} for code, n in counts.items()),
        key=lambda c: c["name"],
    )


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
            "runtime_minutes": film.runtime_minutes,
            "language_name": language_name(film.original_language),
            "is_subtitled": is_subtitled(film.original_language),
            # What the film detail page hands the Worker to ask TMDB about
            # this film directly. Absent on films whose TMDB search hasn't
            # come round yet (the stale rotation fills it in), and the page
            # treats that as "no live layer for this one" rather than an error.
            "tmdb_id": film.tmdb_id,
            "all_offers": all_offers,
        }
    return lookup



def _reclassified_discovery_films(
    discovery_films: dict[str, dict], config: dict[str, CountryConfig],
    global_subscriptions: list[str], revisitable: set[str],
) -> dict[str, dict]:
    """Discovery films with their offers re-judged against today's config.

    A watchlist film's offers are classified on every build, so changing
    what you subscribe to takes effect immediately. A discovery film's were
    classified once, when similar.py found it, and stored that way — so
    they kept whatever verdict was current on the day, and a Settings
    change (or a fix to the matching rules) never reached them. That's how
    recommendation cards ended up still badging YouTube TV as a service
    Josh has, months after nothing else did.

    Nothing is re-fetched: an offer's brand, country and monetization types
    are what _classify reads, and those are facts about the offer rather
    than about the config, so the verdict can simply be recomputed.

    An entry stored before monetization types were kept is still worth
    re-judging, because the two rungs that matter most — is this a service
    you have, or one you could get again — are decided by brand and country
    alone. Only the last rung needs the types, to tell "free to watch" from
    "needs a subscription", and there the stored answer stands. Waiting for
    those entries to age out instead left nine recommendation cards
    claiming Josh had YouTube TV.
    """
    result: dict[str, dict] = {}
    for slug, film in discovery_films.items():
        offers = []
        for offer in film.get("all_offers", []):
            monetization_types = offer.get("monetization_types")
            classification = _classify(
                offer["brand"], offer["country"], set(monetization_types or ()),
                config, global_subscriptions, revisitable)
            if monetization_types is None and classification == "free":
                # "free" here only means the types weren't there to say
                # otherwise. Keep whichever of free/subscription was stored;
                # if the stored answer was have or could_get_again — the very
                # thing just overturned — assume it has to be paid for rather
                # than guess it away.
                stored = offer.get("classification")
                classification = stored if stored in ("free", "subscription") else "subscription"
            offers.append({**offer, "classification": classification})
        result[slug] = {**film, "all_offers": offers}
    return result


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
        "runtime_minutes": film.runtime_minutes,
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


QUICK_WATCH_MIN_MINUTES = 80
QUICK_WATCH_MAX_MINUTES = 100


def _quick_watch_section(state: StateDoc, films_all_offers: dict[str, list[dict]], exclude: set[str],
                          limit: int = RECOMMENDED_COUNT) -> dict:
    """Same ranking approach as _top_rated_section (watchable-now on a have
    service first, then highest-rated fallback), narrowed to films whose
    runtime falls in the "around 90 minutes" window — a distinct enough cut
    of the watchlist (short films skew toward different genres/eras than
    the list as a whole) that it's worth its own section rather than just
    another Films-tab filter."""
    def has_have(slug: str) -> bool:
        return any(o["classification"] == "have" for o in films_all_offers.get(slug, []))

    candidates = [
        slug for slug, film in state.films.items()
        if slug not in exclude and film.runtime_minutes is not None
        and QUICK_WATCH_MIN_MINUTES <= film.runtime_minutes <= QUICK_WATCH_MAX_MINUTES
    ]

    def sort_key(slug: str) -> tuple:
        film = state.films[slug]
        return (-(film.rating or 0), film.title)

    watchable_now = sorted((s for s in candidates if has_have(s)), key=sort_key)
    chosen = watchable_now[:limit]
    if len(chosen) < limit:
        fallback = sorted((s for s in candidates if s not in chosen), key=sort_key)
        chosen += fallback[: limit - len(chosen)]

    return {
        "key": "quick_watch", "header": "Got 90 minutes?",
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
        "runtime_minutes": entry.get("runtime_minutes"),
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


def _watch_together_section(state: StateDoc, watch_together: dict[str, dict], exclude: set[str],
                             limit: int = RECOMMENDED_COUNT) -> dict:
    """Films Sarah has confirmed from the Review tab — ranked alongside
    leaving_soon/recently_added since a shared "yes, let's watch this
    together" is as actionable a signal as either of those. Only slugs still
    on the watchlist and still in `films` are shown — a film that fell off
    the watchlist after being confirmed just quietly stops appearing, same
    as it would everywhere else on the dashboard."""
    confirmed = [
        (info["decided_at"] or "", slug) for slug, info in watch_together.items()
        if info["status"] == "confirmed" and slug in state.films and slug not in exclude
    ]
    confirmed.sort(key=lambda pair: pair[0], reverse=True)
    films = [_mini_card(state.films[slug]) for _, slug in confirmed[:limit]]
    return {"key": "watch_together", "header": "Watch together", "films": films}


def _recently_added_section(state: StateDoc, exclude: set[str], limit: int = 12) -> dict:
    seen: set[str] = set()
    chosen: list[str] = []
    added_service_by_slug: dict[str, str] = {}
    for entry in state.recent_additions:  # already newest-first, retained by age not count (see main.py)
        slug = entry["slug"]
        # The log carries both rungs that mean "watchable without paying
        # more" — a service you have, and a free ad-supported one you
        # don't. Only the first is this section's promise: something you
        # already subscribe to just picked this up.
        if entry.get("classification") != "have":
            continue
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


def _format_cinema_datetime(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%a %-d %b") + ", " + dt.strftime("%-I:%M%p").lower()


def _soonest_cinema_showings(state: StateDoc, now: datetime | None = None) -> dict[str, dict]:
    """Every watchlist film with an upcoming screening at one of the four
    cinemas in cinemas.py, mapped to its single soonest showing — shared
    by the Home section below and the Films tab's own per-card note, so
    both agree on which showing counts as "next" for a given film."""
    now_iso = (now or datetime.now()).isoformat()
    soonest_by_slug: dict[str, dict] = {}

    for showing in state.cinema_showtimes:
        if showing["showtime"] < now_iso:
            continue
        slug = match_watchlist_film(showing["title"], showing["year"], state.films)
        if slug is None or slug not in state.films:
            continue
        current = soonest_by_slug.get(slug)
        if current is None or showing["showtime"] < current["showtime"]:
            soonest_by_slug[slug] = showing

    return soonest_by_slug


def _cinema_note(showing: dict) -> str:
    return f"{showing['cinema']} — {_format_cinema_datetime(showing['showtime'])}"


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


def _custom_list_sections(state: StateDoc, films_all_offers: dict[str, list[dict]],
                          custom_lists: list[CustomList], list_sources: dict[str, set[str]],
                          list_totals: dict[str, int] | None = None) -> list[dict]:
    """One section per config/custom_lists.yaml entry with home: true, in
    config order — the Lists tab. Unlike every Home section these carry
    the *whole* matching set (the page collapses it to a preview
    client-side) and neither read nor feed Home's cross-section dedupe —
    a list is only useful if it's complete, and a De Palma film also
    showing up under "Top rated" is fine. Ordered watchable-now first,
    then by rating, same as _top_rated_section."""
    def has_have(slug: str) -> bool:
        return any(o["classification"] == "have" for o in films_all_offers.get(slug, []))

    sections = []
    for cl in custom_lists:
        if not cl.home:
            continue
        members = [
            slug for slug, film in state.films.items()
            if custom_list_matches(cl, slug, film.director, film.starring, film.year, list_sources)
        ]
        if not members:
            continue
        members.sort(key=lambda s: (not has_have(s), -(state.films[s].rating or 0), state.films[s].title))

        seen = sum(
            1 for slug, entry in state.diary.items()
            if slug not in state.films
            and custom_list_matches(cl, slug, entry.get("director"), entry.get("starring"), entry.get("year"),
                                    list_sources)
        )
        total = (list_totals or {}).get(cl.key)
        parts = [f"{len(members)} on your watchlist"]
        if total is not None:
            parts.append(f"{seen} of {total} seen")
        elif seen:
            parts.append(f"{seen} seen")

        sections.append({
            "key": f"list:{cl.key}", "header": cl.name, "subtitle": " · ".join(parts),
            # The Lists tab groups by this the same way the Films-tab
            # dropdown does — one chip per group, so the eight Cannes
            # lists collapse to one jump target rather than eight.
            "group": cl.group, "custom_list": True,
            "films": [_mini_card(state.films[s]) for s in members],
        })
    return sections


MAX_PERSON_SECTIONS = 4


def _build_home_sections(state: StateDoc, films_all_offers: dict[str, list[dict]],
                          films_by_slug: dict[str, dict], discovery_films: dict[str, dict],
                          dismissed_recommendations: set[str],
                          watch_together: dict[str, dict]) -> list[dict]:
    # Same merge order and the same already-reclassified discovery films as
    # the payload's own films_by_slug, so a card here and the quick-look it
    # opens can't disagree about what a film costs.
    lookup = {**discovery_films, **films_by_slug}
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

    # "Just landed on a service you have" leads: it is the only section
    # that tells you something you could not have known yesterday, and
    # every film in it is watchable right now at no extra cost. Leaving
    # soon second — the other deadline-shaped section, but a 30-day window
    # is a softer one than "this is new today". Cinema screenings used to
    # lead both; they have their own tab, and a London showtime is not
    # something Home can act on the way a new service addition is.
    add(_recently_added_section(state, used))
    add(_leaving_soon_section(state, films_all_offers, used))
    add(_watch_together_section(state, watch_together, used))

    # Recommended-from-recent-watches and top-rated next — general
    # discovery, not tied to a specific person — so they're not buried
    # under however many per-director/per-cast sections exist this run.
    add(_cached_section(state, lookup, "because_you_watched", used))
    add(_top_rated_section(state, films_all_offers, used))

    # One section per unique director/cast member from your last few
    # watches — however many that turns out to be (see main.py, which can
    # genuinely generate a dozen+ on a run with several multi-cast recent
    # watches). Capped here rather than at generation time (main.py still
    # stores all of them, in case a future view wants the rest) — Home
    # itself shouldn't be a wall of a dozen near-identical "More starring
    # X" rows before reaching the general-discovery sections below.
    person_sections_shown = 0
    for prefix in ("director:", "cast:"):
        for cached in state.recommendation_sections:
            if person_sections_shown >= MAX_PERSON_SECTIONS:
                break
            if cached["key"].startswith(prefix):
                before = len(sections)
                add(_section_from_cached(cached, lookup, used))
                if len(sections) > before:
                    person_sections_shown += 1

    # Popular right now moved below the director/cast sections — general
    # trending picks are lower priority than either the sections above or
    # the personalized ones just above it.
    add(_cached_section(state, lookup, "popular_now", used))
    add(_cached_section(state, lookup, "rewatch", used))

    # Longer-tail exploration at the bottom, on purpose — genre/hidden-gem
    # picks are lower-confidence than the sections above.
    add(_cached_section(state, lookup, "by_genre", used))
    add(_cached_section(state, lookup, "hidden_gems", used))

    # Deliberately the very last section: it answers "I have a spare
    # evening and no idea what to put on", which is a question you only
    # reach after nothing above it caught your eye. Running it here rather
    # than mid-page also means it picks from what the sections above
    # did not already show.
    add(_quick_watch_section(state, films_all_offers, used))

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



def _search_taxonomy(
    state: StateDoc, config: dict[str, CountryConfig], global_subscriptions: list[str], revisitable: set[str]
) -> dict:
    """Everything the page needs to classify a *searched* film's offers the
    same way this module classifies a watchlist film's.

    Quick search gets its offers live from the Worker, raw from JustWatch and
    deliberately unclassified (see worker/src/index.js), because the
    have/free/could_get_again/subscription taxonomy is real logic —
    brands.py's suffix stripping and aliasing, config.py's fuzzy service
    matching, _classify's precedence — and a JS reimplementation of it would
    drift silently, with no test on that side to catch it.

    So this ships the *answers* instead of the rules. Every lookup below is
    computed here by the same functions the rest of the dashboard uses, over
    every service name the corpus has actually seen, leaving the page with
    set membership and no string logic at all.

    The fallback for a service that isn't in the corpus (a small regional
    one, on some film nobody has watchlisted) is to treat its name as its own
    brand and classify it from its monetization type. That's the right answer
    for a service you don't subscribe to, and every service you *do* is here
    by construction — so the degradation is invisible in practice, and heals
    on the next run that sees the name.
    """
    # Only variants worth a lookup: a clear name that canonicalizes to itself
    # is exactly what the page's fallback already does, so storing it would
    # add weight to every page load to say nothing. What's left is the ad
    # tiers and channel bundles ("Paramount Plus Basic with Ads" ->
    # "Paramount Plus"), which the page can't work out on its own.
    brand_by_clear_name: dict[str, str] = {}
    brands: set[str] = set()
    for film in state.films.values():
        for offer in film.offers:
            clear_name = offer.package_clear_name
            brand = canonical_brand_name(clear_name)
            brands.add(brand)
            if brand != clear_name:
                brand_by_clear_name[clear_name] = brand

    # The corpus alone isn't enough to answer "do I have this?". It only
    # contains services some watchlist film currently happens to stream on,
    # so a subscription with nothing on it today (or nothing in that country)
    # would be missing from the lists below, and a searched film streaming
    # there would read as one more service to pay for. The config *is* the
    # list of services Josh has, so it seeds the universe too — canonicalized
    # on the way in, since that's the form the page looks brands up by.
    configured_brands = {canonical_brand_name(name) for name in global_subscriptions}
    configured_brands.update(revisitable)
    for country_config in config.values():
        configured_brands.update(canonical_brand_name(name)
                                 for name in country_config.subscriptions + country_config.free_tier)
    brands.update(configured_brands)

    # Same problem one level down: the corpus supplies the *variant* names a
    # service appears under ("Amazon Prime Video with Ads", "MUBI Amazon
    # Channel"), and a variant missing from it falls back to being read as a
    # service of its own — which reads as "subscribe to this" for something
    # already paid for, the most expensive way to be wrong here. The
    # qualifiers are known (brands.py strips exactly these), so the variants
    # of a service Josh has can be written down rather than waited for.
    # Only those services: for anything else the fallback costs a tidier
    # label at worst, never a wrong classification.
    for brand in configured_brands:
        for suffix in KNOWN_VARIANT_SUFFIXES:
            brand_by_clear_name.setdefault(f"{brand} {suffix}", brand)

    # Split global from per-country because a searched film turns up offers in
    # all ~124 JustWatch countries, not just the three configured here: a
    # VPN-portable subscription is "have" in every one of them, while
    # everything else only counts in its own country. Matches
    # is_have_anywhere's own two halves.
    have_brands_global = sorted(
        brand for brand in brands
        if any(service_matches(name, brand) for name in global_subscriptions)
    )
    have_brands_by_country = {
        code: sorted(
            brand for brand in brands
            if is_have_anywhere(brand, code, config, global_subscriptions)
            and brand not in set(have_brands_global)
        )
        for code in sorted(config)
    }

    return {
        "brand_by_clear_name": brand_by_clear_name,
        "have_brands_global": have_brands_global,
        "have_brands_by_country": {k: v for k, v in have_brands_by_country.items() if v},
        "revisitable_brands": sorted(revisitable),
        # Lowercased, the way is_junk_brand compares them.
        "junk_brands": sorted(JUNK_BRANDS),
        "language_names": LANGUAGE_NAMES,
        # The page hands this to the Worker rather than the Worker keeping a
        # second copy of countries.py's list to fall out of step with.
        "justwatch_countries": sorted(ALL_JUSTWATCH_COUNTRIES),
    }


def _cinema_listings(state: StateDoc) -> list[dict]:
    """One row per film for the full Cinemas tab — a matched watchlist
    film showing at several of the four cinemas merges into a single row
    (grouped by slug, the one reliable cross-cinema identity a match
    gives us) with every cinema's showtimes attached, rather than one
    card per (cinema, title) like an unmatched film still gets (title
    alone isn't a safe enough identity to merge across cinemas without a
    match — two different films can share a name). Unlike _cinema_section
    (Home, watchlist-only), this includes everything showing regardless
    of a match, since browsing "what's on generally" is the whole point
    of the tab. Matched films use whatever richer metadata is already
    tracked on the watchlist instead of the venue's own (same
    don't-duplicate-data-we-already-have principle as everywhere else)."""
    grouped: dict[tuple, dict] = {}
    # The resolved matches are keyed by listing, but rows are grouped by the
    # film — so index them by slug once rather than searching per row.
    resolved_by_slug = {
        match["slug"]: match
        for match in state.cinema_matches.values()
        if match and match.get("slug")
    }

    for showing in state.cinema_showtimes:
        slug = match_watchlist_film(showing["title"], showing["year"], state.films)
        # Everything the watchlist can't name — most of the programme — falls
        # back to the Letterboxd film run() resolved for it. That match is
        # just as good an identity for merging across venues, so it groups
        # the same way; what it doesn't bring is JustWatch offers, since
        # nothing has ever looked the film up.
        resolved = None if slug else (state.cinema_matches.get(
            listing_match_key(showing["title"], showing["year"])) or None)
        if resolved is not None and not resolved.get("slug"):
            resolved = None
        group_slug = slug or (resolved["slug"] if resolved else None)
        key = ("matched", group_slug) if group_slug else ("unmatched", showing["cinema"], showing["title"])
        entry = grouped.setdefault(key, {
            "matched_slug": slug, "title": showing["title"], "year": showing["year"],
            "duration_minutes": showing["duration_minutes"], "director": showing["director"],
            "synopsis": showing["synopsis"], "poster_url": showing["poster_url"],
            # The Letterboxd film this listing is showing, when it isn't one
            # the dashboard already tracks. Carries no availability — tapping
            # it looks that up live, the same path a searched film takes.
            "letterboxd_slug": resolved["slug"] if resolved else None,
            "tmdb_id": resolved["tmdb_id"] if resolved else None,
            "showtimes": [],
        })
        entry["showtimes"].append({
            "cinema": showing["cinema"], "showtime": showing["showtime"], "booking_url": showing["booking_url"],
        })

    rows = list(grouped.values())
    for row in rows:
        row["showtimes"].sort(key=lambda s: s["showtime"])
        film = state.films.get(row["matched_slug"]) if row["matched_slug"] else None
        if film is not None:
            row["title"] = film.title
            row["year"] = film.year
            row["poster_url"] = film.poster_url or row["poster_url"]
            row["rating"] = film.rating
            row["genre"] = film.genre
            row["director"] = ", ".join(film.director) if film.director else row["director"]
            row["synopsis"] = film.synopsis or row["synopsis"]
            row["duration_minutes"] = film.runtime_minutes or row["duration_minutes"]
        elif row.get("letterboxd_slug"):
            # Same principle as a watchlist match: prefer what Letterboxd
            # says about the film over what the venue's listing page said,
            # since the venue's is a marketing blurb with the screening's
            # own year on it.
            resolved = resolved_by_slug.get(row["letterboxd_slug"], {})
            row["rating"] = resolved.get("rating")
            row["genre"] = resolved.get("genre") or []
            row["title"] = resolved.get("title") or row["title"]
            row["year"] = resolved.get("year") or row["year"]
            row["poster_url"] = resolved.get("poster_url") or row["poster_url"]
            row["director"] = resolved.get("director") or row["director"]
            row["synopsis"] = resolved.get("synopsis") or row["synopsis"]
            row["duration_minutes"] = resolved.get("runtime_minutes") or row["duration_minutes"]
        else:
            row["rating"] = None
            row["genre"] = []

    rows.sort(key=lambda r: r["showtimes"][0]["showtime"] if r["showtimes"] else "9999")
    return rows


def build_dashboard_data(
    state: StateDoc,
    favorites: set[tuple[str, str]],
    config: dict[str, CountryConfig],
    global_subscriptions: list[str],
    revisitable: set[str],
    dismissed_recommendations: set[str] = frozenset(),
    watch_together: dict[str, dict] | None = None,
    custom_lists: list[CustomList] = (),
    list_sources: dict[str, set[str]] | None = None,
    list_totals: dict[str, int] | None = None,
) -> dict:
    watch_together = watch_together or {}
    # Already narrowed to watchlist/diary slugs by the DB (see
    # db.load_custom_list_memberships) — membership is all that's needed here.
    source_slugs = list_sources or {}

    # state.films is Josh's watchlist UNION Sarah's (see main.py's
    # combined_films) — offers/quick-look are computed for all of it so her
    # films get exactly the same JustWatch/quick-look treatment as his, but
    # his own tabs (Films/Country/Services/home sections) stay scoped to
    # josh_watchlist only via josh_state below, same as before this was
    # unified — her solo-interest films were never meant to bleed into his
    # own browsing or recommendations.
    films_all_offers = {
        slug: _all_offers_for_film(film, config, global_subscriptions, revisitable)
        for slug, film in state.films.items()
    }
    films_by_slug = _films_by_slug(state, films_all_offers)
    for slug, entry in films_by_slug.items():
        entry["watch_together_status"] = watch_together.get(slug, {}).get("status")

    discovery_films = _reclassified_discovery_films(
        state.discovery_films, config, global_subscriptions, revisitable)

    josh_films = {slug: f for slug, f in state.films.items() if slug in state.josh_watchlist}
    josh_offers = {slug: films_all_offers[slug] for slug in josh_films}
    josh_state = dataclasses.replace(state, films=josh_films)

    main_brands = _select_main_brands(josh_state, config, global_subscriptions)
    main_brand_set = set(main_brands)

    rows = [_film_row(film, main_brand_set, josh_offers[slug]) for slug, film in josh_films.items()]
    soonest_cinema_showings = _soonest_cinema_showings(josh_state)
    for r in rows:
        info = watch_together.get(r["slug"], {})
        r["watch_together_status"] = info.get("status")
        r["watch_together_added_at"] = info.get("added_at")
        film = josh_films[r["slug"]]
        r["custom_lists"] = [
            cl.key for cl in custom_lists
            if custom_list_matches(cl, film.slug, film.director, film.starring, film.year, source_slugs)
        ]
        showing = soonest_cinema_showings.get(r["slug"])
        if showing is not None:
            r["cinema_note"] = _cinema_note(showing)
    rows.sort(key=lambda r: r["title"].lower())

    sarah_films = [
        _mini_card(state.films[slug])
        for slug in sorted(state.sarah_watchlist, key=lambda s: state.films[s].title.lower() if s in state.films else "")
        if slug in state.films
    ]

    return {
        "last_run_at": state.last_run_at,
        "letterboxd_watchlist_url": f"https://letterboxd.com/{LETTERBOXD_USERNAME}/watchlist/",
        "main_brands": main_brands,
        "home_sections": _build_home_sections(josh_state, josh_offers, films_by_slug, discovery_films,
                                              dismissed_recommendations, watch_together),
        # The Lists tab. Its own payload key rather than a run of Home
        # sections: fourteen of them buried Home's time-sensitive rows
        # under a screen and a half of scrolling, and they are the one
        # thing on the page you go looking for deliberately.
        "list_sections": _custom_list_sections(josh_state, josh_offers, custom_lists, source_slugs, list_totals),
        "films": rows,
        # Films-tab dropdown options — config order, lists with no current
        # watchlist members left out (same as their Home sections).
        "custom_lists": [
            {"key": cl.key, "name": cl.name, "group": cl.group, "count": n}
            for cl in custom_lists
            if (n := sum(cl.key in r["custom_lists"] for r in rows))
        ],
        "services": _service_rows(josh_state, josh_offers),
        "countries": _country_index(josh_state, josh_offers),
        # Watchlist entries win over discovery ones for the same slug: both
        # can hold the film, but only the watchlist copy was built from this
        # run's data. The other order let a stored recommendation shadow it
        # and show offers judged on some earlier day's config.
        "films_by_slug": {**discovery_films, **films_by_slug},
        "sarah_films": sarah_films,
        "cinemas": _cinema_listings(state),
        "settings": _settings_data(config, global_subscriptions),
        "search_taxonomy": _search_taxonomy(state, config, global_subscriptions, revisitable),
    }


def render_dashboard_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return _TEMPLATE.replace("__DATA__", payload)


_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
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
  .app-bar {
    position: fixed; top: 0; left: 0; right: 0; z-index: 25;
    background: var(--bg); border-bottom: 1px solid var(--hairline);
    padding: 0 32px;
  }
  .app-bar-top {
    display: flex; justify-content: space-between; align-items: center; gap: 16px;
    padding: calc(14px + env(safe-area-inset-top)) 0 12px; flex-wrap: wrap;
  }
  /* The title/film-count text traded away its spot for the nav+filters bar
     it used to sit above — still rendered (and still what mobile shows,
     where there's no sticky-bar problem to solve), just not on desktop. */
  .app-bar-title { display: none; }
  .app-bar-controls { padding: 0 0 12px; }
  .app-bar-controls.empty { display: none; }
  .header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
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
  .icon-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .icon-btn:disabled:hover { color: var(--text-muted); border-color: var(--hairline-strong); }
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
  .tabs { display: flex; gap: 6px; }
  .tab-btn {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 7px 15px 7px 12px; border: none; border-radius: 999px; background: transparent; color: var(--text-muted);
    cursor: pointer; font-size: 12.5px; font-weight: 500; transition: background 0.15s, color 0.15s;
  }
  .tab-btn svg { width: 16px; height: 16px; stroke: currentColor; flex-shrink: 0; }
  .tab-btn:hover { background: var(--hairline); }
  .tab-btn.active { background: var(--text); color: var(--bg); }
  .tab-btn-accent { color: var(--accent); }
  .tab-btn-accent:hover { background: var(--accent-soft); }
  .tab-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .tab-btn:disabled:hover { background: transparent; }
  /* Only visible on desktop (see .header-actions/.mobile-only-bar below) —
     Settings became a peer tab and "Surprise me"/the watchlist link moved
     up here, so mobile's small standalone gear icon and in-section
     surprise button (still needed there) shouldn't double up with these. */
  .header-actions { display: none; }
  .watchlist-link-inline { display: none; }
  .mobile-only-bar { display: none; }
  .controls { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; font-size: 12.5px; }
  .quick-filters { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
  .hint { color: var(--text-faint); font-size: 11.5px; margin-right: 2px; }
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
  /* One shared pill style — a one-click filter you can turn on/off
     (.pill-toggle), a single-select choice among several (.quick-country,
     .sarah-pill) — so every "small clickable filter chip" in the app looks
     and behaves the same regardless of which control renders it. */
  .quick-country, .sarah-pill, .pill-toggle {
    padding: 5px 12px; border-radius: 999px; font-size: 11.5px; font-weight: 500; cursor: pointer;
    background: var(--surface); border: 1px solid var(--hairline-strong); color: var(--text-muted);
    transition: opacity 0.15s; white-space: nowrap;
  }
  .quick-country:hover, .sarah-pill:hover, .pill-toggle:hover { border-color: var(--accent); color: var(--accent); }
  .quick-country.active, .sarah-pill.active, .pill-toggle.active { background: var(--accent); border-color: var(--accent); color: #06201d; }
  .quick-country .count { opacity: 0.65; margin-left: 4px; }
  #cinemaVenueToggles, #cinemaDateFilter {
    display: inline-flex; flex-wrap: wrap; gap: 6px; align-items: center;
  }
  .sarah-filter { display: inline-flex; gap: 4px; align-items: center; flex-wrap: wrap; }
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
  /* No zoom on a phone. Safari has ignored user-scalable=no since iOS 10,
     so the meta tag alone does nothing; what works is denying the gestures
     themselves. pan-x pan-y is every panning direction minus pinch-zoom and
     minus the double-tap that was zooming the page on a second tap of a
     bottom-nav icon.

     Scoped to coarse pointers so a desktop browser is left completely
     alone — its own zoom still reflows the grid, which is the wanted
     behaviour there, and a Mac trackpad pinch (which fires the same gesture
     events Safari uses on iOS) keeps working. */
  @media (pointer: coarse) {
    html, body { touch-action: pan-x pan-y; }
    /* touch-action isn't inherited — the ancestor chain governs the gesture,
       so the rule above is what actually does the work. This is stated on
       the controls anyway, because the bottom nav is where a stray second
       tap lands and it should be obvious there that the zoom is refused on
       purpose. .review-card keeps its own pan-y for the swipe handler. */
    button, a, .poster-tile, .film-card, .bottom-nav-btn, .tab { touch-action: pan-x pan-y; }
  }
  .detail-card {
    display: flex; gap: 16px; padding: 16px 0; border-bottom: 1px solid var(--hairline);
  }
  /* A flex item defaults to min-width:auto, which means it refuses to shrink
     below its widest unbreakable child — one long service name ("Sony
     Pictures Core United States") was therefore setting the card's width and
     pushing it past the modal on 112 of 527 films. min-width:0 lets it
     shrink; the wrapping rules below are what it shrinks into. */
  .detail-body { min-width: 0; flex: 1; }
  .detail-body h3, .detail-meta, .detail-synopsis { overflow-wrap: anywhere; }
  .detail-poster { width: 76px; height: 112px; object-fit: cover; border-radius: 6px; flex-shrink: 0; background: var(--hairline); }
  .detail-poster-placeholder { width: 76px; height: 112px; border-radius: 6px; flex-shrink: 0; background: var(--hairline); }
  .detail-body h3 { margin: 0 0 4px; font-size: 15px; font-weight: 600; }
  .detail-rating { font-size: 12.5px; color: #4ade80; font-weight: 600; margin: 0 0 6px; }
  .detail-meta { font-size: 12px; color: var(--text-muted); margin: 0 0 5px; }
  .detail-meta strong { color: var(--text); font-weight: 600; }
  .detail-genre { color: #c98a7d; }
  .detail-synopsis { font-size: 12.5px; color: var(--text-muted); line-height: 1.5; margin: 4px 0 8px; }
  /* In the modal the synopsis is the one part with no natural ceiling — a
     long one is most of the card on its own. Clamped so quick look stays one
     glance; the whole thing is on the full details page, which is a tap away
     from the same card. */
  .detail-card-compact .detail-synopsis {
    display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
  }
  .badge-wrap { display: flex; flex-wrap: wrap; gap: 2px; min-width: 0; }
  /* A badge is one unbreakable run of text; without this the longest service
     name in a country decides how wide the card has to be. */
  /* nowrap is right for the usual "Netflix United Kingdom" pill, but a
     badge is one unbreakable run of text, so "Sony Pictures Core Turks and
     Caicos Islands" was setting how wide the card had to be. Inside a wrap
     container it may break — between the service and the country first,
     which is where it reads naturally anyway. */
  .badge-wrap .badge { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
  .expiring-notes { margin-top: 8px; }
  .expiring-note { font-size: 11.5px; color: #fbbf24; font-weight: 500; margin: 0 0 3px; }
  .expiring-note i { color: #fbbf24; font-style: italic; opacity: 0.85; }
  .muted { color: var(--text-faint); font-size: 12px; }
  .detail-card.collapsible { cursor: pointer; }
  .detail-card.collapsible .other-services-section { display: none; }
  .detail-card.collapsible.expanded .other-services-section { display: block; }
  /* Quick search — the picker list lives in the same modal the result card
     does, so choosing a film swaps the panel rather than opening a second
     layer on top of the first. */
  .search-modal-input {
    width: 100%; box-sizing: border-box; padding: 11px 14px; font-size: 14px;
    border: 1px solid var(--hairline-strong); border-radius: 10px;
    background: var(--bg); color: var(--text); outline: none; transition: border-color 0.15s;
  }
  .search-modal-input:focus { border-color: var(--accent); }
  .search-section-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text-faint); font-weight: 600; margin: 16px 0 6px;
  }
  .search-row {
    display: flex; gap: 11px; align-items: center; padding: 7px 8px;
    border-radius: 10px; cursor: pointer; transition: background 0.12s;
  }
  .search-row:hover { background: var(--hairline); }
  .search-row-poster {
    width: 34px; height: 50px; object-fit: cover; border-radius: 4px;
    background: var(--hairline); flex-shrink: 0;
  }
  .search-row-body { min-width: 0; }
  .search-row-title {
    font-size: 13px; font-weight: 600; margin: 0 0 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .search-row-meta {
    font-size: 11.5px; color: var(--text-muted); margin: 0;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .search-on-list {
    display: inline-block; margin-left: 6px; padding: 1px 6px; border-radius: 999px;
    background: var(--accent); color: var(--bg); font-size: 9.5px; font-weight: 700;
    vertical-align: middle;
  }
  .search-note { font-size: 11.5px; color: var(--text-faint); margin: 10px 0 0; }
  .search-note-warn { color: #fbbf24; }
  .search-status { font-size: 12px; color: var(--text-faint); padding: 10px 2px; }
  .modal-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0, 0, 0, 0.6); z-index: 50;
    align-items: center; justify-content: center;
    /* An overlay covers the whole screen, status bar included, so a flat
       20px put the top-anchored search card under the notch. */
    padding: calc(20px + env(safe-area-inset-top)) 20px calc(20px + env(safe-area-inset-bottom));
  }
  .modal-overlay.active { display: flex; }
  /* Quick look is opened from things that can themselves be inside the
     search overlay, and both were z-index 50 — with quick look first in the
     DOM, the tie went to search and the film opened underneath it. */
  #quickLookOverlay { z-index: 60; }
  /* While a modal is open the page behind it is frozen where it was. position
     fixed rather than overflow:hidden because iOS ignores the latter on body
     and scrolls the page behind the modal anyway; the offset is what stops
     that freeze from also jumping you to the top. */
  body.modal-open {
    position: fixed; left: 0; right: 0; width: 100%; overflow: hidden;
  }
  .modal-card {
    position: relative; background: var(--surface); border: 1px solid var(--hairline); border-radius: 16px;
    max-width: 560px; width: 100%; max-height: 85vh; overflow-y: auto; padding: 20px; box-shadow: var(--shadow);
  }
  /* Search is the one modal whose height changes while you're using it —
     every keystroke swaps "Searching..." for a different number of rows.
     Centred (like the quick-look modal, which is built once and left
     alone) that re-centres the card on each change and slides the input
     out from under the cursor mid-type. Anchored to the top instead, the
     input cannot move: results grow downwards and scroll within the card,
     which is what the eye expects of a search box anyway.

     dvh after vh on purpose: on a phone the keyboard eats the viewport,
     and dvh accounts for it where it's supported while vh is the fallback
     where it isn't. */
  #searchOverlay { align-items: flex-start; }
  #searchOverlay .modal-card {
    display: flex; flex-direction: column;
    max-height: 85vh; max-height: 85dvh;
    overflow: hidden;  /* the panels below scroll, not the card */
  }
  #searchPanel { display: flex; flex-direction: column; min-height: 0; }
  #searchResults { overflow-y: auto; min-height: 0; -webkit-overflow-scrolling: touch; }
  /* Title and layout switch share a row, wrapping to two on a phone rather
     than squeezing the chips. */
  .detail-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; flex-wrap: wrap; margin-bottom: 16px;
  }
  .detail-head .detail-title { margin-bottom: 0; }
  .layout-switch { display: flex; gap: 6px; }

  /* Poster-only view: the point is seeing a whole service at once, so the
     tiles are as small as the artwork stays recognisable at — three across
     on a phone, as many as fit on a desktop. */
  .poster-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 10px;
  }
  .poster-tile {
    position: relative; padding: 0; border: none; background: none; cursor: pointer;
    border-radius: 8px; overflow: hidden; aspect-ratio: 2 / 3;
    -webkit-tap-highlight-color: transparent;
  }
  .poster-tile img { width: 100%; height: 100%; object-fit: cover; display: block; background: var(--hairline); }
  .poster-tile:hover img, .poster-tile:focus-visible img { opacity: 0.82; }
  .poster-tile:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  /* Poster view drops the service badges, which is the point, but "can I
     watch this tonight" is the one thing worth keeping at a glance. Only
     the two answers that mean yes get a dot — a film needing a new
     subscription gets none, so the marks stay rare enough to scan. */
  .poster-dot {
    position: absolute; top: 6px; right: 6px; width: 9px; height: 9px; border-radius: 50%;
    box-shadow: 0 0 0 2px rgba(0, 0, 0, 0.55);
  }
  .poster-dot-have { background: #4ade80; }
  .poster-dot-free { background: #60a5fa; }
  /* Only for a film with no artwork — every film has one today, but a new
     one can arrive before its poster does. */
  .poster-tile-fallback {
    width: 100%; height: 100%; display: flex; align-items: center; justify-content: center;
    padding: 8px; box-sizing: border-box; background: var(--hairline);
    color: var(--text-muted); font-size: 11px; text-align: center; line-height: 1.3;
  }
  /* Film detail page — the long look at one film, as opposed to quick
     look's glance. Nothing is capped or collapsed here. */
  .film-hero { display: flex; gap: 18px; margin-bottom: 8px; flex-wrap: wrap; }
  .film-hero-poster { width: 150px; border-radius: 10px; background: var(--hairline); flex-shrink: 0; }
  .film-hero-body { flex: 1 1 200px; min-width: 0; }
  /* On a phone the desktop poster is wide enough to push the text onto its
     own line, which wastes the whole right half of the screen on nothing —
     a narrower poster keeps title and facts beside it. */
  @media (max-width: 480px) {
    .film-hero { gap: 14px; }
    .film-hero-poster { width: 112px; }
  }
  .film-hero-body h2 { font-size: 21px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
  .film-hero-body h2 span { color: var(--text-faint); font-weight: 400; }
  .film-hero-facts { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12.5px; color: var(--text-muted); margin: 0 0 10px; }
  .film-hero-facts .rating { color: #4ade80; font-weight: 600; }
  .film-hero-synopsis { font-size: 13px; color: var(--text-muted); line-height: 1.55; margin: 0 0 12px; }
  .film-hero-meta { font-size: 12.5px; color: var(--text-muted); margin: 0 0 5px; }
  .film-hero-meta strong { color: var(--text); font-weight: 600; }
  /* Availability in full: every service, grouped by what it costs you,
     rather than quick look's capped single run of badges. */
  .avail-group { margin-bottom: 12px; }
  .avail-group-head { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-faint); font-weight: 600; margin: 0 0 6px; }
  .film-section { margin-top: 26px; }
  .film-section-head {
    display: flex; align-items: baseline; justify-content: space-between; gap: 10px;
    margin: 0 0 10px; flex-wrap: wrap;
  }
  .film-section-head h3 { font-size: 14.5px; font-weight: 600; margin: 0; }
  .film-section-head .count { font-size: 12px; color: var(--text-faint); }
  .film-section-empty { font-size: 12.5px; color: var(--text-faint); }
  /* A film TMDB knows about but the dashboard doesn't track. Dimmed until
     hovered so a section reads tracked-first at a glance, and outlined so
     its missing availability dot reads as "not asked yet" rather than as
     "not available". */
  .poster-tile-live img, .poster-tile-live .poster-tile-fallback { opacity: 0.62; }
  .poster-tile-live:hover img, .poster-tile-live:focus-visible img { opacity: 0.9; }
  .poster-tile-live::after {
    content: ''; position: absolute; inset: 0; border-radius: 8px;
    border: 1px dashed var(--text-faint); opacity: 0.5; pointer-events: none;
  }
  .relation-more {
    margin-top: 10px; font-size: 12px; padding: 5px 11px; background: none;
    color: var(--text-muted); border: 1px solid var(--hairline); border-radius: 999px;
    cursor: pointer;
  }
  .relation-more:hover { color: var(--text); border-color: var(--text-muted); }
  /* A person's biography runs to several paragraphs; clamped, it leaves the
     filmography — the reason for the page — above the fold. */
  .person-bio { display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;
    overflow: hidden; margin-bottom: 6px; }
  .person-bio-open { display: block; overflow: visible; }
  .person-bio-toggle { margin-top: 0; margin-bottom: 10px; }
  /* Names that lead somewhere, marked as such without turning the hero's
     meta lines into a row of buttons. */
  .person-link {
    background: none; border: none; padding: 0; font: inherit; cursor: pointer;
    color: var(--accent); border-bottom: 1px solid transparent;
  }
  .person-link:hover { border-bottom-color: currentColor; }
  .person-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .film-section-head h3 .person-link { color: inherit; }
  .quick-look-more { margin-top: 14px; font-size: 12.5px; padding: 7px 14px; }
  .modal-card .detail-card { border-bottom: none; padding: 0; }
  .modal-card .detail-poster, .modal-card .detail-poster-placeholder { width: 120px; height: 176px; }
  .modal-close {
    position: absolute; top: 10px; right: 10px; background: var(--hairline); border: none; color: var(--text);
    width: 28px; height: 28px; border-radius: 50%; cursor: pointer; font-size: 14px; z-index: 1;
  }
  .modal-close:hover { background: var(--hairline-strong); }
  /* The close button floats over the card's top-right corner, and a title
     long enough to reach it ran underneath — "2001: A Space Odyssey" with the
     ✕ sitting on the last letter. Only the first line needs the room. */
  .modal-card .detail-body h3 { padding-right: 34px; }
  .home-section { margin-bottom: 24px; }
  .home-section-header { font-size: 14px; font-weight: 600; margin: 0 0 10px; }
  .home-section-subtitle { font-weight: 400; color: var(--text-faint); margin-left: 8px; font-size: 12px; }
  .film-card.hidden, #filmsListSelect.hidden { display: none; }
  .home-section-more {
    margin-top: 10px; background: none; border: 1px solid var(--hairline-strong); color: var(--text);
    border-radius: 999px; padding: 6px 14px; font-size: 12px; cursor: pointer;
  }
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
  .icon-btn { position: relative; }
  .icon-btn .new-badge { position: absolute; top: -3px; right: -3px; margin-left: 0; }

  /* ---------- Review screen: a small hand of films, not a grid ---------- */
  .review-progress {
    text-align: center; font-size: 12px; font-weight: 600; color: var(--text-faint);
    letter-spacing: 0.02em; margin-bottom: 14px;
  }
  .review-grid {
    display: grid; grid-template-columns: 1fr; gap: 22px; max-width: 460px; margin: 0 auto;
  }
  @media (min-width: 900px) {
    .review-grid { grid-template-columns: repeat(3, 1fr); max-width: 1160px; align-items: start; }
  }
  .review-card {
    background: var(--surface); border: 1px solid var(--hairline); border-radius: 18px;
    overflow: hidden; box-shadow: var(--shadow); position: relative; touch-action: pan-y;
    user-select: none; cursor: grab;
  }
  .review-card:active { cursor: grabbing; }
  .review-poster-wrap { position: relative; }
  .review-poster {
    width: 100%; height: min(48vh, 420px); min-height: 260px; object-fit: cover; display: block;
    background: var(--hairline); pointer-events: none;
  }
  .review-poster-placeholder {
    width: 100%; height: min(48vh, 420px); min-height: 260px; background: var(--hairline);
  }
  .review-rating-badge {
    position: absolute; bottom: 12px; left: 14px; background: rgba(0,0,0,0.55); backdrop-filter: blur(6px);
    color: #4ade80; font-weight: 700; font-size: 13.5px; padding: 4px 10px; border-radius: 999px;
  }
  .review-stamp {
    position: absolute; top: 22px; padding: 6px 14px; border-radius: 8px; font-size: 20px; font-weight: 800;
    letter-spacing: 0.06em; border: 3px solid; opacity: 0; pointer-events: none; text-transform: uppercase;
  }
  .review-stamp.yes { left: 18px; color: var(--accent); border-color: var(--accent); transform: rotate(-14deg); }
  .review-stamp.no { right: 18px; color: #f87171; border-color: #f87171; transform: rotate(14deg); }
  .review-body { padding: 18px 20px 22px; }
  .review-title { font-size: 21px; font-weight: 700; margin: 0 0 3px; letter-spacing: -0.01em; }
  .review-title .year { color: var(--text-faint); font-weight: 400; }
  .review-director { font-size: 13.5px; color: var(--text-muted); margin: 0 0 10px; }
  .review-director b { color: var(--text); font-weight: 600; }
  .review-subtitled-badge {
    display: inline-flex; align-items: center; gap: 6px; background: rgba(251, 191, 36, 0.14);
    border: 1px solid rgba(251, 191, 36, 0.35); color: #fbbf24; font-size: 12.5px; font-weight: 700;
    padding: 5px 12px; border-radius: 999px; margin: 0 0 12px;
  }
  .review-genre { font-size: 13px; margin: 0 0 12px; }
  .review-synopsis { font-size: 14px; line-height: 1.6; color: var(--text-muted); margin: 0 0 14px; }
  .review-cast { font-size: 12.5px; color: var(--text-faint); margin: 0 0 4px; }
  .review-cast b { color: var(--text-muted); font-weight: 600; }
  .review-actions { display: flex; gap: 10px; margin-top: 18px; }
  .review-btn {
    flex: 1; border-radius: 999px; padding: 15px 14px; font-size: 15px; font-weight: 700;
    cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px;
    transition: transform 0.1s, background 0.15s;
  }
  .review-btn:active { transform: scale(0.97); }
  .review-btn-no {
    background: none; border: 1.5px solid rgba(248, 113, 113, 0.4); color: #f87171; flex: 0.85;
  }
  .review-btn-no:hover { background: rgba(248, 113, 113, 0.14); }
  .review-btn-yes { background: var(--accent); border: none; color: #06201d; }
  .review-btn-yes:hover { filter: brightness(1.08); }
  .review-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .review-card.entering { animation: review-card-enter 0.24s ease; }
  @keyframes review-card-enter { from { opacity: 0; transform: scale(0.98); } }
  .review-empty {
    text-align: center; padding: 70px 20px; color: var(--text-muted);
  }
  .review-empty .icon { font-size: 34px; margin-bottom: 12px; display: block; }
  .review-empty h3 { font-size: 16px; color: var(--text); margin: 0 0 6px; }
  .review-empty p { font-size: 13.5px; margin: 0; }
  @media (max-width: 700px) {
    .review-poster, .review-poster-placeholder { height: 40vh; }
  }
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
  .film-card-genre { font-size: 11px; color: #c98a7d; }
  .film-card-added-service { font-size: 11px; color: var(--accent); font-weight: 500; margin-top: 2px; }
  .film-card-leaving-note { font-size: 11px; color: #fbbf24; font-weight: 500; margin-top: 2px; }
  .film-card-cinema-note { font-size: 11px; color: #c084fc; font-weight: 500; margin-top: 2px; }
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
  .service-card-aggregate { border-color: var(--accent); background: var(--accent-soft); grid-column: 1 / -1; }
  .service-card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
  .service-card-name { font-weight: 600; font-size: 13.5px; }
  .service-card-name i { color: var(--text-faint); font-style: italic; font-weight: 400; display: block; font-size: 11.5px; }
  .service-card-stats { color: var(--text-muted); font-size: 11.5px; }
  .service-card-stats strong { color: var(--text); font-weight: 600; }
  .subs-link { margin-bottom: 14px; font-size: 12.5px; padding: 7px 14px; }
  .subs-intro { font-size: 12.5px; color: var(--text-muted); line-height: 1.55; margin: 0 0 14px; }
  /* The coverage build-up: one row per service, in the order that adds the
     most, with a bar for how much of everything you can watch is covered by
     that point. */
  .subs-step {
    display: grid; grid-template-columns: 1.5rem 1fr auto; align-items: baseline;
    gap: 8px; font-size: 12.5px;
  }
  .subs-step-rank { color: var(--text-faint); font-variant-numeric: tabular-nums; }
  .subs-step-gain { color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .subs-bar { height: 4px; border-radius: 2px; background: var(--hairline); margin: 3px 0 9px; overflow: hidden; }
  .subs-bar span { display: block; height: 100%; background: var(--accent); }
  .subs-service { margin-top: 26px; }
  .subs-service-head {
    display: flex; align-items: baseline; justify-content: space-between; gap: 10px;
    flex-wrap: wrap; margin: 0 0 8px;
  }
  .subs-service-head h3 { font-size: 14.5px; font-weight: 600; margin: 0; }
  .subs-service-head .count { font-size: 12px; color: var(--text-faint); }
  .subs-nothing { font-size: 12.5px; color: var(--text-faint); margin: 0; }
  .service-detail-filters {
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 14px;
  }
  .bottom-nav { display: none; }
  @media (max-width: 700px) {
    body { padding: calc(16px + env(safe-area-inset-top)) 12px 16px; }
    h1 { font-size: 17px; }
    /* The fixed/icon-nav treatment is a desktop-only fix (that's the only
       place the sticky bar was disappearing on scroll) — mobile already has
       its own always-visible fixed bottom-nav, so the top bar just goes back
       to being normal in-flow content here, same as before that change. */
    .app-bar { position: static; padding: 0; border-bottom: none; }
    .app-bar-top { padding: 0; margin-bottom: 18px; }
    .app-bar-title { display: block; }
    .app-bar-controls { padding: 0; margin-bottom: 11px; }
    .tabs { display: none; }
    /* Adding search made this six controls, one more than fits across a
       phone — so the Letterboxd link moved up to the meta line and what's
       left is five evenly-spread actions, laid out like the bottom nav
       (icon over label) rather than as a row of bare glyphs. Spread rather
       than right-aligned because a thumb reaches the left of the screen
       as easily as the right. */
    /* width rather than the default content sizing: this is a flex item of
       .app-bar-top, so without it the row collapses to ~216px and the
       labels wrap inside 40px buttons. align-items overrides the desktop
       rule's centring, so all five match height whatever their label. */
    .header-actions {
      display: flex; justify-content: space-between; align-items: stretch;
      gap: 5px; width: 100%; margin-top: 4px;
    }
    .header-action {
      position: relative; flex: 1 1 0; min-width: 0;
      display: flex; flex-direction: column; align-items: center; gap: 4px;
      /* 44px of height minimum, per the usual tap-target floor — the 34px
         circles these replace were under it. */
      padding: 7px 2px; min-height: 44px; box-sizing: border-box;
      background: none; border: 1px solid var(--hairline); border-radius: 12px;
      color: var(--text-muted); font-size: 10.5px; font-weight: 500; cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }
    .header-action svg { width: 19px; height: 19px; stroke: currentColor; }
    .header-action:active { background: var(--hairline); color: var(--text); }
    /* Out of the text flow, so a three-digit count can't shove the label
       sideways (it used to sit inline and push "Review" off-centre). */
    .header-action .new-badge { position: absolute; top: 3px; right: 6px; margin: 0; }
    .watchlist-link-inline {
      display: inline-block; margin-top: 7px;
      color: var(--accent); text-decoration: none; font-size: 12.5px; font-weight: 500;
    }
    .mobile-only-bar { display: block; }
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
<div class="app-bar" id="appBar">
  <div class="app-bar-top">
    <div class="app-bar-title">
      <h1>Watchlist streaming dashboard</h1>
      <div class="meta" id="meta"></div>
      <!-- Mobile only (.app-bar-title is desktop-hidden): the same link the
           desktop tab row carries, moved out of the action row below so
           five controls fit across a phone in one row. -->
      <a class="watchlist-link-inline" id="watchlistLink" target="_blank">View watchlist on Letterboxd ↗</a>
    </div>
    <div class="tabs">
      <button class="tab-btn active" id="tab-home">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 11l9-7 9 7"></path><path d="M5 10v10h14V10"></path>
        </svg>
        Home<span class="new-badge home-new-badge hidden"></span>
      </button>
      <button class="tab-btn" id="tab-lists">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 6h2M4 12h2M4 18h2M9 6h11M9 12h11M9 18h11"></path>
        </svg>
        Lists
      </button>
      <button class="tab-btn" id="tab-services">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <ellipse cx="12" cy="5" rx="8" ry="3"></ellipse>
          <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"></path>
          <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"></path>
        </svg>
        Services
      </button>
      <button class="tab-btn" id="tab-films">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="3" width="7" height="7" rx="1.5"></rect>
          <rect x="14" y="3" width="7" height="7" rx="1.5"></rect>
          <rect x="3" y="14" width="7" height="7" rx="1.5"></rect>
          <rect x="14" y="14" width="7" height="7" rx="1.5"></rect>
        </svg>
        Films
      </button>
      <button class="tab-btn" id="tab-cinemas">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2.5" y="6" width="19" height="14" rx="1.5"></rect>
          <path d="M2.5 9.5h19M6 6V3.5M11 6V3.5M16 6V3.5"></path>
        </svg>
        Cinemas
      </button>
      <button class="tab-btn" id="tab-review">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 11l3 3L22 4"></path>
          <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>
        </svg>
        Review<span class="new-badge review-count-badge hidden"></span>
      </button>
      <button class="tab-btn" id="tab-sarah">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z"></path>
        </svg>
        Sarah's list
      </button>
      <button class="tab-btn" id="tab-settings">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3.2"></circle>
          <path d="M12 3v3M12 18v3M21 12h-3M6 12H3M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1M18.4 18.4l-2.1-2.1M7.7 7.7L5.6 5.6"></path>
        </svg>
        Settings
      </button>
    </div>
    <div class="tabs tabs-right">
      <button class="tab-btn" id="filmSearchBtn" title="Search any film on Letterboxd (/)">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4.3-4.3"></path>
        </svg>
        Search
      </button>
      <button class="tab-btn" id="reloadPageBtn" title="Reload this page">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M23 4v6h-6"></path>
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
        </svg>
        Reload
      </button>
      <button class="tab-btn" id="triggerRefreshBtn" title="Re-run the daily check and redeploy">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M17.5 19a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.4-1.5A5 5 0 0 0 7 19h10.5z"></path>
          <path d="M12 12v5M9.5 14.5L12 17l2.5-2.5"></path>
        </svg>
        Refresh data
      </button>
      <button class="tab-btn" id="surpriseMeBtnDesktop">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect x="4" y="4" width="16" height="16" rx="3"></rect>
          <circle cx="8.5" cy="8.5" r="1.1" fill="currentColor" stroke="none"></circle>
          <circle cx="15.5" cy="8.5" r="1.1" fill="currentColor" stroke="none"></circle>
          <circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"></circle>
          <circle cx="8.5" cy="15.5" r="1.1" fill="currentColor" stroke="none"></circle>
          <circle cx="15.5" cy="15.5" r="1.1" fill="currentColor" stroke="none"></circle>
        </svg>
        Surprise me
      </button>
      <a class="tab-btn tab-btn-accent" id="watchlistLinkDesktop" target="_blank">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
          <path d="M15 3h6v6"></path>
          <path d="M10 14L21 3"></path>
        </svg>
        Letterboxd
      </a>
    </div>
    <!-- Five labelled controls spread across the width, built like the
         bottom nav rather than as a row of bare glyphs: the emoji they used
         to be rendered differently on every platform, sat at a 34px tap
         target, and left "☁" to stand for "re-run the daily check". Same
         icons as the desktop tab row, since these do the same things.

         Reload has no button here — pull-to-refresh already does a plain
         page reload on a phone. Refresh-data has no gesture equivalent, so
         it still needs an explicit control. -->
    <div class="header-actions">
      <button class="header-action" id="filmSearchBtnMobile" aria-label="Search films" title="Search any film on Letterboxd">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4.3-4.3"></path>
        </svg>
        Search
      </button>
      <button class="header-action" id="triggerRefreshBtnMobile" aria-label="Refresh data" title="Re-run the daily check and redeploy">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M17.5 19a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.4-1.5A5 5 0 0 0 7 19h10.5z"></path>
          <path d="M12 12v5M9.5 14.5L12 17l2.5-2.5"></path>
        </svg>
        Refresh
      </button>
      <button class="header-action" id="reviewBtnMobile" aria-label="Review" title="Films to review with Sarah">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 11l3 3L22 4"></path>
          <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>
        </svg>
        Review<span class="new-badge review-count-badge-mobile hidden"></span>
      </button>
      <button class="header-action" id="sarahBtnMobile" aria-label="Sarah's list" title="Sarah's watchlist">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z"></path>
        </svg>
        Sarah
      </button>
      <button class="header-action" id="settingsBtn" aria-label="Settings" title="Settings">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3.2"></circle>
          <path d="M12 3v3M12 18v3M21 12h-3M6 12H3M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1M18.4 18.4l-2.1-2.1M7.7 7.7L5.6 5.6"></path>
        </svg>
        Settings
      </button>
    </div>
  </div>
  <div class="app-bar-controls" id="appBarControls">
    <div class="controls" data-view="lists" id="controls-lists">
      <div class="search-wrap">
        <input type="text" id="listSearch" placeholder="Search lists and films...">
        <span class="search-clear hidden" id="listSearchClear">✕</span>
      </div>
      <span class="pill-toggle" id="listsHaveOnly">Only on a service I have</span>
    </div>
    <div class="controls" data-view="services" id="controls-services">
      <select id="serviceSelect"></select>
      <select id="serviceGenreSelect"></select>
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
      <span class="sarah-filter" id="serviceSarahFilter"></span>
    </div>
    <div class="controls" data-view="films" id="controls-films">
      <select id="filmsCountrySelect"></select>
      <select id="filmsGenreSelect"></select>
      <select id="filmsListSelect"></select>
      <div class="search-wrap">
        <input type="text" id="search" placeholder="Search title, year, director, cast...">
        <span class="search-clear hidden" id="searchClear">✕</span>
      </div>
      <select id="filmsSortSelect">
        <option value="title">Sort: Title (A–Z)</option>
        <option value="year">Sort: Year (newest)</option>
        <option value="rating">Sort: Rating (highest)</option>
        <option value="coverage_countries">Sort: Most countries</option>
      </select>
      <span id="filmsFilterToggles"></span>
      <span class="pill-toggle" id="notHaveOnly">Not on a service I have</span>
      <span class="sarah-filter" id="filmsSarahFilter"></span>
      <span class="layout-switch" id="filmsLayoutSwitch"></span>
    </div>
    <div class="controls" data-view="cinemas" id="controls-cinemas">
      <div class="search-wrap">
        <input type="text" id="cinemaSearch" placeholder="Search title, director...">
        <span class="search-clear hidden" id="cinemaSearchClear">✕</span>
      </div>
      <span id="cinemaVenueToggles"></span>
      <span id="cinemaDateFilter"></span>
    </div>
    <div class="controls" data-view="sarah" id="controls-sarah">
      <div class="search-wrap">
        <input type="text" id="sarahSearch" placeholder="Search title, year, director, cast...">
        <span class="search-clear hidden" id="sarahSearchClear">✕</span>
      </div>
    </div>
  </div>
</div>

<section class="view active" id="view-home">
  <div class="surprise-bar mobile-only-bar">
    <button class="surprise-btn" id="surpriseMeBtnMobile">🎲 Surprise me</button>
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

<section class="view" id="view-lists">
  <div class="quick-filters" id="listGroupJump"></div>
  <div id="listSections"></div>
</section>

<section class="view" id="view-services">
  <div class="active-filters" id="activeServiceFilters"></div>
  <button class="surprise-btn subs-link" id="openSubscriptions">What you'd lose →</button>
  <div id="servicesGrid" class="service-cards"></div>
</section>

<section class="view" id="view-subscriptions">
  <button class="back-btn" id="backFromSubscriptions">← Back to services</button>
  <div id="subscriptionsContent"></div>
</section>

<section class="view" id="view-service-detail">
  <button class="back-btn" id="backToServices">← Back to services</button>
  <div class="detail-head">
    <h2 class="detail-title" id="serviceDetailTitle"></h2>
    <div class="layout-switch" id="serviceLayoutSwitch"></div>
  </div>
  <div class="service-detail-filters">
    <span class="pill-toggle" id="serviceUniqueOnly">Only on this service</span>
    <div class="quick-filters" id="serviceCountryPills"></div>
  </div>
  <div id="serviceDetailCards"></div>
</section>

<section class="view" id="view-settings">
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
      A new Letterboxd log already triggers this automatically within about 15 minutes — the
      "Refresh data" button in the top bar is just for forcing it sooner. Re-runs the daily
      check (watchlist, streaming availability, recommendations) and redeploys; takes about
      5 minutes, then reload the page.
    </p>
  </div>
</section>

<section class="view" id="view-films">
  <div class="active-filters" id="activeFilmFilters"></div>
  <div id="filmsGrid" class="film-cards"></div>
</section>

<section class="view" id="view-cinemas">
  <div class="active-filters" id="activeCinemaFilters"></div>
  <div id="cinemasGrid" class="film-cards"></div>
</section>

<section class="view" id="view-review">
  <div id="reviewScreen"></div>
</section>

<section class="view" id="view-sarah">
  <p class="muted" style="margin: 0 0 14px;">Sarah's own Letterboxd watchlist — just for browsing.</p>
  <div id="sarahGrid" class="film-cards"></div>
</section>

<section class="view" id="view-film-detail">
  <button class="back-btn" id="filmDetailBack">← Back</button>
  <div id="filmDetailContent"></div>
</section>

<section class="view" id="view-person-detail">
  <button class="back-btn" id="personDetailBack">← Back</button>
  <div id="personDetailContent"></div>
</section>

<nav class="bottom-nav">
  <button class="bottom-nav-btn active" id="nav-home">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 11l9-7 9 7"></path><path d="M5 10v10h14V10"></path>
    </svg>
    Home<span class="new-badge home-new-badge hidden"></span>
  </button>
  <button class="bottom-nav-btn" id="nav-lists">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M4 6h2M4 12h2M4 18h2M9 6h11M9 12h11M9 18h11"></path>
    </svg>
    Lists
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
  <button class="bottom-nav-btn" id="nav-cinemas">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <rect x="2.5" y="6" width="19" height="14" rx="1.5"></rect>
      <path d="M2.5 9.5h19M6 6V3.5M11 6V3.5M16 6V3.5"></path>
    </svg>
    Cinemas
  </button>
</nav>

<div class="modal-overlay" id="quickLookOverlay">
  <div class="modal-card">
    <button class="modal-close" id="quickLookClose">✕</button>
    <div id="quickLookContent"></div>
  </div>
</div>

<div class="modal-overlay" id="searchOverlay">
  <div class="modal-card">
    <button class="modal-close" id="searchClose">✕</button>
    <div id="searchPanel">
      <input type="text" class="search-modal-input" id="filmSearchInput" autocomplete="off"
             placeholder="Search any film on Letterboxd...">
      <div id="searchResults"></div>
    </div>
  </div>
</div>

<div class="toast hidden" id="toast"></div>

<script>
const DATA = __DATA__;
const TABS = ['home', 'lists', 'services', 'films', 'cinemas'];

function esc(text) {
  if (text == null) return '';
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escAttr(text) {
  return esc(text).replace(/"/g, '&quot;');
}

function formatRuntime(minutes) {
  if (minutes == null) return '';
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return h ? (m ? h + 'h ' + m + 'm' : h + 'h') : m + 'm';
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
document.getElementById('watchlistLinkDesktop').href = DATA.letterboxd_watchlist_url;

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
    // Drawn from the state rather than assumed on: these used to start on
    // without exception, so nothing ever set this at build time. Now that
    // the Services tab starts with one off, a pill that looked enabled
    // while its filter was applied would be the UI lying about itself.
    if (!filterState[key]) span.classList.add('off');
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

// One-click jump chips (Services: your own services; Country: your three
// home markets) — entries: [{value, label, count}]. "All" clears back to
// the unfiltered view — skip it (showAll: false) for a tab like Country
// where exactly one value is always selected, so there's no "unfiltered"
// state to clear back to. Reused across both tabs, same way
// renderClassificationToggles is.
function renderQuickJumpChips(containerId, entries, activeValue, onPick, showAll = true) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = '';
  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.textContent = 'Jump to:';
  container.appendChild(hint);

  const makeChip = (value, label, count) => {
    const chip = document.createElement('span');
    chip.className = 'quick-country' + (activeValue === value ? ' active' : '');
    chip.innerHTML = esc(label) + (count != null ? ' <span class="count">' + count + '</span>' : '');
    chip.addEventListener('click', () => onPick(value));
    container.appendChild(chip);
  };
  if (showAll) makeChip('', 'All', null);
  entries.forEach(e => makeChip(e.value, e.label, e.count));
}

// Services you have/could get again, plus any free service in your three
// home markets, are what you'd actually reach for — used for the
// serviceSelect dropdown's "Have or can get" group, which is fine to cast
// this wide since it's just one optgroup among the full alphabetical list.
function topServiceBrands() {
  return new Set(
    DATA.services
      .filter(r => r.classification === 'have' || r.classification === 'could_get_again' ||
                   (r.classification === 'free' && ['AU', 'GB', 'US'].includes(r.country)))
      .map(r => r.brand)
  );
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
document.getElementById('tab-lists').addEventListener('click', () => showView('lists'));
document.getElementById('tab-services').addEventListener('click', () => showView('services'));
document.getElementById('tab-films').addEventListener('click', () => showView('films'));
document.getElementById('tab-cinemas').addEventListener('click', () => showView('cinemas'));
document.getElementById('tab-review').addEventListener('click', () => showView('review'));
document.getElementById('tab-sarah').addEventListener('click', () => showView('sarah'));
document.getElementById('tab-settings').addEventListener('click', () => { renderSettings(); showView('settings'); });
document.getElementById('nav-home').addEventListener('click', () => showView('home'));
document.getElementById('nav-lists').addEventListener('click', () => showView('lists'));
document.getElementById('nav-services').addEventListener('click', () => showView('services'));
document.getElementById('nav-films').addEventListener('click', () => showView('films'));
document.getElementById('nav-cinemas').addEventListener('click', () => showView('cinemas'));
document.getElementById('backToServices').addEventListener('click', () => showView('services'));
// Mobile keeps small standalone icons for Review/Sarah/Settings (none of
// them are among the bottom-nav's 4 destinations) — desktop's peer tab
// buttons above are the primary entry point for all three.
document.getElementById('reviewBtnMobile').addEventListener('click', () => showView('review'));
document.getElementById('sarahBtnMobile').addEventListener('click', () => showView('sarah'));
document.getElementById('settingsBtn').addEventListener('click', () => { renderSettings(); showView('settings'); });

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

document.getElementById('reloadPageBtn').addEventListener('click', () => location.reload());

const refreshDataBtns = [
  document.getElementById('triggerRefreshBtn'),
  document.getElementById('triggerRefreshBtnMobile'),
];

async function handleRefreshDataClick() {
  refreshDataBtns.forEach(btn => { btn.disabled = true; });
  showToast('Triggering refresh...');
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
      showToast('Refresh triggered — usually takes about 5 minutes, then reload the page.', 7000);
    } else {
      showToast('Unexpected response (' + response.status + ').');
    }
  } catch (error) {
    showToast('Request failed: ' + error.message);
  } finally {
    refreshDataBtns.forEach(btn => { btn.disabled = false; });
  }
}
refreshDataBtns.forEach(btn => btn.addEventListener('click', handleRefreshDataClick));

// touch-action above stops double-tap and pinch through the compositor, but
// Safari on iOS still zooms the *page* from its own gesture events, which
// nothing in CSS reaches. Cancelling them is the only thing that does.
//
// Gated on a coarse pointer for the same reason the CSS is: these are the
// events a Mac trackpad pinch fires too, and desktop zoom is meant to keep
// working.
if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) {
  ['gesturestart', 'gesturechange', 'gestureend'].forEach(name => {
    document.addEventListener(name, event => event.preventDefault(), { passive: false });
  });
}

// Freezing the page behind a modal. Counted rather than a boolean, because
// quick look can be opened from inside the search overlay and closing the
// upper one must not unfreeze the page while the lower one is still up.
let openModalCount = 0;
let lockedScrollY = 0;

function lockPageScroll() {
  if (openModalCount++ > 0) return;
  lockedScrollY = window.scrollY;
  document.body.style.top = (-lockedScrollY) + 'px';
  document.body.classList.add('modal-open');
}

function unlockPageScroll() {
  openModalCount = Math.max(0, openModalCount - 1);
  if (openModalCount > 0) return;
  document.body.classList.remove('modal-open');
  document.body.style.top = '';
  // Instant, not smooth: this is restoring where you already were, and
  // animating it reads as the page jumping about on its own.
  window.scrollTo(0, lockedScrollY);
}

// Pull-to-refresh reloads the page, and the page came back on Home every
// time regardless of where you were. sessionStorage rather than local: it
// is about this visit, and a new tab tomorrow should still start at Home.
const LAST_VIEW_KEY = 'watchlist_last_view_v1';

// Read once, here, before anything renders. Startup ends with showView('home')
// to settle the controls bar, and that write would otherwise overwrite the
// very value the restore below is trying to read back.
const savedLastView = (() => {
  try {
    return JSON.parse(sessionStorage.getItem(LAST_VIEW_KEY) || 'null');
  } catch {
    return null;
  }
})();

function rememberLastView(name, scrollY) {
  try {
    sessionStorage.setItem(LAST_VIEW_KEY, JSON.stringify({ view: name, scrollY: scrollY || 0 }));
  } catch {
    // Private browsing / storage full — a refresh just lands on Home, which
    // is what it did before this existed.
  }
}

// `scroll: 'manual'` means the caller positions the page itself (the detail
// pages do, from their own trail); anything else restores wherever this view
// was last left.
function showView(name, options) {
  // Changing view behind the search overlay leaves the new page underneath
  // it, which is never what's wanted — every route here (Full details from
  // quick look, a director's name, a cinema listing) means "take me there",
  // so the search is done. Closed before the scroll is read, because the
  // page is frozen while the overlay is up and window.scrollY reads 0 until
  // the unlock puts it back.
  closeFilmSearch();
  rememberCurrentScroll();
  document.querySelectorAll('section.view').forEach(el => el.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  // The two drill-downs aren't tabs of their own, so each borrows the tab it
  // belongs to: service-detail always Services, film-detail whichever tab you
  // opened the film from — which can itself be service-detail, hence the
  // second hop rather than a single check.
  let owner = (name === 'film-detail' || name === 'person-detail') ? filmDetailReturnView : name;
  if (owner === 'service-detail' || owner === 'subscriptions') owner = 'services';
  TABS.forEach(n => {
    const isActive = n === owner;
    document.getElementById('tab-' + n).classList.toggle('active', isActive);
    document.getElementById('nav-' + n).classList.toggle('active', isActive);
  });
  // Settings is a fifth desktop tab but not one of mobile's 4 bottom-nav
  // destinations (mobile keeps its own small gear icon instead) — handled
  // separately rather than folded into TABS so that loop above doesn't
  // break looking for a nonexistent "nav-settings" button.
  if (!options || options.scroll !== 'manual') restoreScroll(viewScrollPositions['view-' + name]);
  // The detail pages aren't worth coming back to on a refresh — they're
  // rebuilt from a trail that a reload throws away — so the tab they belong
  // to is what's remembered.
  rememberLastView(DETAIL_VIEW_IDS.includes('view-' + name) ? filmDetailReturnView : name,
                   viewScrollPositions['view-' + name]);
  document.getElementById('tab-settings').classList.toggle('active', owner === 'settings');
  document.getElementById('tab-review').classList.toggle('active', owner === 'review');
  document.getElementById('tab-sarah').classList.toggle('active', owner === 'sarah');

  // The fixed bar's second row holds each tab's own search/sort/filter
  // controls (moved up out of the scrolling content so they never scroll
  // out of reach) — exactly one is shown at a time, matched strictly by
  // view name. service-detail/settings/home have no controls of their own
  // (service-detail is a drill-down; settings has none; home's "Surprise
  // me" moved up into the nav row itself), so the whole row collapses away
  // rather than showing a stale/wrong one.
  const controlsSlot = document.getElementById('appBarControls');
  const matchingControls = controlsSlot.querySelector('[data-view="' + name + '"]');
  controlsSlot.querySelectorAll('[data-view]').forEach(el => {
    el.style.display = (el === matchingControls) ? '' : 'none';
  });
  controlsSlot.classList.toggle('empty', !matchingControls);

  // All tabs share one page-level scroll (only one section is visible at a
  // time), so switching tabs without resetting scroll left whatever the
  // previous tab was scrolled to still in effect on the new one.
  window.scrollTo(0, 0);
  updateAppBarOffset();
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
    ? '<img class="poster-thumb" loading="lazy" src="' + escAttr(row.poster_url) + '" onerror="this.outerHTML=\\'<div class=&quot;poster-placeholder&quot;></div>\\'">'
    : '<div class="poster-placeholder"></div>';
  const director = row.director ? '<div class="film-card-director">' + esc(row.director) + '</div>' : '';
  const metaParts = [];
  if (row.genre && row.genre.length) metaParts.push(esc(row.genre.join(', ')));
  if (row.runtime_minutes != null) metaParts.push(formatRuntime(row.runtime_minutes));
  const genre = metaParts.length ? '<div class="film-card-genre">' + metaParts.join(' · ') + '</div>' : '';
  const addedService = row.added_service
    ? '<div class="film-card-added-service">Added to ' + esc(row.added_service) + '</div>' : '';
  const leavingNote = row.leaving_note
    ? '<div class="film-card-leaving-note">' + esc(row.leaving_note) + '</div>' : '';
  const cinemaNote = row.cinema_note
    ? '<div class="film-card-cinema-note">🎬 ' + esc(row.cinema_note) + '</div>' : '';
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
      director + genre + addedService + leavingNote + cinemaNote + servicesHtml +
    '</div>';
  return div;
}

// Code -> name, built once from the country index the payload ships.
// Up here rather than with the rest of init because countryLabel() below
// is called during the first services/films render, which runs earlier.
DATA.countryNames = {};
DATA.countries.forEach(c => { DATA.countryNames[c.code] = c.name; });

// Your three home markets — same set as FREE_TIER_COUNTRIES server-side.
// The country tab they used to head is gone; these still pick the default
// country for a poster tile and lead the service detail page's pills.
const HOME_COUNTRY_CODES = ['GB', 'US', 'AU'];

function countryLabel(code) {
  return (DATA.countryNames && DATA.countryNames[code]) || code;
}

// A service on every VPN-reachable country (or a film with a dozen credited
// directors) turns one card into a wall of badges and breaks the grid's
// height rhythm — cap the inline list and let a "+N more" reveal the rest
// on demand instead of dropping the data entirely.
const BADGE_CAP = 8;
// Quick look is meant to be one glance at one card. Its own caps are
// tighter than the inline cards' because it has a fixed box to fit inside
// and no room to grow: fewer services shown, and the "leaving soon" notes —
// one per country, so a film leaving Prime everywhere produced 114 lines and
// three thousand pixels of card — capped hard. Everything hidden is still a
// tap away behind the same "+N more" the badges already use.
const QUICK_LOOK_BADGE_CAP = 4;
const EXPIRING_NOTE_CAP = 2;

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
function showToast(message, duration = 4000) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add('hidden'), duration);
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

// ---------- Watch-together review — a small hand of films, not a grid ----------

// Oldest-added first, so nothing sits unreviewed indefinitely (matches the
// backfill order main.py seeds pending rows in). Always the front of this
// list is shown — deciding a film removes it from the pending set entirely,
// so the next-oldest naturally slides into view with no index bookkeeping.
function pendingQueue() {
  return DATA.films
    .filter(f => f.watch_together_status === 'pending')
    .sort((a, b) => (a.watch_together_added_at || '').localeCompare(b.watch_together_added_at || ''));
}

// Wide viewports get three at once (less empty margin either side of one
// narrow card, and lets you triage a few in one glance) — phones stay one
// at a time, matching the 900px grid breakpoint in the CSS above.
function reviewCardsToShow() {
  return window.matchMedia('(min-width: 900px)').matches ? 3 : 1;
}

function updateReviewBadge() {
  const count = pendingQueue().length;
  document.querySelectorAll('.review-count-badge, .review-count-badge-mobile').forEach(el => {
    el.textContent = String(count);
    el.classList.toggle('hidden', count === 0);
  });
}

function animateReviewCardExit(cardEl, direction, dy) {
  const flyX = direction * Math.max(window.innerWidth, 600);
  cardEl.style.transition = 'transform 0.32s ease, opacity 0.32s ease';
  cardEl.style.transform = 'translate(' + flyX + 'px, ' + (dy || 0) + 'px) rotate(' + (direction * 24) + 'deg)';
  cardEl.style.opacity = '0';
}

// Unlike dismissRecommendation, this goes through the regenerate-dashboard
// workflow (not a plain GitHub commit) since the status lives in Postgres,
// not a config YAML file — see db.py's watch_together table. Still
// optimistic: the card flies off and the next film slides into view
// immediately: the actual write/regen happens in the background over the
// next under-a-minute or so. `dy` lets a completed swipe carry on in the
// same direction it was already being dragged, rather than snapping back
// to center before flying off.
//
// The actual write is debounce-batched, not sent per tap — a workflow run
// (checkout, pip install, DB write, dashboard regen, Pages deploy) costs
// real Actions minutes regardless of how small the change, and reviewing
// is normally done in a burst of several/many taps in one sitting, not one
// at a time. See queueDecision/flushPendingDecisions below.
function tagFilm(slug, status, cardEl, dy) {
  animateReviewCardExit(cardEl, status === 'confirmed' ? 1 : -1, dy || 0);
  const row = DATA.films.find(f => f.slug === slug);
  if (row) row.watch_together_status = status;
  const film = DATA.films_by_slug[slug];
  if (film) film.watch_together_status = status;
  setTimeout(renderReview, 300);
  queueDecision(slug, status);
}

// ---------- Debounced batch write for Review decisions ----------

// localStorage-backed (not just an in-memory array) so a decision already
// queued survives a reload or a crash before its debounce fired — flushed
// again on next load below instead of being silently lost.
const REVIEW_QUEUE_KEY = 'watchlist_pending_decisions_v1';
const REVIEW_FLUSH_DEBOUNCE_MS = 6000;
let reviewFlushTimer = null;

function loadPendingDecisions() {
  try {
    return JSON.parse(localStorage.getItem(REVIEW_QUEUE_KEY) || '[]');
  } catch {
    return [];
  }
}

function savePendingDecisions(list) {
  try {
    localStorage.setItem(REVIEW_QUEUE_KEY, JSON.stringify(list));
  } catch {
    // Private browsing / storage full — the batch still sends normally
    // this session, it just won't survive a crash or reload before then.
  }
}

function queueDecision(slug, status) {
  const list = loadPendingDecisions().filter(d => d.slug !== slug);
  list.push({ slug, status });
  savePendingDecisions(list);
  clearTimeout(reviewFlushTimer);
  reviewFlushTimer = setTimeout(flushPendingDecisions, REVIEW_FLUSH_DEBOUNCE_MS);
}

function flushPendingDecisions() {
  clearTimeout(reviewFlushTimer);
  const list = loadPendingDecisions();
  if (!list.length) return;
  // Cleared up front rather than after the request lands — a decision made
  // while this fetch is in flight queues fresh instead of being folded
  // into (and delayed by) a batch that's already on its way out. If the
  // request fails, the catch below merges this batch back in rather than
  // dropping it.
  savePendingDecisions([]);
  const count = list.length;
  fetch(DATA.settings.refresh_worker_url + '/tag-film', {
    method: 'POST',
    headers: {
      'X-Trigger-Secret': DATA.settings.refresh_trigger_secret,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ decisions: list }),
  })
    .then(response => response.json().catch(() => null))
    .then(body => {
      if (!body || !body.ok) {
        savePendingDecisions(loadPendingDecisions().concat(list));
        showToast(count + (count === 1 ? ' decision' : ' decisions') + ' saved locally, but the write failed — will retry.');
      }
    })
    .catch(() => {
      savePendingDecisions(loadPendingDecisions().concat(list));
      showToast(count + (count === 1 ? ' decision' : ' decisions') + ' saved locally, but the write failed — will retry.');
    });
}

// Best-effort flush whenever the tab is hidden or the page is being torn
// down — covers switching apps/tabs and closing the browser, not just
// waiting out the debounce, so a review session doesn't need to sit idle
// for it to actually send.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') flushPendingDecisions();
});
window.addEventListener('pagehide', flushPendingDecisions);

// Anything left queued from a crash or reload before its debounce fired —
// send it now rather than waiting on the next decision to start a new timer.
flushPendingDecisions();

function reviewCardHtml(film) {
  const poster = film.poster_url
    ? '<img class="review-poster" loading="lazy" src="' + escAttr(film.poster_url) +
      '" onerror="this.outerHTML=\\'<div class=&quot;review-poster-placeholder&quot;></div>\\'">'
    : '<div class="review-poster-placeholder"></div>';
  const rating = film.rating != null ? '<div class="review-rating-badge">' + film.rating.toFixed(2) + '★</div>' : '';
  const director = film.director ? '<p class="review-director"><b>Director</b> ' + esc(film.director) + '</p>' : '';
  const runtime = film.runtime_minutes != null
    ? '<p class="review-director"><b>Runtime</b> ' + formatRuntime(film.runtime_minutes) + '</p>' : '';
  // The explicit call-out Sarah asked for — only shown when the film's
  // original language isn't English (see languages.is_subtitled), never a
  // "not subtitled" badge for the common case, same "only show when it's
  // actually relevant" convention leaving_soon/added_service already use.
  const subtitled = film.is_subtitled
    ? '<div class="review-subtitled-badge">🌐 Subtitled film' +
      (film.language_name ? ' · ' + esc(film.language_name) : '') + '</div>'
    : '';
  const genre = (film.genre && film.genre.length)
    ? '<p class="review-genre detail-genre">' + esc(film.genre.join(', ')) + '</p>' : '';
  const synopsis = film.synopsis ? '<p class="review-synopsis"><b>Plot</b> ' + esc(film.synopsis) + '</p>' : '';
  const cast = film.starring ? '<p class="review-cast"><b>Starring</b> ' + esc(film.starring) + '</p>' : '';

  return (
    '<div class="review-card entering" data-slug="' + escAttr(film.slug) + '">' +
      '<div class="review-poster-wrap">' + poster + rating +
        '<div class="review-stamp yes">Watch together</div>' +
        '<div class="review-stamp no">Not for us</div>' +
      '</div>' +
      '<div class="review-body">' +
        '<h2 class="review-title">' + esc(film.title) +
          (film.year ? ' <span class="year">' + film.year + '</span>' : '') + '</h2>' +
        director + runtime + subtitled + genre + synopsis + cast +
        '<div class="review-actions">' +
          '<button type="button" class="review-btn review-btn-no">✕ Not for us</button>' +
          '<button type="button" class="review-btn review-btn-yes">♥ Watch together</button>' +
        '</div>' +
      '</div>' +
    '</div>'
  );
}

// Phase 2: drag-to-decide, Tinder-style — a horizontal drag past the
// threshold flies the card off and decides it, same as tapping a button;
// a mostly-vertical drag is left alone so the page can still scroll on
// mobile instead of the gesture being captured as a failed swipe attempt.
const SWIPE_THRESHOLD = 110;

function attachSwipe(cardEl, film) {
  let dragging = false, direction = null, startX = 0, startY = 0, dx = 0, dy = 0;
  const yesStamp = cardEl.querySelector('.review-stamp.yes');
  const noStamp = cardEl.querySelector('.review-stamp.no');

  function setStamps(progress) {
    yesStamp.style.opacity = dx > 0 ? progress : 0;
    noStamp.style.opacity = dx < 0 ? progress : 0;
  }

  cardEl.addEventListener('pointerdown', event => {
    if (event.target.closest('.review-btn')) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    dragging = true;
    direction = null;
    dx = 0; dy = 0;
    startX = event.clientX; startY = event.clientY;
    cardEl.setPointerCapture(event.pointerId);
    cardEl.style.transition = 'none';
  });

  cardEl.addEventListener('pointermove', event => {
    if (!dragging) return;
    dx = event.clientX - startX;
    dy = event.clientY - startY;
    if (direction === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
      direction = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
    }
    if (direction !== 'h') return;
    cardEl.style.transform = 'translate(' + dx + 'px, ' + (dy * 0.15) + 'px) rotate(' + (dx / 16) + 'deg)';
    setStamps(Math.min(Math.abs(dx) / SWIPE_THRESHOLD, 1));
  });

  function finish() {
    if (!dragging) return;
    dragging = false;
    if (direction === 'h' && Math.abs(dx) > SWIPE_THRESHOLD) {
      cardEl.querySelectorAll('.review-btn').forEach(btn => { btn.disabled = true; });
      tagFilm(film.slug, dx > 0 ? 'confirmed' : 'declined', cardEl, dy);
    } else {
      cardEl.style.transition = 'transform 0.2s ease';
      cardEl.style.transform = '';
      setStamps(0);
    }
    direction = null;
  }
  cardEl.addEventListener('pointerup', finish);
  cardEl.addEventListener('pointercancel', finish);
}

function attachReviewCard(cardEl, film) {
  cardEl.querySelector('.review-btn-yes').addEventListener('click', () => {
    cardEl.querySelectorAll('.review-btn').forEach(btn => { btn.disabled = true; });
    tagFilm(film.slug, 'confirmed', cardEl);
  });
  cardEl.querySelector('.review-btn-no').addEventListener('click', () => {
    cardEl.querySelectorAll('.review-btn').forEach(btn => { btn.disabled = true; });
    tagFilm(film.slug, 'declined', cardEl);
  });
  attachSwipe(cardEl, film);
}

function renderReview() {
  const queue = pendingQueue();
  const container = document.getElementById('reviewScreen');
  updateReviewBadge();

  if (!queue.length) {
    container.innerHTML =
      '<div class="review-empty">' +
        '<span class="icon">🎬</span>' +
        '<h3>You’re all caught up</h3>' +
        '<p>Nothing waiting on a decision right now — new watchlist additions land here automatically.</p>' +
      '</div>';
    return;
  }

  const shown = queue.slice(0, reviewCardsToShow());
  const label = queue.length === 1 ? '1 film to review' : queue.length + ' films to review';
  container.innerHTML =
    '<div class="review-progress">' + label + '</div>' +
    '<div class="review-grid">' + shown.map(reviewCardHtml).join('') + '</div>';

  container.querySelectorAll('.review-card').forEach(cardEl => {
    const film = shown.find(f => f.slug === cardEl.dataset.slug);
    attachReviewCard(cardEl, film);
  });
}

document.getElementById('reviewScreen').addEventListener('click', event => {
  if (event.target.closest('.review-btn')) return;
  const card = event.target.closest('.review-card');
  // A finished drag still fires a click on release — only open quick-look
  // when the card is at rest (no active swipe transform), so a completed
  // or in-progress swipe doesn't also pop the modal open underneath it.
  if (card && !card.style.transform) openQuickLook(card.dataset.slug);
});

let reviewResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(reviewResizeTimer);
  reviewResizeTimer = setTimeout(renderReview, 200);
});

// ---------- Sarah's watchlist (browsing only — additive, not tied to watch_together) ----------

function renderSarah() {
  const q = document.getElementById('sarahSearch').value.trim().toLowerCase();
  const films = DATA.sarah_films.filter(f => !q || searchHaystack(f).includes(q));
  const container = document.getElementById('sarahGrid');
  container.innerHTML = '';
  const frag = document.createDocumentFragment();
  films.forEach(film => frag.appendChild(filmCardShell(film, '')));
  container.appendChild(frag);
  ensureNotEmpty(container, q ? 'No films match your search.' : "Sarah's watchlist is empty (or not configured yet).");
}

document.getElementById('sarahGrid').addEventListener('click', event => {
  if (event.target.closest('a.film-link')) return;
  const card = event.target.closest('.film-card');
  if (card) openQuickLook(card.dataset.slug);
});
document.getElementById('sarahSearch').addEventListener('input', renderSarah);
wireSearchClear('sarahSearch', 'sarahSearchClear', renderSarah);

const CUSTOM_LIST_PREVIEW = 6;

// Home and Lists render the same section shape; only what they are fed
// differs, so the builder is shared rather than copied. `films` is passed
// separately from `section` because Lists filters a section's films down
// before rendering and still wants the section's own header/subtitle.
function renderSections(container, sections, filmsFor) {
  container.innerHTML = '';
  sections.forEach(section => {
    const sectionFilms = filmsFor ? filmsFor(section) : section.films;
    if (!sectionFilms.length) return;
    const wrap = document.createElement('div');
    wrap.className = 'home-section';
    const header = document.createElement('h2');
    header.className = 'home-section-header';
    header.textContent = section.header;
    if (section.subtitle) {
      const sub = document.createElement('span');
      sub.className = 'home-section-subtitle';
      sub.textContent = section.subtitle;
      header.appendChild(sub);
    }
    wrap.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'film-cards';
    sectionFilms.forEach((film, i) => {
      const card = filmCardShell(film, '');
      addDismissButton(card, film.slug);
      // Custom lists carry their whole matching set — show a preview and
      // let the rest expand in place rather than a wall of cards.
      if (section.custom_list && i >= CUSTOM_LIST_PREVIEW) card.classList.add('hidden');
      grid.appendChild(card);
    });
    wrap.appendChild(grid);

    if (section.custom_list && sectionFilms.length > CUSTOM_LIST_PREVIEW) {
      const more = document.createElement('button');
      more.className = 'home-section-more';
      const collapsedLabel = 'Show all ' + sectionFilms.length;
      more.textContent = collapsedLabel;
      more.addEventListener('click', () => {
        const expanding = more.textContent === collapsedLabel;
        grid.querySelectorAll('.film-card').forEach((card, i) => {
          if (i >= CUSTOM_LIST_PREVIEW) card.classList.toggle('hidden', !expanding);
        });
        more.textContent = expanding ? 'Show fewer' : collapsedLabel;
        if (!expanding) wrap.scrollIntoView({ block: 'start' });
      });
      wrap.appendChild(more);
    }

    container.appendChild(wrap);
  });
}

function renderHome() {
  renderSections(document.getElementById('homeSections'), DATA.home_sections);
}

function openQuickLookFromCard(event) {
  if (event.target.closest('a.film-link') || event.target.closest('.dismiss-btn')) return;
  const card = event.target.closest('.film-card');
  if (card) openQuickLook(card.dataset.slug);
}

document.getElementById('homeSections').addEventListener('click', openQuickLookFromCard);
document.getElementById('listSections').addEventListener('click', openQuickLookFromCard);

// ---------- Lists ----------

// The fourteen curated lists from config/custom_lists.yaml, which used to
// sit mid-Home and pushed everything time-sensitive below the fold. Here
// they get the whole tab: a chip per group, a search that matches either a
// list's name or a film inside it, and a filter down to what you can
// actually watch tonight.
let listGroupFilter = null;
let listsHaveOnly = false;

function haveSlugs() {
  if (!DATA._haveSlugs) {
    DATA._haveSlugs = new Set(DATA.films.filter(r => r.have_service).map(r => r.slug));
  }
  return DATA._haveSlugs;
}

function listSectionsInScope() {
  const sections = DATA.list_sections || [];
  if (!listGroupFilter) return sections;
  return sections.filter(s => (s.group || 'Other') === listGroupFilter);
}

function renderListGroupJump() {
  const counts = new Map();
  (DATA.list_sections || []).forEach(s => {
    const group = s.group || 'Other';
    counts.set(group, (counts.get(group) || 0) + 1);
  });
  const entries = [...counts].map(([group, count]) => ({ value: group, label: group, count }));
  renderQuickJumpChips('listGroupJump', entries, listGroupFilter || '', value => {
    listGroupFilter = value || null;
    renderLists();
  });
}

function renderLists() {
  const search = document.getElementById('listSearch').value.trim().toLowerCase();
  const have = haveSlugs();
  document.getElementById('listsHaveOnly').classList.toggle('active', listsHaveOnly);
  renderListGroupJump();

  renderSections(document.getElementById('listSections'), listSectionsInScope(), section => {
    // A search matching the list's own name keeps the whole list — you
    // asked for "Cannes", not for films with Cannes in the title.
    const wholeList = search && section.header.toLowerCase().includes(search);
    return section.films.filter(film => {
      if (listsHaveOnly && !have.has(film.slug)) return false;
      if (!search || wholeList) return true;
      return (film.title + ' ' + (film.director || '')).toLowerCase().includes(search);
    });
  });

  ensureNotEmpty(document.getElementById('listSections'), 'No lists match that.');
}

document.getElementById('listSearch').addEventListener('input', renderLists);
wireSearchClear('listSearch', 'listSearchClear', renderLists);
document.getElementById('listsHaveOnly').addEventListener('click', () => {
  listsHaveOnly = !listsHaveOnly;
  renderLists();
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

function handleSurpriseMeClick() {
  const pool = surprisePool();
  if (!pool.length) return;
  const pick = pool[Math.floor(Math.random() * pool.length)];
  openQuickLook(pick.slug);
}
document.getElementById('surpriseMeBtnDesktop').addEventListener('click', handleSurpriseMeClick);
document.getElementById('surpriseMeBtnMobile').addEventListener('click', handleSurpriseMeClick);

// ---------- Quick search: classifying a searched film's offers ----------
//
// A watchlist film arrives with its offers already classified by
// _all_offers_for_film; a searched one arrives raw from the Worker, because
// the Worker has no business knowing what Josh subscribes to and a JS
// reimplementation of brands.py/config.py would drift with nothing to catch
// it. What follows is the same shape-building as _all_offers_for_film, but
// every judgement in it is a lookup into the table dashboard.py computed
// with the real Python functions (see _search_taxonomy) — no name
// normalization, no fuzzy matching, no precedence rules of its own.

// Mirrors _MONETIZATION_PRIORITY: when one service in one country has
// several qualifying offers, the most watchable one supplies the link.
const MONETIZATION_PRIORITY = { FLATRATE: 0, FREE: 1, ADS: 2 };

function searchTaxonomySets() {
  const taxonomy = DATA.search_taxonomy;
  return {
    brands: taxonomy.brand_by_clear_name,
    // A service name the corpus has never seen isn't one on Josh's own
    // config, so falling back to the raw name and its monetization type
    // lands on the right answer anyway.
    globalHave: new Set(taxonomy.have_brands_global),
    countryHave: taxonomy.have_brands_by_country,
    revisitable: new Set(taxonomy.revisitable_brands),
    junk: new Set(taxonomy.junk_brands),
  };
}

function classifySearchOffers(rawOffers) {
  const sets = searchTaxonomySets();
  const grouped = new Map();

  rawOffers.forEach(offer => {
    const brand = sets.brands[offer.clear_name] || offer.clear_name;
    // Dropped outright, exactly as group_offers_by_brand_and_country does —
    // JustWatch's own aggregator placeholder isn't a service anyone watches.
    if (sets.junk.has(brand.toLowerCase())) return;

    const key = brand + '|' + offer.country;
    let entry = grouped.get(key);
    if (!entry) {
      entry = { brand, country: offer.country, monetizations: new Set(), available_to: null, url: null, urlRank: 99 };
      grouped.set(key, entry);
    }
    entry.monetizations.add(offer.monetization_type);
    if (offer.available_to && (!entry.available_to || offer.available_to < entry.available_to)) {
      entry.available_to = offer.available_to;
    }
    const rank = MONETIZATION_PRIORITY[offer.monetization_type];
    if (offer.url && (rank === undefined ? 9 : rank) < entry.urlRank) {
      entry.url = offer.url;
      entry.urlRank = rank === undefined ? 9 : rank;
    }
  });

  // Same precedence as _classify: a service you have wins over one you could
  // get again, which wins over free-vs-subscription.
  return [...grouped.values()].map(entry => {
    const countryHave = sets.countryHave[entry.country] || [];
    let classification;
    if (sets.globalHave.has(entry.brand) || countryHave.includes(entry.brand)) {
      classification = 'have';
    } else if (sets.revisitable.has(entry.brand)) {
      classification = 'could_get_again';
    } else if (entry.monetizations.has('FLATRATE')) {
      classification = 'subscription';
    } else {
      classification = 'free';
    }
    return {
      brand: entry.brand, country: entry.country, classification,
      available_to: entry.available_to, url: entry.url,
    };
  });
}

// Assembles the films_by_slug-shaped object buildFilmDetailCard expects out
// of the two halves a lookup returns: the picker's TMDB row (which is where
// the language comes from — Letterboxd's own JSON-LD lists every language
// heard in the film, not its primary one, see tmdb_client.search_movie)
// and the Worker's Letterboxd + JustWatch response. Letterboxd wins on
// anything both carry, so a searched film reads exactly like a watchlist one.
function buildSearchedFilm(row, lookup) {
  const letterboxd = lookup.letterboxd && lookup.letterboxd.ok ? lookup.letterboxd : null;
  const language = row.original_language || null;
  return {
    slug: letterboxd ? letterboxd.slug : null,
    // Letterboxd resolves /tmdb/<id>/ to the film's own page, so this links
    // correctly even when reading that page failed a moment ago.
    letterboxd_url: 'https://letterboxd.com/tmdb/' + row.tmdb_id + '/',
    tmdb_id: row.tmdb_id,
    title: (letterboxd && letterboxd.title) || row.title,
    year: row.year,
    rating: letterboxd ? letterboxd.rating : null,
    poster_url: letterboxd ? letterboxd.poster_url : row.poster_url,
    director: (letterboxd && letterboxd.director) || row.director,
    starring: letterboxd ? letterboxd.starring : [],
    synopsis: (letterboxd && letterboxd.synopsis) || row.overview,
    genre: letterboxd ? letterboxd.genre : [],
    runtime_minutes: letterboxd ? letterboxd.runtime_minutes : null,
    language_name: language ? (DATA.search_taxonomy.language_names[language] || language) : null,
    // Mirrors languages.is_subtitled.
    is_subtitled: Boolean(language) && language !== 'en',
    all_offers: classifySearchOffers(lookup.justwatch.offers),
    // Distinguishes "JustWatch has nothing for this film" from "JustWatch
    // couldn't be asked" — the two must not read the same on the card.
    offers_unavailable: Boolean(lookup.justwatch.error),
  };
}

// Every watchlist film has a slug; a searched one only has it once
// Letterboxd resolved, and falls back to the /tmdb/<id>/ redirect (which
// lands on the same page) rather than linking to /film/null/.
function filmLetterboxdUrl(film) {
  if (film.slug) return 'https://letterboxd.com/film/' + film.slug + '/';
  return film.letterboxd_url || 'https://letterboxd.com/';
}

// ---------- Film detail card (shared: quick look + service detail) ----------

function buildFilmDetailCard(film, excludeBrand, excludeCountry, collapsible, options) {
  // `compact` is the modal treatment: the card has to fit its box without
  // scrolling, which is the whole point of a quick look.
  const compact = Boolean(options && options.compact);
  const badgeCap = compact ? QUICK_LOOK_BADGE_CAP : BADGE_CAP;
  const div = document.createElement('div');
  div.className = 'detail-card' + (compact ? ' detail-card-compact' : '');
  const year = film.year ? ' (' + film.year + ')' : '';
  const rating = film.rating != null ? film.rating.toFixed(2) + '★' : '—';
  const poster = film.poster_url
    ? '<img class="detail-poster" loading="lazy" src="' + escAttr(film.poster_url) + '">'
    : '<div class="detail-poster-placeholder"></div>';
  const director = film.director ? '<p class="detail-meta"><strong>Director:</strong> ' + esc(film.director) + '</p>' : '';
  const runtimeLine = film.runtime_minutes != null
    ? '<p class="detail-meta"><strong>Runtime:</strong> ' + formatRuntime(film.runtime_minutes) + '</p>' : '';
  const starring = (film.starring && film.starring.length)
    ? '<p class="detail-meta"><strong>Starring:</strong> ' + esc(film.starring.join(', ')) + '</p>' : '';
  const genreLine = (film.genre && film.genre.length)
    ? '<p class="detail-meta detail-genre"><strong>Genre:</strong> ' + esc(film.genre.join(', ')) + '</p>' : '';
  const languageLine = film.language_name
    ? '<p class="detail-meta"><strong>Language:</strong> ' + esc(film.language_name) +
      (film.is_subtitled ? ' <span title="Subtitled film">🌐</span>' : '') + '</p>' : '';
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

  // excludeCountry === null means "this brand, any country" (the Services
  // tab's merged "All countries" view) — every offer on that brand is
  // pulled out of "other services" and shown up top, url or not, since the
  // whole point there is "here's everywhere this is on the service you
  // picked". For a single country (the normal case), keep the original,
  // stricter behaviour: only a link-bearing match gets the prominent
  // watch-now treatment, so a url-less match still falls through to
  // "other services" as a plain badge rather than vanishing.
  const brandOffers = excludeBrand ? film.all_offers.filter(o => o.brand === excludeBrand) : [];
  const primaryOffers = excludeCountry == null
    ? brandOffers
    : brandOffers.filter(o => o.country === excludeCountry && o.url);
  const primaryHtml = primaryOffers.length
    ? '<p class="detail-meta">' + primaryOffers.map(o => offerBadgeHtml(o, 'watch-now-btn')).join(' ') + '</p>'
    : '';

  const others = film.all_offers
    .filter(o => !primaryOffers.includes(o) && !(excludeCountry != null && o.brand === excludeBrand && o.country === excludeCountry))
    .slice()
    .sort((a, b) => CLASSIFICATION_PRIORITY[a.classification] - CLASSIFICATION_PRIORITY[b.classification]);
  const otherHtml = others.length
    ? capBadges(others.map(offerBadgeHtml), badgeCap)
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
  // Soonest first, so a cap keeps the ones that actually matter.
  const expiringNoteHtml = o => {
    const when = o.daysLeft === 0 ? 'today' : o.daysLeft === 1 ? 'tomorrow' : 'in ' + o.daysLeft + ' days';
    return '<p class="expiring-note">Leaving ' + esc(o.brand) + ' <i>' + esc(countryLabel(o.country)) + '</i> ' + when + '</p>';
  };
  const expiringCap = compact ? EXPIRING_NOTE_CAP : expiring.length;
  let expiringHtml = '';
  if (expiring.length) {
    const shown = expiring.slice(0, expiringCap).map(expiringNoteHtml).join('');
    const rest = expiring.slice(expiringCap);
    const restId = 'exp-' + Math.random().toString(36).slice(2, 9);
    expiringHtml = '<div class="expiring-notes">' + shown +
      (rest.length
        ? '<span class="badges-hidden" id="' + restId + '" hidden>' + rest.map(expiringNoteHtml).join('') + '</span>' +
          '<button type="button" class="badge-more-btn" data-target="' + restId + '">+' +
            rest.length + ' more leaving</button>'
        : '') +
      '</div>';
  }

  // At the cinema now/soon — surfaced ahead of streaming info, same
  // priority a specific screening gets everywhere else in the dashboard.
  const cinemaListing = DATA.cinemas.find(r => r.matched_slug === film.slug);
  const cinemaSection = (cinemaListing && cinemaListing.showtimes.length)
    ? '<div class="detail-meta"><strong>🎬 At the cinema</strong></div>' + cinemaShowtimesHtml(cinemaListing.showtimes)
    : '';

  const otherLabel = excludeBrand ? 'Other services' : 'Where to watch';
  div.innerHTML =
    '<div style="flex-shrink:0;">' + poster + '</div>' +
    '<div class="detail-body">' +
      '<a class="film-link" target="_blank" href="' + escAttr(filmLetterboxdUrl(film)) + '"><h3>' + esc(film.title) + year + '</h3></a>' +
      '<p class="detail-rating">' + rating + '</p>' +
      director + runtimeLine + starring + genreLine + languageLine + synopsis + cinemaSection + primaryHtml +
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

// Films looked up live (from search, or a cinema listing the watchlist
// doesn't track) so they can have a quick look and a detail page like any
// other — they aren't in DATA, which only carries what the build knew about.
// Session-lived and deliberately not merged into films_by_slug: the lists,
// filters and counts elsewhere are about the watchlist, and a film you
// glanced at once doesn't belong in them.
const liveFilms = {};

function filmBySlug(slug) {
  return DATA.films_by_slug[slug] || liveFilms[slug] || null;
}

function registerLiveFilm(film) {
  const slug = film.slug || ('tmdb-' + film.tmdb_id);
  if (!DATA.films_by_slug[slug]) liveFilms[slug] = { ...film, slug };
  return slug;
}

// ---------- Quick look modal (Films + Country card click) ----------

function openQuickLook(slug) {
  const film = filmBySlug(slug);
  if (!film) return;
  const content = document.getElementById('quickLookContent');
  content.innerHTML = '';
  content.appendChild(buildFilmDetailCard(film, null, null, false, { compact: true }));

  // The glance stays the glance; this is the way through to the long look.
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'surprise-btn quick-look-more';
  more.textContent = 'Full details →';
  more.addEventListener('click', () => {
    closeQuickLook();
    openFilmDetail(slug);
  });
  content.appendChild(more);

  showQuickLookOverlay();
}

function showQuickLookOverlay() {
  const overlay = document.getElementById('quickLookOverlay');
  if (overlay.classList.contains('active')) return;   // already up; don't double-count the lock
  overlay.classList.add('active');
  lockPageScroll();
}

function closeQuickLook() {
  const overlay = document.getElementById('quickLookOverlay');
  if (!overlay.classList.contains('active')) return;
  overlay.classList.remove('active');
  unlockPageScroll();
}

document.getElementById('quickLookClose').addEventListener('click', closeQuickLook);
document.getElementById('quickLookOverlay').addEventListener('click', event => {
  if (event.target.id === 'quickLookOverlay') closeQuickLook();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') closeQuickLook();
});

// ---------- Quick search ----------
//
// Two steps on purpose (see the Worker's own note): typing searches, and
// nothing expensive happens until a specific film is picked. Four films
// share the title "Parasite", so picking for the user would be wrong often
// enough to matter.
//
// The watchlist is searched first, locally and instantly, because a film
// already tracked has better data stored than any live lookup would return
// — and that costs no request at all.

const SEARCH_DEBOUNCE_MS = 250;
const LOCAL_RESULT_CAP = 5;

let searchDebounceTimer = null;
// Responses can land out of order once someone types faster than the
// network answers; only the newest query's results may render.
let searchSequence = 0;
let lastSearchResults = [];

function searchWorker(path, body) {
  return fetch(DATA.settings.refresh_worker_url + path, {
    method: 'POST',
    headers: {
      'X-Trigger-Secret': DATA.settings.refresh_trigger_secret,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  }).then(response => response.json());
}

function localFilmMatches(query) {
  const needle = query.toLowerCase();
  const matches = [];
  for (const [slug, film] of Object.entries(DATA.films_by_slug)) {
    if (film.title.toLowerCase().includes(needle)) {
      matches.push({ slug, film, onWatchlist: !isDiscoveryOnly(slug) });
    }
    if (matches.length >= LOCAL_RESULT_CAP * 3) break;
  }
  // A film actually on the watchlist outranks one only ever surfaced as a
  // recommendation, and an earlier match outranks a mid-title one.
  matches.sort((a, b) =>
    (b.onWatchlist - a.onWatchlist) ||
    (a.film.title.toLowerCase().indexOf(needle) - b.film.title.toLowerCase().indexOf(needle)) ||
    a.film.title.localeCompare(b.film.title));
  return matches.slice(0, LOCAL_RESULT_CAP);
}

function searchRowElement({ posterUrl, title, year, meta, badge, onPick }) {
  const row = document.createElement('div');
  row.className = 'search-row';
  const poster = posterUrl
    ? '<img class="search-row-poster" loading="lazy" src="' + escAttr(posterUrl) + '">'
    : '<div class="search-row-poster"></div>';
  row.innerHTML =
    poster +
    '<div class="search-row-body">' +
      '<p class="search-row-title">' + esc(title) + (year ? ' <span class="muted">(' + year + ')</span>' : '') +
        (badge ? '<span class="search-on-list">' + esc(badge) + '</span>' : '') + '</p>' +
      (meta ? '<p class="search-row-meta">' + esc(meta) + '</p>' : '') +
    '</div>';
  row.addEventListener('click', onPick);
  return row;
}

function renderSearchResults(query, localMatches, remote) {
  const container = document.getElementById('searchResults');
  container.innerHTML = '';

  if (localMatches.length) {
    const label = document.createElement('p');
    label.className = 'search-section-label';
    label.textContent = 'Already tracked';
    container.appendChild(label);
    localMatches.forEach(({ slug, film, onWatchlist }) => {
      container.appendChild(searchRowElement({
        posterUrl: film.poster_url, title: film.title, year: film.year,
        meta: film.director, badge: onWatchlist ? 'On your watchlist' : 'Recommended',
        // Stored data, already classified — no lookup needed.
        // On top of the search, not below it — the same overlay any other
        // film opens in, so it reads the same and links on to full details.
        onPick: () => openQuickLook(slug),
      }));
    });
  }

  const label = document.createElement('p');
  label.className = 'search-section-label';
  label.textContent = 'Everything else on Letterboxd';
  container.appendChild(label);

  if (remote === 'loading') {
    const status = document.createElement('p');
    status.className = 'search-status';
    status.textContent = 'Searching…';
    container.appendChild(status);
    return;
  }
  if (remote === 'error') {
    const status = document.createElement('p');
    status.className = 'search-status';
    status.textContent = "Couldn't reach the search service just now.";
    container.appendChild(status);
    return;
  }
  // A film listed above as tracked shouldn't also appear down here as
  // something to look up — it's the same film, and the stored copy is the
  // better one. Title and year are the only identity the two lists share
  // (one is keyed by Letterboxd slug, the other by TMDB id), which is the
  // same match similar.py makes for the same reason.
  const trackedKeys = new Set(localMatches.map(m => searchIdentity(m.film.title, m.film.year)));
  const fresh = remote.filter(row => !trackedKeys.has(searchIdentity(row.title, row.year)));

  if (!fresh.length) {
    const status = document.createElement('p');
    status.className = 'search-status';
    status.textContent = localMatches.length
      ? 'Nothing else — every match is already tracked above.'
      : 'No films found for "' + query + '".';
    container.appendChild(status);
    return;
  }

  fresh.forEach(row => {
    container.appendChild(searchRowElement({
      posterUrl: row.poster_url, title: row.title, year: row.year,
      // Year and director are what actually separate two films sharing a
      // title, so they carry the row rather than the synopsis.
      meta: row.director,
      onPick: () => lookUpSearchResult(row),
    }));
  });
}

function searchIdentity(title, year) {
  return (title || '').toLowerCase().replace(/[^a-z0-9]/g, '') + '|' + (year || '');
}

function runSearch(query) {
  const sequence = ++searchSequence;
  const localMatches = localFilmMatches(query);
  renderSearchResults(query, localMatches, 'loading');

  searchWorker('/search-films', { query })
    .then(body => {
      if (sequence !== searchSequence) return;  // a newer query has since run
      if (!body || !body.ok) {
        renderSearchResults(query, localMatches, 'error');
        if (body && body.error) showToast('Search failed: ' + body.error);
        return;
      }
      lastSearchResults = body.results || [];
      renderSearchResults(query, localMatches, lastSearchResults);
    })
    .catch(() => {
      if (sequence !== searchSequence) return;
      renderSearchResults(query, localMatches, 'error');
    });
}

// A film the dashboard doesn't track, picked from the search list. Same
// live lookup as before, but shown in the quick-look overlay on top of the
// search rather than swapped in underneath it.
function lookUpSearchResult(row) {
  openLiveQuickLook(row);
}

// The picked film used to swap into a second panel here; it opens in the
// quick-look overlay on top now, so all that's left is making sure the
// results are showing when the search is reopened.
function showSearchResultsPanel() {
  document.getElementById('searchPanel').hidden = false;
}

function openFilmSearch() {
  const overlay = document.getElementById('searchOverlay');
  if (!overlay.classList.contains('active')) {
    overlay.classList.add('active');
    lockPageScroll();
  }
  showSearchResultsPanel();
  const input = document.getElementById('filmSearchInput');
  input.focus();
  input.select();
}

function closeFilmSearch() {
  const overlay = document.getElementById('searchOverlay');
  if (!overlay.classList.contains('active')) return;
  overlay.classList.remove('active');
  unlockPageScroll();
}

document.getElementById('filmSearchBtn').addEventListener('click', openFilmSearch);
document.getElementById('filmSearchBtnMobile').addEventListener('click', openFilmSearch);
document.getElementById('searchClose').addEventListener('click', closeFilmSearch);
document.getElementById('searchOverlay').addEventListener('click', event => {
  if (event.target.id === 'searchOverlay') closeFilmSearch();
});

document.getElementById('filmSearchInput').addEventListener('input', event => {
  const query = event.target.value.trim();
  clearTimeout(searchDebounceTimer);
  if (query.length < 2) {
    // Nothing useful to search on yet, and it saves a request per keystroke
    // at the start of every single search.
    searchSequence++;
    document.getElementById('searchResults').innerHTML = '';
    return;
  }
  searchDebounceTimer = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS);
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    closeFilmSearch();
    return;
  }
  // "/" is the usual shortcut, but only when it isn't being typed into
  // something — the Films tab's own filter box is a text input too.
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if ((event.key === '/' && !typing) || (event.key === 'k' && (event.metaKey || event.ctrlKey))) {
    event.preventDefault();
    openFilmSearch();
  }
});

// ---------- Film detail page ----------
//
// Quick look answers "what is this and can I watch it"; this answers "tell
// me about this film, and what else like it can I watch". It's a view
// rather than a modal because it's long, it links on to other films, and
// coming back from one should return you where you were.
//
// Everything here is built from the payload the page already has — the
// films tracked plus the ones discovery surfaced — so opening it costs
// nothing. Reaching past that corpus to the rest of TMDB is the next step,
// deliberately separate.

const FILM_RELATION_CAP = 18;
const ACTOR_SECTIONS_CAP = 3;
// Two shared genres is the line where "also a thriller" becomes "the same
// sort of film"; one is too loose to be worth showing.
const SIMILAR_MIN_SHARED_GENRES = 2;

// Where to return to, and the trail walked through to get here, so a chain
// of film → director → another of their films unwinds one step at a time.
// An entry is {kind: 'film', slug} or {kind: 'person', id, name} — the two
// detail pages share one trail because you move between them freely.
let filmDetailReturnView = 'films';
let detailTrail = [];
let currentDetailStop = null;

const DETAIL_VIEW_IDS = ['view-film-detail', 'view-person-detail'];

// Where the page was scrolled to when you left it, so Back puts it back
// rather than dropping you at the top of a list you were halfway down.
// Keyed by view id for the tabs; a trail entry carries its own.
const viewScrollPositions = {};

function rememberCurrentScroll() {
  const current = document.querySelector('section.view.active');
  if (current) viewScrollPositions[current.id] = window.scrollY;
}

// Scrolling is the only thing that moves the page between view switches, so
// without this the position stored for a refresh is wherever the tab was
// when you arrived at it, not where you actually read to. Throttled because
// this fires continuously and sessionStorage is a synchronous write.
let scrollPersistTimer = null;
window.addEventListener('scroll', () => {
  rememberCurrentScroll();
  if (scrollPersistTimer) return;
  scrollPersistTimer = setTimeout(() => {
    scrollPersistTimer = null;
    const current = document.querySelector('section.view.active');
    if (!current) return;
    const name = current.id.replace('view-', '');
    rememberLastView(DETAIL_VIEW_IDS.includes(current.id) ? filmDetailReturnView : name,
                     DETAIL_VIEW_IDS.includes(current.id)
                       ? viewScrollPositions['view-' + filmDetailReturnView]
                       : window.scrollY);
  }, 400);
}, { passive: true });

// After a render the new content has to exist before the browser can scroll
// to an offset inside it, so this waits a frame rather than scrolling into a
// page that is still the previous one's height.
function restoreScroll(y) {
  requestAnimationFrame(() => window.scrollTo(0, y || 0));
}

function renderDetailStop(stop, scrollTo) {
  if (stop.kind === 'person') {
    renderPersonDetail(stop);
    requestPerson(stop);
    showView('person-detail', { scroll: 'manual' });
  } else {
    renderFilmDetail(stop.slug);
    showView('film-detail', { scroll: 'manual' });
  }
  restoreScroll(scrollTo || 0);
}

// Every way into either detail page goes through here, so the trail and the
// tab to return to are decided in exactly one place.
function pushDetailStop(stop, fromView) {
  const current = document.querySelector('section.view.active');
  rememberCurrentScroll();
  if (current && DETAIL_VIEW_IDS.includes(current.id)) {
    // The stop being left keeps where it was read to, so unwinding a chain
    // returns each page to its own position, not to the top.
    if (currentDetailStop) detailTrail.push({ ...currentDetailStop, scrollY: window.scrollY });
  } else {
    filmDetailReturnView = fromView || (current ? current.id.replace('view-', '') : 'films');
    detailTrail = [];
  }
  currentDetailStop = stop;
  renderDetailStop(stop, 0);   // a page you're opening starts at its top
}

function goBackFromDetail() {
  if (detailTrail.length) {
    const previous = detailTrail.pop();
    currentDetailStop = previous;
    renderDetailStop(previous, previous.scrollY);
    return;
  }
  currentDetailStop = null;
  showView(filmDetailReturnView);
}

function filmDirectors(film) {
  return (film.director || '').split(', ').map(n => n.trim()).filter(Boolean);
}

function allKnownFilms() {
  return Object.entries(DATA.films_by_slug).map(([slug, film]) => ({ ...film, slug }));
}

// The name-only forms are what the person page needs, where there's no
// source film to leave out; the film page's versions are the same thing
// minus the film you're already looking at.
function relatedByDirectorName(director) {
  return allKnownFilms().filter(other => filmDirectors(other).includes(director));
}

function relatedByActorName(actor) {
  return allKnownFilms().filter(other => (other.starring || []).includes(actor));
}

function relatedByDirector(film, director) {
  return relatedByDirectorName(director).filter(other => other.slug !== film.slug);
}

function relatedByActor(film, actor) {
  return relatedByActorName(actor).filter(other => other.slug !== film.slug);
}

// Local stand-in for "similar": films sharing most of this one's genres,
// best-rated first. Not TMDB's notion of similarity — that needs the live
// lookup — but it's honest about what it is and costs nothing.
function similarByGenre(film) {
  const genres = new Set(film.genre || []);
  if (genres.size < SIMILAR_MIN_SHARED_GENRES) return [];
  return allKnownFilms()
    .map(other => ({
      film: other,
      shared: (other.genre || []).filter(g => genres.has(g)).length,
    }))
    .filter(x => x.film.slug !== film.slug && x.shared >= SIMILAR_MIN_SHARED_GENRES)
    .sort((a, b) => (b.shared - a.shared) || ((b.film.rating || 0) - (a.film.rating || 0)))
    .map(x => x.film);
}

function watchlistRank(film) {
  // Watchlist films lead their section: they're the ones already wanted,
  // and a recommendation is a weaker claim than "you put this on the list".
  return isDiscoveryOnly(film.slug) ? 1 : 0;
}

function sortRelated(films) {
  return films.slice().sort((a, b) =>
    (watchlistRank(a) - watchlistRank(b)) || ((b.rating || 0) - (a.rating || 0)));
}

// Matching a live TMDB row back to a film the dashboard already has, so it
// renders as a tracked tile (with its availability dot) rather than as one
// more thing to look up. Two keys, because neither alone covers it: tmdb_id
// is exact but only present on films whose TMDB search has come round, and
// title+year covers the rest — the same identity similar.py matches on, for
// the same reason.
let _trackedIndexes = null;

function trackedIndexes() {
  if (!_trackedIndexes) {
    const byTmdbId = {};
    const byIdentity = {};
    // films_by_slug is discovery films overlaid by watchlist ones, so a slug
    // on both lands here as the watchlist copy — which is the better one.
    for (const [slug, film] of Object.entries(DATA.films_by_slug)) {
      if (film.tmdb_id) byTmdbId[film.tmdb_id] = slug;
      byIdentity[searchIdentity(film.title, film.year)] = slug;
    }
    _trackedIndexes = { byTmdbId, byIdentity };
  }
  return _trackedIndexes;
}

function trackedSlugForRow(row) {
  const { byTmdbId, byIdentity } = trackedIndexes();
  return byTmdbId[row.tmdb_id] || byIdentity[searchIdentity(row.title, row.year)] || null;
}

// Local films first, then whatever TMDB adds that isn't already among them —
// "local first" literally: the films whose availability is already known lead,
// and the rest follow as things to look up.
function mergedRelationEntries(trackedFilms, liveRows) {
  const shown = new Set(trackedFilms.map(f => f.slug));
  const entries = trackedFilms.map(film => ({ tracked: true, film }));
  (liveRows || []).forEach(row => {
    const slug = trackedSlugForRow(row);
    if (slug) {
      if (shown.has(slug)) return;
      shown.add(slug);
      entries.push({ tracked: true, film: { ...DATA.films_by_slug[slug], slug } });
    } else {
      entries.push({ tracked: false, row });
    }
  });
  return entries;
}

function relationEntryTile(entry) {
  if (entry.tracked) {
    // The dot reads against one market, not "streaming somewhere on earth" —
    // left unscoped nearly every tile goes green and the dot stops meaning
    // anything. This page has no country control of its own, so it answers for
    // the first home market, the same one the country chips lead with.
    return buildPosterTile(entry.film, HOME_COUNTRY_CODES[0], film => openFilmDetail(film.slug));
  }
  const tile = buildPosterTile(entry.row, null, () => openLiveQuickLook(entry.row));
  // Marked, because the difference matters: a tracked tile's missing dot
  // means "not streaming anywhere you have", where this one's means
  // "nobody has asked yet".
  tile.classList.add('poster-tile-live');
  tile.title = tile.title + ' — not tracked, tap to look up';
  return tile;
}

// One relation section. `live` is null while the TMDB call is still out,
// 'error' if it failed, and the section says which — a live layer that
// quietly doesn't arrive would read as "there is nothing else", which is
// the one thing it must not say.
function filmRelationSection({ title, trackedFilms, liveRows, emptyNote, live, person }) {
  const section = document.createElement('div');
  section.className = 'film-section';
  const entries = mergedRelationEntries(trackedFilms, liveRows);
  const liveCount = entries.filter(e => !e.tracked).length;

  const head = document.createElement('div');
  head.className = 'film-section-head';
  const counts = [];
  if (entries.length) {
    counts.push(liveCount ? (entries.length - liveCount) + ' tracked' : String(entries.length));
    if (liveCount) counts.push(liveCount + ' more on TMDB');
  }
  if (live === null) counts.push('checking TMDB…');
  if (live === 'error') counts.push('TMDB unavailable');
  // A section about a person doubles as the way to that person's page —
  // the heading is already their name, so it's the obvious thing to tap.
  const heading = person
    ? esc(title.slice(0, title.length - person.name.length)) +
      '<button type="button" class="person-link" data-person="' + escAttr(person.name) + '"' +
      (person.id ? ' data-person-id="' + escAttr(String(person.id)) + '"' : '') + '>' +
      esc(person.name) + '</button>'
    : esc(title);
  head.innerHTML = '<h3>' + heading + '</h3>' +
    (counts.length ? '<span class="count">' + esc(counts.join(' · ')) + '</span>' : '');
  section.appendChild(head);

  if (!entries.length) {
    const note = document.createElement('p');
    note.className = 'film-section-empty';
    note.textContent = live === 'error'
      ? "TMDB couldn't be reached, so this only covers what's already tracked — and there's nothing."
      : emptyNote;
    section.appendChild(note);
    return section;
  }

  const grid = document.createElement('div');
  grid.className = 'poster-grid';
  section.appendChild(grid);

  // A director's whole filmography can run to forty; showing all of it by
  // default would bury the sections below it. Capped, with the way to see
  // the rest right there rather than a link somewhere else.
  let expanded = false;
  const fill = () => {
    grid.innerHTML = '';
    const shown = expanded ? entries : entries.slice(0, FILM_RELATION_CAP);
    shown.forEach(entry => grid.appendChild(relationEntryTile(entry)));
  };
  fill();

  if (entries.length > FILM_RELATION_CAP) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'relation-more';
    more.textContent = 'Show all ' + entries.length + ' →';
    more.addEventListener('click', () => {
      expanded = !expanded;
      more.textContent = expanded ? 'Show fewer ↑' : 'Show all ' + entries.length + ' →';
      fill();
    });
    section.appendChild(more);
  }
  return section;
}

// Every service, grouped by what it would cost, with nothing capped — the
// thing quick look can't do without becoming a wall of badges.
function availabilityGroupsHtml(film) {
  const offers = (film.all_offers || []).slice()
    .sort((a, b) => (CLASSIFICATION_PRIORITY[a.classification] - CLASSIFICATION_PRIORITY[b.classification]) ||
      a.brand.localeCompare(b.brand) || a.country.localeCompare(b.country));
  if (!offers.length) return '<p class="film-section-empty">Not streaming on anything tracked, anywhere.</p>';

  let html = '';
  CLASSIFICATIONS.forEach(key => {
    const group = offers.filter(o => o.classification === key);
    if (!group.length) return;
    const badges = group.map(o => {
      const label = esc(o.brand) + ' <i>' + esc(countryLabel(o.country)) + '</i>';
      const cls = 'badge badge-' + o.classification;
      return o.url
        ? '<a class="' + cls + ' badge-link" href="' + escAttr(o.url) + '" target="_blank" rel="noopener">' + label + ' ↗</a>'
        : '<span class="' + cls + '">' + label + '</span>';
    });
    html += '<div class="avail-group">' +
      '<p class="avail-group-head">' + esc(CLASSIFICATION_LABELS[key]) + ' · ' + group.length + '</p>' +
      '<div class="badge-wrap">' + badges.join(' ') + '</div>' +
    '</div>';
  });
  return html;
}

function filmHeroHtml(film) {
  const year = film.year ? ' <span>(' + film.year + ')</span>' : '';
  // Each fact is its own element: the row is a flex line whose gaps are what
  // separate them, and bare strings would run into each other.
  const facts = [];
  if (film.rating != null) facts.push('<span class="rating">' + film.rating.toFixed(2) + '★</span>');
  if (film.runtime_minutes != null) facts.push('<span>' + esc(formatRuntime(film.runtime_minutes)) + '</span>');
  if (film.genre && film.genre.length) facts.push('<span>' + esc(film.genre.join(', ')) + '</span>');
  if (film.language_name) {
    facts.push('<span>' + esc(film.language_name) + (film.is_subtitled ? ' 🌐' : '') + '</span>');
  }
  const countries = new Set((film.all_offers || []).map(o => o.country));
  if (countries.size) {
    facts.push('<span>streaming in ' + countries.size + (countries.size === 1 ? ' country' : ' countries') + '</span>');
  }

  const poster = film.poster_url
    ? '<img class="film-hero-poster" loading="lazy" src="' + escAttr(film.poster_url) + '">'
    : '<div class="film-hero-poster" style="aspect-ratio:2/3;"></div>';

  return '<div class="film-hero">' +
    '<div>' + poster + '</div>' +
    '<div class="film-hero-body">' +
      '<h2>' + esc(film.title) + year + '</h2>' +
      '<p class="film-hero-facts">' + facts.join('') + '</p>' +
      (film.director
        ? '<p class="film-hero-meta"><strong>Director:</strong> ' + personLinksHtml(filmDirectors(film)) + '</p>' : '') +
      ((film.starring && film.starring.length)
        ? '<p class="film-hero-meta"><strong>Starring:</strong> ' + personLinksHtml(film.starring) + '</p>' : '') +
      (film.synopsis ? '<p class="film-hero-synopsis">' + esc(film.synopsis) + '</p>' : '') +
      '<p class="film-hero-meta"><a class="film-link" target="_blank" href="' +
        escAttr(filmLetterboxdUrl(film)) + '">View on Letterboxd ↗</a></p>' +
    '</div>' +
  '</div>';
}

function renderFilmDetail(slug) {
  const film = { ...filmBySlug(slug), slug };
  const container = document.getElementById('filmDetailContent');
  container.innerHTML = filmHeroHtml(film);
  // Same reasoning as the poster tiles: a broken-image icon reads as a fault
  // in the page, where the empty frame a film with no artwork already gets
  // just reads as no artwork.
  const heroImg = container.querySelector('img.film-hero-poster');
  if (heroImg) {
    heroImg.addEventListener('error', () => {
      const blank = document.createElement('div');
      blank.className = 'film-hero-poster';
      blank.style.aspectRatio = '2/3';
      heroImg.replaceWith(blank);
    });
  }

  const cinema = DATA.cinemas.find(r => r.matched_slug === slug);
  if (cinema && cinema.showtimes.length) {
    const block = document.createElement('div');
    block.className = 'film-section';
    block.innerHTML = '<div class="film-section-head"><h3>🎬 At the cinema</h3></div>' +
      cinemaShowtimesHtml(cinema.showtimes);
    container.appendChild(block);
  }

  const avail = document.createElement('div');
  avail.className = 'film-section';
  avail.innerHTML = '<div class="film-section-head"><h3>Where to watch</h3></div>' + availabilityGroupsHtml(film);
  container.appendChild(avail);

  const relations = document.createElement('div');
  relations.id = 'filmDetailRelations';
  container.appendChild(relations);
  renderRelationSections(film, filmRelationsFor(film));
  requestFilmRelations(film);
}

// ---------- The live TMDB layer ----------
//
// The sections above are drawn from the ~500 films the page ships, which
// only ever answers "of the ones you track". TMDB knows the director's
// whole filmography and its own notion of similar, so the page asks — but
// only after it has already drawn what it can, so opening a film is still
// instant and a slow or failed call costs nothing that was there before.
//
// Cached per film for the session: walking a chain of films and coming
// back shouldn't re-ask, and nothing here changes between two views of
// the same film minutes apart.
const filmRelationsCache = {};

function filmRelationsFor(film) {
  if (!film.tmdb_id) return 'unknown';   // no id stored yet — local only, and said so
  const cached = filmRelationsCache[film.tmdb_id];
  // 'pending' is a call already in flight, which the page draws the same as
  // one not yet started: still waiting.
  return (cached === undefined || cached === 'pending') ? null : cached;
}

function requestFilmRelations(film) {
  if (!film.tmdb_id || filmRelationsCache[film.tmdb_id] !== undefined) return;
  const tmdbId = film.tmdb_id;
  // Claimed before the call goes out, so reopening a film while its first
  // request is still in flight waits on that one instead of starting another.
  filmRelationsCache[tmdbId] = 'pending';
  searchWorker('/film-relations', { tmdb_id: tmdbId })
    .then(body => {
      if (!body || !body.ok) throw new Error((body && body.error) || 'relations lookup failed');
      filmRelationsCache[tmdbId] = body;
    })
    .catch(() => { filmRelationsCache[tmdbId] = 'error'; })
    .then(() => {
      // Only redraw if this is still the film on screen — a fast chain of
      // taps would otherwise land one film's sections under another's hero.
      const stop = currentDetailStop;
      const stopFilm = stop && stop.kind === 'film' ? filmBySlug(stop.slug) : null;
      if (stopFilm && stopFilm.tmdb_id === tmdbId) {
        renderRelationSections({ ...stopFilm, slug: stop.slug },
                               filmRelationsCache[tmdbId]);
      }
    });
}

// `live` is the Worker's payload, null while its call is out, 'error' if it
// failed, or 'unknown' for a film with no TMDB id stored yet.
function renderRelationSections(film, live) {
  const container = document.getElementById('filmDetailRelations');
  if (!container) return;
  container.innerHTML = '';
  const payload = (live && live !== 'error' && live !== 'unknown') ? live : null;
  // 'unknown' is a local-only answer that is not going to improve, so it
  // reads as settled rather than as a failure or a wait.
  const liveState = live === 'unknown' ? undefined : (payload ? payload : live);

  const people = payload ? payload.people : [];
  const liveOf = (role, name) => {
    const person = people.find(p => p.role === role && p.name === name);
    return person ? person.films : null;
  };

  // Local names first, then anyone TMDB credits that Letterboxd's own page
  // didn't — the order stays stable as the live layer arrives.
  const directorNames = [...new Set([
    ...filmDirectors(film),
    ...people.filter(p => p.role === 'director').map(p => p.name),
  ])];
  const idOf = (role, name) => {
    const found = people.find(p => p.role === role && p.name === name);
    return found ? found.tmdb_id : null;
  };

  directorNames.forEach(name => {
    container.appendChild(filmRelationSection({
      title: 'More by ' + name,
      trackedFilms: sortRelated(relatedByDirector(film, name)),
      liveRows: liveOf('director', name),
      emptyNote: 'Nothing else by ' + name + ' on your watchlist or in your recommendations yet.',
      live: liveState,
      person: { name, id: idOf('director', name) },
    }));
  });

  // TMDB's billed cast leads once it's here, because those are the names its
  // filmographies were fetched for — a Letterboxd name it doesn't share would
  // get a section with no live half, which is the weaker of the two to keep
  // under the cap. Letterboxd's own list is the fallback until then, and
  // relatedByActor works off the name either way, so a live name still shows
  // the films already tracked.
  const castNames = [...new Set([
    ...people.filter(p => p.role === 'cast').map(p => p.name),
    ...(film.starring || []),
  ])].slice(0, ACTOR_SECTIONS_CAP);
  castNames.forEach(name => {
    const trackedFilms = sortRelated(relatedByActor(film, name));
    const liveRows = liveOf('cast', name);
    // Still no section for an actor with nothing to show — but now that
    // includes "and TMDB had nothing either", not just "nothing tracked".
    if (!trackedFilms.length && !(liveRows && liveRows.length)) return;
    container.appendChild(filmRelationSection({
      title: 'More with ' + name,
      trackedFilms, liveRows, emptyNote: '', live: liveState,
      person: { name, id: idOf('cast', name) },
    }));
  });

  // TMDB's own "similar" is the real answer to this one; the genre overlap
  // below is the stand-in the page can compute by itself, so it only stands
  // in while there's no live answer to replace it.
  container.appendChild(filmRelationSection({
    title: 'Similar films',
    trackedFilms: payload ? [] : similarByGenre(film),
    liveRows: payload ? payload.similar : null,
    emptyNote: 'Nothing else tracked shares enough of its genres.',
    live: liveState,
  }));
}

// A film TMDB surfaced that the dashboard doesn't track has no stored
// availability, so tapping it runs the same one-film live lookup quick
// search uses, and shows the result in the same quick-look modal.
function openLiveQuickLook(row) {
  const content = document.getElementById('quickLookContent');
  content.innerHTML = '<p class="search-status">Looking up ' + esc(row.title) + '…</p>';
  showQuickLookOverlay();

  // buildSearchedFilm reads the picker's own row shape, which carries three
  // fields a relation row doesn't. Absent, not empty — Letterboxd supplies
  // all three on a successful lookup anyway, and undefined would render as
  // the word "undefined" if it didn't.
  const searchRow = {
    ...row,
    director: row.director || null,
    original_language: row.original_language || null,
    overview: row.overview || null,
  };

  searchWorker('/film-lookup', {
    tmdb_id: row.tmdb_id,
    title: row.title,
    year: row.year,
    countries: DATA.search_taxonomy.justwatch_countries,
  })
    .then(body => {
      if (!body || !body.ok) throw new Error((body && body.error) || 'lookup failed');
      const film = buildSearchedFilm(searchRow, body);
      content.innerHTML = '';
      content.appendChild(buildFilmDetailCard(film, null, null, false, { compact: true }));

      // The same way through to the long look a tracked film gets. It works
      // because the film is registered above: the detail page reads through
      // filmBySlug, and its TMDB id is what the live relations layer needs.
      const slug = registerLiveFilm(film);
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'surprise-btn quick-look-more';
      more.textContent = 'Full details →';
      more.addEventListener('click', () => {
        closeQuickLook();
        openFilmDetail(slug);   // showView closes the search behind it
      });
      content.appendChild(more);

      const notes = [{ text: 'Not on your watchlist — this was looked up live.', warn: false }];
      if (!(body.letterboxd && body.letterboxd.ok)) {
        notes.push({ text: "Letterboxd details couldn't be read, so the rating and cast are missing.", warn: true });
      }
      if (film.offers_unavailable) {
        notes.push({ text: "Streaming availability couldn't be checked just now — try again in a moment.", warn: true });
      }
      notes.forEach(note => {
        const el = document.createElement('p');
        el.className = 'search-note' + (note.warn ? ' search-note-warn' : '');
        el.textContent = note.text;
        content.appendChild(el);
      });
    })
    .catch(() => {
      content.innerHTML = '<p class="search-status">Couldn\\'t look that film up just now.</p>';
    });
}

// A name that leads to its own page. Rendered as HTML rather than wired up
// element by element because these sit inside strings the hero already
// builds; one delegated listener below turns any of them into a click.
function personLinksHtml(names) {
  return (names || [])
    .map(name => '<button type="button" class="person-link" data-person="' + escAttr(name) + '">' +
      esc(name) + '</button>')
    .join(', ');
}

document.addEventListener('click', event => {
  const link = event.target.closest('.person-link');
  if (!link) return;
  const id = link.dataset.personId ? Number(link.dataset.personId) : null;
  openPersonDetail(link.dataset.person || null, id || null);
});

// ---------- Person detail page ----------
//
// One director or actor: who they are, and everything they made. The
// filmography is TMDB's, merged against what the dashboard already tracks
// exactly as the film page's sections are — so a film you have reads as a
// film you have, wherever you meet it.
//
// Cached per person for the session, keyed by id where there is one and by
// name where there isn't, so walking a chain of people doesn't re-ask.
const personCache = {};

function personCacheKey(stop) {
  return stop.id ? 'id:' + stop.id : 'name:' + (stop.name || '').toLowerCase();
}

function personAgeParts(person) {
  // Birth and death as TMDB has them, plus the age those two imply —
  // the arithmetic is the only part TMDB doesn't hand over, and it's the
  // part anyone reading a filmography actually wants.
  if (!person.birthday) return null;
  const born = new Date(person.birthday);
  if (isNaN(born)) return null;
  const end = person.deathday ? new Date(person.deathday) : new Date();
  if (isNaN(end)) return null;
  let age = end.getFullYear() - born.getFullYear();
  const monthDelta = end.getMonth() - born.getMonth();
  if (monthDelta < 0 || (monthDelta === 0 && end.getDate() < born.getDate())) age -= 1;
  return age >= 0 && age < 130 ? age : null;
}

function formatPersonDate(iso) {
  const date = new Date(iso);
  if (isNaN(date)) return iso;
  return date.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
}

function personHeroHtml(person, { name }) {
  const facts = [];
  if (person) {
    if (person.known_for_department) facts.push('<span>' + esc(person.known_for_department) + '</span>');
    if (person.birthday) {
      const age = personAgeParts(person);
      const born = 'Born ' + esc(formatPersonDate(person.birthday));
      facts.push('<span>' + born + (age !== null && !person.deathday ? ' (' + age + ')' : '') + '</span>');
    }
    if (person.deathday) {
      const age = personAgeParts(person);
      facts.push('<span>Died ' + esc(formatPersonDate(person.deathday)) +
        (age !== null ? ' (aged ' + age + ')' : '') + '</span>');
    }
    if (person.place_of_birth) facts.push('<span>' + esc(person.place_of_birth) + '</span>');
  }

  const photo = person && person.profile_url
    ? '<img class="film-hero-poster" loading="lazy" src="' + escAttr(person.profile_url) + '">'
    : '<div class="film-hero-poster" style="aspect-ratio:2/3;"></div>';

  const tmdbLink = person
    ? '<p class="film-hero-meta"><a class="film-link" target="_blank" rel="noopener" href="' +
        escAttr('https://www.themoviedb.org/person/' + person.tmdb_id) + '">View on TMDB ↗</a></p>'
    : '';

  return '<div class="film-hero">' +
    '<div>' + photo + '</div>' +
    '<div class="film-hero-body">' +
      '<h2>' + esc((person && person.name) || name || 'Unknown') + '</h2>' +
      (facts.length ? '<p class="film-hero-facts">' + facts.join('') + '</p>' : '') +
      (person && person.biography
        ? '<p class="film-hero-synopsis person-bio">' + esc(person.biography) + '</p>' : '') +
      tmdbLink +
    '</div>' +
  '</div>';
}

// A biography runs to several paragraphs and would push the filmography off
// the screen, so it's clamped with a way to open it — the same bargain the
// relation sections strike with "Show all".
function attachBioToggle(container) {
  const bio = container.querySelector('.person-bio');
  if (!bio) return;
  // Only worth a control if it's actually being cut off.
  if (bio.scrollHeight <= bio.clientHeight + 4) { bio.classList.add('person-bio-open'); return; }
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'relation-more person-bio-toggle';
  toggle.textContent = 'Read more ↓';
  toggle.addEventListener('click', () => {
    const open = bio.classList.toggle('person-bio-open');
    toggle.textContent = open ? 'Read less ↑' : 'Read more ↓';
  });
  bio.insertAdjacentElement('afterend', toggle);
}

// The films this person made that the dashboard already tracks, found the
// same two ways a relation row is: by TMDB id, else by title and year.
function trackedFilmsForPerson(name, role) {
  if (!name) return [];
  const films = role === 'director' ? relatedByDirectorName(name) : relatedByActorName(name);
  return sortRelated(films);
}

function renderPersonDetail(stop) {
  const container = document.getElementById('personDetailContent');
  const cached = personCache[personCacheKey(stop)];
  const payload = (cached && cached !== 'pending' && cached !== 'error') ? cached : null;
  const person = payload ? payload.person : null;
  const name = (person && person.name) || stop.name;

  container.innerHTML = personHeroHtml(person, { name: stop.name });
  const heroImg = container.querySelector('img.film-hero-poster');
  if (heroImg) {
    heroImg.addEventListener('error', () => {
      const blank = document.createElement('div');
      blank.className = 'film-hero-poster';
      blank.style.aspectRatio = '2/3';
      heroImg.replaceWith(blank);
    });
  }
  attachBioToggle(container);

  if (cached === 'error') {
    const note = document.createElement('p');
    note.className = 'film-section-empty';
    note.textContent = "TMDB couldn't be reached, so only what's already tracked is shown.";
    container.appendChild(note);
  }

  const live = cached === 'error' ? 'error' : (payload || null);
  // Directing first for a director, acting first for everyone else — the
  // section someone came here for shouldn't be the one below the fold.
  const directingFirst = !person || person.known_for_department === 'Directing';
  const sections = [
    { title: 'Directed', role: 'director', rows: payload ? payload.directed : null,
      empty: 'Nothing directed that you track.' },
    { title: 'Acted in', role: 'actor', rows: payload ? payload.acted : null,
      empty: 'Nothing they appear in that you track.' },
  ];
  if (!directingFirst) sections.reverse();

  sections.forEach(section => {
    const trackedFilms = trackedFilmsForPerson(name, section.role);
    // A director with no acting credits shouldn't get an empty "Acted in".
    if (!trackedFilms.length && !(section.rows && section.rows.length) && live !== null) return;
    container.appendChild(filmRelationSection({
      title: section.title,
      trackedFilms,
      liveRows: section.rows,
      emptyNote: section.empty,
      live,
    }));
  });
}

function requestPerson(stop) {
  const key = personCacheKey(stop);
  if (personCache[key] !== undefined) return;
  personCache[key] = 'pending';
  const body = stop.id ? { person_id: stop.id } : { name: stop.name };
  searchWorker('/person', body)
    .then(response => {
      if (!response || !response.ok) throw new Error((response && response.error) || 'person lookup failed');
      personCache[key] = response;
      // A person first opened by name now has an id, so opening them again
      // from a film that does know it hits the same cache entry.
      if (response.person && response.person.tmdb_id) {
        personCache['id:' + response.person.tmdb_id] = response;
      }
    })
    .catch(() => { personCache[key] = 'error'; })
    .then(() => {
      const current = currentDetailStop;
      if (current && current.kind === 'person' && personCacheKey(current) === key) {
        renderPersonDetail(current);
      }
    });
}

function openFilmDetail(slug, fromView) {
  if (!filmBySlug(slug)) return;
  pushDetailStop({ kind: 'film', slug }, fromView);
}

// `id` is TMDB's, when the page has it; without one the Worker finds the
// person by name. Either way the name is what's shown while it loads.
function openPersonDetail(name, id, fromView) {
  if (!name && !id) return;
  pushDetailStop({ kind: 'person', id: id || null, name: name || null }, fromView);
}

document.getElementById('filmDetailBack').addEventListener('click', goBackFromDetail);
document.getElementById('personDetailBack').addEventListener('click', goBackFromDetail);

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
let activeList = null;
const filmsFilterState = { have: true, free: true, could_get_again: true, subscription: true };
let filmsSarahFilter = 'all';
let notHaveOnly = false;

function renderNotHaveOnlyToggle() {
  document.getElementById('notHaveOnly').classList.toggle('active', notHaveOnly);
}

// A Sarah filter set to "no"/"yes" only ever filters FOR that status (to
// review/reconsider it) rather than hiding it by default anywhere —
// nothing disappears from a tab unless this is switched away from "all".
function sarahFilterMatches(status, mode) {
  if (mode === 'yes') return status === 'confirmed';
  if (mode === 'no') return status === 'declined';
  return true;
}

const SARAH_FILTER_OPTIONS = [
  { value: 'all', label: 'All' },
  { value: 'yes', label: '♥ Sarah yes' },
  { value: 'no', label: '✕ Sarah no' },
];

// Single-select 3-way pill group (All / Sarah yes / Sarah no), replacing
// the old "only show declined" checkbox — same visual pattern as
// renderClassificationToggles/renderQuickJumpChips, reused across all
// three tabs that filter on watch_together_status.
function renderSarahFilterToggle(containerId, current, onPick) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  // Sits directly under a visually-identical quick-jump chip row on
  // Services/Country (same shared pill styling) — a label makes it clear
  // at a glance this row filters by Sarah's verdict, not by service/country.
  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.textContent = 'Sarah:';
  container.appendChild(hint);
  SARAH_FILTER_OPTIONS.forEach(opt => {
    const pill = document.createElement('span');
    pill.className = 'sarah-pill' + (current === opt.value ? ' active' : '');
    pill.textContent = opt.label;
    pill.addEventListener('click', () => onPick(opt.value));
    container.appendChild(pill);
  });
}

function baseFilteredFilms() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  return DATA.films.filter(row => {
    if (q && !searchHaystack(row).includes(q)) return false;
    if (notHaveOnly && row.have_service) return false;
    if (activeList && !(row.custom_lists || []).includes(activeList)) return false;
    if (!sarahFilterMatches(row.watch_together_status, filmsSarahFilter)) return false;
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

// Counts are each list's full watchlist membership (static), not narrowed
// by the other active filters — a list is a fixed set you pick into, not
// a facet of whatever's currently shown like genre is.
function renderFilmsListSelect() {
  const select = document.getElementById('filmsListSelect');
  const lists = DATA.custom_lists || [];
  select.classList.toggle('hidden', !lists.length);
  select.innerHTML = '';
  const allOpt = document.createElement('option');
  allOpt.value = '';
  allOpt.textContent = 'Focus on a list...';
  select.appendChild(allOpt);
  // Grouped lists go under an <optgroup> per `group` (first-seen order,
  // same as config order); ungrouped ones sit at the top level.
  const groups = new Map();
  lists.forEach(list => {
    const opt = document.createElement('option');
    opt.value = list.key;
    opt.textContent = list.name + ' (' + list.count + ')';
    if (!list.group) { select.appendChild(opt); return; }
    if (!groups.has(list.group)) {
      const og = document.createElement('optgroup');
      og.label = list.group;
      groups.set(list.group, og);
      select.appendChild(og);
    }
    groups.get(list.group).appendChild(opt);
  });
  select.value = activeList || '';
}

function customListName(key) {
  const list = (DATA.custom_lists || []).find(l => l.key === key);
  return list ? list.name : key;
}

// The number beside each country has to be the number of rows that
// selecting it leaves behind, so it counts exactly what the row filter
// below counts: any offer in that country — main brand or other service —
// whose classification is currently switched on. It used to count only
// "have" offers, which made GB read 110 and then show 210.
function countryCountsFromRows(rows) {
  const counts = {};
  rows.forEach(row => {
    const countries = new Set();
    const note = e => { if (filmsFilterState[e.classification]) countries.add(e.country); };
    Object.values(row.main).forEach(entries => entries.forEach(note));
    row.other_services.forEach(note);
    countries.forEach(c => { counts[c] = (counts[c] || 0) + 1; });
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
  // A badge click can set activeCountry to a country every one of whose
  // offers is currently filtered out by the classification toggles — keep
  // it selectable rather than silently dropping it out of the dropdown.
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
  const anyToggleOff = CLASSIFICATIONS.some(k => !filmsFilterState[k]);
  if (!activeCountry && !activeService && !activeGenre && !activeList && !searchVal && !notHaveOnly && filmsSarahFilter === 'all' && !anyToggleOff) return;
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
  if (activeList) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = customListName(activeList) + ' ✕';
    chip.addEventListener('click', () => { activeList = null; renderFilms(); });
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
    chip.addEventListener('click', () => { notHaveOnly = false; renderNotHaveOnlyToggle(); renderFilms(); });
    container.appendChild(chip);
  }
  if (filmsSarahFilter !== 'all') {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = (filmsSarahFilter === 'yes' ? 'Sarah said yes' : 'Sarah said no') + ' ✕';
    chip.addEventListener('click', () => { filmsSarahFilter = 'all'; renderFilmSarahFilter(); renderFilms(); });
    container.appendChild(chip);
  }
  if (anyToggleOff) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = 'Type filters ✕';
    chip.addEventListener('click', () => {
      CLASSIFICATIONS.forEach(k => { filmsFilterState[k] = true; });
      renderFilmFilterToggles();
      renderFilms();
    });
    container.appendChild(chip);
  }
  const clearAll = document.createElement('span');
  clearAll.className = 'filter-chip clear-all-chip';
  clearAll.textContent = 'Clear all ✕';
  clearAll.addEventListener('click', () => {
    activeCountry = null;
    activeService = null;
    activeGenre = null;
    activeList = null;
    document.getElementById('search').value = '';
    document.getElementById('searchClear').classList.add('hidden');
    notHaveOnly = false;
    renderNotHaveOnlyToggle();
    filmsSarahFilter = 'all';
    renderFilmSarahFilter();
    CLASSIFICATIONS.forEach(k => { filmsFilterState[k] = true; });
    renderFilmFilterToggles();
    renderFilms();
  });
  container.appendChild(clearAll);
}

function renderFilmFilterToggles() {
  renderClassificationToggles('filmsFilterToggles', filmsFilterState, renderFilms);
}

function renderFilmSarahFilter() {
  renderSarahFilterToggle('filmsSarahFilter', filmsSarahFilter, value => {
    filmsSarahFilter = value;
    renderFilmSarahFilter();
    renderFilms();
  });
}

function renderFilms() {
  renderFilmsListSelect();
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
      const filtered = entries.filter(e =>
        filmsFilterState[e.classification] && (!activeCountry || e.country === activeCountry));
      if (filtered.length) visibleMain[brand] = filtered;
    });
    const visibleOther = showOtherServices
      ? row.other_services.filter(o =>
          filmsFilterState[o.classification] && (!activeCountry || o.country === activeCountry))
      : [];

    // A film with no tracked offer anywhere matches none of the four
    // classification filters, so it used to fall out of this tab entirely —
    // unfindable even by searching its exact title, though it's on the
    // watchlist and shows up on Home. Mostly films that aren't released
    // yet. They belong in the list when the list isn't being asked an
    // availability question; the moment one is asked — a classification
    // turned off, a country or service chosen — they're correctly absent.
    const askingAboutAvailability =
      CLASSIFICATIONS.some(k => !filmsFilterState[k]) || activeCountry || activeService;
    const include = Object.keys(visibleMain).length > 0 || visibleOther.length > 0 ||
      (!row.any_service && !askingAboutAvailability);
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
  renderLayoutSwitch('filmsLayoutSwitch', 'films', renderFilms);

  // Posters show whatever the filters and sort left behind, in that order —
  // the view changes, the list it's showing doesn't. The container's own
  // class carries the grid, so the two layouts don't need separate elements.
  if (posterLayout('films') === 'posters' && processed.length) {
    container.className = 'poster-grid';
    processed.forEach(({ row }) => container.appendChild(buildPosterTile(row, activeCountry)));
    return;
  }
  // Back to the card grid — and for the empty state too, so "no films match"
  // lays out as a message rather than as a lone grid cell.
  container.className = 'film-cards';

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
document.getElementById('notHaveOnly').addEventListener('click', () => {
  notHaveOnly = !notHaveOnly;
  renderNotHaveOnlyToggle();
  renderFilms();
});
document.getElementById('filmsCountrySelect').addEventListener('change', e => {
  activeCountry = e.target.value || null;
  renderFilms();
});
document.getElementById('filmsGenreSelect').addEventListener('change', e => {
  activeGenre = e.target.value || null;
  renderFilms();
});
document.getElementById('filmsListSelect').addEventListener('change', e => {
  activeList = e.target.value || null;
  renderFilms();
});
document.getElementById('filmsSortSelect').addEventListener('change', e => {
  filmSortKey = e.target.value;
  filmSortDir = filmCols.find(c => c.key === filmSortKey).dir;
  renderFilms();
});
document.getElementById('filmsGrid').addEventListener('click', onBadgeDelegateClick);

// ---------- Cinemas ----------

const CINEMA_VENUES = ['Prince Charles Cinema', 'Barbican', 'Vue Fulham Broadway', 'Riverside Studios'];
// Single-select, same as Services/Country's quick-jump chips — one click
// on a venue shows only that venue (not "toggle this one off"), '' means
// no filter.
let cinemaVenueFilter = '';
let cinemaDateMode = 'all';

function renderCinemaVenueFilter() {
  const entries = CINEMA_VENUES.map(venue => ({
    value: venue, label: venue,
    count: DATA.cinemas.filter(r => r.showtimes.some(s => s.cinema === venue)).length,
  }));
  renderQuickJumpChips('cinemaVenueToggles', entries, cinemaVenueFilter, value => {
    cinemaVenueFilter = value;
    renderCinemaVenueFilter();
    renderCinemas();
  });
}

const CINEMA_DATE_OPTIONS = [
  { value: 'all', label: 'All' },
  { value: 'today', label: 'Today' },
  { value: 'tomorrow', label: 'Tomorrow' },
];

function renderCinemaDateFilter() {
  const container = document.getElementById('cinemaDateFilter');
  container.innerHTML = '';
  CINEMA_DATE_OPTIONS.forEach(opt => {
    const pill = document.createElement('span');
    pill.className = 'pill-toggle' + (cinemaDateMode === opt.value ? ' active' : '');
    pill.textContent = opt.label;
    pill.addEventListener('click', () => {
      cinemaDateMode = opt.value;
      renderCinemaDateFilter();
      renderCinemas();
    });
    container.appendChild(pill);
  });
}

function formatShowtimeChip(showtime, bookingUrl) {
  const d = new Date(showtime);
  const label = d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' }) +
    ', ' + d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  if (!bookingUrl) return '<span class="badge badge-subscription">' + esc(label) + '</span>';
  return '<a class="badge badge-have badge-link" href="' + escAttr(bookingUrl) + '" target="_blank" rel="noopener">' +
    esc(label) + ' ↗</a>';
}

// A merged (matched) row can span several of the four cinemas — grouped
// here so both the Cinemas-tab card and quick-look's showtimes section
// render "one sub-list per cinema" instead of one flat, unlabeled list.
// Groups are ordered by their own soonest showing, same "soonest first"
// priority as everywhere else cinema data is ranked.
function groupShowtimesByCinema(showtimes) {
  const byCinema = new Map();
  showtimes.forEach(s => {
    if (!byCinema.has(s.cinema)) byCinema.set(s.cinema, []);
    byCinema.get(s.cinema).push(s);
  });
  return [...byCinema.entries()].sort((a, b) => a[1][0].showtime < b[1][0].showtime ? -1 : 1);
}

function cinemaShowtimesHtml(showtimes) {
  return groupShowtimesByCinema(showtimes).map(([cinema, times]) => {
    const chips = capBadges(times.map(s => formatShowtimeChip(s.showtime, s.booking_url)), BADGE_CAP);
    return '<div class="cinema-venue-group">' +
      '<div class="film-card-cinema-note">🎬 ' + esc(cinema) + '</div>' +
      '<div class="service-group">' + chips + '</div>' +
    '</div>';
  }).join('');
}

function cinemaCardHtml(row) {
  const year = row.year ? ' (' + row.year + ')' : '';
  const rating = row.rating != null ? row.rating.toFixed(2) + '★' : '—';
  const poster = row.poster_url
    ? '<img class="poster-thumb" loading="lazy" src="' + escAttr(row.poster_url) + '" onerror="this.outerHTML=\\'<div class=&quot;poster-placeholder&quot;></div>\\'">'
    : '<div class="poster-placeholder"></div>';
  const director = row.director ? '<div class="film-card-director">' + esc(row.director) + '</div>' : '';
  const metaParts = [];
  if (row.genre && row.genre.length) metaParts.push(esc(row.genre.join(', ')));
  if (row.duration_minutes != null) metaParts.push(formatRuntime(row.duration_minutes));
  const genre = metaParts.length ? '<div class="film-card-genre">' + metaParts.join(' · ') + '</div>' : '';

  // Either kind of match gives the film a Letterboxd page to link to; only
  // a watchlist one gives it a slug the dashboard can open its own card for.
  const linkSlug = row.matched_slug || row.letterboxd_slug;
  const titleHtml = linkSlug
    ? '<a class="film-link film-card-title" target="_blank" href="https://letterboxd.com/film/' + escAttr(linkSlug) + '/">' +
      esc(row.title) + year + '</a>'
    : '<span class="film-card-title">' + esc(row.title) + year + '</span>';

  const div = document.createElement('div');
  div.className = 'film-card';
  if (row.matched_slug) div.dataset.slug = row.matched_slug;
  // Not tracked, but TMDB knows it — so "can I stream this instead" is one
  // tap away, through the same live lookup a searched film uses.
  else if (row.tmdb_id) {
    div.dataset.liveTmdbId = String(row.tmdb_id);
    div.dataset.liveTitle = row.title;
    if (row.year != null) div.dataset.liveYear = String(row.year);
  }
  div.innerHTML = poster +
    '<div class="film-card-body">' +
      '<div class="film-card-title-row">' + titleHtml +
        '<span class="film-card-end"><span class="film-card-rating">' + rating + '</span></span>' +
      '</div>' +
      director + genre + cinemaShowtimesHtml(row.showtimes) +
    '</div>';
  return div;
}

function cinemaSearchHaystack(row) {
  const cinemas = [...new Set(row.showtimes.map(s => s.cinema))];
  return [row.title, row.director, ...cinemas].filter(Boolean).join(' ').toLowerCase();
}

function renderActiveCinemaFilters() {
  const container = document.getElementById('activeCinemaFilters');
  container.innerHTML = '';
  const q = document.getElementById('cinemaSearch').value.trim();
  if (!q && cinemaDateMode === 'all' && !cinemaVenueFilter) return;

  if (q) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = '"' + q + '" ✕';
    chip.addEventListener('click', () => {
      document.getElementById('cinemaSearch').value = '';
      document.getElementById('cinemaSearchClear').classList.add('hidden');
      renderCinemas();
    });
    container.appendChild(chip);
  }
  if (cinemaDateMode !== 'all') {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = (cinemaDateMode === 'today' ? 'Today' : 'Tomorrow') + ' ✕';
    chip.addEventListener('click', () => { cinemaDateMode = 'all'; renderCinemaDateFilter(); renderCinemas(); });
    container.appendChild(chip);
  }
  if (cinemaVenueFilter) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = cinemaVenueFilter + ' ✕';
    chip.addEventListener('click', () => { cinemaVenueFilter = ''; renderCinemaVenueFilter(); renderCinemas(); });
    container.appendChild(chip);
  }
  const clearAll = document.createElement('span');
  clearAll.className = 'filter-chip clear-all-chip';
  clearAll.textContent = 'Clear all ✕';
  clearAll.addEventListener('click', () => {
    document.getElementById('cinemaSearch').value = '';
    document.getElementById('cinemaSearchClear').classList.add('hidden');
    cinemaDateMode = 'all';
    renderCinemaDateFilter();
    cinemaVenueFilter = '';
    renderCinemaVenueFilter();
    renderCinemas();
  });
  container.appendChild(clearAll);
}

function renderCinemas() {
  const container = document.getElementById('cinemasGrid');
  container.innerHTML = '';
  const q = document.getElementById('cinemaSearch').value.trim().toLowerCase();
  renderActiveCinemaFilters();

  const now = new Date();
  const todayIso = now.toISOString().slice(0, 10);
  const tomorrowIso = new Date(now.getTime() + 86400000).toISOString().slice(0, 10);

  const frag = document.createDocumentFragment();
  DATA.cinemas.forEach(row => {
    if (q && !cinemaSearchHaystack(row).includes(q)) return;
    let showtimes = row.showtimes;
    if (cinemaVenueFilter) showtimes = showtimes.filter(s => s.cinema === cinemaVenueFilter);
    if (cinemaDateMode === 'today') showtimes = showtimes.filter(s => s.showtime.slice(0, 10) === todayIso);
    else if (cinemaDateMode === 'tomorrow') showtimes = showtimes.filter(s => s.showtime.slice(0, 10) === tomorrowIso);
    if (!showtimes.length) return;
    frag.appendChild(cinemaCardHtml({ ...row, showtimes }));
  });
  container.appendChild(frag);
  ensureNotEmpty(container, 'No showtimes match your search and filters.');
}

function onCinemaCardClick(event) {
  if (event.target.closest('a')) return;
  const card = event.target.closest('.film-card');
  if (!card) return;
  if (card.dataset.slug) { openQuickLook(card.dataset.slug); return; }
  // A film showing locally that isn't tracked: no stored offers, so this is
  // the live lookup — which is exactly the question worth asking about a
  // film you've just seen is on at the Prince Charles.
  if (card.dataset.liveTmdbId) {
    openLiveQuickLook({
      tmdb_id: Number(card.dataset.liveTmdbId),
      title: card.dataset.liveTitle,
      year: card.dataset.liveYear ? Number(card.dataset.liveYear) : null,
      poster_url: null,
    });
  }
}
document.getElementById('cinemasGrid').addEventListener('click', onCinemaCardClick);
document.getElementById('cinemaSearch').addEventListener('input', renderCinemas);
wireSearchClear('cinemaSearch', 'cinemaSearchClear', renderCinemas);

renderCinemaVenueFilter();
renderCinemaDateFilter();
renderCinemas();

// ---------- Services cards ----------

const serviceCols = [
  { key: 'brand', sort: r => r.brand.toLowerCase(), dir: 1 },
  { key: 'film_count', sort: r => r.film_count, dir: -1 },
  { key: 'unique_film_count', sort: r => r.unique_film_count, dir: -1 },
];
// Every type on by default, like the Films and Country tabs. Subscription-
// needed used to start off, back when the tab was a row per (service,
// country) and those were 847 of 1,662 rows — one service per card is 327,
// sorted with the ones you have at the top, so hiding the rest costs more
// in surprise than it saves in scrolling.
const serviceFilterState = { have: true, could_get_again: true, free: true, subscription: true };
let serviceSarahFilter = 'all';

let serviceSortKey = 'film_count', serviceSortDir = -1;

function populateServiceSelects() {
  // Surfaced in their own group above the long tail of everything else
  // this film happens to be on.
  const topBrands = topServiceBrands();
  const serviceNames = [...new Set(DATA.services.map(r => r.brand))].sort((a, b) => a.localeCompare(b));
  const topNames = serviceNames.filter(n => topBrands.has(n));
  const restNames = serviceNames.filter(n => !topBrands.has(n));

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

  // Services rows aren't per-film, so unlike Films/Country there's no
  // per-context genre list to narrow to — just every genre across the
  // whole watchlist, same set Films would show with no other filter active.
  const genreSelect = document.getElementById('serviceGenreSelect');
  const genres = [...new Set(DATA.films.flatMap(f => f.genre || []))].sort((a, b) => a.localeCompare(b));
  genreSelect.innerHTML = '<option value="">Focus on a genre...</option>' +
    genres.map(g => '<option value="' + esc(g) + '">' + esc(g) + '</option>').join('');
}

function renderActiveServiceFilters() {
  const container = document.getElementById('activeServiceFilters');
  container.innerHTML = '';
  const serviceQ = document.getElementById('serviceSelect').value;
  const genreQ = document.getElementById('serviceGenreSelect').value;
  const filmQ = document.getElementById('serviceFilmSearch').value.trim();
  const anyToggleOff = CLASSIFICATIONS.some(k => !serviceFilterState[k]);
  if (!serviceQ && !genreQ && !filmQ && serviceSarahFilter === 'all' && !anyToggleOff) return;

  if (genreQ) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = genreQ + ' ✕';
    chip.addEventListener('click', () => { document.getElementById('serviceGenreSelect').value = ''; renderServicesRows(); });
    container.appendChild(chip);
  }
  if (serviceQ) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = serviceQ + ' ✕';
    chip.addEventListener('click', () => { document.getElementById('serviceSelect').value = ''; renderServicesRows(); });
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
  if (serviceSarahFilter !== 'all') {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    chip.textContent = (serviceSarahFilter === 'yes' ? 'Sarah said yes' : 'Sarah said no') + ' ✕';
    chip.addEventListener('click', () => { serviceSarahFilter = 'all'; renderServiceSarahFilter(); renderServicesRows(); });
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
    document.getElementById('serviceGenreSelect').value = '';
    document.getElementById('serviceFilmSearch').value = '';
    document.getElementById('serviceFilmSearchClear').classList.add('hidden');
    serviceSarahFilter = 'all';
    renderServiceSarahFilter();
    CLASSIFICATIONS.forEach(k => { serviceFilterState[k] = true; });
    renderServiceFilterToggles();
    renderServicesRows();
  });
  container.appendChild(clearAll);
}

function renderServiceFilterToggles() {
  renderClassificationToggles('serviceFilterToggles', serviceFilterState, renderServicesRows);
}

function renderServiceSarahFilter() {
  renderSarahFilterToggle('serviceSarahFilter', serviceSarahFilter, value => {
    serviceSarahFilter = value;
    renderServiceSarahFilter();
    renderServicesRows();
  });
}

function renderServicesRows() {
  const container = document.getElementById('servicesGrid');
  container.innerHTML = '';
  const serviceQ = document.getElementById('serviceSelect').value;
  const genreQ = document.getElementById('serviceGenreSelect').value;
  const filmQ = document.getElementById('serviceFilmSearch').value.trim().toLowerCase();
  const sarahActive = serviceSarahFilter !== 'all';
  renderActiveServiceFilters();

  // Service rows aren't per-film, so a genre/Sarah filter here means
  // "narrow each service's own film list down to that criterion" —
  // recomputing film_count/unique_film_count from the narrowed list (rather
  // than just hiding non-matching services outright) so the counts on
  // screen always match what's actually being counted. openServiceDetail
  // re-reads the row straight from DATA.services, so drilling in still
  // shows everything — scoped to keep this a top-level-grid filter, not a
  // deeper feature.
  let rows = DATA.services.map(row => {
    if (!genreQ && !sarahActive) return row;
    const uniqueSet = new Set(row.unique_slugs);
    let matchingSlugs = row.slugs;
    if (genreQ) matchingSlugs = matchingSlugs.filter(s => (DATA.films_by_slug[s]?.genre || []).includes(genreQ));
    if (sarahActive) matchingSlugs = matchingSlugs.filter(s =>
      sarahFilterMatches(DATA.films_by_slug[s]?.watch_together_status, serviceSarahFilter));
    const matchingUniqueCount = matchingSlugs.filter(s => uniqueSet.has(s)).length;
    return { ...row, slugs: matchingSlugs, film_count: matchingSlugs.length, unique_film_count: matchingUniqueCount };
  });
  if (genreQ || sarahActive) rows = rows.filter(row => row.film_count > 0);

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

  const visibleRows = rows.filter(row => {
    if (serviceQ && row.brand !== serviceQ) return false;
    if (filmQ && !row.slugs.some(s => searchHaystack(DATA.films_by_slug[s]).includes(filmQ))) return false;
    if (!serviceFilterState[row.classification]) return false;
    return true;
  });

  const frag = document.createDocumentFragment();

  visibleRows.forEach(row => {
    const card = document.createElement('div');
    card.className = 'service-card';
    // How many countries a service is in is worth knowing — it's the
    // difference between something genuinely everywhere and something that
    // only exists behind a VPN — but it isn't what the card is about.
    const reach = row.country_count === 1
      ? row.countries[0].name
      : row.country_count + ' countries';
    card.innerHTML =
      '<div class="service-card-head">' +
        '<span class="service-card-name">' + esc(row.brand) + '<i>' + esc(reach) + '</i></span>' +
        '<span class="badge badge-' + row.classification + '">' + classificationBadgeLabel(row.classification) + '</span>' +
      '</div>' +
      '<div class="service-card-stats">' + row.film_count + ' films · ' +
        '<strong>' + row.unique_film_count + '</strong> only here</div>';
    card.addEventListener('click', () => openServiceDetail(row.brand));
    frag.appendChild(card);
  });
  container.appendChild(frag);
  ensureNotEmpty(container, 'No services match your search and filters.');
}

// ---------- Service detail: one service's films, two ways ----------
//
// Detailed is the default and unchanged: a card per film, with its
// availability and expiry. Posters trades all of that for seeing the whole
// service at once, and a tap opens the same quick-look every other poster
// on the dashboard does.
//
// Which service is open is held here rather than implied by whatever was
// last rendered, so flipping the switch can redraw the same films without
// the caller having to remember how it got here.
// Remembered per page rather than globally: the two lists are read for
// different reasons — one service's catalogue is browsed by artwork, the
// whole watchlist is often browsed by what's streaming where — so a choice
// made on one shouldn't silently change the other.
const LAYOUT_KEYS = {
  service: 'watchlist_service_layout_v1',
  films: 'watchlist_films_layout_v1',
};
let currentServiceDetail = null;   // {brand, country, uniqueOnly} — country null = every country

// Posters is the default: both lists are long enough that the first thing
// wanted of them is usually "what's in here", which artwork answers faster
// than a column of cards. Detailed is one tap away and, once chosen, is
// remembered — so only an explicit "detailed" overrides this, not the
// absence of a preference.
function posterLayout(page) {
  try {
    return localStorage.getItem(LAYOUT_KEYS[page]) === 'detailed' ? 'detailed' : 'posters';
  } catch {
    return 'posters';
  }
}

function setPosterLayout(page, layout, rerender) {
  try {
    localStorage.setItem(LAYOUT_KEYS[page], layout);
  } catch {
    // Private browsing, or storage that's full or blocked — the switch still
    // works for this visit, it just won't be remembered for the next one.
  }
  rerender();
}

// Letterboxd serves its posters through a resizer whose dimensions are in
// the path, so a grid can ask for tiles instead of the 600x900 the card
// view wants — about 23KB rather than 69KB each, which is the difference
// between 1.7MB and 5MB for a service with 215 films. Any URL that doesn't
// match the expected shape is left exactly as it was, and a request that
// fails falls back to the original.
const LETTERBOXD_POSTER_SIZE_RE = /-0-600-0-900-/;

// A grid tile measures 345-387 device pixels across on a phone (115-129 CSS
// px at DPR 3), so the 150px thumb this used to ask for was being upscaled
// 2.3x and looked it. 300 costs ~9KB more per poster and lands close enough
// to 1:1 to read as sharp; the full 600 stays the retry for a thumb that
// fails, and what the hero and cards use.
function posterThumbUrl(url) {
  return url && LETTERBOXD_POSTER_SIZE_RE.test(url) ? url.replace(LETTERBOXD_POSTER_SIZE_RE, '-0-300-0-450-') : url;
}

// A tile is built from two different shapes: films_by_slug entries carry
// all_offers, while a Films-tab row carries main/other_services. Both say
// the same thing, so this reads whichever is present rather than making
// the callers convert.
//
// The country matters. "Have" across all 124 countries is true of 80% of
// the watchlist — most things are on Netflix or Prime somewhere, given a
// VPN — so a dot meaning that would be green almost everywhere and say
// almost nothing. Narrowed to one country it's 27%, which is the question
// actually being asked: can I watch this tonight, here. So the dot follows
// whatever country the view is already scoped to, and only falls back to
// "anywhere" when the view isn't scoped at all.
function watchableNowClass(film, country) {
  const entries = film.all_offers
    ? film.all_offers
    : [...Object.values(film.main || {}).flat(), ...(film.other_services || [])];
  const relevant = country ? entries.filter(e => e.country === country) : entries;
  if (relevant.some(e => e.classification === 'have')) return 'have';
  if (relevant.some(e => e.classification === 'free')) return 'free';
  return null;   // needs a subscription, or isn't streaming anywhere
}

function buildPosterTile(film, country, onPick, options) {
  const tile = document.createElement('button');
  tile.type = 'button';
  tile.className = 'poster-tile';
  const label = film.title + (film.year ? ' (' + film.year + ')' : '');
  tile.title = label;
  // An element rather than a string, because it gets swapped in for a
  // failed image below — rewriting the tile's innerHTML there would take
  // the availability dot with it.
  function titledPlaceholder() {
    const span = document.createElement('span');
    span.className = 'poster-tile-fallback';
    span.textContent = label;
    return span;
  }

  if (!film.poster_url) {
    tile.appendChild(titledPlaceholder());
  } else {
    const thumb = posterThumbUrl(film.poster_url);
    tile.innerHTML = '<img loading="lazy" alt="' + escAttr(label) + '" src="' + escAttr(thumb) + '"' +
      (thumb === film.poster_url ? '' : ' data-full="' + escAttr(film.poster_url) + '"') + '>';
    const img = tile.querySelector('img');
    img.addEventListener('error', () => {
      // A resized URL gets one retry at the size the card view uses, in case
      // only the thumbnail is missing. After that show the title instead:
      // a broken-image icon with alt text spilling over the tile is worse
      // than the placeholder a film with no artwork already gets.
      if (img.dataset.full) {
        img.src = img.dataset.full;
        delete img.dataset.full;
        return;
      }
      img.replaceWith(titledPlaceholder());
    });
  }
  const watchable = (options && options.hideDot) ? null : watchableNowClass(film, country);
  if (watchable) {
    const dot = document.createElement('span');
    dot.className = 'poster-dot poster-dot-' + watchable;
    dot.title = watchable === 'have' ? 'On a service you have' : 'Free to watch';
    tile.appendChild(dot);
  }

  tile.addEventListener('click', onPick ? () => onPick(film) : () => openQuickLook(film.slug));
  return tile;
}

function renderLayoutSwitch(containerId, page, rerender) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  const active = posterLayout(page);
  [['detailed', 'Detailed'], ['posters', 'Posters']].forEach(([value, label]) => {
    const chip = document.createElement('span');
    chip.className = 'quick-country' + (active === value ? ' active' : '');
    chip.textContent = label;
    chip.addEventListener('click', () => { if (active !== value) setPosterLayout(page, value, rerender); });
    container.appendChild(chip);
  });
}

// Both lists render the same tiles into whichever container they own, so
// the two pages can't drift apart on sizing or on what a tap does.
function fillPosterGrid(container, films, country, onPick, options) {
  const grid = document.createElement('div');
  grid.className = 'poster-grid';
  films.forEach(film => grid.appendChild(buildPosterTile(film, country, onPick, options)));
  container.appendChild(grid);
}

// The films this service has, narrowed by whichever country pill is on and
// by whether you asked for only what nothing else you have covers.
function serviceDetailSlugs({ brand, country, uniqueOnly }) {
  const row = DATA.services.find(r => r.brand === brand);
  if (!row) return [];
  const slugs = country === null ? row.slugs : (row.slugs_by_country[country] || []);
  if (!uniqueOnly) return slugs;
  // unique_slugs is computed across every country, so this stays the same
  // question whichever country pill is on: is this film on anything else I
  // have, anywhere. A film reachable on another of your services in another
  // market isn't one this subscription is buying you.
  const unique = new Set(row.unique_slugs);
  return slugs.filter(s => unique.has(s));
}

// Netflix is in 118 countries and pills for all of them would bury the
// films they're meant to filter. The list is already busiest-first, so the
// cap keeps the markets with something in them and the rest are one tap away.
const SERVICE_COUNTRY_PILL_CAP = 12;
let serviceCountryPillsExpanded = false;

function renderServiceCountryPills() {
  const { brand, country } = currentServiceDetail;
  const row = DATA.services.find(r => r.brand === brand);
  const container = document.getElementById('serviceCountryPills');
  container.innerHTML = '';
  if (!row || row.countries.length < 2) return;   // nothing to choose between

  // Busiest-first is how the data arrives, which for Netflix leads with
  // South Korea — true, and not the question. The markets actually watched
  // in come first, then the rest by how much is on them.
  const home = HOME_COUNTRY_CODES
    .map(code => row.countries.find(c => c.code === code))
    .filter(Boolean);
  const homeCodes = new Set(home.map(c => c.code));
  const all = home.concat(row.countries.filter(c => !homeCodes.has(c.code)));
  const capped = serviceCountryPillsExpanded ? all : all.slice(0, SERVICE_COUNTRY_PILL_CAP);
  // A country picked from the long list stays visible after the list closes.
  const shown = (country && !capped.some(c => c.code === country))
    ? capped.concat(all.filter(c => c.code === country))
    : capped;
  const entries = shown.map(c => ({ value: c.code, label: c.name, count: c.film_count }));

  renderQuickJumpChips('serviceCountryPills', entries, country === null ? '' : country, value => {
    // '' is the All chip; tapping the country you're already in also clears,
    // so the pills are their own way back to every country.
    currentServiceDetail.country =
      (value === '' || value === currentServiceDetail.country) ? null : value;
    renderServiceDetail();
  });

  if (!serviceCountryPillsExpanded && all.length > SERVICE_COUNTRY_PILL_CAP) {
    const more = document.createElement('span');
    more.className = 'quick-country';
    more.textContent = '+' + (all.length - SERVICE_COUNTRY_PILL_CAP) + ' more';
    more.addEventListener('click', () => { serviceCountryPillsExpanded = true; renderServiceDetail(); });
    container.appendChild(more);
  }
}

function renderServiceDetail() {
  if (!currentServiceDetail) return;
  const { brand, country } = currentServiceDetail;
  const row = DATA.services.find(r => r.brand === brand);
  const films = serviceDetailSlugs(currentServiceDetail)
    .map(slug => DATA.films_by_slug[slug])
    .filter(Boolean);

  const where = country === null
    ? (row && row.country_count > 1 ? 'All ' + row.country_count + ' countries' : (row && row.countries[0] ? row.countries[0].name : ''))
    : countryLabel(country);
  document.getElementById('serviceDetailTitle').innerHTML =
    esc(brand) + ' <i>' + esc(where) + ' · ' + films.length + ' film' + (films.length === 1 ? '' : 's') + '</i>';
  renderLayoutSwitch('serviceLayoutSwitch', 'service', renderServiceDetail);
  renderServiceCountryPills();

  const uniqueToggle = document.getElementById('serviceUniqueOnly');
  // 'active' is what the pill styles read — 'on' is the classification
  // toggles' own class and does nothing here.
  uniqueToggle.classList.toggle('active', Boolean(currentServiceDetail.uniqueOnly));
  // The count is the answer to "what would I lose", so it belongs on the
  // control rather than only in the list below it.
  uniqueToggle.textContent = 'Only on this service' + (row ? ' (' + row.unique_film_count + ')' : '');

  const container = document.getElementById('serviceDetailCards');
  container.innerHTML = '';

  if (!films.length) {
    ensureNotEmpty(container, currentServiceDetail.uniqueOnly
      ? 'Nothing here that isn\\'t on another service you have.'
      : 'Nothing tracked on this service here.');
    return;
  }

  if (posterLayout('service') === 'posters') {
    fillPosterGrid(container, films, country);
    return;
  }
  films.forEach(film => container.appendChild(buildFilmDetailCard(film, brand, country, true)));
}

document.getElementById('serviceUniqueOnly').addEventListener('click', () => {
  currentServiceDetail.uniqueOnly = !currentServiceDetail.uniqueOnly;
  renderServiceDetail();
});

// ---------- What you'd lose ----------
//
// Opened rarely and deliberately off to one side: the Services tab answers
// "what's on this", this answers "which of these am I actually paying for".
//
// Everything here is derived from DATA.services, which already carries each
// service's films and the ones on nothing else you have — so this page adds
// no payload, only a way of reading what's there.

function haveServiceRows() {
  return DATA.services
    .filter(r => r.classification === 'have')
    .slice()
    .sort((a, b) => b.unique_film_count - a.unique_film_count || b.film_count - a.film_count);
}

// The order that covers the most soonest: repeatedly take whichever service
// adds the most films nothing before it had. It answers "how far down this
// list do I have to go", which is the question behind cancelling anything —
// and it's why a service with a big catalogue can still be near-worthless
// once the ones above it are counted.
//
// Greedy, so it isn't provably the smallest set that covers everything; it's
// the order you'd actually subscribe in, which is what's being read here.
function coverageBuildUp(rows) {
  const remaining = new Map(rows.map(r => [r.brand, new Set(r.slugs)]));
  const covered = new Set();
  const steps = [];
  while (remaining.size) {
    let best = null, bestGain = 0;
    remaining.forEach((slugs, brand) => {
      let gain = 0;
      slugs.forEach(s => { if (!covered.has(s)) gain++; });
      if (gain > bestGain) { bestGain = gain; best = brand; }
    });
    if (!best) break;   // nothing left adds anything
    remaining.get(best).forEach(s => covered.add(s));
    remaining.delete(best);
    steps.push({ brand: best, gain: bestGain, cumulative: covered.size });
  }
  return { steps, total: covered.size, leftover: [...remaining.keys()] };
}

function renderSubscriptions() {
  const container = document.getElementById('subscriptionsContent');
  container.innerHTML = '';
  const rows = haveServiceRows();

  const title = document.createElement('h2');
  title.className = 'detail-title';
  title.textContent = "What you'd lose";
  container.appendChild(title);

  if (!rows.length) {
    const note = document.createElement('p');
    note.className = 'subs-nothing';
    note.textContent = 'No services configured as ones you have — set them in Settings.';
    container.appendChild(note);
    return;
  }

  const { steps, total, leftover } = coverageBuildUp(rows);
  const intro = document.createElement('p');
  intro.className = 'subs-intro';
  intro.innerHTML = 'Your ' + rows.length + ' services put <strong>' + total + '</strong> watchlist films within reach. ' +
    'In the order that covers the most soonest:';
  container.appendChild(intro);

  steps.forEach((step, i) => {
    const row = document.createElement('div');
    row.className = 'subs-step';
    row.innerHTML =
      '<span class="subs-step-rank">' + (i + 1) + '.</span>' +
      '<span class="subs-step-name">' + esc(step.brand) + '</span>' +
      '<span class="subs-step-gain">+' + step.gain + ' → ' +
        Math.round(100 * step.cumulative / total) + '%</span>';
    container.appendChild(row);
    const bar = document.createElement('div');
    bar.className = 'subs-bar';
    bar.innerHTML = '<span style="width:' + (100 * step.cumulative / total) + '%"></span>';
    container.appendChild(bar);
  });

  if (leftover.length) {
    // A service every one of whose films another already covers never wins a
    // round, so it never appears above — worth saying rather than omitting.
    const note = document.createElement('p');
    note.className = 'subs-intro';
    note.textContent = 'Adds nothing the others already cover: ' + leftover.join(', ') + '.';
    container.appendChild(note);
  }

  rows.forEach(row => {
    const section = document.createElement('div');
    section.className = 'subs-service';
    const head = document.createElement('div');
    head.className = 'subs-service-head';
    head.innerHTML = '<h3>' + esc(row.brand) + '</h3>' +
      '<span class="count">' + row.film_count + ' films · lose ' + row.unique_film_count + '</span>';
    section.appendChild(head);

    if (!row.unique_film_count) {
      const note = document.createElement('p');
      note.className = 'subs-nothing';
      note.textContent = 'Everything on it is on something else you have.';
      section.appendChild(note);
    } else {
      const films = row.unique_slugs.map(slug => DATA.films_by_slug[slug]).filter(Boolean);
      // No availability dot: every film here is on a service you have by
      // definition, so a dot on all of them would say nothing.
      fillPosterGrid(section, films, null, null, { hideDot: true });
    }
    container.appendChild(section);
  });
}

function openSubscriptions() {
  renderSubscriptions();
  showView('subscriptions');
}

document.getElementById('openSubscriptions').addEventListener('click', openSubscriptions);
document.getElementById('backFromSubscriptions').addEventListener('click', () => showView('services'));

// A service is one thing now, so opening one no longer means choosing a
// country first — that's what the pills inside are for.
function openServiceDetail(brand) {
  serviceCountryPillsExpanded = false;   // a fresh service starts with the short list
  currentServiceDetail = { brand, country: null, uniqueOnly: false };
  renderServiceDetail();
  showView('service-detail');
}

document.getElementById('serviceSelect').addEventListener('change', renderServicesRows);
document.getElementById('serviceGenreSelect').addEventListener('change', renderServicesRows);
document.getElementById('serviceFilmSearch').addEventListener('input', renderServicesRows);
wireSearchClear('serviceFilmSearch', 'serviceFilmSearchClear', renderServicesRows);
document.getElementById('servicesSortSelect').addEventListener('change', e => {
  serviceSortKey = e.target.value;
  serviceSortDir = serviceCols.find(c => c.key === serviceSortKey).dir;
  renderServicesRows();
});

populateServiceSelects();
renderServiceFilterToggles();
renderServiceSarahFilter();
renderServicesRows();

// ---------- Init ----------

// Desktop: the fixed bar needs the page content pushed down by exactly its
// own height (which varies — each tab's controls row is a different
// height) — measured directly rather than guessed, so it's correct at any
// width/font-scale, and recomputed on resize since text can wrap
// differently at different widths. Mobile reverts to a plain in-flow bar
// (see the max-width:700px CSS), so no inline override should linger there.
function updateAppBarOffset() {
  const isDesktop = window.matchMedia('(min-width: 701px)').matches;
  if (!isDesktop) {
    document.body.style.paddingTop = '';
    return;
  }
  const barHeight = document.getElementById('appBar').offsetHeight;
  document.body.style.paddingTop = (barHeight + 20) + 'px';
}
window.addEventListener('resize', updateAppBarOffset);

renderHome();
renderLists();
renderFilmFilterToggles();
renderFilmSarahFilter();
renderNotHaveOnlyToggle();
renderFilms();
renderReview();
renderSarah();
// The static HTML has all four controls blocks visible at once (no JS has
// run yet to hide the non-active ones) — showView('home') both fixes that
// and measures the now-correct bar height, rather than duplicating that
// hide/measure logic here.
showView('home');

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

// ---------- Come back where you left off ----------
//
// The browser's own scroll restoration measures the page before this script
// has switched to the right view, so it restores against the wrong content
// height. The view and offset are restored explicitly below instead.
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

//
// Pull-to-refresh reloads the page, which dropped you on Home however far
// into the Films tab you were. Restored last, after every tab has rendered,
// so the view being switched to is already built and its scroll offset
// means something.
(function restoreLastView() {
  const saved = savedLastView;
  if (!saved || !saved.view) return;
  // A stored name has to still be a view — a stale key from an older build
  // would otherwise throw on the getElementById below and take the whole
  // page's scripts with it.
  if (!document.getElementById('view-' + saved.view)) return;
  if (saved.view === 'settings') renderSettings();
  viewScrollPositions['view-' + saved.view] = saved.scrollY || 0;
  showView(saved.view);
})();

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
