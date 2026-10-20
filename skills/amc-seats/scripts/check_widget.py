#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright==1.62.0"]
# ///
"""Check a seat-map page in headless WebKit and save a screenshot of it.

    uv run check_widget.py <page.html> <screenshot.png>

It taps seats the way a person would and checks the AMC link follows: the best pair starts selected,
open seats toggle in and out, taken seats can't be picked, wheelchair and companion seats can be and
carry their icons, and switching shows switches the map. Exits 1 on any failure.
"""
import asyncio
import sys
from pathlib import Path
from urllib.parse import unquote

from playwright.async_api import async_playwright


async def main(page_path, shot):
    fails = []

    def check(ok, what):
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            fails.append(what)

    async with async_playwright() as p:
        browser = await p.webkit.launch()
        page = await browser.new_page(viewport={"width": 720, "height": 1400})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(Path(page_path).resolve().as_uri())
        link = page.locator("#am-link")
        href = lambda: link.get_attribute("href")
        seat = lambda sel: page.locator(f"#am-big rect[data-s]{sel}").first

        picked = await page.eval_on_selector_all("#am-big rect.pk", "rs => rs.map(r => r.dataset.s)")
        start = unquote(await href())
        check(bool(picked) and all(n in start for n in picked), f"best pair {picked} starts selected and is in the link")
        heading = await page.locator("#am-detail .cap").first.text_content()
        check("AMC " in heading and " open" in heading, f"heading names the theatre and format: {heading}")

        name = await seat('[data-o="1"][data-a="0"]:not(.pk)').get_attribute("data-s")
        await seat(f'[data-s="{name}"]').click()
        check(f"{name}" in unquote(await href()), f"tapping open seat {name} adds it to the link")
        await seat(f'[data-s="{name}"]').click()
        check(f"{name}," not in unquote(await href()) and not unquote(await href()).endswith(name), f"tapping {name} again removes it")

        before = await href()
        if await page.locator('#am-big rect[data-o="0"]').count():
            await seat('[data-o="0"]').click()
            check(await href() == before, "taken seats can't be picked")

        accessible = await page.locator('#am-big rect[data-s]:not([data-a="0"])').count()
        icons = await page.locator("#am-big use").count()
        check(icons == accessible, f"{accessible} wheelchair and companion seats carry {icons} icons")
        if await page.locator('#am-big rect[data-o="1"]:not([data-a="0"])').count():
            acc = await seat('[data-o="1"]:not([data-a="0"])').get_attribute("data-s")
            await seat(f'[data-s="{acc}"]').click()
            check(acc in unquote(await href()), f"wheelchair or companion seat {acc} can be picked")

        cards = page.locator(".mm")
        if await cards.count() > 1:
            before = await href()
            other = cards.nth(0) if await cards.nth(0).get_attribute("class") != "mm on" else cards.nth(1)
            await other.click()
            check(await href() != before and "on" in await other.get_attribute("class"), "tapping another show switches the map and link")

        await page.screenshot(path=shot, full_page=True)
        check(not errors, f"no script errors {errors or ''}")
        await browser.close()
    print(f"Screenshot: {shot}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]))
