#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright==1.62.0"]
# ///
"""Find AMC showtimes for a movie at one theatre or every nearby one, pull every seat map, and write a seat-map page.

Reads public pages through a headless WebKit session (plain HTTP clients get a Cloudflare 403).
It never selects, holds or buys seats.
"""
import argparse
import asyncio
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

AMC = "https://www.amctheatres.com"
DEFAULT_THEATRE = "amc-lincoln-square-13"
# Center zone, as fractions of the seat grid's depth (0 = front row) and width.
ZONE = {"y0": 0.4, "y1": 0.95, "x0": 0.275, "x1": 0.725}
# AMC's seat types for wheelchair users and their companions, and each one's code in the seat map.
ACCESSIBLE = {"Wheelchair": 1, "Companion": 2}
# Seconds to wait between tries when AMC's Cloudflare answers 429 "Just a moment...". That happens
# after several runs back to back, and for some single URLs even when everything else loads.
BACKOFF = (0, 20, 60, 120)
SEAT_BACKOFF = (0, 10, 30)
SECTION = '[aria-label$="Showtimes by Features and Accesibility"]'  # AMC's own spelling

# Every showtime in AMC's rendered list for one date. A theatre page lists that theatre;
# a movie page lists the movie at every theatre near the one it is anchored to.
SHOWTIMES_JS = r"""
(section) => {
  const out = [];
  for (const sec of document.querySelectorAll(section)) {
    const label = sec.getAttribute('aria-label').replace(/ Showtimes by Features and Accesibility$/, '');
    const i = label.indexOf(', ');
    const theatre = label.slice(0, i), movie = label.slice(i + 2);
    for (const li of sec.querySelectorAll('li[aria-label$=" Showtimes"]')) {
      const format = li.getAttribute('aria-label').replace(/ Showtimes$/, '');
      for (const a of li.querySelectorAll('a[href^="/showtimes/"]')) {
        const time = (a.textContent.match(/\d{1,2}:\d{2}\s*[ap]m/i) || [''])[0].replace(/\s/g, '').toLowerCase();
        out.push({id: a.getAttribute('href').split('/')[2], theatre, movie, format, time});
      }
    }
  }
  return out;
}
"""
FETCH_JS = "async (u) => { const r = await fetch(u, {credentials: 'include'}); return {status: r.status, body: await r.text()}; }"
# The seat page is server-rendered; its seatingLayout JSON rides in the Next.js RSC payload.
RSC_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def blocked(status, url):
    sys.exit(f"AMC kept answering HTTP {status} for {url}; it is probably rate-limiting this connection. Try again in a few minutes.")


async def pause(wait, status):
    if wait:
        print(f"AMC answered HTTP {status}; waiting {wait}s", file=sys.stderr)
        await asyncio.sleep(wait)


async def fetch_text(page, url, backoff=BACKOFF, required=True):
    """GET a same-origin page from inside the browser session; None if not required and AMC won't serve it."""
    status = None
    for wait in backoff:
        await pause(wait, status)
        r = await page.evaluate(FETCH_JS, url)
        status = r["status"]
        if status == 200:
            return r["body"]
    if required:
        blocked(status, url)
    print(f"AMC kept answering HTTP {status} for {url}", file=sys.stderr)


async def goto(page, url):
    status = None
    for wait in BACKOFF:
        await pause(wait, status)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        if page.url.startswith("https://queue.amctheatres.com"):
            print("Waiting in AMC's queue...", file=sys.stderr)
            await page.wait_for_url(re.compile(r"^https://www\.amctheatres\.com/"), timeout=180_000)
            return
        status = response.status if response else None
        if status is None or status < 400:
            return
    blocked(status, url)


async def resolve_theatre(page, market, theatre):
    url = f"{AMC}/movie-theatres/{market}"
    await goto(page, url)
    if re.fullmatch(r"amc-[a-z0-9-]+", theatre):
        return theatre
    # Neither list is complete on its own: the market page skips some Manhattan theatres,
    # and a theatre's own switcher lists only the ones near it.
    slugs = set(re.findall(rf"/movie-theatres/{market}/([a-z0-9-]+)", await fetch_text(page, url)))
    if slugs:
        nearby = await fetch_text(page, f"{url}/{min(slugs)}/showtimes")
        slugs |= set(re.findall(r'\\"value\\":\\"(amc-[a-z0-9-]+)\\"', nearby))
    slugs = sorted(slugs)
    hits = [s for s in slugs if has_words(s, theatre)]
    if len(hits) == 1:
        return hits[0]
    sys.exit(f"Theatre {theatre!r} matched {hits or 'nothing'} in {market}. Theatres: {', '.join(slugs)}")


def resolve_movie(theatre_page, movie):
    """The movie's slug (digger-79125), from the movie switcher on a theatre page."""
    slugs = {s for s in re.findall(r'\\"value\\":\\"([a-z0-9-]+-\d+)\\"', theatre_page)
             if not s.startswith("amc-") and re.search("[a-z]", s)}
    words = re.findall(r"[a-z0-9]+", movie.lower())
    hits = sorted(s for s in slugs if all(w in s for w in words))
    exact = [s for s in hits if re.sub(r"-\d+$", "", s) == "-".join(words)]
    if len(exact) == 1 or len(hits) == 1:
        return (exact or hits)[0]
    sys.exit(f"Movie {movie!r} matched {hits or 'nothing'}. Movies: {', '.join(sorted(slugs))}")


def has_words(text, query):
    """True when every word of query is in text: 'lincoln square' or 'amc-lincoln-square-13' in 'AMC Lincoln Square 13'."""
    return all(w in text.lower() for w in re.findall(r"[a-z0-9]+", (query or "").lower()))


def wanted(s, args):
    return (has_words(s["movie"], args.movie) and has_words(s["format"], args.format)
            and (args.theatre == "all" or has_words(s["theatre"], args.theatre)))


async def listed_shows(page, url):
    """Every showtime a date page lists, or [] if it never renders its list."""
    await goto(page, url)
    try:
        await page.wait_for_selector(SECTION, timeout=15_000)
    except PWTimeout:
        print(f"{url} has showtimes but never rendered its showtime list; AMC's page may have changed.", file=sys.stderr)
        return []
    # A movie page renders its anchor theatre first and the nearby theatres a moment later.
    count = -1
    for _ in range(15):
        n = await page.locator(SECTION).count()
        if n == count:
            break
        count = n
        await page.wait_for_timeout(700)
    return await page.evaluate(SHOWTIMES_JS, SECTION)


def seat_page(page_html, id):
    """Seats, runtime and the no-trailers flag from a showtime's seat page; None when it has no seat map."""
    payload = "".join(json.loads(m.group(1)) for m in RSC_PUSH.finditer(page_html))
    k = payload.find('"seatingLayout":')
    if k < 0:
        return None
    layout, _ = json.JSONDecoder().raw_decode(payload, payload.index("{", k))
    runtime = re.search(r'"runTime":(\d+)', payload)
    # Only this showtime's own attributes count: the movie also carries a runTimeAttributes list
    # that names notrailer for every movie.
    i = payload.find(f'"showtimeId":{id}')
    j = payload.find('"prices"', i)
    return {
        "seats": [[x["row"], x["column"], x["name"], x["type"], int(x["available"])] for x in layout["seats"] if x.get("shouldDisplay")],
        "runtime": int(runtime.group(1)) if runtime else None,
        "notrailer": i >= 0 and '"code":"notrailer"' in payload[i : j if j > i else i + 3000],
    }


async def seat_maps(page, ids):
    gate = asyncio.Semaphore(3)

    async def one(id):
        async with gate:
            body = await fetch_text(page, f"{AMC}/showtimes/{id}/seats", SEAT_BACKOFF, required=False)
            return id, seat_page(body, id) if body else None

    return dict(await asyncio.gather(*(one(id) for id in ids)))


def seat_number(name):
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else None


def analyze(seats, size):
    rows = [s[0] for s in seats]
    cols = [s[1] for s in seats]
    r0, r1, c0, c1 = min(rows), max(rows), min(cols), max(cols)
    ny = lambda r: (r - r0) / max(1, r1 - r0)
    nx = lambda c: (c - c0) / max(1, c1 - c0)

    def score(x, y):
        # Centered matters most, ideal depth is ~2/3 of the way back. A weighted product rather
        # than a sum, so a dead-center front-row seat can't outrank a decent seat further back.
        horizontal = 1 - min(1, abs(x - 0.5) * 2)
        vertical = 1 - min(1, abs(y - 0.68) / 0.68)
        return round(100 * horizontal**0.65 * vertical**0.35)

    def in_zone(r, c):
        return ZONE["y0"] <= ny(r) <= ZONE["y1"] and ZONE["x0"] <= nx(c) <= ZONE["x1"]

    usable = [s for s in seats if s[4] and s[3] not in ACCESSIBLE]
    by_row = {}
    for s in usable:
        by_row.setdefault(s[0], []).append(s)
    blocks = []
    for row in by_row.values():
        row.sort(key=lambda s: s[1])
        for i in range(len(row) - size + 1):
            run = row[i : i + size]
            if run[-1][1] - run[0][1] != size - 1:
                continue
            mid = (run[0][1] + run[-1][1]) / 2
            blocks.append({"names": sorted((s[2] for s in run), key=seat_number), "score": score(nx(mid), ny(run[0][0]))})
    blocks.sort(key=lambda b: -b["score"])
    return {
        "open": len(usable),
        "zone_open": sum(in_zone(s[0], s[1]) for s in usable),
        "best": blocks[0] if blocks else None,
    }


def label(names):
    if len(names) == 1:
        return names[0]
    low, high = sorted(names, key=seat_number)[:: len(names) - 1]
    return f"{low}–{seat_number(high)}"


def minutes(time):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})([ap])m", time)
    return (int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)) * 60 + int(m.group(2)) if m else 0


def clock(m):
    h = m // 60 % 24
    return f"{h % 12 or 12}:{m % 60:02d}{'am' if h < 12 else 'pm'}"


def film_window(s):
    """Rough start and end of the film itself, or None without a runtime.

    AMC says films start 25-30 minutes after the listed time; a "No Trailers" show starts on time.
    The window takes the earliest start and the latest end.
    """
    if not s.get("runtime"):
        return None
    t = minutes(s["time"])
    early, late = (0, 0) if s["notrailer"] else (25, 30)
    return clock(t + early), clock(t + late + s["runtime"])


def started(s, now):
    """True once the film itself has begun, so a show still listed after its start isn't offered."""
    late = 0 if s.get("notrailer") else 25
    return s["date"] < now.date().isoformat() or (s["date"] == now.date().isoformat() and minutes(s["time"]) + late <= now.hour * 60 + now.minute)


def day_label(date):
    return dt.date.fromisoformat(date).strftime("%a %b %-d")


async def collect(args):
    """Showtimes for the date range, with seat maps for the ones that match.

    With --movie, each date is read from the movie's page, which lists every nearby theatre, and
    --theatre filters the result; without it, from the theatre's own page.
    """
    async with async_playwright() as p:
        browser = await p.webkit.launch()
        page = await browser.new_page(locale="en-US", timezone_id="America/New_York", viewport={"width": 1280, "height": 900})
        anchor = await resolve_theatre(page, args.market, DEFAULT_THEATRE if args.theatre == "all" else args.theatre)
        if args.theatre != "all":
            args.theatre = anchor
        base = f"{AMC}/movie-theatres/{args.market}/{anchor}/showtimes"
        if args.movie:
            slug = resolve_movie(await fetch_text(page, base), args.movie)
            date_url = lambda d: f"{AMC}/movies/{slug}/showtimes?date={d}&theatre={anchor}"
        else:
            date_url = lambda d: f"{base}?date={d}"

        listed, last_found, empty_run = [], None, 0
        day = args.start
        while day <= args.end:
            date = day.isoformat()
            url = date_url(date)
            found = []
            # Cheap check first: a date with nothing on sale has no showtime links at all.
            if re.search(r"showtimes/\d{6,}", await fetch_text(page, url)):
                found = await listed_shows(page, url)
            for s in found:
                s["date"] = date
            listed += found
            matches = sum(wanted(s, args) for s in found)
            print(f"{date}: {matches} matching shows", file=sys.stderr)
            if matches:
                last_found, empty_run = date, 0
            elif last_found:
                empty_run += 1
                if empty_run >= args.stop_after_empty:
                    break
            day += dt.timedelta(days=1)

        shows, ids = [], set()
        for s in sorted(listed, key=lambda s: (s["date"], minutes(s["time"]), s["theatre"])):
            if wanted(s, args) and s["id"] not in ids:
                ids.add(s["id"])
                shows.append(s)
        details = await seat_maps(page, [s["id"] for s in shows])
        await browser.close()

    for s in shows:
        s["url"] = f"{AMC}/showtimes/{s['id']}"
        if details.get(s["id"]):
            s.update(details[s["id"]])
        else:
            print(f"AMC wouldn't serve the seat map for {s['theatre']} {s['date']} {s['time']} ({s['url']}); left out.")
    return {
        "pulled": dt.datetime.now().isoformat(timespec="minutes"),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "last_found": last_found,
        "checked_to": min(day, args.end).isoformat(),
        "seen": sorted({(s["theatre"], s["movie"], s["format"]) for s in listed}),
        "shows": [s for s in shows if s.get("seats")],
    }


def explain_empty(data, args):
    """Say what AMC does list, so a miss on format or theatre doesn't need another run to understand."""
    print(f"\nNo matching shows with seat maps from {data['start']} to {data['checked_to']}.")
    playing = {}
    for theatre, movie, fmt in data["seen"]:
        if has_words(movie, args.movie):
            playing.setdefault(theatre, set()).add(fmt if args.movie else f"{movie} ({fmt})")
    if not playing:
        print(f"AMC doesn't list {args.movie or 'anything'} on those dates.")
        return
    print(f"Where AMC lists {args.movie} on those dates:" if args.movie else "What AMC lists on those dates:")
    for theatre, what in sorted(playing.items()):
        print(f"  {theatre}: {', '.join(sorted(what))}")
    if args.format and args.theatre not in (None, "all"):
        others = [t for t, fmts in sorted(playing.items()) if not has_words(t, args.theatre) and any(has_words(f, args.format) for f in fmts)]
        if others:
            print(f"{', '.join(others)} {'has' if len(others) == 1 else 'have'} it in {args.format}; --theatre all includes {'it' if len(others) == 1 else 'them'}.")


def seat_map(shows, selected, many_movies, summary, pulled):
    """The seat-map fragment, small enough to paste into an inline chat widget.

    Shows in one auditorium share their geometry, so it is stored once per auditorium
    and each show carries only its open/taken bits as hex (after a leading 1 that keeps zeros).
    """
    layouts, theatres, packed = {}, {}, []
    for s in shows:
        seats = sorted([x[0], x[1], x[2], ACCESSIBLE.get(x[3], 0), x[4]] for x in s["seats"])
        key = json.dumps([x[:4] for x in seats], separators=(",", ":"))
        show = {"id": s["id"], "date": s["date"], "time": s["time"], "l": layouts.setdefault(key, len(layouts)),
                "t": theatres.setdefault(s["theatre"], len(theatres)), "f": s["format"],
                "a": format(int("1" + "".join(str(x[4]) for x in seats), 2), "x")}
        if many_movies:
            show["m"] = s["movie"]
        if s.get("best"):
            show["b"] = ",".join(s["best"]["names"])
        window = film_window(s)
        if window:
            show["w"] = f"film ~{window[0]}–{window[1]}" + (", no trailers" if s["notrailer"] else "")
            show["e"] = window[1]
        packed.append(show)
    data = {"zone": ZONE, "selected": selected, "theatres": list(theatres), "layouts": [json.loads(k) for k in layouts], "shows": packed}
    template = (Path(__file__).parent / "seat-map.html").read_text()
    return (template.replace("__SUMMARY__", html.escape(summary)).replace("__PULLED__", html.escape(pulled))
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def pick_url(s):
    """AMC's seat page reads ?seats=A1,A2 and selects those seats when the user opens it."""
    return f"{s['url']}/seats?seats={','.join(s['best']['names'])}" if s.get("best") else s["url"]


def write_page(path, title, subtitle, fragment):
    template = (Path(__file__).parent / "seat-page.html").read_text()
    page = template.replace("__TITLE__", html.escape(title)).replace("__SUBTITLE__", html.escape(subtitle))
    path.write_text(page.replace("__FRAGMENT__", fragment))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--movie", default="", help="part of the movie name, e.g. 'dune'; omit for every movie")
    ap.add_argument("--theatre", help="AMC slug or words from one, e.g. 'lincoln square', or 'all' for every theatre the movie's page lists "
                    "(needs --movie). Defaults to Lincoln Square, or with --from-json to every theatre in the file")
    ap.add_argument("--market", default="new-york-city")
    ap.add_argument("--format", help="part of the format label, e.g. 'imax 70mm', 'dolby'")
    ap.add_argument("--from", dest="start", type=dt.date.fromisoformat, help="first date, default today")
    ap.add_argument("--to", dest="end", type=dt.date.fromisoformat, help="last date, default 27 days after --from")
    ap.add_argument("--seats", type=int, default=2, help="seats needed side by side")
    ap.add_argument("--stop-after-empty", type=int, default=4, help="stop after this many empty dates in a row once shows were found")
    ap.add_argument("--out", type=Path, help="page to write; default amc-seats.html, or next to --from-json")
    ap.add_argument("--from-json", type=Path, help="rebuild from a saved run's .json instead of asking AMC; the filters above narrow it")
    args = ap.parse_args()

    if args.from_json:
        data = json.loads(args.from_json.read_text())
        start, end = args.start or dt.date.fromisoformat(data["start"]), args.end or dt.date.fromisoformat(data["end"])
        data["shows"] = [s for s in data["shows"] if wanted(s, args) and start.isoformat() <= s["date"] <= end.isoformat()]
        out = args.out or args.from_json.with_suffix(".html")
    else:
        if args.theatre == "all" and not args.movie:
            sys.exit("--theatre all needs --movie: only a movie's page lists every theatre.")
        args.theatre = args.theatre or DEFAULT_THEATRE
        start = args.start = args.start or dt.date.today()
        end = args.end = args.end or start + dt.timedelta(days=27)
        data = asyncio.run(collect(args))
        out = args.out or Path("amc-seats.html")

    now = dt.datetime.now()
    shows = [s for s in data["shows"] if not started(s, now)]
    if len(shows) < len(data["shows"]):
        print(f"Left out {len(data['shows']) - len(shows)} shows that have already started.")
    if not shows:
        explain_empty(data, args)
        return
    theatres = sorted({s["theatre"] for s in shows})
    movies = sorted({s["movie"] for s in shows})
    formats = sorted({s["format"] for s in shows})
    many = lambda xs, noun: " / ".join(xs) if len(xs) < 4 else f"{len(xs)} {noun}"
    what = " · ".join([many(theatres, "theatres"), many(movies, "movies"), many(formats, "formats")])
    name = lambda s: " · ".join([s["format"]] + [s["theatre"]] * (len(theatres) > 1) + [s["movie"]] * (len(movies) > 1))
    pulled = dt.datetime.fromisoformat(data["pulled"]).strftime("%a %b %-d, %-I:%M %p")
    print(f"\n{what} · {start} to {end} · seats as of {pulled}")
    for s in shows:
        s.update(analyze(s["seats"], args.seats))
    if data["last_found"] and data["last_found"] < end.isoformat():
        print(f"Nothing matching on sale after {data['last_found']} (checked through {data['checked_to']}).")

    print(f"\nshow | open | open in center zone | best {args.seats} together (score 0-100) | film | link with them selected")
    for s in shows:
        best = f"{label(s['best']['names'])} ({s['best']['score']})" if s["best"] else "none"
        window = film_window(s)
        film = f"~{window[0]}–{window[1]}" + (" no trailers" if s["notrailer"] else "") if window else "?"
        print(f"{day_label(s['date'])} {s['time']:>7} {name(s)} | {s['open']} | {s['zone_open']} | {best} | {film} | {pick_url(s)}")

    ranked = sorted((s for s in shows if s["best"]), key=lambda s: (-s["best"]["score"], -s["zone_open"]))
    print(f"\nTop {args.seats}-together options:")
    for s in ranked[:5]:
        print(f"  {label(s['best']['names'])} score {s['best']['score']} · {day_label(s['date'])} {s['time']} {name(s)} · {pick_url(s)}")

    top = ranked[0] if ranked else shows[0]
    summary = f"Seat maps for {len(shows)} shows: {what}."
    if top["best"]:
        summary += f" Best {args.seats} together: {label(top['best']['names'])}, {day_label(top['date'])} {top['time']}."
    fragment = seat_map(shows, top["id"], len(movies) > 1, summary, pulled)
    out = out.expanduser().resolve()
    widget = out.with_suffix(".widget.html")
    write_page(out, what, f"{start} to {end} · seats as of {pulled}", fragment)
    widget.write_text(fragment)
    wrote = [str(out), f"{widget} ({len(fragment) // 1000} KB)"]
    if not args.from_json:
        out.with_suffix(".json").write_text(json.dumps(data))
        wrote.append(str(out.with_suffix(".json")))
    print(f"\nWrote {', '.join(wrote)}")
    if len(fragment) > 25_000:
        print("The widget is over 25 KB, too big to paste reliably. Narrow it with --from-json and --from/--to, --theatre or --format.")


if __name__ == "__main__":
    main()
