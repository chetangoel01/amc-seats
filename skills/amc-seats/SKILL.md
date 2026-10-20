---
name: amc-seats
description: Find AMC showtimes for a movie and show every seat at every show as seat maps, with the best open seats ranked. Use whenever the user asks for movie tickets, showtimes or seats at an AMC theatre (Lincoln Square, Empire 25, 34th Street, Kips Bay and the rest), mentions IMAX, IMAX 70mm, Dolby or another premium format, asks for good, great, center or side-by-side seats, wants to see a seat map, or asks whether a show still has seats, even if they never say "AMC" or "seat map". It reads only; it never selects, holds or buys seats.
---

# AMC seats

One script does the whole job: it finds a movie's showtimes at one AMC theatre, or at every nearby one, over a date range. It reads every show's seat map, ranks the best open seats, and writes a self-contained seat-map page.

## Run it

If the user hasn't named a date or range, ask first (with `AskUserQuestion` where available: "Which dates?": Today, This weekend, Next 7 days, Next 4 weeks; "Other" covers specific dates). Turn the answer into `--from` and `--to`. A narrower range also means fewer requests to AMC.

The scripts live in this skill's `scripts/` folder. Run them with `uv`, which installs their one dependency:

```bash
uv run <this skill's directory>/scripts/find_seats.py --movie dune --theatre "lincoln square" --format "imax 70mm" --from 2027-01-08 --out <temp dir>/dune-lincoln-square.html
```

When the user names a movie and a format but no theatre ("Digger in Dolby"), use `--theatre all`. It costs the same number of requests as one theatre, because each date comes from the movie's page, which lists every nearby AMC.

The defaults are New York's: market `new-york-city`, theatre AMC Lincoln Square 13. For another city, pass `--market` and a `--theatre` there.

| Flag | Meaning |
|---|---|
| `--movie` | Part of the title. Omit it for every movie (useful with `--format`, e.g. "what's in Dolby tonight"). |
| `--theatre` | A slug (`amc-lincoln-square-13`), words from one ("lincoln square", "empire"), or `all` for every theatre the movie's page lists (needs `--movie`). Defaults to Lincoln Square. |
| `--market` | AMC's market slug from the theatre URL (`amctheatres.com/movie-theatres/<market>/<theatre>`); defaults to `new-york-city`. |
| `--format` | Part of AMC's format label: "imax 70mm", "imax", "dolby", "laser", "prime". |
| `--from`, `--to` | `YYYY-MM-DD`. Defaults to today through 27 days out. It stops once 4 dates in a row after the last match are empty. |
| `--seats` | Seats needed side by side (default 2). |
| `--out` | Where to write the page. Two files go next to it: `.widget.html`, the same seat map as a fragment for the chat, and `.json`, the saved run. Put them in a temporary or scratch directory. |
| `--from-json` | Rebuild from a saved `.json` without asking AMC. `--movie`, `--theatre`, `--format`, `--from`, `--to` and `--seats` narrow or re-rank it. Use it for any follow-up on the same run ("just the weekend", "3 seats", "only Empire"), so AMC isn't hit twice. |

A run takes 30–60 seconds for about 30 shows. **Run it once per request.** AMC sits behind Cloudflare, which answers HTTP 429 after a few back-to-back runs. The script backs off, and if a date page stays blocked it exits and says so. When that happens, tell the user and wait a few minutes rather than retrying or working around it. Never complete a CAPTCHA or Turnstile check.

When nothing matches, the script prints where AMC lists the movie and in which formats, and names any other theatre that has the requested format. Answer from that; don't rerun to find out. When it says another theatre has the format, offer a `--theatre all` run. Never treat a blocked run as "no shows". The script also drops shows whose film has already started. Repeated hits to the same page can also get one show's seat map refused while the rest load (another reason to run once). The script then leaves that show out and prints "AMC wouldn't serve the seat map for …". Name that show in your answer, with its link, so the user can check it on AMC themselves.

## Show the result

1. If the script says the widget is over 25 KB, narrow it with `--from-json` first. Where an inline widget tool is available (the visualize tool's `show_widget` in the Claude apps; load it with ToolSearch and call its `read_me` once per session first), show the map inline in the chat. `cat` the `.widget.html` file and pass it as `widget_code` exactly as written, with nothing added or reworded. Each show's seats are packed as hex, so a single wrong character moves seats around. Without such a tool, give the user the path to the `.html` page (or send the file, if you can) to open in a browser.

   The map opens with "Seats as of" the pull time. Every show gets a mini seat map labelled with its time, theatre, format, open count and estimated end. Below them is a full map of the best show, with the screen at the top, row letters on both sides, seat numbers on hover or tap, wheelchair and companion seats marked with their icons, and the center zone dashed. Its heading gives the estimated film window: AMC starts the film 25–30 minutes after the listed time, or on time for a "No Trailers" show, and the window runs from the earliest start to the latest end. Clicking a mini map switches the full map to that show. On the full map that show's best pair starts out selected in blue. Tapping open seats, wheelchair and companion seats included, adds or removes them, and the "Open on AMC with …" link opens AMC's seat page with exactly those seats selected. You can't see what the widget draws, so check your paste: the best show's mini map should have the same open count as the script's printout, and a show with every seat open should be solid green.
2. Answer from the script's printout in a few lines. Lead with whether great seats exist, then give the best options with their AMC links. The printed links already carry each show's best pair, so they open AMC's seat page with those seats selected. Pass on "Nothing matching on sale after X": it means AMC hasn't released later dates yet, not that the run ends there.

How to read the numbers:
- **Center zone**: 40–95% of the way back and the middle 45% of the width. "0 in center zone" means the good seats are gone, so call any pick outside it a compromise.
- **Score (0–100)**: being centered matters most, and about two-thirds of the way back is ideal. Front rows score near zero even when dead center.
- Wheelchair and companion seats are left out of the counts and the best pairs, but the user can pick them on the map.
- On a huge screen, close rows are hard work even when centered. Lincoln Square's IMAX has 12 rows (A–M) and row D is the 4th from the screen.

## Boundaries

- Read only: never select, hold or buy seats, and never sign in to AMC. Give the AMC link and let the user buy. A link ending in `?seats=H9,H10` selects those seats when it loads, so never open one yourself; it's for the user.
- Sold-out shows that AMC no longer links won't appear.
- When the dates the user wants aren't on sale yet, point them to [drop70mm](https://drop70mm.com)'s "Notify me" for IMAX 70mm, or offer a re-run later (scheduled, if you can schedule tasks).
- AMC only. For Regal, Alamo or Angelika, work in a browser by hand.

## When it breaks

- `Executable doesn't exist ... webkit-2336`: Playwright's WebKit isn't installed, or the browser cache was purged. Run `uv run --with playwright==1.62.0 playwright install webkit`.
- "never rendered its showtime list", or every seat map missing: AMC changed its pages. Inspect one in a browser and fix the selector or parser in `scripts/find_seats.py`. The script relies on three things:
  - Showtime sections labelled `"<Theatre>, <Movie> Showtimes by Features and Accesibility"` (AMC's misspelling), each holding `li[aria-label="<FORMAT> Showtimes"]` with `a[href="/showtimes/<id>"]` links.
  - The `seatingLayout` JSON inside the `self.__next_f.push(...)` payload of `/showtimes/<id>/seats`. The same payload carries the movie's `runTime` and the showtime's own `attributes` (`notrailer`), which give the film window.
  - The movie switcher on a theatre page, `\"value\":\"<movie-slug>-<id>\"`, which turns `--movie` into the movie page's slug.
- After changing `seat-map.html` or the widget code, run `uv run <this skill's directory>/scripts/check_widget.py <page.html> <temp dir>/check.png` on a built page. It taps seats in headless WebKit, checks the link and icons, saves a screenshot, and exits 1 on any failure.
- `--theatre all` misses a theatre, or the date pages time out: the movie page renders its own theatre first and the nearby ones after hydration. `listed_shows` waits until the section count stops changing; give it longer if AMC got slower.
- Links open AMC with no seats selected: the seat page stopped reading `?seats=`. As of Oct 20, 2026 its JS calls `useSearchParams().get("seats")`, splits the value on commas, and selects each named seat that's still open. Search the page's `/_next/` scripts for `"seats"` to find the new parameter, without loading a link that selects seats.
- Don't fall back to `graph.amctheatres.com`. In-page calls to it have failed since Sep 22, 2026.
- `curl` and `requests` get a Cloudflare 403. A real browser session from a home connection works; datacenter IPs (cloud VMs, VPSes) are blocked.
