# Minimal Mercari -> Discord watcher (robust + debug)
# Requirements:
#   pip install playwright requests python-dotenv
#   python -m playwright install chromium
#
# Env:
#   DISCORD_WEBHOOK (required)
#   MAX_PRICE       (optional, default 9999)
#   DEBUG           (optional: "1"/"true" to save search.png and print extra info)

import asyncio, os, re
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
MAX_PRICE = int(os.getenv("MAX_PRICE", "9999"))
DEBUG = os.getenv("DEBUG", "").lower() in ("1","true","yes","on")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Keep search basic; some params (like status) can hide results in some regions
SEARCH_PARAMS = {"keyword": "boygenius baggu", "sort": "created_time"}

def build_search_url():
    return f"https://www.mercari.com/search/?{urlencode(SEARCH_PARAMS)}"

def parse_price(text: str):
    if not text: return None
    m = re.search(r"\$([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else None

def send_discord(content: str):
    if not DISCORD_WEBHOOK:
        print("[NO WEBHOOK SET]", content); return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
    except Exception as e:
        print("[Discord] error:", e)

async def fetch_listings():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 1800},
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()

        url = build_search_url()
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        # Let the client render & lazy-load a bit
        for _ in range(3):
            await page.wait_for_timeout(900)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # Prefer specific cards if present; fall back to any anchor with /item/
        selectors = [
            "[data-testid='ItemCell'] a[href*='/item/']",
            "[data-test='item-cell'] a[href*='/item/']",
            "li a[href*='/item/']",
            "a[href*='/item/']",
            # XPath fallback
            "//a[contains(@href,'/item/')]",
        ]

        results = []
        seen = set()

        for sel in selectors:
            loc = page.locator(sel) if not sel.startswith("//") else page.locator(f"{sel}")
            try:
                count = await loc.count()
            except Exception:
                count = 0

            if DEBUG:
                print(f"[DEBUG] Selector {sel!r} count: {count}")

            if count == 0:
                continue

            elements = await loc.all()
            for el in elements:
                try:
                    href = await el.get_attribute("href")
                except Exception:
                    href = None
                if not href:
                    continue

                url = f"https://www.mercari.com{href}" if href.startswith("/") else href
                if "/item/" not in url or url in seen:
                    continue
                seen.add(url)

                # Title + price (best-effort from the card)
                try:
                    title = (await el.text_content()) or ""
                except Exception:
                    title = ""

                price_el = el.locator("text=/\\$\\d[\\d,]*/").first
                try:
                    has_price = await price_el.count()
                except Exception:
                    has_price = 0
                price_txt = await price_el.text_content() if has_price else ""
                price = parse_price(price_txt)

                # If price not on the anchor, try within the parent card
                if price is None:
                    parent = el.locator("xpath=ancestor::*[self::li or self::div][1]")
                    price_el2 = parent.locator("text=/\\$\\d[\\d,]*/").first
                    try:
                        has_p2 = await price_el2.count()
                    except Exception:
                        has_p2 = 0
                    price_txt = await price_el2.text_content() if has_p2 else ""
                    price = parse_price(price_txt)

                if price is None:
                    continue

                results.append({
                    "title": " ".join((title or "").split()),
                    "price": price,
                    "url": url
                })

            if results:
                break  # we found items with this selector, no need to try the rest

        if DEBUG:
            try:
                await page.screenshot(path="search.png", full_page=True)
                print("[DEBUG] Saved screenshot: search.png")
                print(f"[DEBUG] Found {len(results)} card(s) before price filter.")
            except Exception as e:
                print("[DEBUG] Screenshot failed:", e)

        await browser.close()
        return results

async def main():
    listings = await fetch_listings()
    listings = [x for x in listings if x["price"] <= MAX_PRICE]

    if not listings:
        msg = f"✅ Scan complete: nothing found under ${MAX_PRICE}."
        print(msg); send_discord(msg)
    else:
        msg_lines = [f"✅ Scan complete: {len(listings)} listing(s) under ${MAX_PRICE}:"]
        for item in listings[:10]:
            msg_lines.append(f"- {item['title']} — ${item['price']} — {item['url']}")
        msg = "\n".join(msg_lines)
        print(msg); send_discord(msg)

if __name__ == "__main__":
    asyncio.run(main())
