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


def _film_row(
    film, main_brands: set[str], config: dict[str, CountryConfig], global_subscriptions: list[str],
    revisitable: set[str]
) -> dict:
    by_brand_country = group_offers_by_brand_and_country(film.offers)

    main_availability: dict[str, list[dict]] = {}
    other_services: list[str] = []
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
                other_services.append(f"{brand} ({country})")
        if entries:
            entries.sort(key=lambda e: (e["classification"] != "have", e["country"]))
            main_availability[brand] = entries

    return {
        "title": film.title,
        "year": film.year,
        "slug": film.slug,
        "rating": film.rating,
        "any_service": bool(by_brand_country),
        "have_service": any_have,
        "coverage_countries": len(all_countries),
        "main": main_availability,
        "other_services": sorted(other_services),
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
<title>Watchlist streaming dashboard</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 24px;
         background: #fafaf8; color: #111; }
  h1 { font-size: 20px; font-weight: 500; margin: 0 0 4px; }
  .meta { color: #666; font-size: 13px; margin-bottom: 16px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 12px; }
  .tab-btn { padding: 6px 14px; border: 1px solid #ccc; border-radius: 6px; background: #fff; cursor: pointer;
             font-size: 13px; }
  .tab-btn.active { background: #222; color: #fff; border-color: #222; }
  .controls { display: flex; gap: 14px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; font-size: 13px; }
  input[type=text] { padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; width: 220px; }
  label { color: #333; display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .legend { color: #666; }
  .table-wrap { overflow: auto; max-height: 78vh; border: 1px solid #ddd; border-radius: 8px; background: #fff; }
  table { border-collapse: collapse; font-size: 13px; table-layout: fixed; }
  th, td { padding: 6px 8px; border-bottom: 1px solid #eee; border-right: 1px solid #f2f2f2; text-align: left;
           vertical-align: top; overflow: hidden; }
  th { position: sticky; top: 0; background: #f4f4f2; cursor: pointer; user-select: none; z-index: 2; }
  th.sticky-col, td.sticky-col { position: sticky; left: 0; background: #fff; z-index: 1; }
  th.sticky-col { z-index: 3; background: #f4f4f2; }
  td.yes { color: #0a7a2f; font-weight: 500; }
  td.no { color: #b3352a; }
  .cell-scroll { max-height: 2.6em; overflow-y: auto; overflow-wrap: break-word; }
  a.film-link { color: inherit; text-decoration: none; border-bottom: 1px dotted #999; }
  a.film-link:hover { border-bottom-style: solid; }
  section.view { display: none; }
  section.view.active { display: block; }
  select { padding: 7px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 12px; margin: 1px 4px 1px 0; }
  .badge-have { background: #e2f4e6; color: #0a7a2f; }
  .badge-could_get_again { background: #f0e6fb; color: #6a3fa0; }
  .badge-free { background: #e5f0fb; color: #1a5fa8; }
  .badge-subscription { background: #f3f3f0; color: #555; }
  .filter-toggle { padding: 3px 10px; border-radius: 10px; font-size: 12px; cursor: pointer; border: 1px solid transparent; }
  .filter-toggle.off { opacity: 0.35; }
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

<script>
const DATA = __DATA__;

document.getElementById('meta').textContent =
  DATA.films.length + ' films, ' + DATA.main_brands.length + ' main services, last checked ' + (DATA.last_run_at || 'never');

document.getElementById('tab-films').addEventListener('click', () => switchTab('films'));
document.getElementById('tab-services').addEventListener('click', () => switchTab('services'));
document.getElementById('tab-country').addEventListener('click', () => switchTab('country'));
function switchTab(name) {
  ['films', 'services', 'country'].forEach(n => {
    document.getElementById('tab-' + n).classList.toggle('active', n === name);
    document.getElementById('view-' + n).classList.toggle('active', n === name);
  });
}

function badgeHtml(entries, brandLabel) {
  return entries.map(e => {
    const label = brandLabel ? brandLabel : e.country;
    return '<span class="badge badge-' + e.classification + '">' + label + '</span>';
  }).join(' ');
}

// ---------- Films table ----------

const filmCols = [
  { key: 'title', label: 'Film', width: 260, sort: r => r.title.toLowerCase(), dir: 1 },
  { key: 'year', label: 'Year', width: 60, sort: r => r.year || 0, dir: -1 },
  { key: 'rating', label: 'Rating', width: 60, sort: r => r.rating == null ? -1 : r.rating, dir: -1 },
  { key: 'any_service', label: 'Any service?', width: 90, sort: r => r.any_service ? 1 : 0, dir: -1 },
  { key: 'have_service', label: 'Have?', width: 70, sort: r => r.have_service ? 1 : 0, dir: -1 },
  { key: 'coverage_countries', label: '# countries', width: 90, sort: r => r.coverage_countries, dir: -1 },
];

let filmSortKey = 'title', filmSortDir = 1;

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
    const tr = document.createElement('tr');

    filmCols.forEach((col, i) => {
      const td = document.createElement('td');
      if (col.key === 'title') {
        const year = row.year ? ' (' + row.year + ')' : '';
        td.innerHTML = '<a class="film-link" target="_blank" href="https://letterboxd.com/film/' +
          row.slug + '/">' + row.title.replace(/</g, '&lt;') + year + '</a>';
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
      const entries = row.main[brand];
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
    otherInner.textContent = row.other_services.join(', ');
    otherTd.appendChild(otherInner);
    tr.appendChild(otherTd);

    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

document.getElementById('search').addEventListener('input', renderFilmsRows);
document.getElementById('notHaveOnly').addEventListener('change', renderFilmsRows);

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
  { key: 'title', label: 'Film', width: 280, sort: r => r.title.toLowerCase(), dir: 1 },
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
        const year = row.year ? ' (' + row.year + ')' : '';
        td.innerHTML = '<a class="film-link" target="_blank" href="https://letterboxd.com/film/' +
          row.slug + '/">' + row.title.replace(/</g, '&lt;') + year + '</a>';
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
