"""
Ottawa Pool Schedule Scraper
Fetches Public Swim schedules from Ottawa.ca facility pages using Playwright.
Outputs: schedule_data.json
"""

import json
import re
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Pool definitions ────────────────────────────────────────────────────────
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

# Only scrape rows whose label contains these strings (case-insensitive)
PUBLIC_SWIM_KEYWORDS = ["public swim", "wave swim"]


def parse_time_str(s):
    """Convert '9:30 am' or '9 am' or '9:30am' → minutes since midnight."""
    s = s.strip().lower().replace("\u2013", "-").replace("\u00a0", " ")
    # Handle ranges like "9:30 - 11 am" — we only want individual endpoints
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
    Parse one table cell that may contain multiple time ranges separated by
    commas or newlines. Returns list of {start, end} dicts (minutes).
    e.g. "Noon - 1 pm,\n4:30 - 9 pm" → [{start:720,end:780},{start:990,end:1260}]
    """
    text = cell_text.strip()
    if not text or text.lower() in ("n/a", "—", "-", ""):
        return []

    # Normalise "Noon" → "12:00 pm", "Midnight" → "12:00 am"
    text = re.sub(r"\bnoon\b", "12:00 pm", text, flags=re.I)
    text = re.sub(r"\bmidnight\b", "12:00 am", text, flags=re.I)

    # Split on comma or newline to get individual ranges
    parts = re.split(r"[,\n]+", text)
    results = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Match "X - Y am/pm" patterns. The am/pm on the end applies to both
        # unless the first time already has one.
        range_m = re.match(
            r"(\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)\s*[-–]\s*(\d{1,2}(?::\d{2})?\s*[ap]m)",
            part,
            re.I,
        )
        if not range_m:
            continue

        start_str, end_str = range_m.group(1), range_m.group(2)

        # If start has no am/pm, inherit from end
        if not re.search(r"[ap]m", start_str, re.I):
            ap_match = re.search(r"([ap]m)", end_str, re.I)
            if ap_match:
                start_str += " " + ap_match.group(1)

        start = parse_time_str(start_str)
        end = parse_time_str(end_str)
        if start is not None and end is not None:
            results.append({"start": start, "end": end})

    return results


def scrape_pool(page, pool):
    """
    Navigate to a pool's Ottawa.ca page and extract Public Swim schedule.
    Returns list of sessions per day: {day(0-6), label, start, end, playFree}
    """
    print(f"  Scraping: {pool['name']} ...", flush=True)
    sessions = []

    try:
        page.goto(pool["url"], wait_until="networkidle", timeout=30000)
    except PWTimeout:
        print(f"    ⚠ Timeout loading page, trying domcontentloaded...")
        try:
            page.goto(pool["url"], wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            print(f"    ✗ Failed to load page: {e}")
            return sessions

    # Wait for tables to appear
    try:
        page.wait_for_selector("table", timeout=10000)
    except PWTimeout:
        print(f"    ✗ No tables found on page")
        return sessions

    tables = page.query_selector_all("table")
    print(f"    Found {len(tables)} table(s)")

    for table in tables:
        table_text = table.inner_text()

        # Only process tables that contain any swim-related content
        if not any(k in table_text.lower() for k in ["swim", "aquafit", "lane"]):
            continue

        # Get header row to find day columns
        header_cells = table.query_selector_all("thead th, tr:first-child th, tr:first-child td")
        if not header_cells:
            continue

        header_texts = [c.inner_text().strip() for c in header_cells]

        # Build mapping: column index → day-of-week integer
        col_to_day = {}
        for i, h in enumerate(header_texts):
            for day_name, day_idx in DAY_INDEX.items():
                if day_name.lower() in h.lower():
                    col_to_day[i] = day_idx
                    break

        if not col_to_day:
            print(f"    ⚠ Could not find day columns in table")
            continue

        # Process data rows
        rows = table.query_selector_all("tbody tr")
        for row in rows:
            cells = row.query_selector_all("td, th")
            if not cells:
                continue

            row_label = cells[0].inner_text().strip()

            # Only include Public Swim rows (and Wave Swim for wave pools)
            is_public = any(k in row_label.lower() for k in PUBLIC_SWIM_KEYWORDS)
            if not is_public:
                continue

            # For each day column, parse the time cell
            for col_idx, day_idx in col_to_day.items():
                if col_idx >= len(cells):
                    continue
                cell_text = cells[col_idx].inner_text().strip()

                # Check for Play Free marker
                play_free = "__play free__" in cell_text.lower() or "play free" in cell_text.lower()
                # Clean the play free annotation before parsing times
                cell_clean = re.sub(r"__.*?__|\(play free\)", "", cell_text, flags=re.I).strip()

                time_ranges = parse_time_range(cell_clean)
                for tr in time_ranges:
                    sessions.append({
                        "day": day_idx,
                        "label": row_label,
                        "start": tr["start"],
                        "end": tr["end"],
                        "playFree": play_free,
                    })

    print(f"    ✓ Found {len(sessions)} public swim session slots")
    return sessions


def main():
    print("=== Ottawa Pool Schedule Scraper ===")
    print(f"Started: {datetime.utcnow().isoformat()}Z\n")

    output = {
        "scraped_at": datetime.utcnow().isoformat() + "Z",
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

        # Suppress noisy console messages from the page
        page.on("console", lambda msg: None)

        for pool in POOLS:
            sessions = scrape_pool(page, pool)
            output["pools"].append({
                **{k: v for k, v in pool.items() if k != "url"},
                "url": pool["url"],
                "sessions": sessions,
            })

        browser.close()

    # Write output
    out_path = "schedule_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Wrote {out_path}")

    # Quick summary
    total = sum(len(p["sessions"]) for p in output["pools"])
    if total == 0:
        print("\n⚠ WARNING: No sessions were scraped. Ottawa.ca may have blocked the scraper")
        print("  or changed its page structure. Check the output carefully.")
        sys.exit(1)
    else:
        print(f"  Total session slots scraped: {total}")
        for p in output["pools"]:
            print(f"  • {p['name']}: {len(p['sessions'])} slots")


if __name__ == "__main__":
    main()
