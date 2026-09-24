"""Minimal stand-ins for the three Letterboxd page types the taste engine
reads, trimmed from the live markup (September 2026) down to what the
parsers look at — so the tests exercise the real structure without
committing anyone's actual username or ratings."""


def grid_page(films: list[tuple[str, str, int | None]], *, next_href: str | None = None) -> str:
    """films: (slug, display name, half-stars or None for watched-unrated)."""
    items = []
    for slug, name, half_stars in films:
        rating = (f'<span class="rating -micro -darker rated-{half_stars}">★</span>'
                  if half_stars is not None else "")
        items.append(
            f'<li class="griditem"><div class="react-component" data-component-class="LazyPoster" '
            f'data-item-name="{name}" data-item-slug="{slug}" data-item-link="/film/{slug}/"> '
            f'<div class="poster film-poster"></div> </div> '
            f'<p class="poster-viewingdata" data-item-uid="film:1"> {rating} </p></li>'
        )
    return f'<div class="poster-grid"><ul class="grid -p70">{"".join(items)}</ul></div>{_pagination(next_href)}'


def members_page(members: list[tuple[str, int]], *, next_href: str | None = None) -> str:
    """members: (username, half-stars) — a /film/<slug>/members/rated/<stars>/ table."""
    rows = []
    for username, half_stars in members:
        rows.append(
            f'<tr>\n\t<td class="col-member table-person"><div class="person-summary"> '
            f'<a class="avatar -a40" href="/{username}/" > <img src="a.png" alt="{username}" /> </a> '
            f'<h3 class="title-3"> <a href="/{username}/" class="name"> {username} </a> </h3> '
            f'<small class="metadata"><a href="/{username}/film/x/activity/">Activity for film</a></small> '
            f'</div></td>\n\t\t<td class="col-rating -padding-inline-large"> '
            f'<span class="rating -green rated-{half_stars}"> ★ </span> </td> '
            f'<td class="col-like -align-center -padding-inline-large"></td></tr>'
        )
    return f'<table class="person-table"><tbody>{"".join(rows)}</tbody></table>{_pagination(next_href)}'


def following_page(people: list[tuple[str, int]], *, next_href: str | None = None) -> str:
    """people: (username, films watched)."""
    rows = []
    for username, watched in people:
        rows.append(
            f'<tr>\n\t<td class="col-member table-person"><div class="person-summary"> '
            f'<a class="avatar -a40" href="/{username}/" > </a> '
            f'<h3 class="title-3"> <a href="/{username}/" class="name"> {username} </a> </h3> '
            f'<small class="metadata"> <a href="/{username}/followers/" class="_nobr">9&nbsp;followers</a> '
            f'</small> </div></td>\n\t\t<td class="col-watched -padding-inline-large table-stats">'
            f'<a class="has-icon icon-16 icon-watched" href="/{username}/films/">{watched:,}</a></td></tr>'
        )
    return f'<table class="person-table"><tbody>{"".join(rows)}</tbody></table>{_pagination(next_href)}'


def film_page(tmdb_kind: str | None) -> str:
    """The IMDb/TMDB buttons from a /film/<slug>/ page, with the body's
    data-tmdb-type saying "movie" whatever the film is, as the live site's does."""
    tmdb = (f'<a href="https://www.themoviedb.org/{tmdb_kind}/84958/" class="micro-button track-event" '
            f'data-track-action="TMDB" target="_blank" >TMDB</a>' if tmdb_kind else "")
    return (f'<body class="film backdropped" data-tmdb-type="movie" data-tmdb-id="84958"> '
            f'<p class="text-link text-footer"> <a href="http://www.imdb.com/title/tt9140554/maindetails" '
            f'class="micro-button track-event" data-track-action="IMDb" target="_blank" >IMDb</a> {tmdb} </p></body>')


CHALLENGE_PAGE = ('<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
                  '<meta http-equiv="refresh" content="360"></head><body></body></html>')


def _pagination(next_href: str | None) -> str:
    if next_href is None:
        return ('<div class="pagination"> <div class="paginate-nextprev paginate-disabled">'
                '<span class="next">Older</span></div> </div>')
    return (f'<div class="pagination"> <div class="paginate-nextprev">'
            f'<a class="next" href="{next_href}">Older</a></div> </div>')
