# Minimal Mercari -> Discord watcher using __NEXT_DATA__ (CI friendly)
# Requirements:
#   pip install playwright requests python-dotenv
#   python -m playwright install chromium
#
# Env:
#   DISCORD_WEBHOOK (required)
#   MAX_PRICE       (optional, default 9999)
#   DEBUG           (optional: "1"/"true" to print debug and save nextdata.json)

import asyncio, os, re, json
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

SEARCH_PARAMS = {"keyword": "boygenius baggu", "sort": "created_time"}  # keep simple

def build_search_url():
    return f"https://www.mercari.com/search/?{urlencode(SEARCH_PARAMS)}"

def send_discord(content: str):
    if not DISCORD_WEBHOOK:
        print("[NO WEBHOOK SET]", content); return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
    except Exception as e:
        print("[Discord] error:", e)

def parse_price_scalar(v):
    # v might be int already or "$123"
    if v is None: return None
    if isinstance(v, (int, float)): return int(v)
    m = re.search(r"\$?\s*([\d,]+)", str(v))
    return int(m.group(1).replace(",", "")) if m else None

def walk_items(obj):
    """
    Recursively find item-like dicts inside Next.js data.
    We look for dicts that have an id and either name/title and a price/amount/currentPrice.
    Returns list of dicts with title, price, id.
    """
    found = []
    def _walk(x):
        if isinstance(x, dict):
            # try extract
            keys = set(x.keys())
            possible_id = x.get("id") or x.get("itemId") or x.get("uuid")
            title = x.get("name") or x.get("title") or x.get("itemName")
            # price might be nested or flat
            price = None
            for k in ("price", "currentPrice", "amount", "itemPrice"):
                if k in x:
                    price = parse_price_scalar(x[k]); break
            # sometimes nested in 'prices' or 'salePrice'
            if price is None and "price" in x and isinstance(x["price"], dict):
                for k in ("value", "amount", "current", "number"):
                    if k in x["price"]:
                        price = parse_price_scalar(x["price"][k]); break
            # Accept this as an item if it looks right
            if possible_id and title and isinstance(title, str) and price is not None:
                found.append({
                    "id": str(possible_id),
                    "title": " ".join(title.split()),
                    "price": price,
                })
            # continue walking
            for v in x.values():
                _walk(v)
        elif isinstance(x, list):
            for v in x:
                _walk(v)
    _walk(obj)
    # de-dup by (id, price)
    dedup = {}
    for it in found:
        dedup[(it["id"], it["price"])] = it
    return list(dedup.values())

async def fetch_listings_from_nextdata(page):
    # Wait for Next.js data script, then parse
    try:
        await page.wait_for_selector("script#__NEXT_DATA__", timeout=15000)
    except PWTimeout:
        return []
    text = await page.locator("script#__NEXT_DATA__").first.text_content()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if DEBUG:
        try:
            with open("nextdata.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print("[DEBUG] Saved nextdata.json")
        except Exception as e:
            print("[DEBUG] Could not save nextdata.json:", e)
    items = walk_items(data)
    # Build URLs when id looks like Mercari item id (starts with m and alphanum)
    listings = []
    for it in items:
        iid = it["id"]
        if not isinstance(iid, str):
            iid = str(iid)
        if "/item/" in iid:  # sometimes the id is actually a URL/path
            url = f"https://www.mercari.com{iid}" if iid.startswith("/") else iid
        else:
            # Mercari US item IDs typically look like 'm12345678901'
            url = f"https://www.mercari.com/us/item/{iid}/"
        listings.append({"title": it["title"], "price": it["price"], "url": url})
    return listings

async def fetch_listings():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 1800},
        )
        # reduce webdriver fingerprint
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        page = await ctx.new_page()
        url = build_search_url()
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        # small yield for client hydration
        await page.wait_for_timeout(1500)

        # primary: parse __NEXT_DATA__
        listings = await fetch_listings_from_nextdata(page)

        # fallback: try to harvest anchor URLs if nextdata empty
        if not listings:
            if DEBUG: print("[DEBUG] NEXT_DATA empty, trying anchors fallback")
            for _ in range(2):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)
            anchors = await page.locator("a[href*='/item/']").all()
            urls = []
            for a in anchors:
                href = await a.get_attribute("href")
                if href and "/item/" in href:
                    urls.append(href if href.startswith("http") else f"https://www.mercari.com{href}")
            # visit a few item pages to read their own __NEXT_DATA__
            urls = list(dict.fromkeys(urls))[:8]
            for item_url in urls:
                p2 = await ctx.new_page()
                try:
                    await p2.goto(item_url, wait_until="domcontentloaded", timeout=90000)
                    items2 = await fetch_listings_from_nextdata(p2)
                    # item pages usually give exactly one item; filter for the exact URL
                    for it in items2:
                        listings.append(it)
                finally:
                    await p2.close()

        if DEBUG:
            print(f"[DEBUG] Total listings parsed (pre-filter): {len(listings)}")

        await browser.close()
        return listings

async def main():
    listings = await fetch_listings()
    # filter by price
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
