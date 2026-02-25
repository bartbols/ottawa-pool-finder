"""
Ottawa Pool Schedule Scraper
Handles both table-based and div-based schedule layouts on Ottawa.ca.
Saves debug HTML files as GitHub Actions artifacts for diagnosis.
"""

import json
import re
import sys
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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
    text = cell_text.strip()
    if not text or text.lower() in ("n/a", "—", "-", ""):
        return []
    text = re.sub(r"\bnoon\b", "12:00 pm", text, flags=re.I)
    text = re.sub(r"\bmidnight\b", "12:00 am", text, flags=re.I)
    results = []
    for part in re.split(r"[,\n]+", text):
        part = part.strip()
        if not part:
            continue
        range_m = re.match(
            r"(\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)\s*[-\u2013]\s*(\d{1,2}(?::\d{2})?\s*[ap]m)",
            part, re.I,
        )
        if not range_m:
            continue
        start_str, end_str = range_m.group(1), range_m.group(2)
        if not re.search(r"[ap]m", start_str, re.I):
            ap_match = re.search(r"([ap]m)", end_str, re.I)
            if ap_match:
                start_str += " " + ap_match.group(1)
        start = parse_time_str(start_str)
        end = parse_time_str(end_str)
        if start is not None and end is not None:
            results.append({"start": start, "end": end})
    return results


def parse_from_table(table):
    sessions = []
    header_cells = table.query_selector_all(
        "thead th, thead td, tr:first-child th, tr:first-child td"
    )
    if not header_cells:
        return sessions
    header_texts = [c.inner_text().strip() for c in header_cells]
    col_to_day = {}
    for i, h in enumerate(header_texts):
        for day_name, day_idx in DAY_INDEX.items():
            if day_name.lower() in h.lower():
                col_to_day[i] = day_idx
                break
    if not col_to_day:
        return sessions
    for row in table.query_selector_all("tbody tr, tr:not(:first-child)"):
        cells = row.query_selector_all("td, th")
        if not cells:
            continue
        row_label = cells[0].inner_text().strip()
        if not any(k in row_label.lower() for k in PUBLIC_SWIM_KEYWORDS):
            continue
        for col_idx, day_idx in col_to_day.items():
            if col_idx >= len(cells):
                continue
            cell_text = cells[col_idx].inner_text().strip()
            play_free = "play free" in cell_text.lower()
            cell_clean = re.sub(r"__.*?__|\(play free\)", "", cell_text, flags=re.I).strip()
            for tr in parse_time_range(cell_clean):
                sessions.append({
                    "day": day_idx, "label": row_label,
                    "start": tr["start"], "end": tr["end"], "playFree": play_free,
                })
    return sessions


def parse_from_text(raw_text):
    """
    Parse schedule from plain text extracted from div-based layouts.
    Handles Ottawa.ca's whitespace-separated column format.
    """
    sessions = []
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

    # Find the header row containing day names
    header_idx = None
    col_days = []  # ordered list of (day_name, day_index)
    for i, line in enumerate(lines):
        found = []
        for day in DAYS:
            if day.lower() in line.lower():
                found.append((day, DAY_INDEX[day]))
        if len(found) >= 3:
            header_idx = i
            col_days = found
            print(f"      Header row [{i}]: {line[:100]}")
            break

    if header_idx is None:
        print("      Could not locate day-header row in text.")
        return sessions

    # Each subsequent line is a schedule row; columns separated by 2+ spaces or tabs
    for line in lines[header_idx + 1:]:
        if not any(k in line.lower() for k in PUBLIC_SWIM_KEYWORDS):
            continue
        print(f"      Public Swim row: {line[:120]}")
        parts = re.split(r"\t+|\s{2,}", line)
        if len(parts) < 2:
            continue
        row_label = parts[0].strip()
        play_free = "play free" in line.lower()
        for j, (day_name, day_idx) in enumerate(col_days):
            if j + 1 < len(parts):
                cell = re.sub(r"__.*?__|\(play free\)", "", parts[j + 1], flags=re.I).strip()
                for tr in parse_time_range(cell):
                    sessions.append({
                        "day": day_idx, "label": row_label,
                        "start": tr["start"], "end": tr["end"], "playFree": play_free,
                    })
    return sessions


def scrape_pool(page, pool):
    print(f"\n  [{pool['id']}] Scraping: {pool['name']}", flush=True)
    sessions = []

    # ── Load page ────────────────────────────────────────────────────────────
    loaded = False
    for wait_mode in ("networkidle", "domcontentloaded"):
        try:
            resp = page.goto(pool["url"], wait_until=wait_mode, timeout=30000)
            print(f"    HTTP {resp.status} via {wait_mode}")
            loaded = True
            break
        except PWTimeout:
            print(f"    Timeout ({wait_mode}), retrying...")
        except Exception as e:
            print(f"    Load error: {e}")
            break

    if not loaded:
        print("    ✗ Could not load page.")
        return sessions

    # Extra wait for JS rendering
    page.wait_for_timeout(4000)

    # ── Save debug HTML as artifact ──────────────────────────────────────────
    html = page.content()
    debug_path = f"debug_{pool['id']}.html"
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"    Saved {debug_path} ({len(html):,} bytes)")

    # Quick sanity check
    if "Public Swim" in html:
        idx = html.find("Public Swim")
        print(f"    ✓ 'Public Swim' found in HTML at pos {idx}")
        print(f"      Context: {html[max(0,idx-80):idx+160].strip()!r}")
    else:
        print("    ✗ 'Public Swim' NOT in HTML — Ottawa.ca may be blocking or structure changed")
        print(f"    Page title: {page.title()!r}")
        return sessions

    # ── Strategy 1: standard <table> ─────────────────────────────────────────
    for table in page.query_selector_all("table"):
        txt = table.inner_text()
        if any(k in txt.lower() for k in ["swim", "lane", "aquafit"]):
            s = parse_from_table(table)
            if s:
                print(f"    ✓ {len(s)} sessions from <table>")
                sessions.extend(s)

    if sessions:
        return sessions

    # ── Strategy 2: Ottawa.ca field/view divs ───────────────────────────────
    print("    No table sessions — trying div containers...")
    candidates = []
    for sel in [
        "[class*='schedule']", "[class*='field--name']",
        "[class*='view-content']", "[class*='activity']",
        "article", "main", ".field--type-text-long",
    ]:
        try:
            for el in page.query_selector_all(sel):
                txt = el.inner_text()
                if any(k in txt.lower() for k in PUBLIC_SWIM_KEYWORDS):
                    candidates.append((sel, el, len(txt)))
        except Exception:
            pass

    # Sort by text length — prefer smallest container that still has the content
    candidates.sort(key=lambda x: x[2])
    for sel, el, length in candidates[:3]:
        print(f"      Candidate: {sel} ({length} chars)")
        text = el.inner_text()
        s = parse_from_text(text)
        if s:
            print(f"    ✓ {len(s)} sessions from div ({sel})")
            sessions.extend(s)
            break

    if not sessions:
        # ── Strategy 3: dump full page text for manual inspection ────────────
        print("    Dumping full page innerText for inspection...")
        full_text = page.evaluate("() => document.body.innerText")
        text_path = f"debug_{pool['id']}.txt"
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(full_text)
        print(f"    Saved {text_path} — check artifact for page structure")

        # Still try parsing the full text
        s = parse_from_text(full_text)
        if s:
            print(f"    ✓ {len(s)} sessions from full-page text")
            sessions.extend(s)

    print(f"    → {len(sessions)} total sessions")
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
        print("\n⚠ WARNING: No sessions scraped.")
        print("  Upload the debug_*.html / debug_*.txt artifacts to diagnose the page structure.")
        sys.exit(1)
    else:
        print(f"\n✓ Done — {total} total sessions scraped.")


if __name__ == "__main__":
    main()
