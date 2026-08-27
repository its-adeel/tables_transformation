#!/usr/bin/env python3
"""
crawl_hollywood_ldr.py

Automates the "browse the site to find each new table" step for the
City of Hollywood, FL Zoning and Land Development Regulations, hosted
on American Legal Publishing's Code Library.

HOW IT WORKS
------------
Every page in this code has a "Next Doc" link that walks through the
whole document tree in reading order (same order you'd get by clicking
through the left-hand nav one section at a time). This script starts
at the Zoning & LDR root page and follows "Next Doc" links until it
either leaves that document (walks into a different code entirely) or
runs out of pages, saving the raw HTML of every table it finds along
the way — the same raw <table>...</table> markup you've been pasting
into chat by hand.

It does NOT touch table_to_yaml_converter.py or your cases/ folder —
it only automates the "find and fetch" step. Converting each new table
and deciding whether to accept its golden output stays a manual review
step, exactly like every table so far in this project.

INCREMENTAL / SAFE TO RE-RUN
-----------------------------
Every visited URL and every table's content hash is recorded in
crawl_state.json. Re-running the script later (e.g. after the city
publishes a new ordinance supplement) skips everything already seen
and only saves genuinely NEW tables — matching your current workflow
of periodically checking the site for new material.

SETUP
-----
    pip install requests beautifulsoup4

USAGE
-----
    python crawl_hollywood_ldr.py                  # crawl from the start
    python crawl_hollywood_ldr.py --max-pages 20    # sanity-check a small run first
    python crawl_hollywood_ldr.py --reset           # ignore prior state, re-crawl everything

OUTPUT
------
New tables are saved to scraped_tables/table_NNNN_html.txt (raw HTML,
one file per table) — review and hand-pick the ones you want into your
cases/ folder the same way as always.

IMPORTANT — UNTESTED AGAINST THE LIVE SITE
-------------------------------------------
This was written by inspecting the real page structure (fetched twice
during development) but could not be run end-to-end against the live
site from the environment that wrote it. Please do a small supervised
run first (--max-pages 5 or so), check the output, and only then let
it run further. If the site's markup has changed since, the two
things most likely to need adjusting are find_next_doc_url() and the
class regex in extract_tables().
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://codelibrary.amlegal.com"
START_URL = f"{BASE}/codes/hollywood/latest/hollywoodldr_fl/0-0-0-5468"
# Stay within this document's own URL space — the site hosts many other
# documents (the general Code of Ordinances, other cities, etc.) under
# sibling paths, and "Next Doc" could in principle walk past the end of
# this one into whatever comes next in the library.
DOC_PREFIX = "/codes/hollywood/latest/hollywoodldr_fl/"

OUTPUT_DIR = "scraped_tables"
STATE_FILE = "crawl_state.json"
REQUEST_DELAY_SECONDS = 1.0  # be polite to the site
MAX_PAGES_DEFAULT = 5000     # safety valve against an unexpected crawl loop

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; zoning-table-crawler/1.0; "
        "personal research use)"
    )
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"visited_urls": [], "table_hashes": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_next_doc_url(soup):
    """Find the 'Next Doc' link's href, resolved to an absolute URL."""
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() == "next doc":
            href = a["href"]
            return href if href.startswith("http") else BASE + href
    return None


def extract_tables(soup):
    """Every table on the page matching this site's zoning-table markup."""
    return soup.find_all("table", class_=re.compile(r"makeExpandableTable|makeFixedTable"))


def existing_table_count():
    if not os.path.isdir(OUTPUT_DIR):
        return 0
    return len([f for f in os.listdir(OUTPUT_DIR) if f.endswith("_html.txt")])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-pages", type=int, default=MAX_PAGES_DEFAULT,
        help=f"Stop after fetching this many NEW pages (default {MAX_PAGES_DEFAULT})",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Ignore crawl_state.json and re-crawl from the start",
    )
    args = parser.parse_args()

    state = {"visited_urls": [], "table_hashes": []} if args.reset else load_state()
    visited = set(state["visited_urls"])
    seen_hashes = set(state["table_hashes"])

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    counter = existing_table_count()

    url = START_URL
    pages_fetched = 0
    new_tables = 0

    while url and pages_fetched < args.max_pages:
        if url in visited:
            print(f"Already visited (loop or caught up with a prior run) — stopping: {url}")
            break
        if DOC_PREFIX not in url:
            print(f"Left the Zoning & LDR document — stopping: {url}")
            break

        print(f"Fetching: {url}")
        try:
            html = fetch(url)
        except requests.RequestException as e:
            print(f"  Failed to fetch {url}: {e}", file=sys.stderr)
            break

        soup = BeautifulSoup(html, "html.parser")
        visited.add(url)
        pages_fetched += 1

        for table in extract_tables(soup):
            table_html = str(table)
            h = hashlib.sha256(table_html.encode("utf-8")).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            counter += 1
            out_path = os.path.join(OUTPUT_DIR, f"table_{counter:04d}_html.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(table_html)
            print(f"  Saved new table -> {out_path}")
            new_tables += 1

        next_url = find_next_doc_url(soup)

        # Save progress after every page, not just at the end, so an
        # interrupted run (Ctrl-C, network hiccup) doesn't lose work
        # already done.
        state["visited_urls"] = sorted(visited)
        state["table_hashes"] = sorted(seen_hashes)
        save_state(state)

        url = next_url
        if url:
            time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nDone. Fetched {pages_fetched} new page(s), saved {new_tables} new table(s).")
    print(f"Tables saved under: {OUTPUT_DIR}/")
    if url and pages_fetched >= args.max_pages:
        print(f"(Stopped at --max-pages={args.max_pages}; re-run to continue from {url})")


if __name__ == "__main__":
    main()