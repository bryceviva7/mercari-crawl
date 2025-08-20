# watcher.py (simple)
# ------------------------------
# Requirements:
#   pip install playwright requests python-dotenv
#   python -m playwright install chromium
#
# Env vars (.env or GitHub Actions secrets):
#   DISCORD_WEBHOOK   (required) - Discord channel webhook URL

import asyncio, os, re
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

def build_search_url():
    params = {
        "keyword": "boygenius baggu",
        "status": "on_sale",
        "sort": "created_time",
    }
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
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()
        await page.goto(build_search_url(), wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        anchors = await page.locator("a[href^='/us/item/']").all()
        results = []
        for a in anchors:
            href = await a.get_attribute("href")
            if not href: continue
            url = f"https://www.mercari.com{href}" if href.startswith("/") else href
            title = (await a.text_content()) or ""
            price_el = a.locator("text=/\\$\\d[\\d,]*/").first
            price_text = await price_el.text_content() if await price_el.count() else ""
            price = parse_price(price_text)
            if price is None: continue
            results.append({"title": title.strip(), "price": price, "url": url})
        await browser.close()
        return results

async def main():
    listings = await fetch_listings()
    if not listings:
        msg = "✅ Scan complete: nothing found."
        print(msg)
        send_discord_message(msg)
    else:
        msg_lines = ["✅ Scan complete: here are the listings:"]
        for item in listings[:10]:  # show first 10 to avoid huge spam
            msg_lines.append(f"- {item['title']} — ${item['price']} — {item['url']}")
        msg = "\n".join(msg_lines)
        print(msg)
        send_discord_message(msg)

if __name__ == "__main__":
    asyncio.run(main())
