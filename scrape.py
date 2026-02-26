"""
Ottawa Pool Schedule Scraper
Uses Playwright to load pages and BeautifulSoup to parse the schedule tables.
Ottawa.ca tables use <th> for both header row AND row label column,
so we handle that explicitly.
"""

import json
import re
import sys
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

POOLS = [
    {
        "id": "splash",
        "name": "Splash Wave Pool",
        "area": "East · Gloucester",
        "address": "2720 Queensview Dr",
        "wave": True,
        "url": "https://ottawa.ca/en/recreation-and-parks/facilities/place-listing/splash-wave-pool",
    },
    {
        "id": "rayfriel",
        "name": "Ray Friel Recreation Complex",
        "area": "East · Orléans",
        "address": "1585 Tenth Line Rd",
        "wave": True,
        "url": "https://ottawa.ca/en/recreation-and-parks/facilities/place-listing/ray-friel-recreation-complex",
    },
    {
        "id": "dupuis",
        "name": "François Dupuis Recreation Centre",
        "area": "East · Orléans",
        "address": "2263 St-Laurent Blvd",
        "wave": False,
        "url": "https://ottawa.ca/en/recreation-and-parks/facilities/place-listing/francois-dupuis-recreation-centre",
    },
]

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}
PUBLIC_SWIM_KEYWORDS = ["public swim", "wave swim"]


def parse_time_str(s):
    """'9:30 am' or '9 am' → minutes since midnight, or None."""
    s = s.strip().lower().replace("\u2013", "-").replace("\u00a0", " ")
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", s)
    if not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + mn


def parse_time_range(cell_text):
    """
    Parse a table cell that may contain multiple time ranges.
    e.g. 'Noon - 1 pm, 4:30 - 9 pm' → [{start:720,end:780},{start:990,end:1260}]
    """
    text = cell_text.strip()
    if not text or text.lower() in ("n/a", "—", "-", ""):
        return []

    # Normalise special words
    text = re.sub(r"\bnoon\b", "12:00 pm", text, flags=re.I)
    text = re.sub(r"\bmidnight\b", "12:00 am", text, flags=re.I)
    # Remove Play Free links/annotations
    text = re.sub(r"\(play free\)", "", text, flags=re.I)
    text = re.sub(r"__.*?__", "", text)

    results = []
    for part in re.split(r"[,\n]+", text):
        part = part.strip()
        if not part:
            continue
        # Match "X - Y am/pm" — end token must have am/pm
        range_m = re.match(
            r"(\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)\s*[-\u2013]\s*(\d{1,2}(?::\d{2})?\s*[ap]m)",
            part, re.I,
        )
        if not range_m:
            continue
        start_str, end_str = range_m.group(1).strip(), range_m.group(2).strip()
        # If start has no am/pm, inherit from end
        if not re.search(r"[ap]m", start_str, re.I):
            ap_m = re.search(r"([ap]m)", end_str, re.I)
            if ap_m:
                start_str += " " + ap_m.group(1)
        s = parse_time_str(start_str)
        e = parse_time_str(end_str)
        if s is not None and e is not None:
            results.append({"start": s, "end": e})
    return results


def parse_schedule_tables(html):
    """
    Parse all swim schedule tables from the page HTML.
    Ottawa.ca table structure:
      - First <tr> in <thead> (or first <tr>) = header with day names in <th>
      - Each data row has a <th> for the activity label + <td> for each day
    Returns list of session dicts.
    """
    soup = BeautifulSoup(html, "html.parser")
    sessions = []

    for table in soup.find_all("table"):
        # Only process swim/aquafit tables
        table_text = table.get_text()
        if not any(k in table_text.lower() for k in ["swim", "aquafit", "lane"]):
            continue

        caption = table.find("caption")
        print(f"    Table: {caption.get_text(strip=True)[:80] if caption else 'no caption'}")

        # Build column → day mapping from header row
        rows = table.find_all("tr")
        if not rows:
            continue

        header_row = rows[0]
        header_cells = header_row.find_all(["th", "td"])
        col_to_day = {}
        for i, cell in enumerate(header_cells):
            text = cell.get_text(strip=True)
            for day_name, day_idx in DAY_INDEX.items():
                if day_name.lower() in text.lower():
                    col_to_day[i] = day_idx
                    break

        if not col_to_day:
            print(f"      No day columns found in header, skipping")
            continue

        print(f"      Days mapped: { {DAYS[v]: k for k, v in col_to_day.items()} }")

        # Process data rows (skip header row)
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue

            row_label = cells[0].get_text(separator=" ", strip=True)

            # Only include Public Swim and Wave Swim rows
            if not any(k in row_label.lower() for k in PUBLIC_SWIM_KEYWORDS):
                continue

            print(f"      Row: {row_label}")

            for col_idx, day_idx in col_to_day.items():
                if col_idx >= len(cells):
                    continue

                cell = cells[col_idx]
                cell_text = cell.get_text(separator=" ", strip=True)

                # Check for Play Free (may be a link inside the cell)
                play_free = "play free" in cell_text.lower()

                for tr in parse_time_range(cell_text):
                    sessions.append({
                        "day": day_idx,
                        "label": row_label,
                        "start": tr["start"],
                        "end": tr["end"],
                        "playFree": play_free,
                    })

    return sessions


def scrape_pool(page, pool):
    print(f"\n  [{pool['id']}] {pool['name']}", flush=True)

    # Load page
    loaded = False
    for wait_mode in ("networkidle", "domcontentloaded"):
        try:
            resp = page.goto(pool["url"], wait_until=wait_mode, timeout=30000)
            print(f"    HTTP {resp.status} ({wait_mode})")
            loaded = True
            break
        except PWTimeout:
            print(f"    Timeout with {wait_mode}")
        except Exception as e:
            print(f"    Error: {e}")
            break

    if not loaded:
        return []

    # Brief wait for any late JS rendering
    page.wait_for_timeout(3000)

    html = page.content()

    # Save debug HTML as artifact
    with open(f"debug_{pool['id']}.html", "w", encoding="utf-8") as f:
        f.write(html)

    if "Public Swim" not in html and "public swim" not in html.lower():
        print(f"    ✗ 'Public Swim' not found in page HTML")
        return []

    sessions = parse_schedule_tables(html)
    print(f"    → {len(sessions)} public swim sessions found")
    return sessions


def main():
    print("=== Ottawa Pool Schedule Scraper ===")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}\n")

    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "pools": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-CA",
        )
        page = context.new_page()
        page.on("console", lambda _: None)

        for pool in POOLS:
            sessions = scrape_pool(page, pool)
            output["pools"].append({**pool, "sessions": sessions})

        browser.close()

    with open("schedule_data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Wrote schedule_data.json")

    total = sum(len(p["sessions"]) for p in output["pools"])
    for p in output["pools"]:
        print(f"  • {p['name']}: {len(p['sessions'])} sessions")

    if total == 0:
        print("\n⚠ WARNING: No sessions scraped. Check debug_*.html artifacts.")
        sys.exit(1)
    else:
        print(f"\n✓ Done — {total} total session slots scraped.")


if __name__ == "__main__":
    main()
