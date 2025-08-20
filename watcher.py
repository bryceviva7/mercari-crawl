# Minimal Mercari -> Discord watcher (with robust fallbacks)
# Requirements:
#   pip install playwright requests python-dotenv
#   python -m playwright install chromium
#
# Env:
#   DISCORD_WEBHOOK (required) - Discord channel webhook URL
#   MAX_PRICE       (optional) - max price to include (default 9999)

import asyncio, os, re
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
MAX_PRICE = int(os.getenv("MAX_PRICE", "9999"))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

SEARCH_PARAMS = {"keyword": "boygenius baggu", "status": "on_sale", "sort": "created_time"}

def build_search_url():
    return f"https://www.mercari.com/search/?{urlencode(SEARCH_PARAMS)}"

def parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else None

def send_discord(content: str):
    if not DISCORD_WEBHOOK:
        print("[NO WEBHOOK SET]", content); return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
    except Exception as e:
        print("[Discord] error:", e)

async def scrape_cards(page):
    """Primary strategy: read listing cards rendered on the search page."""
    # Common test IDs/selectors Mercari uses; we’ll try several.
    card_selectors = [
        "[data-testid='ItemCell']",
        "[data-test='item-cell']",
        "li a[href*='/item/']",
        "a[href*='/item/']",
    ]

    seen = set()
    results = []

    for sel in card_selectors:
        cards = await page.locator(sel).all()
        for el in cards:
            # Find the anchor with the item URL
            a = el if await el.evaluate("el => el.tagName.toLowerCase()==='a'") else el.locator("a[href*='/item/']").first
            if await a.count() == 0:
                continue
            href = await a.get_attribute("href")
            if not href:
                continue
            url = f"https://www.mercari.com{href}" if href.startswith("/") else href
            if "/item/" not in url or url in seen:
                continue
            seen.add(url)

            # Title (best-effort from the card)
            try:
                title = (await a.text_content()) or ""
            except:
                title = ""

            # Price within the card
            price_el = a.locator("text=/\\$\\d[\\d,]*/").first
            price_txt = await price_el.text_content() if await price_el.count() else ""
            price = parse_price(price_txt)
            if price is None:
                # try within the card container
                price_el2 = el.locator("text=/\\$\\d[\\d,]*/").first
                price_txt = await price_el2.text_content() if await price_el2.count() else ""
                price = parse_price(price_txt)

            if price is None:
                continue

            results.append({"title": " ".join(title.split()), "price": price, "url": url})

    return results

async def scrape_from_html_then_visit(ctx, html, limit=8):
    """
    Fallback: regex item URLs from the page HTML, then visit a few item pages
    to read title + price directly.
    """
    urls = []
    for m in re.finditer(r'href="(/(?:us/)?item/[^"]+)"', html):
        u = m.group(1)
        if u not in urls:
            urls.append(u)
    full_urls = [f"https://www.mercari.com{u}" if u.startswith("/") else u for u in urls]
    results = []
    for url in full_urls[:limit]:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            # title: page H1 or document title as fallback
            title = ""
            try:
                title = await page.locator("h1, [data-testid='item-title']").first.text_content()
            except:
                pass
            if not title:
                try:
                    title = await page.title()
                except:
                    title = url

            # price: look for $### anywhere visible near buy box
            price_txt = ""
            try:
                price_txt = await page.locator("text=/\\$\\d[\\d,]*/").first.text_content()
            except:
                pass
            price = parse_price(price_txt)
            if price is None:
                continue

            results.append({"title": " ".join((title or "").split()), "price": price, "url": url})
        finally:
            await page.close()
    return results

async def fetch_listings():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await ctx.new_page()

        # Load search (don’t wait for networkidle; CI pages often never settle)
        await page.goto(build_search_url(), wait_until="domcontentloaded", timeout=90000)

        # Nudge lazy loaders
        for _ in range(3):
            await page.wait_for_timeout(800)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # Strategy 1: cards
        results = await scrape_cards(page)

        # Strategy 2: regex item URLs from HTML, then visit a few
        if not results:
            html = await page.content()
            fallback = await scrape_from_html_then_visit(ctx, html, limit=8)
            results.extend(fallback)

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
