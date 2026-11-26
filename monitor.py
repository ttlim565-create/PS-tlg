"""
三合一捡漏监控: Amazon SG + Shopee MY + Lazada MY
- 本地电脑: 循环运行，Shopee/Lazada 需要先登录一次(HEADLESS=False 弹出浏览器登录)
- GitHub Actions: 设 CI_MODE=1，只跑一轮就退出，建议只开 amazon

环境变量(都可选):
  PLATFORMS  默认 "amazon,shopee,lazada"，GitHub 上用 "amazon"
  KEYWORDS_DEAL / KEYWORDS_ERROR  两套关键词，逗号分隔
  DEAL_*  / ERROR_*  两套阈值 (MIN_DISCOUNT / ABS_LOW / MIN_MARKET)
  TG_TOKEN / TG_CHAT  Telegram 通知
"""
import csv, json, os, re, statistics, time, urllib.parse, urllib.request
from playwright.sync_api import sync_playwright

# ================= 配置 =================
CI = os.getenv("CI_MODE") == "1"
PLATFORMS = [p.strip() for p in os.getenv("PLATFORMS", "amazon,shopee,lazada").split(",") if p.strip()]
AMAZON_DOMAIN = "amazon.sg"       # 价格 S$
SHOPEE_DOMAIN = "shopee.com.my"   # 价格 RM
LAZADA_DOMAIN = "lazada.com.my"   # 价格 RM


def _kw(name, default):
    return [k.strip() for k in os.getenv(name, default).split(",") if k.strip()]


def _f(name, default):
    return float(os.getenv(name, default))


# 两种模式: 普通优惠(日常好价/高折扣) 和 价格错误(贵价货被错标成极低价)
PROFILES = [
    {"name": "普通优惠", "title": "OFFER", "icon": "🔥",
     "keywords": _kw("KEYWORDS_DEAL", "water bottle,kettle,earphones,power bank"),
     "min_discount": _f("DEAL_MIN_DISCOUNT", "0.55"),
     "abs_low": _f("DEAL_ABS_LOW", "2"),
     "min_market": _f("DEAL_MIN_MARKET", "15")},
    {"name": "价格错误", "title": "PRICE ERROR", "icon": "🚨",
     "keywords": _kw("KEYWORDS_ERROR", "airpods pro,sony headphones,nintendo switch,dyson vacuum,laptop,iphone,ps5,gopro"),
     "min_discount": _f("ERROR_MIN_DISCOUNT", "0.70"),
     "abs_low": _f("ERROR_ABS_LOW", "0"),
     "min_market": _f("ERROR_MIN_MARKET", "100")},
]
MAX_DETAIL_CHECKS = 10            # 每个平台每个关键词最多进多少个商品页
INTERVAL_MIN = 20                 # 本地循环间隔(分钟)
HEADLESS = True if CI else False
PROFILE_DIR = "./browser_profile" # 本地保存登录状态
TELEGRAM_TOKEN = os.getenv("TG_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TG_CHAT", "")
ACCESSORY_WORDS = ["lid", "cover", "cap only", "spare", "replacement", "sticker", "strap", "gasket",
                   "ring", "compatible with", "refill", "case", "protector", "tempered", "skin",
                   "pouch", "holder", "adapter only", "for iphone", "for samsung", "for ipad", "盖", "配件", "贴纸", "替换", "单拍", "补差",
                   "邮费", "运费", "postage", "shipping fee"]
SEEN_FILE, CSV_FILE = "seen.json", "deals.csv"
# =======================================


def notify(msg):
    print(msg)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT:
        try:
            data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data, timeout=10)
        except Exception as e:
            print("telegram失败:", e)


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def to_price(text):
    if not text:
        return None
    m = re.search(r"(\d[\d,]*\.?\d*)", text.replace("\xa0", " "))
    return float(m.group(1).replace(",", "")) if m else None


def is_accessory(name, kw=""):
    """标题含配件词则视为配件；关键词里本来就有的词不算(搜 'phone case' 时 case 不是配件)"""
    n, k = (name or "").lower(), (kw or "").lower()
    for w in ACCESSORY_WORDS:
        if w in k:
            continue
        if w.isascii():
            if re.search(r"\b" + re.escape(w) + r"\b", n):
                return True
        elif w in n:
            return True
    return False


def relevant(name, kw):
    """标题必须包含关键词里的每个词，避免搜 'airpods pro' 却捞到壳/贴纸"""
    n = (name or "").lower()
    return all(t in n for t in re.findall(r"[a-z0-9]+", kw.lower()))


def capture(page, url, pattern, wait_ms=5000, scroll=True):
    got = []

    def on_resp(resp):
        if pattern in resp.url:
            try:
                got.append(resp.json())
            except Exception:
                pass

    page.on("response", on_resp)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(wait_ms)
        if scroll:
            for _ in range(3):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1200)
    except Exception as e:
        print("打开失败:", url, e)
    page.remove_listener("response", on_resp)
    return got


def amazon_captcha(page):
    try:
        html = page.content().lower()
        return "enter the characters you see below" in html or "/captcha/" in html
    except Exception:
        return False


def amazon_goto(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
    except Exception as e:
        print("打开失败:", url, e)
        return False
    if amazon_captcha(page):
        if CI:
            print("Amazon 验证码，本轮跳过")
            return False
        notify("⚠️ Amazon 出现验证码，请在浏览器里手动通过(最多等5分钟)")
        for _ in range(60):
            time.sleep(5)
            if not amazon_captcha(page):
                return True
        return False
    return True


# ---------------- Amazon ----------------
def amazon_search_page(page, kw, sort=None, extra=False):
    url = f"https://www.{AMAZON_DOMAIN}/s?k={urllib.parse.quote(kw)}"
    if sort:
        url += f"&s={sort}"
    if not amazon_goto(page, url):
        return []
    items = []
    for card in page.query_selector_all('div[data-component-type="s-search-result"]'):
        asin = card.get_attribute("data-asin")
        price_el = card.query_selector(".a-price:not(.a-text-price) .a-offscreen")
        price = to_price(price_el.inner_text() if price_el else "")
        if not asin or price is None:
            continue
        t = card.query_selector("h2")
        was_el = card.query_selector(".a-price.a-text-price .a-offscreen")
        items.append({"id": f"az{asin}", "name": t.inner_text().strip() if t else "",
                      "list_price": price, "was": to_price(was_el.inner_text() if was_el else ""),
                      "url": f"https://www.{AMAZON_DOMAIN}/dp/{asin}", "asin": asin, "platform": "Amazon SG",
                      "extra": extra})
    return items


def amazon_search(page, kw):
    """先按相关度搜(用来算市场中位价)，再按价格从低到高搜(只作候选，专门捞价格错误)"""
    items = amazon_search_page(page, kw)
    seen_ids = {i["id"] for i in items}
    for it in amazon_search_page(page, kw, sort="price-asc-rank", extra=True):
        if it["id"] not in seen_ids:
            items.append(it)
            seen_ids.add(it["id"])
    return items


def amazon_detail(page, item):
    """返回 (价格, 备注)"""
    if not amazon_goto(page, item["url"]):
        return None, "验证码/打开失败"
    txt = page.inner_text("body").lower()
    if "currently unavailable" in txt or "no featured offers" in txt:
        return None, "无库存/无buy box"
    el = (page.query_selector("#corePrice_feature_div .a-price .a-offscreen")
          or page.query_selector("#corePriceDisplay_desktop_feature_div .a-price .a-offscreen")
          or page.query_selector("#price_inside_buybox"))
    price = to_price(el.inner_text() if el else "")
    if price is None:
        return None, "读不到价格"
    notes = []
    t = page.query_selector("#productTitle")
    if is_accessory(t.inner_text() if t else item["name"], item.get("kw", "")):
        notes.append("疑似配件")
    seller = page.query_selector("#merchantInfoFeature_feature_div, #merchant-info")
    if seller:
        notes.append("卖家: " + seller.inner_text().strip().replace("\n", " ")[:80])
    if page.query_selector("#twister, #variation_size_name, #variation_color_name"):
        notes.append("有多个规格，请确认所选规格价格")
    cart = f"https://www.{AMAZON_DOMAIN}/gp/aws/cart/add.html?ASIN.1={item['asin']}&Quantity.1=1"
    notes.append(f"加购: {cart}")
    return price, "；".join(notes)


# ---------------- Shopee ----------------
def shopee_search(page, kw):
    url = f"https://{SHOPEE_DOMAIN}/search?keyword={urllib.parse.quote(kw)}&sortBy=relevancy"
    items = []
    for js in capture(page, url, "search_items"):
        for it in (js.get("items") or find_key(js, "items") or []):
            b = it.get("item_basic", it)
            if not b.get("itemid"):
                continue
            items.append({"id": f"sp{b['shopid']}_{b['itemid']}", "name": b.get("name", ""),
                          "list_price": (b.get("price_min") or b.get("price") or 0) / 100000, "was": None,
                          "url": f"https://{SHOPEE_DOMAIN}/product/{b['shopid']}/{b['itemid']}",
                          "platform": "Shopee MY"})
    return items


def models_real_price(models):
    ok = [m for m in models if m["stock"] > 0 and m["price"] is not None] or models
    if not ok:
        return None, "无规格数据"
    main = [m for m in ok if not is_accessory(m["name"])]
    note = "已排除疑似配件规格" if main and len(main) < len(ok) else ""
    prices = sorted(m["price"] for m in (main or ok))
    if len(prices) >= 2 and prices[0] < prices[-1] / 3:
        return statistics.median(prices), (note + " 规格价差>3倍,取中位价,请人工确认").strip()
    return prices[0], note


def shopee_detail(page, item):
    for js in capture(page, item["url"], "pdp/get_pc", wait_ms=4500, scroll=False):
        models = find_key(js, "models")
        if models:
            return models_real_price([{"name": m.get("name", ""), "price": (m.get("price") or 0) / 100000,
                                       "stock": m.get("stock", 0) or m.get("normal_stock", 0) or 0}
                                      for m in models])
        price = find_key(js, "price")
        if price is not None:
            return price / 100000, ""
    return None, "读不到详情"


# ---------------- Lazada ----------------
def lazada_search(page, kw):
    url = f"https://www.{LAZADA_DOMAIN}/catalog/?ajax=true&q={urllib.parse.quote(kw)}"
    items = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        data = json.loads(page.inner_text("body"))
    except Exception as e:
        print("Lazada搜索失败(可能触发验证码):", e)
        return items
    for it in (data.get("mods", {}).get("listItems") or []):
        try:
            link = it["itemUrl"]
            items.append({"id": f"lz{it.get('itemId') or link}", "name": it.get("name", ""),
                          "list_price": float(it.get("price") or 0), "was": None,
                          "url": ("https:" + link) if link.startswith("//") else link,
                          "platform": "Lazada MY"})
        except Exception:
            continue
    return items


def lazada_detail(page, item):
    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        skus = find_key(page.evaluate("window.__moduleData__"), "skuInfos") or {}
        out = []
        for sid, s in skus.items():
            if sid == "0" and len(skus) > 1:
                continue
            val = find_key(s.get("price", {}), "value")
            out.append({"name": s.get("skuName") or s.get("name") or "",
                        "price": float(val) if val is not None else None,
                        "stock": s.get("stock") if s.get("stock") is not None else 1})
        return models_real_price(out)
    except Exception as e:
        return None, f"详情失败: {e}"


ADAPTERS = {"amazon": (amazon_search, amazon_detail),
            "shopee": (shopee_search, shopee_detail),
            "lazada": (lazada_search, lazada_detail)}


# ---------------- 主流程 ----------------
def load_seen():
    return set(json.load(open(SEEN_FILE))) if os.path.exists(SEEN_FILE) else set()


def save_deal(row):
    if CI:
        return
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "platform", "name", "price", "market_median", "discount", "note", "url"])
        w.writerow(row)


def scan(page, platform, kw, seen, cfg):
    search, detail = ADAPTERS[platform]
    items = [i for i in search(page, kw) if i["list_price"] is not None]
    core = [i for i in items if not i.get("extra")]
    if len(core) < 5:
        print(f"[{platform}] '{kw}' 结果太少({len(core)})，跳过(可能被拦截)")
        return
    market = statistics.median([i["list_price"] for i in core if i["list_price"] > 0] or [0])
    if market < cfg["min_market"]:
        print(f"[{platform}] '{kw}' 市场中位价 {market:.2f} 太低，跳过")
        return
    cands = []
    for i in items:
        if i["id"] in seen or is_accessory(i["name"], kw):
            continue
        if platform == "amazon" and not relevant(i["name"], kw):
            continue
        shown = (1 - i["list_price"] / i["was"]) if i.get("was") else 0
        if i["list_price"] <= cfg["abs_low"] or i["list_price"] <= market * (1 - cfg["min_discount"]) or shown >= cfg["min_discount"]:
            cands.append(i)
    cands.sort(key=lambda i: i["list_price"])
    print(f"[{cfg['name']}|{platform}] '{kw}' 市场中位价 {market:.2f}，候选 {len(cands)} 个")
    for it in cands[:MAX_DETAIL_CHECKS]:
        it["kw"] = kw
        price, note = detail(page, it)
        seen.add(it["id"])
        if price is None:
            print("  跳过:", it["name"][:40], "-", note)
            continue
        disc = 1 - price / market
        if price <= cfg["abs_low"] or disc >= cfg["min_discount"]:
            notify(f"{cfg['icon']} {cfg['title']} | {it['platform']}\n{it['name'][:80]}\n"
                   f"价格 {price:.2f} (市场中位 {market:.2f}，便宜 {disc:.0%})\n"
                   f"{note}\n{it['url']}")
            save_deal([time.strftime("%F %T"), it["platform"], it["name"], round(price, 2),
                       round(market, 2), f"{disc:.0%}", note, it["url"]])
        time.sleep(2)


def run_round(page, seen):
    for cfg in PROFILES:
        for kw in cfg["keywords"]:
            for platform in PLATFORMS:
                if platform not in ADAPTERS:
                    print("未知平台:", platform)
                    continue
                try:
                    scan(page, platform, kw, seen, cfg)
                except Exception as e:
                    print("出错:", platform, kw, e)
                json.dump(list(seen), open(SEEN_FILE, "w"))
                time.sleep(4)


def main():
    seen = load_seen()
    locale = "en-SG" if PLATFORMS == ["amazon"] else "en-MY"
    with sync_playwright() as p:
        if CI:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(locale=locale, viewport={"width": 1280, "height": 900})
        else:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR, headless=HEADLESS, viewport={"width": 1280, "height": 900}, locale=locale)
        page = ctx.new_page()
        if CI:
            run_round(page, seen)
            return
        while True:
            run_round(page, seen)
            print(f"本轮结束，{INTERVAL_MIN} 分钟后再扫")
            time.sleep(INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
