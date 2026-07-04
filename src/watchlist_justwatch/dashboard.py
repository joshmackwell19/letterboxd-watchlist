import html
import json
from collections import defaultdict

from .brands import canonical_brand_name
from .state import StateDoc

MIN_COUNTRIES_FOR_MAIN_BRAND = 5


def _global_brand_countries(state: StateDoc) -> dict[str, set[str]]:
    brand_countries: dict[str, set[str]] = defaultdict(set)
    for film in state.films.values():
        for offer in film.offers:
            brand_countries[canonical_brand_name(offer.package_clear_name)].add(offer.country)
    return brand_countries


def _main_brands(brand_countries: dict[str, set[str]], favorites: set[tuple[str, str]]) -> list[str]:
    favorited_brands = {brand for brand, _country in favorites}
    main = {
        brand for brand, countries in brand_countries.items()
        if len(countries) >= MIN_COUNTRIES_FOR_MAIN_BRAND or brand in favorited_brands
    }
    return sorted(main, key=lambda b: (-len(brand_countries.get(b, ())), b))


def _film_row(film, main_brands: set[str], favorites: set[tuple[str, str]]) -> dict:
    by_brand: dict[str, set[str]] = defaultdict(set)
    for offer in film.offers:
        by_brand[canonical_brand_name(offer.package_clear_name)].add(offer.country)

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
        "any_service": any_service,
        "subscribed_service": subscribed_service,
        "coverage_countries": coverage_countries,
        "main": main_availability,
        "other_services": other_services,
    }


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
  .controls { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  input[type=text] { padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; width: 260px; }
  label { font-size: 13px; color: #333; display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .table-wrap { overflow: auto; max-height: 82vh; border: 1px solid #ddd; border-radius: 8px; background: #fff; }
  table { border-collapse: collapse; font-size: 13px; white-space: nowrap; }
  th, td { padding: 6px 10px; border-bottom: 1px solid #eee; border-right: 1px solid #f2f2f2; text-align: left;
           vertical-align: top; }
  th { position: sticky; top: 0; background: #f4f4f2; cursor: pointer; user-select: none; z-index: 2; }
  th.sticky-col, td.sticky-col { position: sticky; left: 0; background: #fff; z-index: 1; }
  th.sticky-col { z-index: 3; background: #f4f4f2; }
  td.yes { color: #0a7a2f; font-weight: 500; }
  td.no { color: #b3352a; }
  .fav { color: #0a7a2f; font-weight: 500; }
  .avail { color: #2a6fc9; }
  .other-cell { white-space: normal; max-width: 320px; color: #555; }
  .cell-scroll { max-height: 2.4em; overflow-y: auto; }
  a.film-link { color: inherit; text-decoration: none; border-bottom: 1px dotted #999; }
  a.film-link:hover { border-bottom-style: solid; }
  tr.hidden { display: none; }
</style>
</head>
<body>
<h1>Watchlist streaming dashboard</h1>
<div class="meta" id="meta"></div>
<div class="controls">
  <input type="text" id="search" placeholder="Search films...">
  <label><span class="fav">green</span> = favourited country &nbsp; <span class="avail">blue</span> = available but not favourited</label>
</div>
<div class="table-wrap"><table id="grid"><thead></thead><tbody></tbody></table></div>

<script>
const DATA = __DATA__;

document.getElementById('meta').textContent =
  DATA.films.length + ' films, ' + DATA.main_brands.length + ' main services, last checked ' + (DATA.last_run_at || 'never');

const compactCols = [
  { key: 'title', label: 'Film', sort: r => r.title.toLowerCase() },
  { key: 'year', label: 'Year', sort: r => r.year || 0 },
  { key: 'any_service', label: 'Any service?', sort: r => r.any_service ? 1 : 0 },
  { key: 'subscribed_service', label: 'Favourited service?', sort: r => r.subscribed_service ? 1 : 0 },
  { key: 'coverage_countries', label: '# countries', sort: r => r.coverage_countries },
];

let sortKey = 'title', sortDir = 1;

function brandCellHtml(row, brand) {
  const info = row.main[brand];
  if (!info) return '';
  const parts = [];
  if (info.favorited.length) parts.push('<span class="fav">' + info.favorited.join(', ') + '</span>');
  if (info.other.length) parts.push('<span class="avail">' + info.other.join(', ') + '</span>');
  return parts.join(' ');
}

function render() {
  const thead = document.querySelector('#grid thead');
  thead.innerHTML = '';

  const headRow = document.createElement('tr');
  compactCols.forEach((col, i) => {
    const th = document.createElement('th');
    th.textContent = col.label;
    if (i === 0) th.classList.add('sticky-col');
    th.addEventListener('click', () => {
      sortDir = (sortKey === col.key) ? -sortDir : 1;
      sortKey = col.key;
      renderRows();
    });
    headRow.appendChild(th);
  });
  DATA.main_brands.forEach(brand => {
    const th = document.createElement('th');
    th.textContent = brand;
    headRow.appendChild(th);
  });
  const otherTh = document.createElement('th');
  otherTh.textContent = 'Other services (not main)';
  headRow.appendChild(otherTh);
  thead.appendChild(headRow);

  renderRows();
}

function renderRows() {
  const tbody = document.querySelector('#grid tbody');
  tbody.innerHTML = '';
  const q = document.getElementById('search').value.trim().toLowerCase();

  let rows = DATA.films.slice();
  const col = compactCols.find(c => c.key === sortKey);
  if (col) {
    rows.sort((a, b) => {
      const av = col.sort(a), bv = col.sort(b);
      return av < bv ? -sortDir : av > bv ? sortDir : 0;
    });
  }

  const frag = document.createDocumentFragment();
  rows.forEach(row => {
    if (q && !row.title.toLowerCase().includes(q)) return;
    const tr = document.createElement('tr');

    compactCols.forEach((col, i) => {
      const td = document.createElement('td');
      if (col.key === 'title') {
        const year = row.year ? ' (' + row.year + ')' : '';
        td.innerHTML = '<a class="film-link" target="_blank" href="https://letterboxd.com/film/' +
          row.slug + '/">' + row.title.replace(/</g, '&lt;') + year + '</a>';
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
    otherTd.classList.add('other-cell');
    const otherInner = document.createElement('div');
    otherInner.classList.add('cell-scroll');
    otherInner.textContent = row.other_services.join(', ');
    otherTd.appendChild(otherInner);
    tr.appendChild(otherTd);

    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

document.getElementById('search').addEventListener('input', renderRows);

render();
</script>
</body>
</html>
"""
