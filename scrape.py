"""
Ottawa Pool & Skating Schedule Scraper
Discovers and scrapes public swim and public skating sessions from Ottawa.ca.
Outputs schedule_data.json with separate pools[] and rinks[] arrays.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

# Primary URLs (current as of 2026-03)
POOL_INDEX_URL = (
    "https://ottawa.ca/en/recreation-and-parks/swimming/"
    "drop-swimming-and-aquafitness/indoor-pools-drop-locations"
)
# Fallback if primary 404s
POOL_INDEX_URL_FALLBACK = (
    "https://ottawa.ca/en/recreation-and-parks/swimming/"
    "drop-swimming-and-aquafit"
)
RINK_INDEX_URL = (
    "https://ottawa.ca/en/recreation-and-parks/skating/"
    "drop-skating/drop-skating-locations"
)
RINK_INDEX_URL_FALLBACK = (
    "https://ottawa.ca/en/recreation-and-parks/skating/"
    "drop-skating"
)
BASE_URL = "https://ottawa.ca"

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}

PUBLIC_SWIM_KEYWORDS  = ["public swim", "wave swim", "lane swim"]
PUBLIC_SKATE_KEYWORDS = ["public skate", "public skating", "family skate", "family skating", "50+ skate"]
WAVE_POOL_KEYWORDS    = ["wave pool", "wave tank", "splash wave", "wave swim"]

HOLIDAY_KEYWORDS = [
    "march break", "spring break", "pa day", "p.a. day",
    "holiday", "statutory holiday", "civic holiday",
    "christmas", "new year", "thanksgiving", "family day",
    "victoria day", "canada day", "labour day",
    "reading week", "winter break", "summer schedule",
]

PAGE_TIMEOUT    = 60000   # ms — page load timeout
WAIT_AFTER_LOAD = 3000    # ms — extra wait for JS rendering
RETRY_ATTEMPTS  = 3       # retries per URL
RETRY_DELAY     = 8       # seconds between retries


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
    if not text or text.lower() in ("n/a", "\u2014", "-", ""):
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


def is_holiday_table(table):
    """Return (True, reason) if this table is a special holiday/break schedule."""
    caption = table.find("caption")
    if caption:
        cap_text = caption.get_text(strip=True).lower()
        if any(k in cap_text for k in HOLIDAY_KEYWORDS):
            return True, cap_text

    node = table
    for _ in range(3):
        node = node.find_previous_sibling()
        if node is None:
            break
        if node.name in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div"):
            text = node.get_text(strip=True).lower()
            if any(k in text for k in HOLIDAY_KEYWORDS):
                return True, text[:80]

    return False, None


def parse_schedule_tables(html, row_keywords):
    soup = BeautifulSoup(html, "html.parser")
    sessions = []

    for table in soup.find_all("table"):
        table_text = table.get_text().lower()
        if not any(k in table_text for k in row_keywords):
            continue

        holiday, reason = is_holiday_table(table)
        if holiday:
            print("      SKIP holiday table: " + reason)
            continue

        caption = table.find("caption")
        print("      Table: " + (caption.get_text(strip=True)[:70] if caption else "(no caption)"))

        rows = table.find_all("tr")
        if not rows:
            continue

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
            if not any(k in row_label.lower() for k in row_keywords):
                continue

            print("        tick " + row_label)
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

    # Deduplicate: drop any session with identical (day, label, start, end)
    seen = set()
    unique = []
    for s in sessions:
        key = (s["day"], s["label"].lower(), s["start"], s["end"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    if len(unique) < len(sessions):
        print("      Deduped " + str(len(sessions) - len(unique)) + " duplicate session(s)")
    return unique


def load_page(page, url):
    """Try to load a URL. Returns True on success (HTTP < 400)."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        for wait_mode in ("networkidle", "domcontentloaded"):
            try:
                resp = page.goto(url, wait_until=wait_mode, timeout=PAGE_TIMEOUT)
                code = resp.status if resp else 0
                print("  HTTP " + str(code) + " (" + wait_mode + ")" +
                      (" [attempt " + str(attempt) + "]" if attempt > 1 else ""))
                if resp and resp.status < 400:
                    page.wait_for_timeout(WAIT_AFTER_LOAD)
                    return True
                break  # 4xx/5xx — no point retrying same wait_mode
            except PWTimeout:
                print("  Timeout (" + wait_mode + ") [attempt " + str(attempt) + "]")
            except Exception as e:
                print("  Error: " + str(e))
                break
        if attempt < RETRY_ATTEMPTS:
            print("  Waiting " + str(RETRY_DELAY) + "s before retry...")
            time.sleep(RETRY_DELAY)
    return False


def discover_venues(page, primary_url, label, fallback_url=None):
    print("\nDiscovering " + label + "...")
    urls = [primary_url] + ([fallback_url] if fallback_url else [])

    for url in urls:
        print("  Trying: " + url)
        if not load_page(page, url):
            print("  Could not load — " + ("trying fallback..." if url != urls[-1] else "giving up."))
            continue

        html = page.content()
        safe_label = label.replace(" ", "_")
        with open("debug_index_" + safe_label + ".html", "w", encoding="utf-8") as f:
            f.write(html)

        soup = BeautifulSoup(html, "html.parser")
        seen_urls = set()
        venues = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
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
            slug = href.rstrip("/").split("/")[-1]
            venues.append({"id": slug, "name": name, "url": href})

        if venues:
            print("  Found " + str(len(venues)) + " " + label + " links")
            for v in venues:
                print("    * " + v["name"] + "  ->  " + v["url"])
            return venues

        print("  No venue links found — " + ("trying fallback..." if url != urls[-1] else "giving up."))

    return []


def scrape_venue(page, venue, row_keywords, check_keywords, wave_check=False):
    print("\n  [" + venue["id"] + "] " + venue["name"])

    if not load_page(page, venue["url"]):
        print("    FAILED to load venue page, skipping")
        return [], False

    html = page.content()
    safe_id = re.sub(r"[^a-z0-9_-]", "_", venue["id"])
    with open("debug_" + safe_id + ".html", "w", encoding="utf-8") as f:
        f.write(html)

    html_lower = html.lower()
    wave = any(k in html_lower for k in WAVE_POOL_KEYWORDS) if wave_check else False

    if not any(k in html_lower for k in check_keywords):
        print("    - No relevant sessions found, skipping")
        return [], wave

    sessions = parse_schedule_tables(html, row_keywords)
    print("    -> " + str(len(sessions)) + " sessions")
    return sessions, wave


def extract_address(page):
    try:
        soup = BeautifulSoup(page.content(), "html.parser")
        for sel in [
            "[class*='field--name-field-address']",
            "[class*='address']",
            "[class*='location']",
        ]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator=" ", strip=True)
                m = re.search(
                    r"\d+\s+\w[\w\s]+(?:St|Ave|Rd|Dr|Blvd|Way|Cres|Pl|Pkwy|Lane|Ln)\b",
                    text, re.I,
                )
                if m:
                    return m.group(0).strip()
    except Exception:
        pass
    return ""


def main():
    print("=== Ottawa Pool & Skating Schedule Scraper ===")
    print("Started: " + datetime.now(timezone.utc).isoformat())

    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "pools": [],
        "rinks": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-CA",
        )
        page = context.new_page()
        page.on("console", lambda _: None)

        # ── Pools ─────────────────────────────────────────────────────────────
        pools = discover_venues(page, POOL_INDEX_URL, "pools", POOL_INDEX_URL_FALLBACK)
        if not pools:
            print("WARNING: Could not discover any pools — keeping previous schedule_data.json.")
            sys.exit(0)  # exit 0: old data is better than empty

        print("\n=== Scraping " + str(len(pools)) + " pool pages ===")
        for pool in pools:
            sessions, wave = scrape_venue(
                page, pool,
                row_keywords=PUBLIC_SWIM_KEYWORDS,
                check_keywords=["public swim", "wave swim", "lane swim"],
                wave_check=True,
            )
            if sessions:
                output["pools"].append({
                    "id": pool["id"],
                    "name": pool["name"],
                    "address": extract_address(page),
                    "wave": wave,
                    "url": pool["url"],
                    "sessions": sessions,
                })

        # ── Rinks ─────────────────────────────────────────────────────────────
        rinks = discover_venues(page, RINK_INDEX_URL, "rinks", RINK_INDEX_URL_FALLBACK)
        if rinks:
            print("\n=== Scraping " + str(len(rinks)) + " rink pages ===")
            for rink in rinks:
                sessions, _ = scrape_venue(
                    page, rink,
                    row_keywords=PUBLIC_SKATE_KEYWORDS,
                    check_keywords=["public skate", "public skating", "family skate", "family skating"],
                    wave_check=False,
                )
                if sessions:
                    output["rinks"].append({
                        "id": rink["id"],
                        "name": rink["name"],
                        "address": extract_address(page),
                        "url": rink["url"],
                        "sessions": sessions,
                    })
        else:
            print("WARNING: Could not discover any rinks (non-fatal).")

        browser.close()

    pool_sessions = sum(len(p["sessions"]) for p in output["pools"])
    rink_sessions = sum(len(r["sessions"]) for r in output["rinks"])

    if pool_sessions == 0 and rink_sessions == 0:
        print("WARNING: Scraper ran but found zero sessions — keeping previous schedule_data.json.")
        sys.exit(0)  # exit 0: old data is better than empty

    with open("schedule_data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print("\nDone. Wrote schedule_data.json")
    print("  Pools: " + str(len(output["pools"])) + " venues, " + str(pool_sessions) + " sessions")
    for p in output["pools"]:
        print("    * " + p["name"] + ": " + str(len(p["sessions"])) + " sessions")
    print("  Rinks: " + str(len(output["rinks"])) + " venues, " + str(rink_sessions) + " sessions")
    for r in output["rinks"]:
        print("    * " + r["name"] + ": " + str(len(r["sessions"])) + " sessions")


if __name__ == "__main__":
    main()
