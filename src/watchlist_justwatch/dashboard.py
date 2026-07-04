import html
import json
from collections import defaultdict

from .brands import canonical_brand_name, is_junk_brand, is_major_brand
from .state import StateDoc

MIN_COUNTRIES_FOR_MAIN_BRAND = 5


def _brand_groups(film) -> dict[str, set[str]]:
    by_brand: dict[str, set[str]] = defaultdict(set)
    for offer in film.offers:
        brand = canonical_brand_name(offer.package_clear_name)
        if is_junk_brand(brand):
            continue
        by_brand[brand].add(offer.country)
    return by_brand


def _global_brand_countries(state: StateDoc) -> dict[str, set[str]]:
    brand_countries: dict[str, set[str]] = defaultdict(set)
    for film in state.films.values():
        for brand, countries in _brand_groups(film).items():
            brand_countries[brand] |= countries
    return brand_countries


def _main_brands(brand_countries: dict[str, set[str]], favorites: set[tuple[str, str]]) -> list[str]:
    favorited_brands = {brand for brand, _country in favorites}
    main = {
        brand for brand, countries in brand_countries.items()
        if is_major_brand(brand) and (len(countries) >= MIN_COUNTRIES_FOR_MAIN_BRAND or brand in favorited_brands)
    }
    return sorted(main, key=lambda b: (-len(brand_countries.get(b, ())), b))


def _film_row(film, main_brands: set[str], favorites: set[tuple[str, str]]) -> dict:
    by_brand = _brand_groups(film)

    main_availability: dict[str, dict[str, list[str]]] = {}
    for brand in main_brands:
        countries = sorted(by_brand.get(brand, ()))
        if not countries:
            continue
        favorited = [c for c in countries if (brand, c) in favorites]
        other = [c for c in countries if (brand, c) not in favorites]
        main_availability[brand] = {"favorited": favorited, "other": other}

    other_services = sorted(
        f"{brand} ({country})"
        for brand, countries in by_brand.items()
        if brand not in main_brands
        for country in countries
    )

    any_service = bool(by_brand)
    subscribed_service = any((brand, c) in favorites for brand, countries in by_brand.items() for c in countries)
    coverage_countries = len({c for countries in by_brand.values() for c in countries})

    return {
        "title": film.title,
        "year": film.year,
        "slug": film.slug,
        "rating": film.rating,
        "any_service": any_service,
        "subscribed_service": subscribed_service,
        "coverage_countries": coverage_countries,
        "main": main_availability,
        "other_services": other_services,
    }


def _service_rows(state: StateDoc, favorites: set[tuple[str, str]]) -> list[dict]:
    by_brand_country: dict[tuple[str, str], list[str]] = defaultdict(list)
    for film in state.films.values():
        for brand, countries in _brand_groups(film).items():
            for country in countries:
                by_brand_country[(brand, country)].append(film.title)

    rows = []
    for (brand, country), titles in by_brand_country.items():
        titles = sorted(titles)
        rows.append({
            "brand": brand,
            "country": country,
            "favorited": (brand, country) in favorites,
            "film_count": len(titles),
            "titles": titles,
        })
    rows.sort(key=lambda r: (-r["film_count"], r["brand"], r["country"]))
    return rows


def build_dashboard_data(state: StateDoc, favorites: set[tuple[str, str]]) -> dict:
    brand_countries = _global_brand_countries(state)
    main_brands = _main_brands(brand_countries, favorites)
    main_brand_set = set(main_brands)

    rows = [_film_row(film, main_brand_set, favorites) for film in state.films.values()]
    rows.sort(key=lambda r: r["title"].lower())

    return {
        "last_run_at": state.last_run_at,
        "main_brands": main_brands,
        "films": rows,
        "services": _service_rows(state, favorites),
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
  input[type=text] { padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; width: 260px; }
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
  .fav { color: #0a7a2f; font-weight: 500; }
  .avail { color: #2a6fc9; }
  .cell-scroll { max-height: 2.6em; overflow-y: auto; overflow-wrap: break-word; }
  a.film-link { color: inherit; text-decoration: none; border-bottom: 1px dotted #999; }
  a.film-link:hover { border-bottom-style: solid; }
  .sort-arrow { font-size: 10px; opacity: 0.6; }
  section.view { display: none; }
  section.view.active { display: block; }
</style>
</head>
<body>
<h1>Watchlist streaming dashboard</h1>
<div class="meta" id="meta"></div>
<div class="tabs">
  <button class="tab-btn active" id="tab-films">By film</button>
  <button class="tab-btn" id="tab-services">By service</button>
</div>

<section class="view active" id="view-films">
  <div class="controls">
    <input type="text" id="search" placeholder="Search films...">
    <label><input type="checkbox" id="notFavOnly"> Only films not on a favourited service</label>
    <span class="legend"><span class="fav">green</span> = favourited country &nbsp; <span class="avail">blue</span> = available, not favourited</span>
  </div>
  <div class="table-wrap"><table id="filmsGrid"><thead></thead><tbody></tbody></table></div>
</section>

<section class="view" id="view-services">
  <div class="controls">
    <input type="text" id="serviceSearch" placeholder="Search services...">
    <label><input type="checkbox" id="favOnlyServices"> Only favourited services</label>
  </div>
  <div class="table-wrap"><table id="servicesGrid"><thead></thead><tbody></tbody></table></div>
</section>

<script>
const DATA = __DATA__;

document.getElementById('meta').textContent =
  DATA.films.length + ' films, ' + DATA.main_brands.length + ' main services, last checked ' + (DATA.last_run_at || 'never');

document.getElementById('tab-films').addEventListener('click', () => switchTab('films'));
document.getElementById('tab-services').addEventListener('click', () => switchTab('services'));
function switchTab(name) {
  document.getElementById('tab-films').classList.toggle('active', name === 'films');
  document.getElementById('tab-services').classList.toggle('active', name === 'services');
  document.getElementById('view-films').classList.toggle('active', name === 'films');
  document.getElementById('view-services').classList.toggle('active', name === 'services');
}

// ---------- Films table ----------

const filmCols = [
  { key: 'title', label: 'Film', width: 260, sort: r => r.title.toLowerCase(), dir: 1 },
  { key: 'year', label: 'Year', width: 60, sort: r => r.year || 0, dir: -1 },
  { key: 'rating', label: 'Rating', width: 60, sort: r => r.rating == null ? -1 : r.rating, dir: -1 },
  { key: 'any_service', label: 'Any service?', width: 90, sort: r => r.any_service ? 1 : 0, dir: -1 },
  { key: 'subscribed_service', label: 'Favourited?', width: 90, sort: r => r.subscribed_service ? 1 : 0, dir: -1 },
  { key: 'coverage_countries', label: '# countries', width: 90, sort: r => r.coverage_countries, dir: -1 },
];

let filmSortKey = 'title', filmSortDir = 1;

function brandCellHtml(row, brand) {
  const info = row.main[brand];
  if (!info) return '';
  const parts = [];
  if (info.favorited.length) parts.push('<span class="fav">' + info.favorited.join(', ') + '</span>');
  if (info.other.length) parts.push('<span class="avail">' + info.other.join(', ') + '</span>');
  return '<div class="cell-scroll">' + parts.join(' ') + '</div>';
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
    th.style.width = '110px';
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
  const notFavOnly = document.getElementById('notFavOnly').checked;

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
    if (notFavOnly && row.subscribed_service) return;
    const tr = document.createElement('tr');

    filmCols.forEach((col, i) => {
      const td = document.createElement('td');
      if (col.key === 'title') {
        const year = row.year ? ' (' + row.year + ')' : '';
        td.innerHTML = '<a class="film-link" target="_blank" href="https://letterboxd.com/film/' +
          row.slug + '/">' + row.title.replace(/</g, '&lt;') + year + '</a>';
      } else if (col.key === 'rating') {
        td.textContent = row.rating == null ? '' : row.rating.toFixed(2);
      } else if (col.key === 'any_service' || col.key === 'subscribed_service') {
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
      td.innerHTML = brandCellHtml(row, brand);
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
document.getElementById('notFavOnly').addEventListener('change', renderFilmsRows);

renderFilmsHead();
renderFilmsRows();

// ---------- Services table ----------

const serviceCols = [
  { key: 'brand', label: 'Service', width: 220, sort: r => r.brand.toLowerCase(), dir: 1 },
  { key: 'country', label: 'Country', width: 80, sort: r => r.country, dir: 1 },
  { key: 'favorited', label: 'Favourited?', width: 100, sort: r => r.favorited ? 1 : 0, dir: -1 },
  { key: 'film_count', label: '# films', width: 80, sort: r => r.film_count, dir: -1 },
  { key: 'titles', label: 'Films', width: 500 },
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
  const favOnly = document.getElementById('favOnlyServices').checked;

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
    if (favOnly && !row.favorited) return;
    const tr = document.createElement('tr');

    serviceCols.forEach((col, i) => {
      const td = document.createElement('td');
      if (col.key === 'favorited') {
        td.textContent = row.favorited ? 'Y' : 'N';
        td.classList.add(row.favorited ? 'yes' : 'no');
      } else if (col.key === 'titles') {
        const inner = document.createElement('div');
        inner.classList.add('cell-scroll');
        inner.textContent = row.titles.join(', ');
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
document.getElementById('favOnlyServices').addEventListener('change', renderServicesRows);

renderServicesHead();
renderServicesRows();
</script>
</body>
</html>
"""
