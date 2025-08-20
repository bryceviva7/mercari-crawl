# watcher.py (simple with MAX_PRICE + robust selectors)
# Requirements:
#   pip install playwright requests python-dotenv
#   python -m playwright install chromium
#
# Env vars (.env or GitHub Actions secrets):
#   DISCORD_WEBHOOK   (required) - Discord channel webhook URL
#   MAX_PRICE         (optional) - maximum price to include (default 9999)

import asyncio, os, re
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
MAX_PRICE = int(os.getenv("MAX_PRICE", "9999"))  # set e.g. 200 to catch $142

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

def build_search_url():
    # Newest, only active listings; keyword covers both orders (boygenius/baggu)
    params = {"keyword": "boygenius baggu", "status": "on_sale", "sort": "created_time"}
    return f"https://www.mercari.com/search/?{urlencode(params)}"

def parse_price(text: str):
    if not text: return None
    m = re.search(r"\$([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else None

def send_discord_message(content: str):
    if not DISCORD_WEBHOOK:
        print("[NO WEBHOOK SET]", content)
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
    except Exception as e:
        print("[Discord] error:", e)

async def fetch_listings():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        # Reduce “webdriver” fingerprint
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        page = await ctx.new_page()
        await page.goto(build_search_url(), wait_until="networkidle")

        # Wait for at least one item anchor to appear, but don't die if none
        try:
            await page.wait_for_selector("a[href*='/item/']", timeout=5000)
        except PlaywrightTimeout:
            pass  # continue; we might still get items after scroll

        # Light scrolls to trigger lazy load
        for _ in range(2):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

        # Match BOTH '/item/' and '/us/item/'
        anchors = await page.locator("a[href*='/item/']").all()
        results = []
        for a in anchors:
            href = await a.get_attribute("href")
            if not href: continue
            url = f"https://www.mercari.com{href}" if href.startswith("/") else href

            # Title from the card (best-effort)
            title = (await a.text_content()) or ""

            # Price: look for a $### token within the anchor
            price_el = a.locator("text=/\\$\\d[\\d,]*/").first
            price_text = await price_el.text_content() if await price_el.count() else ""
            price = parse_price(price_text)
            if price is None: continue

            results.append({"title": title.strip(), "price": price, "url": url})

        await browser.close()
        return results

async def main():
    listings = await fetch_listings()

    # Keep only listings at or under MAX_PRICE
    listings = [item for item in listings if item["price"] <= MAX_PRICE]

    if not listings:
        msg = f"✅ Scan complete: nothing found under ${MAX_PRICE}."
        print(msg)
        send_discord_message(msg)
    else:
        msg_lines = [f"✅ Scan complete: {len(listings)} listing(s) under ${MAX_PRICE}:"]
        for item in listings[:10]:  # show first 10
            msg_lines.append(f"- {item['title']} — ${item['price']} — {item['url']}")
        msg = "\n".join(msg_lines)
        print(msg)
        send_discord_message(msg)

if __name__ == "__main__":
    asyncio.run(main())
