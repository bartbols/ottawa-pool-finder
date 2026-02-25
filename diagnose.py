"""
diagnose.py — Run this once on GitHub Actions to see what HTML Ottawa.ca
actually renders for the schedule. Check the Actions log for the output,
then share it so the scraper can be fixed.
"""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from datetime import datetime, timezone

URL = "https://ottawa.ca/en/recreation-and-parks/facilities/place-listing/splash-wave-pool"

print(f"=== Ottawa.ca Diagnostic ===")
print(f"Time: {datetime.now(timezone.utc).isoformat()}")
print(f"URL:  {URL}\n")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
        locale="en-CA",
    ).new_page()

    try:
        resp = page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        print(f"HTTP status : {resp.status}")
        print(f"Final URL   : {page.url}")
        print(f"Page title  : {page.title()}\n")
    except PWTimeout:
        print("TIMEOUT on initial load — trying networkidle...")
        resp = page.goto(URL, wait_until="networkidle", timeout=40000)

    # Wait a moment for any JS rendering
    page.wait_for_timeout(3000)

    # --- 1. Count common structural elements ---
    print("=== Element counts ===")
    for selector in ["table", "tr", "th", "td", "[class*='schedule']", "[class*='program']",
                     "[class*='activity']", "[class*='swim']", "[class*='grid']",
                     "[class*='row']", "[class*='col']", "h2", "h3", "h4"]:
        try:
            count = len(page.query_selector_all(selector))
            if count > 0:
                print(f"  {selector:30s} → {count}")
        except Exception:
            pass

    # --- 2. Find any element whose text contains "Public Swim" ---
    print("\n=== Elements containing 'Public Swim' ===")
    found = page.evaluate("""() => {
        const results = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const node = walker.currentNode;
            if (node.textContent.includes('Public Swim')) {
                let el = node.parentElement;
                // Walk up to find a meaningful container
                for (let i = 0; i < 4; i++) {
                    if (!el) break;
                    results.push({
                        tag: el.tagName,
                        id: el.id || '',
                        cls: el.className || '',
                        text: el.innerText ? el.innerText.substring(0, 300) : ''
                    });
                    el = el.parentElement;
                }
            }
        }
        return results.slice(0, 20);
    }""")
    for item in found:
        print(f"\n  TAG={item['tag']}  ID='{item['id']}'  CLASS='{item['cls'][:80]}'")
        print(f"  TEXT: {item['text'][:200].strip()}")

    # --- 3. Dump a portion of the raw HTML around "Public Swim" ---
    print("\n=== Raw HTML snippet around 'Public Swim' ===")
    html = page.content()
    idx = html.find("Public Swim")
    if idx == -1:
        idx = html.find("public swim")
    if idx >= 0:
        snippet = html[max(0, idx-300):idx+600]
        print(snippet)
    else:
        print("  'Public Swim' not found anywhere in page HTML!")
        print("  This may mean the content is loaded by a separate API call.")

        # --- 4. Check network requests for any JSON/API calls ---
        print("\n=== Checking for XHR/fetch API calls ===")
        api_calls = []
        page.on("request", lambda req: api_calls.append(req.url) if "json" in req.url or "api" in req.url or "program" in req.url.lower() else None)
        page.reload(wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
        print(f"  Intercepted {len(api_calls)} potential API calls:")
        for url in api_calls[:20]:
            print(f"    {url}")

    browser.close()
    print("\n=== Diagnostic complete ===")

