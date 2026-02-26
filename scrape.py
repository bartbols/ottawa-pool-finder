"""
Ottawa Pool Schedule Scraper
Step 1: Auto-discovers all indoor pool locations from Ottawa.ca's index page.
Step 2: Scrapes the Public Swim / Wave Swim schedule from each pool's page.
Uses Playwright (headless browser) + BeautifulSoup (HTML parsing).
"""

import json
import re
import sys
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

# Index page listing all indoor pool drop-in locations
POOL_INDEX_URL = (
    "https://ottawa.ca/en/recreation-and-parks/swimming/"
    "drop-swimming-and-aquafitness/drop-ins-indoor-pool-locations"
)

# Base for resolving relative URLs
BASE_URL = "https://ottawa.ca"

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}
PUBLIC_SWIM_KEYWORDS = ["public swim", "wave swim"]

# Pools known to have wave tanks (used to set the wave flag)
WAVE_POOL_KEYWORDS = ["wave pool", "wave tank", "splash wave", "wave swim"]


# ── Time parsing ──────────────────────────────────────────────────────────────

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
    Parse a cell like 'Noon - 1 pm, 4:30 - 9 pm'
    → [{"start": 720, "end": 780}, {"start": 990, "end": 1260}]
    """
    text = cell_text.strip()
    if not text or text.lower() in ("n/a", "—", "-", ""):
        return []
    text = re.sub(r"\bnoon\b", "12:00 pm", text, flags=re.I)
    text = re.sub(r"\bmidnight\b", "12:00 am", text, flags=re.I)
    text = re.sub(r"\(play free\)", "", text, flags=re.I)
    text = re.sub(r"__.*?__", "", text)

    results = []
    for part in re.split(r"[,\n]+", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(
            r"(\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)\s*[-\u2013]\s*(\d{1,2}(?::\d{2})?\s*[ap]m)",
            part, re.I,
        )
        if not m:
            continue
        start_str, end_str = m.group(1).strip(), m.group(2).strip()
        if not re.search(r"[ap]m", start_str, re.I):
            ap_m = re.search(r"([ap]m)", end_str, re.I)
            if ap_m:
                start_str += " " + ap_m.group(1)
        s = parse_time_str(start_str)
        e = parse_time_str(end_str)
        if s is not None and e is not None:
            results.append({"start": s, "end": e})
    return results


# ── Schedule table parser ─────────────────────────────────────────────────────

def parse_schedule_tables(html):
    """
    Parse all swim schedule tables from a pool page's HTML.
    Ottawa.ca uses <th> for both the header row AND the row-label column.
    Returns list of session dicts.
    """
    soup = BeautifulSoup(html, "html.parser")
    sessions = []

    for table in soup.find_all("table"):
        table_text = table.get_text()
        if not any(k in table_text.lower() for k in ["swim", "aquafit", "lane"]):
            continue

        caption = table.find("caption")
        print(f"      Table: {caption.get_text(strip=True)[:70] if caption else '(no caption)'}")

        rows = table.find_all("tr")
        if not rows:
            continue

        # Build col→day map from first row
        header_cells = rows[0].find_all(["th", "td"])
        col_to_day = {}
        for i, cell in enumerate(header_cells):
            text = cell.get_text(strip=True)
            for day_name, day_idx in DAY_INDEX.items():
                if day_name.lower() in text.lower():
                    col_to_day[i] = day_idx
                    break

        if not col_to_day:
            continue

        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            row_label = cells[0].get_text(separator=" ", strip=True)
            if not any(k in row_label.lower() for k in PUBLIC_SWIM_KEYWORDS):
                continue

            print(f"        ✓ {row_label}")
            for col_idx, day_idx in col_to_day.items():
                if col_idx >= len(cells):
                    continue
                cell_text = cells[col_idx].get_text(separator=" ", strip=True)
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


# ── Pool discovery ────────────────────────────────────────────────────────────

def discover_pools(page):
    """
    Visit the Ottawa.ca pool index page and extract all pool name + URL pairs.
    Returns list of dicts: {id, name, url}
    """
    print(f"Discovering pools from index page...")
    pools = []

    try:
        resp = page.goto(POOL_INDEX_URL, wait_until="networkidle", timeout=30000)
        print(f"  Index page HTTP {resp.status}")
    except PWTimeout:
        page.goto(POOL_INDEX_URL, wait_until="domcontentloaded", timeout=30000)

    page.wait_for_timeout(2000)
    html = page.content()

    # Save index page for debugging
    with open("debug_index.html", "w", encoding="utf-8") as f:
        f.write(html)

    soup = BeautifulSoup(html, "html.parser")

    # Pool links on Ottawa.ca are in the /facilities/place-listing/ path
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Resolve relative URLs
        if href.startswith("/"):
            href = BASE_URL + href
        if "place-listing" not in href:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        name = a.get_text(strip=True)
        if not name or len(name) < 4:
            continue

        # Generate a simple slug ID from the URL path
        slug = href.rstrip("/").split("/")[-1]

        pools.append({
            "id": slug,
            "name": name,
            "url": href,
        })

    print(f"  Found {len(pools)} pool links")
    for p in pools:
        print(f"    • {p['name']}  →  {p['url']}")

    return pools


# ── Per-pool scraper ──────────────────────────────────────────────────────────

def scrape_pool(page, pool):
    print(f"\n  [{pool['id']}] {pool['name']}", flush=True)

    loaded = False
    for wait_mode in ("networkidle", "domcontentloaded"):
        try:
            resp = page.goto(pool["url"], wait_until=wait_mode, timeout=30000)
            print(f"    HTTP {resp.status} ({wait_mode})")
            loaded = True
            break
        except PWTimeout:
            print(f"    Timeout ({wait_mode}), retrying...")
        except Exception as e:
            print(f"    Error: {e}")
            break

    if not loaded:
        return [], False

    page.wait_for_timeout(3000)
    html = page.content()

    # Save debug HTML
    safe_id = re.sub(r"[^a-z0-9_-]", "_", pool["id"])
    with open(f"debug_{safe_id}.html", "w", encoding="utf-8") as f:
        f.write(html)

    html_lower = html.lower()

    # Detect wave pool
    wave = any(k in html_lower for k in WAVE_POOL_KEYWORDS)

    if "public swim" not in html_lower and "wave swim" not in html_lower:
        print(f"    — No public swim schedule found, skipping")
        return [], wave

    sessions = parse_schedule_tables(html)
    print(f"    → {len(sessions)} sessions")
    return sessions, wave


# ── Address extraction ────────────────────────────────────────────────────────

def extract_address(page):
    """Try to pull the street address from an already-loaded pool page."""
    try:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        # Ottawa.ca typically has address in a .address or .field--name-field-address element
        for sel in [
            "[class*='field--name-field-address']",
            "[class*='address']",
            "[class*='location']",
        ]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator=" ", strip=True)
                # Look for something that looks like a street address
                m = re.search(r"\d+\s+\w[\w\s]+(?:St|Ave|Rd|Dr|Blvd|Way|Cres|Pl|Pkwy|Lane|Ln)\b",
                              text, re.I)
                if m:
                    return m.group(0).strip()
    except Exception:
        pass
    return ""


# ── Main ──────────────────────────────────────────────────────────────────────

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

        # Step 1: discover all pools from the index page
        pools = discover_pools(page)

        if not pools:
            print("\n⚠ Could not discover any pools from index page.")
            print("  Check debug_index.html artifact.")
            sys.exit(1)

        # Step 2: scrape each pool
        for pool in pools:
            sessions, wave = scrape_pool(page, pool)
            if sessions:
                address = extract_address(page)
                output["pools"].append({
                    "id": pool["id"],
                    "name": pool["name"],
                    "address": address,
                    "wave": wave,
                    "url": pool["url"],
                    "sessions": sessions,
                })

        browser.close()

    with open("schedule_data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Wrote schedule_data.json")

    total = sum(len(p["sessions"]) for p in output["pools"])
    print(f"  Pools with sessions: {len(output['pools'])}")
    for p in output["pools"]:
        print(f"  • {p['name']}: {len(p['sessions'])} sessions")

    if total == 0:
        print("\n⚠ WARNING: No sessions scraped. Check debug_*.html artifacts.")
        sys.exit(1)
    else:
        print(f"\n✓ Done — {total} total session slots across {len(output['pools'])} pools.")


if __name__ == "__main__":
    main()
