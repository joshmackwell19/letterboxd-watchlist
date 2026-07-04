import html
import json
from collections import defaultdict

from .brands import group_offers_by_brand_and_country, is_major_brand
from .config import CountryConfig, is_have_anywhere
from .countries import country_name
from .state import StateDoc

FREE_MONETIZATION_TYPES = {"ADS", "FREE"}
FREE_TIER_COUNTRIES = {"AU", "GB", "US"}
ALWAYS_MAIN_BRANDS = {"Netflix", "HBO Max"}
TOP_COUNTRIES_LIMIT = 10


def _classify(brand: str, country: str, monetization_types: set[str], config: dict[str, CountryConfig],
              global_subscriptions: list[str], revisitable: set[str]) -> str:
    if is_have_anywhere(brand, country, config, global_subscriptions):
        return "have"
    if brand in revisitable:
        return "could_get_again"
    if "FLATRATE" in monetization_types:
        return "subscription"
    return "free"


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


def _top_have_countries(
    state: StateDoc, config: dict[str, CountryConfig], global_subscriptions: list[str], limit: int = TOP_COUNTRIES_LIMIT
) -> list[dict]:
    """Countries ranked by how many watchlist films have a "have" offer
    there — quick-filter shortcuts for the films tab."""
    counts: dict[str, int] = defaultdict(int)
    for film in state.films.values():
        countries_with_have = {
            country
            for brand, by_country in group_offers_by_brand_and_country(film.offers).items()
            for country, _monetization_types in by_country.items()
            if is_have_anywhere(brand, country, config, global_subscriptions)
        }
        for country in countries_with_have:
            counts[country] += 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"code": code, "name": country_name(code), "count": count} for code, count in ranked]


def _film_row(
    film, main_brands: set[str], config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str]
) -> dict:
    by_brand_country = group_offers_by_brand_and_country(film.offers)

    main_availability: dict[str, list[dict]] = {}
    other_services: list[dict] = []
    any_have = False
    all_countries: set[str] = set()

    for brand, by_country in by_brand_country.items():
        entries = []
        for country, monetization_types in by_country.items():
            all_countries.add(country)
            classification = _classify(brand, country, monetization_types, config, global_subscriptions, revisitable)
            if classification == "have":
                any_have = True
            if brand in main_brands:
                entries.append({"country": country, "classification": classification})
            else:
                other_services.append({"brand": brand, "country": country})
        if entries:
            entries.sort(key=lambda e: (e["classification"] != "have", e["country"]))
            main_availability[brand] = entries

    other_services.sort(key=lambda o: (o["brand"], o["country"]))

    return {
        "title": film.title,
        "year": film.year,
        "slug": film.slug,
        "rating": film.rating,
        "poster_url": film.poster_url,
        "director": ", ".join(film.director) if film.director else None,
        "any_service": bool(by_brand_country),
        "have_service": any_have,
        "coverage_countries": len(all_countries),
        "main": main_availability,
        "other_services": other_services,
    }


def _service_rows(
    state: StateDoc, config: dict[str, CountryConfig], global_subscriptions: list[str], film_has_have: dict[str, bool]
) -> list[dict]:
    by_brand_country: dict[tuple[str, str], dict] = {}

    for film in state.films.values():
        by_brand_country_types = group_offers_by_brand_and_country(film.offers)
        for brand, by_country in by_brand_country_types.items():
            for country, monetization_types in by_country.items():
                key = (brand, country)
                entry = by_brand_country.setdefault(key, {"titles": [], "monetization_types": set()})
                entry["titles"].append((film.slug, film.title))
                entry["monetization_types"] |= monetization_types

    rows = []
    for (brand, country), entry in by_brand_country.items():
        titles = sorted(t for _slug, t in entry["titles"])
        unique_titles = sorted(t for slug, t in entry["titles"] if not film_has_have.get(slug, False))
        rows.append({
            "brand": brand,
            "country": country,
            "country_name": country_name(country),
            "have": is_have_anywhere(brand, country, config, global_subscriptions),
            "paid_subscription_needed": "FLATRATE" in entry["monetization_types"],
            "film_count": len(titles),
            "titles": titles,
            "unique_film_count": len(unique_titles),
            "unique_titles": unique_titles,
        })
    rows.sort(key=lambda r: (-r["film_count"], r["brand"], r["country"]))
    return rows


def _country_rows(
    state: StateDoc, config: dict[str, CountryConfig], global_subscriptions: list[str], revisitable: set[str]
) -> list[dict]:
    by_country: dict[str, list[dict]] = defaultdict(list)

    for film in state.films.values():
        by_brand_country = group_offers_by_brand_and_country(film.offers)
        country_services: dict[str, list[dict]] = defaultdict(list)
        for brand, by_c in by_brand_country.items():
            for country, monetization_types in by_c.items():
                classification = _classify(brand, country, monetization_types, config, global_subscriptions,
                                            revisitable)
                country_services[country].append({"brand": brand, "classification": classification})

        for country, services in country_services.items():
            services.sort(key=lambda s: (s["classification"] != "have", s["brand"]))
            by_country[country].append({
                "title": film.title, "year": film.year, "slug": film.slug, "rating": film.rating,
                "poster_url": film.poster_url,
                "director": ", ".join(film.director) if film.director else None,
                "services": services,
                "has_have": any(s["classification"] == "have" for s in services),
            })

    countries = []
    for code, films in by_country.items():
        films.sort(key=lambda f: f["title"].lower())
        countries.append({"code": code, "name": country_name(code), "films": films})
    countries.sort(key=lambda c: c["name"])
    return countries


def build_dashboard_data(
    state: StateDoc,
    favorites: set[tuple[str, str]],
    config: dict[str, CountryConfig],
    global_subscriptions: list[str],
    revisitable: set[str],
) -> dict:
    main_brands = _select_main_brands(state, config, global_subscriptions)
    main_brand_set = set(main_brands)

    film_has_have: dict[str, bool] = {}
    for slug, film in state.films.items():
        film_has_have[slug] = any(
            is_have_anywhere(offer.package_clear_name, offer.country, config, global_subscriptions)
            for offer in film.offers
        )

    rows = [_film_row(film, main_brand_set, config, global_subscriptions, revisitable)
            for film in state.films.values()]
    rows.sort(key=lambda r: r["title"].lower())

    return {
        "last_run_at": state.last_run_at,
        "main_brands": main_brands,
        "top_countries": _top_have_countries(state, config, global_subscriptions),
        "films": rows,
        "services": _service_rows(state, config, global_subscriptions, film_has_have),
        "countries": _country_rows(state, config, global_subscriptions, revisitable),
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
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="theme-color" content="#1f5f5b">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Watchlist">
<style>
  :root {
    color-scheme: light dark;
    --bg: #fbfbfa;
    --surface: #ffffff;
    --text: #14171a;
    --text-muted: #6b7280;
    --text-faint: #9aa1a9;
    --hairline: rgba(20, 23, 26, 0.08);
    --hairline-strong: rgba(20, 23, 26, 0.14);
    --accent: #1f5f5b;
    --accent-soft: #e7f1f0;
    --shadow: 0 1px 2px rgba(20, 23, 26, 0.04), 0 8px 24px rgba(20, 23, 26, 0.06);
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
    margin: 0; padding: 32px 36px 60px;
    background: var(--bg); color: var(--text);
    -webkit-font-smoothing: antialiased;
  }
  h1 { font-size: 21px; font-weight: 600; margin: 0 0 3px; letter-spacing: -0.01em; }
  .meta { color: var(--text-muted); font-size: 13px; margin-bottom: 20px; }
  .tabs { display: flex; gap: 6px; margin-bottom: 18px; }
  .tab-btn {
    padding: 7px 16px; border: none; border-radius: 999px; background: transparent; color: var(--text-muted);
    cursor: pointer; font-size: 13px; font-weight: 500; transition: background 0.15s, color 0.15s;
  }
  .tab-btn:hover { background: var(--hairline); }
  .tab-btn.active { background: var(--text); color: var(--surface); }
  .controls { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; font-size: 13px; }
  .quick-filters { display: flex; gap: 8px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  .quick-filters .hint { color: var(--text-faint); font-size: 12px; margin-right: 2px; }
  input[type=text] {
    padding: 9px 13px; border: 1px solid var(--hairline-strong); border-radius: 10px; font-size: 13px; width: 210px;
    background: var(--surface); color: var(--text); outline: none; transition: border-color 0.15s;
  }
  input[type=text]:focus { border-color: var(--accent); }
  label { color: var(--text-muted); display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 13px; }
  .table-wrap {
    overflow: auto; max-height: 76vh; border-radius: 14px; background: var(--surface);
    box-shadow: var(--shadow); border: 1px solid var(--hairline);
  }
  table { border-collapse: collapse; font-size: 13px; table-layout: fixed; width: 100%; }
  th, td {
    padding: 10px 14px; border-bottom: 1px solid var(--hairline); text-align: left;
    vertical-align: middle; overflow: hidden;
  }
  th {
    position: sticky; top: 0; background: var(--surface); cursor: pointer; user-select: none; z-index: 2;
    font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted);
    border-bottom: 1px solid var(--hairline-strong);
  }
  th:hover { color: var(--text); }
  tbody tr { transition: background 0.1s; }
  tbody tr:hover td { background: rgba(20, 23, 26, 0.02); }
  th.sticky-col, td.sticky-col { position: sticky; left: 0; background: var(--surface); z-index: 1; }
  th.sticky-col { z-index: 3; }
  tbody tr:hover td.sticky-col { background: #f7f8f7; }
  td.yes { color: #0f7a4a; font-weight: 600; }
  td.no { color: var(--text-faint); }
  .cell-scroll { max-height: 3.2em; overflow-y: auto; overflow-wrap: break-word; }
  a.film-link { color: inherit; text-decoration: none; }
  a.film-link:hover { color: var(--accent); }
  section.view { display: none; }
  section.view.active { display: block; }
  select {
    padding: 8px 12px; border: 1px solid var(--hairline-strong); border-radius: 10px; font-size: 13px;
    background: var(--surface); color: var(--text);
  }
  .badge {
    display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 500;
    margin: 1px 4px 1px 0; white-space: nowrap;
  }
  .badge-have { background: #e4f3ea; color: #0f7a4a; }
  .badge-could_get_again { background: #efe7fa; color: #6d3fb8; }
  .badge-free { background: #e6f1fb; color: #1c68b0; }
  .badge-subscription { background: #f0f0ee; color: #6b6b68; }
  .filter-toggle { cursor: pointer; border: 1.5px solid transparent; transition: opacity 0.15s; }
  .filter-toggle.off { opacity: 0.3; }
  .quick-country {
    padding: 5px 13px; border-radius: 999px; font-size: 12.5px; font-weight: 500; cursor: pointer;
    background: var(--surface); border: 1px solid var(--hairline-strong); color: var(--text-muted);
    transition: all 0.15s;
  }
  .quick-country:hover { border-color: var(--accent); color: var(--accent); }
  .quick-country.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .quick-country .count { color: inherit; opacity: 0.6; margin-left: 4px; }
  .film-cell { display: flex; align-items: center; gap: 11px; }
  .poster-thumb {
    width: 38px; height: 56px; object-fit: cover; border-radius: 5px; flex-shrink: 0;
    background: var(--hairline); box-shadow: 0 1px 3px rgba(0,0,0,0.12);
  }
  .poster-placeholder {
    width: 38px; height: 56px; border-radius: 5px; flex-shrink: 0; background: var(--hairline);
  }
  .film-meta { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .film-title { font-weight: 600; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .film-sub { font-size: 11.5px; color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bottom-nav { display: none; }
  @media (max-width: 700px) {
    body { padding: 16px 12px 88px; }
    h1 { font-size: 18px; }
    .tabs { display: none; }
    .controls { gap: 8px; }
    .controls input[type=text] { width: auto; flex: 1 1 130px; }
    .table-wrap { max-height: none; border-radius: 10px; }
    .bottom-nav {
      display: flex; position: fixed; bottom: 0; left: 0; right: 0; z-index: 20;
      background: var(--surface); border-top: 1px solid var(--hairline-strong);
      padding: 6px 6px calc(6px + env(safe-area-inset-bottom));
      box-shadow: 0 -2px 16px rgba(20, 23, 26, 0.07);
    }
    .bottom-nav-btn {
      flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px;
      padding: 6px 2px; background: none; border: none; color: var(--text-faint);
      font-size: 10.5px; font-weight: 500; cursor: pointer;
    }
    .bottom-nav-btn.active { color: var(--accent); }
    .bottom-nav-btn svg { width: 22px; height: 22px; stroke: currentColor; }
  }
</style>
</head>
<body>
<h1>Watchlist streaming dashboard</h1>
<div class="meta" id="meta"></div>
<div class="tabs">
  <button class="tab-btn active" id="tab-films">By film</button>
  <button class="tab-btn" id="tab-services">By service</button>
  <button class="tab-btn" id="tab-country">By VPN country</button>
</div>

<section class="view active" id="view-films">
  <div class="controls">
    <input type="text" id="search" placeholder="Search films...">
    <label><input type="checkbox" id="notHaveOnly"> Only films not on a service I have</label>
  </div>
  <div class="quick-filters" id="quickCountryFilters"></div>
  <div class="table-wrap"><table id="filmsGrid"><thead></thead><tbody></tbody></table></div>
</section>

<section class="view" id="view-country">
  <div class="controls">
    <select id="countrySelect"></select>
    <input type="text" id="countryFilmSearch" placeholder="Search films...">
    <span id="countryFilterToggles"></span>
  </div>
  <div class="table-wrap"><table id="countryGrid"><thead></thead><tbody></tbody></table></div>
</section>

<section class="view" id="view-services">
  <div class="controls">
    <input type="text" id="serviceSearch" placeholder="Search services...">
    <input type="text" id="serviceCountrySearch" placeholder="Search country...">
    <input type="text" id="serviceFilmSearch" placeholder="Search film...">
    <label><input type="checkbox" id="haveOnlyServices"> Only services I have</label>
  </div>
  <div class="table-wrap"><table id="servicesGrid"><thead></thead><tbody></tbody></table></div>
</section>

<nav class="bottom-nav">
  <button class="bottom-nav-btn active" id="nav-films">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1.5"></rect>
      <rect x="14" y="3" width="7" height="7" rx="1.5"></rect>
      <rect x="3" y="14" width="7" height="7" rx="1.5"></rect>
      <rect x="14" y="14" width="7" height="7" rx="1.5"></rect>
    </svg>
    Films
  </button>
  <button class="bottom-nav-btn" id="nav-services">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <ellipse cx="12" cy="5" rx="8" ry="3"></ellipse>
      <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"></path>
      <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"></path>
    </svg>
    Services
  </button>
  <button class="bottom-nav-btn" id="nav-country">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="9"></circle>
      <path d="M3 12h18M12 3c2.5 2.5 4 6 4 9s-1.5 6.5-4 9c-2.5-2.5-4-6-4-9s1.5-6.5 4-9z"></path>
    </svg>
    Country
  </button>
</nav>

<script>
const DATA = __DATA__;

document.getElementById('meta').textContent =
  DATA.films.length + ' films, ' + DATA.main_brands.length + ' main services, last checked ' + (DATA.last_run_at || 'never');

document.getElementById('tab-films').addEventListener('click', () => switchTab('films'));
document.getElementById('tab-services').addEventListener('click', () => switchTab('services'));
document.getElementById('tab-country').addEventListener('click', () => switchTab('country'));
document.getElementById('nav-films').addEventListener('click', () => switchTab('films'));
document.getElementById('nav-services').addEventListener('click', () => switchTab('services'));
document.getElementById('nav-country').addEventListener('click', () => switchTab('country'));
function switchTab(name) {
  ['films', 'services', 'country'].forEach(n => {
    document.getElementById('tab-' + n).classList.toggle('active', n === name);
    document.getElementById('nav-' + n).classList.toggle('active', n === name);
    document.getElementById('view-' + n).classList.toggle('active', n === name);
  });
}

function filmCellHtml(row) {
  const year = row.year ? ' (' + row.year + ')' : '';
  const poster = row.poster_url
    ? '<img class="poster-thumb" loading="lazy" src="' + row.poster_url + '" onerror="this.outerHTML=\\'<div class=&quot;poster-placeholder&quot;></div>\\'">'
    : '<div class="poster-placeholder"></div>';
  const sub = row.director ? '<span class="film-sub">' + row.director.replace(/</g, '&lt;') + '</span>' : '';
  return '<div class="film-cell">' + poster +
    '<div class="film-meta"><a class="film-link film-title" target="_blank" href="https://letterboxd.com/film/' +
    row.slug + '/">' + row.title.replace(/</g, '&lt;') + year + '</a>' + sub + '</div></div>';
}

function badgeHtml(entries, brandLabel) {
  return entries.map(e => {
    const label = brandLabel ? brandLabel : e.country;
    return '<span class="badge badge-' + e.classification + '">' + label + '</span>';
  }).join(' ');
}

// ---------- Films table ----------

const filmCols = [
  { key: 'title', label: 'Film', width: 300, sort: r => r.title.toLowerCase(), dir: 1 },
  { key: 'year', label: 'Year', width: 60, sort: r => r.year || 0, dir: -1 },
  { key: 'rating', label: 'Rating', width: 60, sort: r => r.rating == null ? -1 : r.rating, dir: -1 },
  { key: 'any_service', label: 'Any service?', width: 90, sort: r => r.any_service ? 1 : 0, dir: -1 },
  { key: 'have_service', label: 'Have?', width: 70, sort: r => r.have_service ? 1 : 0, dir: -1 },
  { key: 'coverage_countries', label: '# countries', width: 90, sort: r => r.coverage_countries, dir: -1 },
];

let filmSortKey = 'title', filmSortDir = 1;
let quickCountry = null;

function renderQuickCountryFilters() {
  const container = document.getElementById('quickCountryFilters');
  container.innerHTML = '';
  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.textContent = 'Focus on:';
  container.appendChild(hint);
  DATA.top_countries.forEach(c => {
    const span = document.createElement('span');
    span.className = 'quick-country';
    span.innerHTML = c.name + '<span class="count">' + c.count + '</span>';
    span.addEventListener('click', () => {
      quickCountry = (quickCountry === c.code) ? null : c.code;
      renderQuickCountryFilters();
      renderFilmsRows();
    });
    if (quickCountry === c.code) span.classList.add('active');
    container.appendChild(span);
  });
}

function renderFilmsHead() {
  const thead = document.querySelector('#filmsGrid thead');
  thead.innerHTML = '';
  const headRow = document.createElement('tr');

  filmCols.forEach((col, i) => {
    const th = document.createElement('th');
    th.style.width = col.width + 'px';
    th.textContent = col.label;
    if (i === 0) th.classList.add('sticky-col');
    th.addEventListener('click', () => {
      filmSortDir = (filmSortKey === col.key) ? -filmSortDir : col.dir;
      filmSortKey = col.key;
      renderFilmsRows();
    });
    headRow.appendChild(th);
  });
  DATA.main_brands.forEach(brand => {
    const th = document.createElement('th');
    th.style.width = '130px';
    th.textContent = brand;
    headRow.appendChild(th);
  });
  const otherTh = document.createElement('th');
  otherTh.style.width = '260px';
  otherTh.textContent = 'Other services';
  headRow.appendChild(otherTh);
  thead.appendChild(headRow);
}

function renderFilmsRows() {
  const tbody = document.querySelector('#filmsGrid tbody');
  tbody.innerHTML = '';
  const q = document.getElementById('search').value.trim().toLowerCase();
  const notHaveOnly = document.getElementById('notHaveOnly').checked;

  let rows = DATA.films.slice();
  const col = filmCols.find(c => c.key === filmSortKey);
  if (col) {
    rows.sort((a, b) => {
      const av = col.sort(a), bv = col.sort(b);
      return av < bv ? -filmSortDir : av > bv ? filmSortDir : 0;
    });
  }

  const frag = document.createDocumentFragment();
  rows.forEach(row => {
    if (q && !row.title.toLowerCase().includes(q)) return;
    if (notHaveOnly && row.have_service) return;

    const visibleMain = {};
    let anyVisible = false;
    DATA.main_brands.forEach(brand => {
      const entries = row.main[brand];
      if (!entries) return;
      const filtered = quickCountry ? entries.filter(e => e.country === quickCountry) : entries;
      if (filtered.length) {
        visibleMain[brand] = filtered;
        anyVisible = true;
      }
    });
    const visibleOther = quickCountry
      ? row.other_services.filter(o => o.country === quickCountry)
      : row.other_services;
    if (visibleOther.length) anyVisible = true;

    if (quickCountry && !anyVisible) return;

    const tr = document.createElement('tr');

    filmCols.forEach((col, i) => {
      const td = document.createElement('td');
      if (col.key === 'title') {
        td.innerHTML = filmCellHtml(row);
      } else if (col.key === 'rating') {
        td.textContent = row.rating == null ? '' : row.rating.toFixed(2);
      } else if (col.key === 'any_service' || col.key === 'have_service') {
        const val = row[col.key];
        td.textContent = val ? 'Y' : 'N';
        td.classList.add(val ? 'yes' : 'no');
      } else {
        td.textContent = row[col.key];
      }
      if (i === 0) td.classList.add('sticky-col');
      tr.appendChild(td);
    });

    DATA.main_brands.forEach(brand => {
      const td = document.createElement('td');
      const entries = visibleMain[brand];
      if (entries) {
        const inner = document.createElement('div');
        inner.classList.add('cell-scroll');
        inner.innerHTML = badgeHtml(entries, null);
        td.appendChild(inner);
      }
      tr.appendChild(td);
    });

    const otherTd = document.createElement('td');
    const otherInner = document.createElement('div');
    otherInner.classList.add('cell-scroll');
    otherInner.textContent = visibleOther.map(o => o.brand + ' (' + o.country + ')').join(', ');
    otherTd.appendChild(otherInner);
    tr.appendChild(otherTd);

    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

document.getElementById('search').addEventListener('input', renderFilmsRows);
document.getElementById('notHaveOnly').addEventListener('change', renderFilmsRows);

renderQuickCountryFilters();
renderFilmsHead();
renderFilmsRows();

// ---------- Services table ----------

const serviceCols = [
  { key: 'brand', label: 'Service', width: 220, sort: r => r.brand.toLowerCase(), dir: 1 },
  { key: 'country_name', label: 'Country', width: 110, sort: r => r.country_name, dir: 1 },
  { key: 'have', label: 'Have?', width: 70, sort: r => r.have ? 1 : 0, dir: -1 },
  { key: 'paid_subscription_needed', label: 'Paid sub needed?', width: 100, sort: r => r.paid_subscription_needed ? 1 : 0, dir: -1 },
  { key: 'film_count', label: '# films', width: 70, sort: r => r.film_count, dir: -1 },
  { key: 'titles', label: 'Films', width: 750 },
  { key: 'unique_film_count', label: '# unique', width: 80, sort: r => r.unique_film_count, dir: -1 },
  { key: 'unique_titles', label: 'Films not on a service I have', width: 400 },
];

let serviceSortKey = 'film_count', serviceSortDir = -1;

function renderServicesHead() {
  const thead = document.querySelector('#servicesGrid thead');
  thead.innerHTML = '';
  const headRow = document.createElement('tr');
  serviceCols.forEach((col, i) => {
    const th = document.createElement('th');
    th.style.width = col.width + 'px';
    th.textContent = col.label;
    if (i === 0) th.classList.add('sticky-col');
    if (col.sort) {
      th.addEventListener('click', () => {
        serviceSortDir = (serviceSortKey === col.key) ? -serviceSortDir : col.dir;
        serviceSortKey = col.key;
        renderServicesRows();
      });
    }
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
}

function renderServicesRows() {
  const tbody = document.querySelector('#servicesGrid tbody');
  tbody.innerHTML = '';
  const q = document.getElementById('serviceSearch').value.trim().toLowerCase();
  const countryQ = document.getElementById('serviceCountrySearch').value.trim().toLowerCase();
  const filmQ = document.getElementById('serviceFilmSearch').value.trim().toLowerCase();
  const haveOnly = document.getElementById('haveOnlyServices').checked;

  let rows = DATA.services.slice();
  const col = serviceCols.find(c => c.key === serviceSortKey);
  if (col && col.sort) {
    rows.sort((a, b) => {
      const av = col.sort(a), bv = col.sort(b);
      return av < bv ? -serviceSortDir : av > bv ? serviceSortDir : 0;
    });
  }

  const frag = document.createDocumentFragment();
  rows.forEach(row => {
    if (q && !row.brand.toLowerCase().includes(q)) return;
    if (countryQ && !row.country_name.toLowerCase().includes(countryQ)) return;
    if (filmQ && !row.titles.some(t => t.toLowerCase().includes(filmQ))) return;
    if (haveOnly && !row.have) return;
    const tr = document.createElement('tr');

    serviceCols.forEach((col, i) => {
      const td = document.createElement('td');
      if (col.key === 'have' || col.key === 'paid_subscription_needed') {
        td.textContent = row[col.key] ? 'Y' : 'N';
        td.classList.add(row[col.key] ? 'yes' : 'no');
      } else if (col.key === 'titles' || col.key === 'unique_titles') {
        const inner = document.createElement('div');
        inner.classList.add('cell-scroll');
        inner.textContent = row[col.key].join(', ');
        td.appendChild(inner);
      } else {
        td.textContent = row[col.key];
      }
      if (i === 0) td.classList.add('sticky-col');
      tr.appendChild(td);
    });

    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

document.getElementById('serviceSearch').addEventListener('input', renderServicesRows);
document.getElementById('serviceCountrySearch').addEventListener('input', renderServicesRows);
document.getElementById('serviceFilmSearch').addEventListener('input', renderServicesRows);
document.getElementById('haveOnlyServices').addEventListener('change', renderServicesRows);

renderServicesHead();
renderServicesRows();

// ---------- By VPN country ----------

const countryCols = [
  { key: 'title', label: 'Film', width: 300, sort: r => r.title.toLowerCase(), dir: 1 },
  { key: 'year', label: 'Year', width: 70, sort: r => r.year || 0, dir: -1 },
  { key: 'rating', label: 'Rating', width: 70, sort: r => r.rating == null ? -1 : r.rating, dir: -1 },
  { key: 'services', label: 'Services', width: 480 },
];

const countryClassifications = ['have', 'could_get_again', 'free', 'subscription'];
const countryClassificationLabels = { have: 'have', could_get_again: 'could get again', free: 'free', subscription: 'subscription needed' };
const countryFilterState = { have: true, could_get_again: true, free: true, subscription: true };

let countrySortKey = 'rating', countrySortDir = -1;

function populateCountrySelect() {
  const select = document.getElementById('countrySelect');
  DATA.countries.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.code;
    opt.textContent = c.name + ' (' + c.films.length + ' films)';
    select.appendChild(opt);
  });
  if (DATA.countries.length) select.value = DATA.countries[0].code;
}

function renderCountryFilterToggles() {
  const container = document.getElementById('countryFilterToggles');
  container.innerHTML = '';
  countryClassifications.forEach(key => {
    const span = document.createElement('span');
    span.textContent = countryClassificationLabels[key];
    span.classList.add('filter-toggle', 'badge', 'badge-' + key);
    span.addEventListener('click', () => {
      countryFilterState[key] = !countryFilterState[key];
      span.classList.toggle('off', !countryFilterState[key]);
      renderCountryRows();
    });
    container.appendChild(span);
  });
}

function currentCountry() {
  const code = document.getElementById('countrySelect').value;
  return DATA.countries.find(c => c.code === code);
}

function renderCountryHead() {
  const thead = document.querySelector('#countryGrid thead');
  thead.innerHTML = '';
  const headRow = document.createElement('tr');
  countryCols.forEach((col, i) => {
    const th = document.createElement('th');
    th.style.width = col.width + 'px';
    th.textContent = col.label;
    if (i === 0) th.classList.add('sticky-col');
    if (col.sort) {
      th.addEventListener('click', () => {
        countrySortDir = (countrySortKey === col.key) ? -countrySortDir : col.dir;
        countrySortKey = col.key;
        renderCountryRows();
      });
    }
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
}

function renderCountryRows() {
  const tbody = document.querySelector('#countryGrid tbody');
  tbody.innerHTML = '';
  const country = currentCountry();
  if (!country) return;

  const q = document.getElementById('countryFilmSearch').value.trim().toLowerCase();

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
    if (q && !row.title.toLowerCase().includes(q)) return;
    const visibleServices = row.services.filter(s => countryFilterState[s.classification]);
    if (!visibleServices.length) return;
    const tr = document.createElement('tr');

    countryCols.forEach((col, i) => {
      const td = document.createElement('td');
      if (col.key === 'title') {
        td.innerHTML = filmCellHtml(row);
      } else if (col.key === 'rating') {
        td.textContent = row.rating == null ? '' : row.rating.toFixed(2);
      } else if (col.key === 'services') {
        const inner = document.createElement('div');
        inner.classList.add('cell-scroll');
        inner.innerHTML = visibleServices.map(s =>
          '<span class="badge badge-' + s.classification + '">' + s.brand + '</span>').join(' ');
        td.appendChild(inner);
      } else {
        td.textContent = row[col.key];
      }
      if (i === 0) td.classList.add('sticky-col');
      tr.appendChild(td);
    });

    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

document.getElementById('countrySelect').addEventListener('change', renderCountryRows);
document.getElementById('countryFilmSearch').addEventListener('input', renderCountryRows);

populateCountrySelect();
renderCountryFilterToggles();
renderCountryHead();
renderCountryRows();
</script>
</body>
</html>
"""
