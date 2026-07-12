import html as html_lib
from datetime import date

from .availability import bucket_offers
from .config import CountryConfig
from .countries import country_name
from .diff import Report, ReportEntry

_BUCKET_LABELS = [
    ("have", "✅ Already available on a service you have"),
    ("could_get_again", "\U0001F91D Could get again (friends/family, previous subscription)"),
    ("free", "\U0001F193 Available without a subscription (free/ad-supported)"),
    ("subscription", "\U0001F4B3 Available on other subscription services"),
]

_STYLE_CARD = "margin:0 0 28px; border-bottom:1px solid #eee; padding-bottom:20px;"
_STYLE_POSTER = "border-radius:4px; display:block;"
_STYLE_TITLE = "margin:0 0 6px; font-size:17px;"
_STYLE_META = "margin:0 0 4px; color:#555; font-size:13px;"
_STYLE_SYNOPSIS = "margin:8px 0 10px; font-size:13px; color:#333;"
_STYLE_BUCKET_HEADING = "margin:10px 0 2px; font-size:13px; font-weight:bold;"
_STYLE_BUCKET_LINE = "margin:0 0 2px; font-size:13px; color:#333;"


def _esc(text: str | None) -> str:
    return html_lib.escape(text) if text else ""


def _bucket_lines_html(entries: list[tuple[str, str]]) -> str:
    by_brand: dict[str, list[str]] = {}
    for brand, country in entries:
        by_brand.setdefault(brand, []).append(country_name(country))

    return "".join(
        f'<p style="{_STYLE_BUCKET_LINE}">{_esc(brand)}: {_esc(", ".join(sorted(countries)))}</p>'
        for brand, countries in sorted(by_brand.items())
    )


def _film_header_html(film) -> tuple[str, str]:
    """Poster + title/rating/director/starring/synopsis shared by every film
    card in the email — the rich look "new to your watchlist" already had,
    now reused everywhere a film is mentioned rather than a single line."""
    year = f" ({film.year})" if film.year else ""
    rating = f" — Letterboxd {film.rating:.2f}★" if film.rating is not None else ""
    poster = (
        f'<img src="{_esc(film.poster_url)}" width="100" style="{_STYLE_POSTER}" alt="">'
        if film.poster_url else ""
    )
    director = f'<p style="{_STYLE_META}">Directed by {_esc(", ".join(film.director))}</p>' if film.director else ""
    starring = f'<p style="{_STYLE_META}">Starring {_esc(", ".join(film.starring))}</p>' if film.starring else ""
    synopsis = f'<p style="{_STYLE_SYNOPSIS}">{_esc(film.synopsis)}</p>' if film.synopsis else ""
    title_html = (
        f'<p style="{_STYLE_TITLE}"><a href="https://letterboxd.com/film/{film.slug}/" '
        f'style="color:#111; text-decoration:none;">{_esc(film.title)}{year}</a>{rating}</p>'
        f"{director}{starring}{synopsis}"
    )
    return poster, title_html


def _film_card_table_html(poster: str, body_html: str) -> str:
    return f"""
<table role="presentation" style="{_STYLE_CARD}" width="100%">
  <tr>
    <td style="vertical-align:top; width:112px; padding-right:16px;">{poster}</td>
    <td style="vertical-align:top;">{body_html}</td>
  </tr>
</table>
"""


def render_film_card_html(film, config: dict[str, CountryConfig], global_subscriptions: list[str],
                           revisitable: set[str]) -> str:
    poster, title_html = _film_header_html(film)

    buckets = bucket_offers(film.offers, config, global_subscriptions, revisitable)
    buckets_html = ""
    if any(buckets.values()):
        for key, label in _BUCKET_LABELS:
            if buckets[key]:
                buckets_html += f'<p style="{_STYLE_BUCKET_HEADING}">{label}</p>'
                buckets_html += _bucket_lines_html(buckets[key])
    else:
        buckets_html = (f'<p style="{_STYLE_BUCKET_LINE}">Not currently streaming '
                         f'(subscription/free) in any tracked country.</p>')

    return _film_card_table_html(poster, title_html + buckets_html)


def render_new_offer_card_html(film, offers: list, heading: str) -> str:
    """Same rich poster/title/director/starring/synopsis card as a brand-new
    watchlist addition, but for an existing film that just gained specific
    new offers — the heading + offer list replaces the full-availability
    breakdown since the point here is what's new, not everything it's on."""
    poster, title_html = _film_header_html(film)
    offers_html = f'<p style="{_STYLE_BUCKET_HEADING}">{heading}</p>'
    offers_html += _bucket_lines_html([(o.package_clear_name, o.country) for o in offers])
    return _film_card_table_html(poster, title_html + offers_html)


def _wrap_document(body: str) -> str:
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; '
        'color:#111; max-width:640px;">' + body + "</div>"
    )


def render_report_html(report: Report, config: dict[str, CountryConfig], global_subscriptions: list[str],
                        revisitable: set[str]) -> str | None:
    if report.is_empty():
        return None

    body = f'<h2 style="font-size:18px; font-weight:500;">Letterboxd Watchlist — JustWatch Update ({date.today().isoformat()})</h2>'

    if report.new_films:
        body += '<h3 style="font-size:15px;">\U0001F3AC New to your watchlist</h3>'
        for film in report.new_films:
            body += render_film_card_html(film, config, global_subscriptions, revisitable)

    if report.new_have or report.new_free_tier or report.new_possible:
        body += _render_classified_section("✅ Available on a service you have", report.new_have)
        body += _render_classified_section("\U0001F193 Available via free-tier app", report.new_free_tier)
        body += _render_classified_section("\U0001F195 Available elsewhere (you don't have this service)",
                                            report.new_possible)

    if report.unmatched:
        body += '<h3 style="font-size:15px;">⚠️ Could not confidently match on JustWatch</h3>'
        for film in report.unmatched:
            year = f" ({film.year})" if film.year else ""
            reason = "no search results" if film.confidence == "unmatched" else "low-confidence match"
            body += f'<p style="{_STYLE_BUCKET_LINE}">{_esc(film.title)}{year} — {reason}</p>'

    return _wrap_document(body)


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
    film with new offers in several countries gets one card listing them
    all, rather than one bare line per country."""
    order: list[str] = []
    by_slug: dict[str, dict] = {}
    for e in entries:
        group = by_slug.setdefault(e.film.slug, {"film": e.film, "offers": []})
        if e.film.slug not in order:
            order.append(e.film.slug)
        group["offers"].append(e.offer)
    return [(by_slug[slug]["film"], by_slug[slug]["offers"]) for slug in order]


def _render_classified_section(heading: str, entries: list[ReportEntry]) -> str:
    if not entries:
        return ""
    section = f'<h3 style="font-size:15px;">{heading}</h3>'
    for film, offers in _group_by_film(_dedupe_by_film_country(entries)):
        section += render_new_offer_card_html(film, offers, "Newly available:")
    return section


def render_film_audit_html(films: list, config: dict[str, CountryConfig], global_subscriptions: list[str],
                            revisitable: set[str]) -> str:
    body = (
        f'<h2 style="font-size:18px; font-weight:500;">Watchlist films not on a service you have '
        f'({date.today().isoformat()})</h2>'
        f'<p style="font-size:13px; color:#555; margin:0 0 20px;">{len(films)} films from your watchlist '
        f"aren't currently available on any of your current subscriptions.</p>"
    )
    for film in films:
        body += render_film_card_html(film, config, global_subscriptions, revisitable)
    return _wrap_document(body)


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


def _film_service_summary_html(services: list[dict]) -> str:
    by_bucket: dict[str, list[str]] = {}
    for s in services:
        by_bucket.setdefault(s["classification"], []).append(s["brand"])
    parts = [
        f'<span style="color:#333;">{emoji} {_esc(", ".join(sorted(by_bucket[key])))}</span>'
        for key, emoji in _COUNTRY_BUCKET_LABELS if key in by_bucket
    ]
    return "&nbsp;&nbsp;".join(parts)


def render_country_audit_html(countries: list[dict]) -> str:
    total_films = sum(len(c["films"]) for c in countries)
    body = (
        f'<h2 style="font-size:18px; font-weight:500;">Watchlist films not on a service you have, '
        f'by VPN country ({date.today().isoformat()})</h2>'
        f'<p style="font-size:13px; color:#555; margin:0 0 20px;">{total_films} film/country combinations '
        f"across {len(countries)} countries.</p>"
    )
    for country in countries:
        body += (f'<h3 style="font-size:15px; margin:20px 0 6px; border-bottom:1px solid #eee; padding-bottom:4px;">'
                 f'{_esc(country["name"])} ({len(country["films"])} films)</h3>')
        for film in country["films"]:
            year = f" ({film['year']})" if film["year"] else ""
            rating = f" — {film['rating']:.2f}★" if film["rating"] is not None else ""
            body += (
                f'<p style="{_STYLE_BUCKET_LINE}">'
                f'<a href="https://letterboxd.com/film/{film["slug"]}/" style="color:#111;">{_esc(film["title"])}</a>'
                f'{year}{rating} — {_film_service_summary_html(film["services"])}</p>'
            )
    return _wrap_document(body)
