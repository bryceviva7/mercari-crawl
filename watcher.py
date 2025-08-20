# Minimal Mercari -> Discord watcher (CI friendly, simple)
# Requirements:
#   pip install playwright requests python-dotenv
#   python -m playwright install webkit
#
# Env:
#   DISCORD_WEBHOOK (required) - Discord channel webhook URL
#   MAX_PRICE       (optional) - max price to include (default 9999)
#   PLAYWRIGHT_ENGINE (optional) - webkit|chromium|firefox (default webkit)
#   DEBUG           (optional) - "1"/"true" to save nextdata.json + screenshot

import asyncio, os, re, json
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
DISCORD_WEBHOOK   = os.getenv("DISCORD_WEBHOOK", "").strip()
MAX_PRICE         = int(os.getenv("MAX_PRICE", "9999"))
ENGINE            = os.getenv("PLAYWRIGHT_ENGINE", "webkit").lower()
DEBUG             = os.getenv("DEBUG", "").lower() in ("1","true","yes","on")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Keep the search simple and US-scoped
SEARCH_PARAMS = {"keyword": "boygenius baggu", "sort": "created_time"}
def build_search_url():
    return f"https://www.mercari.com/us/search/?{urlencode(SEARCH_PARAMS)}"

def send_discord(content: str):
    if not DISCORD_WEBHOOK:
        print("[NO WEBHOOK SET]", content); return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
    except Exception as e:
        print("[Discord] error:", e)

def parse_price_scalar(v):
    if v is None: return None
    if isinstance(v, (int, float)): return int(v)
    m = re.search(r"\$?\s*([\d,]+)", str(v))
    return int(m.group(1).replace(",", "")) if m else None

def walk_items(obj):
    """Find item-like dicts inside Next.js data. Return [{'title','price','id'}]."""
    found = []
    def _walk(x):
        if isinstance(x, dict):
            pid = x.get("id") or x.get("itemId") or x.get("uuid")
            title = x.get("name") or x.get("title") or x.get("itemName")
            price = None
            # common price shapes
            for k in ("price", "currentPrice", "amount", "itemPrice"):
                if k in x:
                    price = parse_price_scalar(x[k]); break
            if price is None and isinstance(x.get("price"), dict):
                for k in ("value","amount","current","number"):
                    if k in x["price"]:
                        price = parse_price_scalar(x["price"][k]); break
            if pid and title and price is not None:
                found.append({"id": str(pid), "title": " ".join(str(title).split()), "price": price})
            for v in x.values(): _walk(v)
        elif isinstance(x, list):
            for v in x: _walk(v)
    _walk(obj)
    # de-dup
    uniq = {}
    for it in found:
        uniq[(it["id"], it["price"])] = it
    return list(uniq.values())

async def fetch_listings():
    async with async_playwright() as pw:
        launcher = {"webkit": pw.webkit, "chromium": pw.chromium, "firefox": pw.firefox}.get(ENGINE, pw.webkit)
        browser = await launcher.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 1800},
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()

        url = build_search_url()
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(1200)  # small hydrate wait

        # Primary: parse Next.js data
        text = await page.locator("script#__NEXT_DATA__").first.text_content()
        items = []
        if text:
            try:
                data = json.loads(text)
                if DEBUG:
                    with open("nextdata.json","w",encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
                items = walk_items(data)
            except Exception:
                items = []

        # If nothing, quick fallback: anchors -> visit a few item pages' __NEXT_DATA__
        listings = []
        if not items:
            anchors = await page.locator("a[href*='/item/']").all()
            urls = []
            for a in anchors:
                href = await a.get_attribute("href")
                if href and "/item/" in href:
                    urls.append(href if href.startswith("http") else f"https://www.mercari.com{href}")
            urls = list(dict.fromkeys(urls))[:6]
            for item_url in urls:
                p2 = await ctx.new_page()
                try:
                    await p2.goto(item_url, wait_until="domcontentloaded", timeout=90000)
                    t2 = await p2.locator("script#__NEXT_DATA__").first.text_content()
                    if not t2: continue
                    d2 = json.loads(t2)
                    for it in walk_items(d2):
                        # accept the first good-looking item
                        listings.append({
                            "title": it["title"],
                            "price": it["price"],
                            "url": item_url
                        })
                        break
                except Exception:
                    pass
                finally:
                    await p2.close()
        else:
            # Build URLs when ID looks like Mercari item id
            for it in items:
                iid = str(it["id"])
                if "/item/" in iid:
                    url = f"https://www.mercari.com{iid}" if iid.startswith("/") else iid
                else:
                    url = f"https://www.mercari.com/us/item/{iid}/"
                listings.append({"title": it["title"], "price": it["price"], "url": url})

        if DEBUG:
            try:
                await page.screenshot(path="search.png", full_page=True)
            except Exception:
                pass

        await browser.close()
        return listings

async def main():
    listings = await fetch_listings()
    listings = [x for x in listings if x["price"] <= MAX_PRICE]

    if not listings:
        msg = f"✅ Scan complete: nothing found under ${MAX_PRICE}."
        print(msg); send_discord(msg)
    else:
        msg_lines = [f"✅ Scan complete: {len(listings)} listing(s) under ${MAX_PRICE}:"]
        for it in listings[:10]:
            msg_lines.append(f"- {it['title']} — ${it['price']} — {it['url']}")
        msg = "\n".join(msg_lines)
        print(msg); send_discord(msg)

if __name__ == "__main__":
    asyncio.run(main())
