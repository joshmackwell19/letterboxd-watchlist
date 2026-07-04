import html
import json
from pathlib import Path

from .config import CountryConfig, canonical_display_name, classify_offer
from .state import StateDoc

_STATUS_PRIORITY = ["have", "free_tier", "new_possible", "none"]


def _film_row(film, config: dict[str, CountryConfig]) -> dict:
    row = {
        "title": film.title,
        "year": film.year,
        "confidence": film.confidence,
        "any_service": False,
        "subscribed_service": False,
        "countries": {},
        "matrix": {},
    }

    for country, country_config in config.items():
        offers_here = [o for o in film.offers if o.country == country]
        by_name: dict[str, str] = {}
        for offer in offers_here:
            name = canonical_display_name(offer, country_config)
            classification = classify_offer(offer, country_config)
            existing = by_name.get(name)
            if existing is None or _STATUS_PRIORITY.index(classification) < _STATUS_PRIORITY.index(existing):
                by_name[name] = classification

        have = sorted(n for n, c in by_name.items() if c == "have")
        free = sorted(n for n, c in by_name.items() if c == "free_tier")
        other = sorted(n for n, c in by_name.items() if c == "new_possible")

        if have:
            status = "have"
        elif free:
            status = "free_tier"
        elif other:
            status = "new_possible"
        else:
            status = "none"

        row["countries"][country] = {"status": status, "have": have, "free": free, "other": other}
        for name in have + free + other:
            row["matrix"][f"{country}: {name}"] = True

        if status != "none":
            row["any_service"] = True
        if status == "have":
            row["subscribed_service"] = True

    return row


def build_dashboard_data(state: StateDoc, config: dict[str, CountryConfig]) -> dict:
    rows = [_film_row(film, config) for film in state.films.values()]
    rows.sort(key=lambda r: r["title"].lower())

    matrix_columns: list[str] = []
    seen = set()
    for country in config:
        names = sorted({name for row in rows for name in row["matrix"] if name.startswith(f"{country}: ")})
        for name in names:
            if name not in seen:
                seen.add(name)
                matrix_columns.append(name)

    return {
        "last_run_at": state.last_run_at,
        "countries": list(config.keys()),
        "matrix_columns": matrix_columns,
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
  .table-wrap { overflow: auto; max-height: 80vh; border: 1px solid #ddd; border-radius: 8px; background: #fff; }
  table { border-collapse: collapse; font-size: 13px; white-space: nowrap; }
  th, td { padding: 6px 10px; border-bottom: 1px solid #eee; border-right: 1px solid #f2f2f2; text-align: left; }
  th { position: sticky; top: 0; background: #f4f4f2; cursor: pointer; user-select: none; z-index: 2; }
  th.sticky-col, td.sticky-col { position: sticky; left: 0; background: #fff; z-index: 1; }
  th.sticky-col { z-index: 3; background: #f4f4f2; }
  td.yes { color: #0a7a2f; font-weight: 500; }
  td.no { color: #b3352a; }
  .matrix-col { display: none; text-align: center; }
  .matrix-col.show { display: table-cell; }
  .status-have { color: #0a7a2f; }
  .status-free_tier { color: #b8860b; }
  .status-new_possible { color: #2a6fc9; }
  .status-none { color: #999; }
  tr.hidden { display: none; }
</style>
</head>
<body>
<h1>Watchlist streaming dashboard</h1>
<div class="meta" id="meta"></div>
<div class="controls">
  <input type="text" id="search" placeholder="Search films...">
  <label><input type="checkbox" id="matrixToggle"> Show full service matrix (every service as its own column)</label>
</div>
<div class="table-wrap"><table id="grid"><thead></thead><tbody></tbody></table></div>

<script>
const DATA = __DATA__;

document.getElementById('meta').textContent =
  DATA.films.length + ' films, last checked ' + (DATA.last_run_at || 'never');

const compactCols = [
  { key: 'title', label: 'Film', sort: r => r.title.toLowerCase() },
  { key: 'year', label: 'Year', sort: r => r.year || 0 },
  { key: 'any_service', label: 'Any service?', sort: r => r.any_service ? 1 : 0 },
  { key: 'subscribed_service', label: 'Subscribed?', sort: r => r.subscribed_service ? 1 : 0 },
];
for (const country of DATA.countries) {
  compactCols.push({ key: 'status_' + country, label: country + ' status', sort: r => r.countries[country].status });
  compactCols.push({ key: 'have_' + country, label: country + ' have' });
  compactCols.push({ key: 'other_' + country, label: country + ' elsewhere' });
}

let sortKey = null, sortDir = 1;

function cellValue(row, col) {
  if (col.key === 'title') return row.title + (row.year ? ' (' + row.year + ')' : '');
  if (col.key === 'year') return row.year || '';
  if (col.key === 'any_service') return row.any_service ? 'Y' : 'N';
  if (col.key === 'subscribed_service') return row.subscribed_service ? 'Y' : 'N';
  if (col.key.startsWith('status_')) {
    const c = col.key.slice(7);
    return row.countries[c].status;
  }
  if (col.key.startsWith('have_')) {
    const c = col.key.slice(5);
    return row.countries[c].have.concat(row.countries[c].free).join(', ');
  }
  if (col.key.startsWith('other_')) {
    const c = col.key.slice(6);
    return row.countries[c].other.join(', ');
  }
  return '';
}

function render() {
  const thead = document.querySelector('#grid thead');
  const tbody = document.querySelector('#grid tbody');
  thead.innerHTML = '';
  tbody.innerHTML = '';

  const headRow = document.createElement('tr');
  compactCols.forEach((col, i) => {
    const th = document.createElement('th');
    th.textContent = col.label;
    if (i < 2) th.classList.add('sticky-col');
    th.addEventListener('click', () => {
      if (!col.sort) return;
      sortDir = (sortKey === col.key) ? -sortDir : 1;
      sortKey = col.key;
      renderRows();
    });
    headRow.appendChild(th);
  });
  DATA.matrix_columns.forEach(name => {
    const th = document.createElement('th');
    th.textContent = name;
    th.classList.add('matrix-col');
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);

  renderRows();
}

function renderRows() {
  const tbody = document.querySelector('#grid tbody');
  tbody.innerHTML = '';
  const q = document.getElementById('search').value.trim().toLowerCase();

  let rows = DATA.films.slice();
  if (sortKey) {
    const col = compactCols.find(c => c.key === sortKey);
    rows.sort((a, b) => {
      const av = col.sort(a), bv = col.sort(b);
      return av < bv ? -sortDir : av > bv ? sortDir : 0;
    });
  }

  rows.forEach(row => {
    if (q && !row.title.toLowerCase().includes(q)) return;
    const tr = document.createElement('tr');
    compactCols.forEach((col, i) => {
      const td = document.createElement('td');
      const val = cellValue(row, col);
      td.textContent = val;
      if (i < 2) td.classList.add('sticky-col');
      if (col.key === 'any_service' || col.key === 'subscribed_service') {
        td.classList.add(val === 'Y' ? 'yes' : 'no');
      }
      if (col.key.startsWith('status_')) td.classList.add('status-' + val);
      tr.appendChild(td);
    });
    DATA.matrix_columns.forEach(name => {
      const td = document.createElement('td');
      td.textContent = row.matrix[name] ? '✓' : '';
      td.classList.add('matrix-col');
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

document.getElementById('search').addEventListener('input', renderRows);
document.getElementById('matrixToggle').addEventListener('change', e => {
  document.querySelectorAll('.matrix-col').forEach(el => el.classList.toggle('show', e.target.checked));
});

render();
</script>
</body>
</html>
"""
