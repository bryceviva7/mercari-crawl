# requirements:
#   pip install -r requirements.txt
#   python -m playwright install chromium
#
# run:
#   python watcher.py

import asyncio, json, os, re, csv, time
from pathlib import Path
from urllib.parse import urlencode
from dotenv import load_dotenv
import requests
from playwright.async_api import async_playwright

load_dotenv()

DISCORD_WEBHOOK   = os.getenv("DISCORD_WEBHOOK", "")
DISCORD_USERNAME  = os.getenv("DISCORD_USERNAME", "Mercari Watch")
DISCORD_AVATAR    = os.getenv("DISCORD_AVATAR", "")
ALERT_THRESHOLD   = int(os.getenv("ALERT_THRESHOLD", "100"))  # alert if price <= this

SEEN_PATH   = Path("seen.json")
CSV_PATH    = Path("results.csv")
SEEN        = set(json.loads(SEEN_PATH.read_text()) if SEEN_PATH.exists() else [])

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

def build_search_url():
    # Keep it simple: newest, only active listings
    params = {
        "keyword": "boygenius baggu",
        "status": "on_sale",
        "sort": "created_time",
    }
    return f"https://www.mercari.com/search/?{urlencode(params)}"

def norm_title(t: str) -> str:
    # normalize spacing/punctuation and common variants
    t = (t or "").lower()
    # unify common variants: "boy genius" -> "boygenius"
    t = re.sub(r"\bboy\s+genius\b", "boygenius", t)
    # collapse whitespace and strip
    t = " ".join(re.sub(r"[^\w\s]", " ", t).split())
    return t

def title_is_target(t: str) -> bool:
    n = norm_title(t)
    # require both tokens to avoid false positives
    return ("boygenius" in n) and ("baggu" in n)

def parse_price(text: str):
    if not text: return None
    m = re.search(r"\$([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else None

def write_csv_row(item):
    header = ["ts_epoch", "title", "price", "url", "id"]
    exists = CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not exists: w.writeheader()
        w.writerow({
            "ts_epoch": int(time.time()),
            "title": item["title"],
            "price": item["price"],
            "url": item["url"],
            "id": item["id"],
        })

def notify_discord(item):
    if not DISCORD_WEBHOOK:
        print(f"[NO WEBHOOK] Would alert: {item['title']} ${item['price']} {item['url']}")
        return
    embeds = [{
        "title": item["title"][:240],
        "url": item["url"],
        "description": f"**${item['price']}** — BOYGENIUS x BAGGU",
        "footer": {"text": "Mercari Watch"},
    }]
    payload = {
        "username": DISCORD_USERNAME,
        "avatar_url": DISCORD_AVATAR or None,
        "embeds": embeds,
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print("[Discord] webhook error:", e)

def save_seen():
    SEEN_PATH.write_text(json.dumps(sorted(SEEN)))

async def fetch_listings(page):
    url = build_search_url()
    await page.goto(url, wait_until="domcontentloaded")
    # allow client rendering
    await page.wait_for_timeout(2000)

    # Optionally: light scroll to surface more cards
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)
    except:
        pass

    anchors = await page.locator("a[href^='/us/item/']").all()
    results = []
    for a in anchors:
        href = await a.get_attribute("href")
        if not href: continue
        url = f"https://www.mercari.com{href}" if href.startswith("/") else href

        # card text often includes title; price is separate text node
        title_text = (await a.text_content()) or ""
        price_el = a.locator("text=/\\$\\d[\\d,]*/").first
        price_text = await price_el.text_content() if await price_el.count() else ""
        price = parse_price(price_text)
        if price is None: continue

        m = re.search(r"/item/([^/]+)/?", url)
        item_id = m.group(1) if m else url

        results.append({
            "id": item_id,
            "title": " ".join(title_text.split()),
            "price": price,
            "url": url
        })
    return results

async def run_once():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()

        listings = await fetch_listings(page)
        await browser.close()

    # Keep only titles that clearly match BOYGENIUS + BAGGU
    matches = [x for x in listings if title_is_target(x["title"])]

    new_alerts = 0
    for item in matches:
        write_csv_row(item)  # always log
        if item["id"] in SEEN:
            continue
        SEEN.add(item["id"])
        if item["price"] <= ALERT_THRESHOLD:
            notify_discord(item)
            new_alerts += 1

    save_seen()
    print(f"Scanned {len(listings)}; {len(matches)} matches; {new_alerts} alerts (<= ${ALERT_THRESHOLD}).")

if __name__ == "__main__":
    asyncio.run(run_once())
