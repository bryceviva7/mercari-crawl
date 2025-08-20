# Minimal Mercari -> Discord watcher
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

def build_search_url():
    params = {"keyword": "boygenius baggu", "status": "on_sale", "sort": "created_time"}
    return f"https://www.mercari.com/search/?{urlencode(params)}"

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
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await ctx.new_page()
        # Load page (DOM ready). Network can stay “busy” forever; don't wait for networkidle.
        await page.goto(build_search_url(), wait_until="domcontentloaded", timeout=90000)

        # Try to let the client render and lazy-load a bit
        for _ in range(2):
            await page.wait_for_timeout(900)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # Grab anchors that look like item cards
        anchors = await page.locator("a[href*='/item/']").all()
        results = []
        for a in anchors:
            href = await a.get_attribute("href")
            if not href: continue
            url = f"https://www.mercari.com{href}" if href.startswith("/") else href

            title = (await a.text_content()) or ""
            price_el = a.locator("text=/\\$\\d[\\d,]*/").first
            price_txt = await price_el.text_content() if await price_el.count() else ""
            price = parse_price(price_txt)
            if price is None: continue

            results.append({"title": " ".join(title.split()), "price": price, "url": url})

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
        for item in listings[:10]:  # cap to first 10 lines for Discord
            msg_lines.append(f"- {item['title']} — ${item['price']} — {item['url']}")
        msg = "\n".join(msg_lines)
        print(msg); send_discord(msg)

if __name__ == "__main__":
    asyncio.run(main())
