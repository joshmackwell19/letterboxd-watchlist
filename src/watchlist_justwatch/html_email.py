import html as html_lib
import zlib
from datetime import date
from types import SimpleNamespace

from .availability import bucket_offers
from .config import CountryConfig
from .countries import country_name
from .diff import Report, ReportEntry

_BUCKET_LABELS = [
    ("have", "Available on a service you have"),
    ("could_get_again", "Could get again (friends/family, previous subscription)"),
    ("free", "Available without a subscription (free/ad-supported)"),
    ("subscription", "Available on other subscription services"),
]

DASHBOARD_URL = "https://joshmackwell19.github.io/letterboxd-watchlist/"

# ---------------------------------------------------------------- design tokens
# "Dispatch" — the product-transactional style (KPI tiles, poster grids, colour
# chips per service) chosen after a round of mockups compared against a darker
# app-branded style and an editorial/serif style. Deliberately email-safe: no
# custom @font-face, table-based layout throughout, no CSS the major clients
# strip.

_INK = "#15181d"
_MUTED = "#6b7178"
_BORDER = "#e7e8eb"
_ACCENT = "#0f9e8f"
_ACCENT_DK = "#0a7a6e"
_ACCENT_SOFT = "#eafaf7"
_WARN = "#b45309"
_SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif"

# Soft badge backgrounds paired with a readable foreground of the same hue —
# assigned deterministically per service brand so a given brand always gets
# the same chip colour across every email and every run.
_CHIP_PALETTE = [
    ("#eef2ff", "#4338ca"), ("#f0fdfa", "#0f766e"), ("#fdf4ff", "#a21caf"),
    ("#fff7ed", "#c2410c"), ("#f0f9ff", "#0369a1"), ("#fef2f2", "#b91c1c"),
    ("#f7fee7", "#4d7c0f"), ("#fdf2f8", "#be185d"),
]


def _esc(text) -> str:
    return html_lib.escape(str(text)) if text else ""


def _chip_colors(brand: str) -> tuple[str, str]:
    return _CHIP_PALETTE[zlib.crc32(brand.encode()) % len(_CHIP_PALETTE)]


def _chips_html(chips: list[str], limit: int = 4) -> str:
    """A film on a dozen services would otherwise flood a single grid cell
    with chips and blow out that row's height relative to its neighbours —
    cap the visible list and fold the rest into a neutral "+N more" chip."""
    visible, overflow = chips[:limit], len(chips) - limit
    html = "".join(_chip(c) for c in visible)
    if overflow > 0:
        html += (f'<span style="display:inline-block; font:600 10px {_SANS}; color:{_MUTED}; background:#f2f3f4; '
                 f'border-radius:5px; padding:2px 8px; margin:0 5px 4px 0;">+{overflow} more</span>')
    return html


def _chip(brand: str) -> str:
    bg, fg = _chip_colors(brand)
    return (f'<span style="display:inline-block; font:600 10px {_SANS}; color:{fg}; background:{bg}; '
            f'border-radius:5px; padding:2px 8px; margin:0 5px 4px 0;">{_esc(brand)}</span>')


def _header(pill_label: str) -> str:
    return (
        f'<table role="presentation" width="100%" style="margin-bottom:22px;"><tr>'
        f'<td style="font:800 15px {_SANS}; color:{_INK}; letter-spacing:-.01em;">Watchlist</td>'
        f'<td align="right"><span style="display:inline-block; font:600 10.5px {_SANS}; color:{_ACCENT_DK}; '
        f'background:{_ACCENT_SOFT}; border-radius:20px; padding:5px 12px;">{_esc(pill_label)}</span></td>'
        f'</tr></table>'
    )


def _kpi_row(stats: list[tuple]) -> str:
    if not stats:
        return ""
    n = len(stats)
    tds = "".join(
        f'<td style="width:{100 // n}%; padding:16px; background:#fafbfb; border:1px solid {_BORDER}; '
        f'{"border-radius:10px 0 0 10px;" if i == 0 else ("border-radius:0 10px 10px 0; border-left:none;" if i == n - 1 else "border-left:none;")}">'
        f'<div style="font:700 24px/1 {_SANS}; color:{_INK}; font-variant-numeric:tabular-nums;">{value}</div>'
        f'<div style="font:500 11px {_SANS}; color:{_MUTED}; margin-top:5px;">{_esc(label)}</div></td>'
        for i, (value, label) in enumerate(stats)
    )
    return f'<table role="presentation" width="100%" style="margin-bottom:26px; border-collapse:collapse;"><tr>{tds}</tr></table>'


def _section(text: str) -> str:
    return f'<div style="font:700 11.5px {_SANS}; color:{_INK}; margin:28px 0 12px;">{_esc(text)}</div>'


def _empty_note(text: str) -> str:
    return f'<p style="font:400 13px {_SANS}; color:{_MUTED}; margin:0 0 8px;">{_esc(text)}</p>'


def _poster_img(url: str | None, w: int, h: int) -> str:
    if not url:
        return f'<div style="width:{w}px; height:{h}px; background:#eef0f2; border-radius:8px;"></div>'
    return f'<img src="{_esc(url)}" width="{w}" height="{h}" style="border-radius:8px; display:block; object-fit:cover;" alt="">'


def _grid(cells: list[str], cols: int = 3) -> str:
    """Lay a list of pre-rendered <td> cells out into a poster grid, cols
    per row — the visual language chosen for every film list in this
    system (new arrivals, availability changes, audits) after comparing
    it against a plain row-list and a denser text-only layout."""
    rows = ""
    for i in range(0, len(cells), cols):
        chunk = cells[i:i + cols]
        rows += "<tr>" + "".join(chunk) + ("<td></td>" * (cols - len(chunk))) + "</tr>"
    return f'<table role="presentation" width="100%" style="border-collapse:collapse;">{rows}</table>'


def _grid_item(film, *, chips: list[str] | None = None, caption: str | None = None, badge: str | None = None,
               cols: int = 3) -> str:
    year = f" ({film.year})" if getattr(film, "year", None) else ""
    ry = getattr(film, "rating", None)
    rating_html = f'<div style="font:600 10.5px {_SANS}; color:{_MUTED}; margin-top:3px;">&#9733; {ry:.2f}</div>' if ry is not None else ""
    badge_html = f'<div style="font:600 10px {_SANS}; color:{_WARN}; margin-top:3px;">{_esc(badge)}</div>' if badge else ""
    caption_html = f'<div style="font:400 11px {_SANS}; color:{_MUTED}; margin-top:3px;">{_esc(caption)}</div>' if caption else ""
    chips_html = _chips_html(chips) if chips else ""
    return (
        f'<td style="width:{100 // cols}%; padding:0 8px 16px 0; vertical-align:top;">'
        f'<a href="https://letterboxd.com/film/{_esc(film.slug)}/">{_poster_img(film.poster_url, 100, 144)}</a>'
        f'<div style="font:600 12px {_SANS}; color:{_INK}; margin-top:7px; line-height:1.3;">{_esc(film.title)}{year}</div>'
        f'{rating_html}{badge_html}<div style="margin-top:4px;">{chips_html}</div>{caption_html}</td>'
    )


def _cta() -> str:
    return (f'<table role="presentation" width="100%" style="margin-top:26px;"><tr><td>'
            f'<a href="{DASHBOARD_URL}" style="display:inline-block; font:600 13px {_SANS}; color:#ffffff; '
            f'background:{_ACCENT}; padding:12px 24px; border-radius:8px; text-decoration:none;">Open dashboard</a>'
            f'</td></tr></table>')


def _footer() -> str:
    return (f'<div style="margin-top:28px; padding-top:16px; border-top:1px solid {_BORDER}; '
            f'font:400 11.5px {_SANS}; color:{_MUTED};">Sent automatically by Watchlist, tracking your Letterboxd '
            f'watchlist against JustWatch.</div>')


def _wrap(body: str) -> str:
    return (f'<div style="font-family:{_SANS}; background:#ffffff; color:{_INK}; padding:32px; max-width:600px; '
            f'border:1px solid {_BORDER}; border-radius:12px;">{body}</div>')


# ---------------------------------------------------------------- daily update

def _bucket_chips(entries: list[tuple[str, str]]) -> list[str]:
    return sorted({brand for brand, _country in entries})


def render_report_html(report: Report, config: dict[str, CountryConfig], global_subscriptions: list[str],
                        revisitable: set[str]) -> str | None:
    if report.is_empty():
        return None

    changed_count = len(report.new_have) + len(report.new_free_tier) + len(report.new_possible)
    body = _header("Daily update")
    body += f'<div style="font:700 19px {_SANS}; color:{_INK}; margin-bottom:6px;">Here&rsquo;s what changed today</div>'
    body += (f'<div style="font:400 13px {_SANS}; color:{_MUTED}; margin-bottom:20px;">'
              f'A summary of your watchlist and streaming availability, tracked automatically.</div>')
    body += _kpi_row([
        (len(report.new_films), "New to watchlist"),
        (len(_group_by_film(report.new_have)), "Now on your services"),
        (len(_group_by_film(report.new_free_tier)) + len(_group_by_film(report.new_possible)), "Newly streaming elsewhere"),
    ])

    if report.new_films:
        body += _section("New to your watchlist")
        cells = []
        for film in report.new_films:
            caption = f"Directed by {', '.join(film.director)}" if film.director else None
            cells.append(_grid_item(film, caption=caption))
        body += _grid(cells)

    if report.new_have:
        body += _section("Available on a service you have")
        cells = [_grid_item(film, chips=sorted({o.package_clear_name for o in offers}))
                 for film, offers in _group_by_film(_dedupe_by_film_country(report.new_have))]
        body += _grid(cells)

    if report.new_free_tier:
        body += _section("Free or ad-supported")
        cells = [_grid_item(film, chips=sorted({o.package_clear_name for o in offers}))
                 for film, offers in _group_by_film(_dedupe_by_film_country(report.new_free_tier))]
        body += _grid(cells)

    if report.new_possible:
        body += _section("On a service you don't have")
        cells = [_grid_item(film, chips=sorted({o.package_clear_name for o in offers}))
                 for film, offers in _group_by_film(_dedupe_by_film_country(report.new_possible))]
        body += _grid(cells)

    if report.unmatched:
        body += _section("Could not confidently match on JustWatch")
        for film in report.unmatched:
            year = f" ({film.year})" if film.year else ""
            reason = "no search results" if film.confidence == "unmatched" else "low-confidence match"
            body += _empty_note(f"{film.title}{year} — {reason}")

    body += _cta()
    body += _footer()
    return _wrap(body)


_MONETIZATION_PRIORITY = {"FLATRATE": 0, "FREE": 1, "ADS": 2}


def _dedupe_by_film_country(entries: list[ReportEntry]) -> list[ReportEntry]:
    best: dict[tuple[str, str], ReportEntry] = {}
    for entry in entries:
        key = (entry.film.slug, entry.offer.country)
        current_best = best.get(key)
        if current_best is None:
            best[key] = entry
            continue
        rank = (_MONETIZATION_PRIORITY.get(entry.offer.monetization_type, 9), len(entry.offer.package_clear_name))
        current_rank = (_MONETIZATION_PRIORITY.get(current_best.offer.monetization_type, 9),
                        len(current_best.offer.package_clear_name))
        if rank < current_rank:
            best[key] = entry
    return list(best.values())


def _group_by_film(entries: list[ReportEntry]) -> list[tuple]:
    """One (film, offers) group per film, preserving first-seen order — a
    film with new offers in several countries gets one grid card listing
    every brand it's newly on, rather than one card per country."""
    order: list[str] = []
    by_slug: dict[str, dict] = {}
    for e in entries:
        group = by_slug.setdefault(e.film.slug, {"film": e.film, "offers": []})
        if e.film.slug not in order:
            order.append(e.film.slug)
        group["offers"].append(e.offer)
    return [(by_slug[slug]["film"], by_slug[slug]["offers"]) for slug in order]


# ---------------------------------------------------------------- availability audits

def render_film_audit_html(films: list, config: dict[str, CountryConfig], global_subscriptions: list[str],
                            revisitable: set[str]) -> str:
    body = _header("Availability audit")
    body += (f'<div style="font:700 19px {_SANS}; color:{_INK}; margin-bottom:6px;">'
              f'{len(films)} films not on a service you have</div>')
    body += (f'<div style="font:400 13px {_SANS}; color:{_MUTED}; margin-bottom:20px;">'
              f"These aren't currently available on any of your current subscriptions.</div>")
    body += _kpi_row([(len(films), "Not on your services")])

    cells = []
    for film in films:
        buckets = bucket_offers(film.offers, config, global_subscriptions, revisitable)
        chips: list[str] = []
        for key, _label in _BUCKET_LABELS[1:]:
            chips += _bucket_chips(buckets[key])
        cells.append(_grid_item(film, chips=chips or None))
    body += _grid(cells)
    body += _cta()
    body += _footer()
    return _wrap(body)


def render_film_audit_text(films: list) -> str:
    lines = [f"Watchlist films not on a service you have ({date.today().isoformat()})",
             f"{len(films)} films aren't currently available on any of your current subscriptions.", ""]
    for film in films:
        year = f" ({film.year})" if film.year else ""
        rating = f" — {film.rating:.2f}★" if film.rating is not None else ""
        lines.append(f"  {film.title}{year}{rating}")
    return "\n".join(lines)


_COUNTRY_BUCKET_LABELS = [
    ("could_get_again", "\U0001F91D"),
    ("free", "\U0001F193"),
    ("subscription", "\U0001F4B3"),
]


def _film_service_summary_text(services: list[dict]) -> str:
    by_bucket: dict[str, list[str]] = {}
    for s in services:
        by_bucket.setdefault(s["classification"], []).append(s["brand"])
    parts = [f"{emoji} {', '.join(sorted(by_bucket[key]))}" for key, emoji in _COUNTRY_BUCKET_LABELS if key in by_bucket]
    return "; ".join(parts)


def render_country_audit_text(countries: list[dict]) -> str:
    total_films = sum(len(c["films"]) for c in countries)
    lines = [f"Watchlist films not on a service you have, by VPN country ({date.today().isoformat()})",
             f"{total_films} film/country combinations across {len(countries)} countries.", ""]
    for country in countries:
        lines.append(f"{country['name']} ({len(country['films'])} films)")
        for film in country["films"]:
            year = f" ({film['year']})" if film["year"] else ""
            rating = f" — {film['rating']:.2f}★" if film["rating"] is not None else ""
            lines.append(f"  {film['title']}{year}{rating} — {_film_service_summary_text(film['services'])}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_country_audit_html(countries: list[dict]) -> str:
    total_films = sum(len(c["films"]) for c in countries)
    body = _header("Country audit")
    body += (f'<div style="font:700 19px {_SANS}; color:{_INK}; margin-bottom:6px;">'
              f'Not on a service you have, by VPN country</div>')
    body += (f'<div style="font:400 13px {_SANS}; color:{_MUTED}; margin-bottom:20px;">'
              f'{total_films} film/country combinations across {len(countries)} countries.</div>')
    body += _kpi_row([(total_films, "Combinations"), (len(countries), "Countries")])

    for country in countries:
        body += _section(f"{country['name']} · {len(country['films'])} films")
        cells = []
        for film in country["films"]:
            chips = sorted({s["brand"] for s in film["services"]})
            entry = SimpleNamespace(
                slug=film["slug"], title=film["title"], year=film["year"],
                rating=film["rating"], poster_url=film.get("poster_url"),
            )
            cells.append(_grid_item(entry, chips=chips))
        body += _grid(cells)

    body += _cta()
    body += _footer()
    return _wrap(body)


# ---------------------------------------------------------------- weekly digest

def _days_phrase(n: int) -> str:
    if n == 0:
        return "leaving today"
    if n == 1:
        return "leaving tomorrow"
    return f"leaving in {n} days"


def _country_list(codes: list[str], limit: int = 2) -> str:
    names = sorted({country_name(c) for c in codes})
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" +{len(names) - limit}"


# -- text (unchanged plain-text fallback) --

def _film_title_year(film) -> str:
    year = f" ({film.year})" if film.year else ""
    return f"{film.title}{year}"


def _film_rating(film) -> str:
    return f"{film.rating:.2f}★" if film.rating is not None else ""


def _main_group_text(main_group: dict[str, list[dict]], *, days_key: str | None = None) -> list[str]:
    lines = []
    for brand in sorted(main_group):
        lines.append(f"{brand}:")
        for e in main_group[brand]:
            film = e["film"]
            extra = f" — leaving in {e[days_key]} days" if days_key else ""
            lines.append(f"  {_film_title_year(film)} {_film_rating(film)}{extra} — {_country_list(e['countries'], limit=3)}")
        lines.append("")
    return lines


def _other_list_text(entries: list[dict], *, days_key: str | None = None) -> list[str]:
    lines = []
    for entry in entries:
        film = entry["film"]
        extra = f" — leaving in {entry[days_key]} days" if days_key else ""
        services = sorted(entry["services"].items())[:4]
        service_bits = [f"{brand} ({_country_list(countries, limit=3)})" for brand, countries in services]
        overflow = max(0, len(entry["services"]) - 4)
        if overflow:
            service_bits.append(f"+{overflow} more services")
        lines.append(f"  {_film_title_year(film)} {_film_rating(film)}{extra}")
        lines.append(f"    {'; '.join(service_bits)}")
    return lines


def render_weekly_digest_text(digest: dict) -> str:
    week_range = f"{digest['week_start'].strftime('%d %b')}–{digest['week_end'].strftime('%d %b %Y')}"
    lines = [f"This week on your watchlist ({week_range})",
             f"{digest['total_added']} added, {digest['total_leaving']} leaving on your main and other services.", ""]

    lines.append("ADDED TO YOUR MAIN SERVICES")
    if digest["main_additions"]:
        lines += _main_group_text(digest["main_additions"])
    else:
        lines += ["  Nothing new this week.", ""]
    if digest["other_additions"]:
        lines.append("ALSO ADDED ELSEWHERE")
        lines += _other_list_text(digest["other_additions"])
        lines.append("")

    lines.append("LEAVING YOUR MAIN SERVICES THIS WEEK")
    if digest["main_leaving"]:
        lines += _main_group_text(digest["main_leaving"], days_key="days")
    else:
        lines += ["  Nothing leaving this week.", ""]
    if digest["other_leaving"]:
        lines.append("ALSO LEAVING ELSEWHERE")
        lines += _other_list_text(digest["other_leaving"], days_key="min_days")

    return "\n".join(lines).rstrip()


# -- html --

def _digest_grid_main(main_group: dict[str, list[dict]], *, days_key: str | None = None) -> str:
    body = ""
    for brand in sorted(main_group):
        body += f'<div style="font:600 12px {_SANS}; color:{_INK}; margin:16px 0 8px;">{_esc(brand)}</div>'
        cells = []
        for e in main_group[brand]:
            film = e["film"]
            badge = _days_phrase(e[days_key]) if days_key else None
            cells.append(_grid_item(film, caption=_country_list(e["countries"]), badge=badge))
        body += _grid(cells)
    return body


def _digest_grid_other(entries: list[dict], *, days_key: str | None = None) -> str:
    cells = []
    for entry in entries:
        film = entry["film"]
        chips = sorted(entry["services"].keys())
        badge = _days_phrase(entry[days_key]) if days_key else None
        cells.append(_grid_item(film, chips=chips, badge=badge))
    return _grid(cells)


def render_weekly_digest_html(digest: dict) -> str:
    week_range = f"{digest['week_start'].strftime('%d %b')}&ndash;{digest['week_end'].strftime('%d %b %Y')}"
    body = _header("Weekly digest")
    body += f'<div style="font:700 19px {_SANS}; color:{_INK}; margin-bottom:4px;">This week on your watchlist</div>'
    body += f'<div style="font:400 13px {_SANS}; color:{_MUTED}; margin-bottom:20px;">{week_range}</div>'
    body += _kpi_row([(digest["total_added"], "Added this week"), (digest["total_leaving"], "Leaving this week")])

    body += _section("Added to your main services")
    if digest["main_additions"]:
        body += _digest_grid_main(digest["main_additions"])
    else:
        body += _empty_note("Nothing new this week.")
    if digest["other_additions"]:
        body += _section("Also added elsewhere")
        body += _digest_grid_other(digest["other_additions"])

    body += _section("Leaving your main services this week")
    if digest["main_leaving"]:
        body += _digest_grid_main(digest["main_leaving"], days_key="days")
    else:
        body += _empty_note("Nothing leaving this week.")
    if digest["other_leaving"]:
        body += _section("Also leaving elsewhere")
        body += _digest_grid_other(digest["other_leaving"], days_key="min_days")

    body += _cta()
    body += _footer()
    return _wrap(body)
