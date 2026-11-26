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
     "min_market": _f("DEAL_MIN_MARKET", "15"),
     "ref_ratio": 0},
    {"name": "价格错误", "title": "PRICE ERROR", "icon": "🚨",
     "keywords": _kw("KEYWORDS_ERROR", "airpods pro,sony headphones,nintendo switch,dyson vacuum,laptop,iphone,ps5,gopro"),
     "min_discount": _f("ERROR_MIN_DISCOUNT", "0.70"),
     "abs_low": _f("ERROR_ABS_LOW", "0"),
     "min_market": _f("ERROR_MIN_MARKET", "100"),
     "ref_ratio": _f("ERROR_REF_RATIO", "0.5")},  # 商品自己的原价必须 >= 市场中位价的这个比例，否则是另一类商品/配件
]
MAX_DETAIL_CHECKS = 10            # 每个平台每个关键词最多进多少个商品页
INTERVAL_MIN = 20                 # 本地循环间隔(分钟)
HEADLESS = True if CI else False
PROFILE_DIR = "./browser_profile" # 本地保存登录状态
TELEGRAM_TOKEN = os.getenv("TG_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TG_CHAT", "")
# 强配件词: 标题出现就当配件(关键词本身含该词则豁免)
ACCESSORY_WORDS = ["lid", "cover", "cap only", "spare", "replacement", "sticker", "strap", "gasket",
                   "ring", "compatible with", "refill", "case", "protector", "tempered", "skin",
                   "pouch", "holder", "adapter only", "accessories", "accessory", "mount", "mounting",
                   "bracket", "tripod", "selfie", "housing", "filter", "lens cap", "memory card", "sd card",
                   "microsd", "backpack", "sleeve", "carrying", "grip", "handle", "clamp", "suction",
                   "ear tips", "earpads", "ear pads", "silicone", "keycap", "decal", "wrap",
                   "盖", "配件", "贴纸", "替换", "单拍", "补差", "邮费", "运费", "postage", "shipping fee"]
# 弱配件词: 很多主商品也会带(如"充电宝自带线")，只在标题前6个词里出现才算配件
WEAK_ACCESSORY_WORDS = ["cable", "charger", "adapter", "battery", "batteries", "stand", "band", "clip",
                        "pad", "pads", "tips", "remote", "dock", "bag", "controller", "charging", "kit"]
SEEN_FILE, CSV_FILE, HISTORY_FILE = "seen.json", "deals.csv", "history.json"
ALERT_TTL = float(os.getenv("ALERT_TTL_HOURS", "24")) * 3600  # 同一商品推送后，多久内不再重复推送
CHECK_TTL = 6 * 3600                                         # 检查过但没达标的商品，多久后再复查
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
    """判断标题是不是配件/周边，不是关键词指的主商品。关键词里本来就有的词不算(搜 'phone case' 时 case 不是配件)"""
    n, k = (name or "").lower(), (kw or "").lower()
    n = n.replace("charging case", " ")  # AirPods 自带的充电盒是主商品的一部分，不算配件
    ktoks = re.findall(r"[a-z0-9]+", k)
    # 规则1: "for / compatible with ... <品牌词>" 说明是给该品牌配套的东西
    if ktoks:
        brand = re.escape(ktoks[0])
        if re.search(r"\b(for|fits?|compatible with|suitable for|replacement for|designed for)\s+"
                     r"(?:the\s+|new\s+|all\s+)?(?:\w+\s+){0,2}?" + brand + r"\b", n):
            return True
    # 规则2: 强配件词
    for w in ACCESSORY_WORDS:
        if w in k:
            continue
        if w.isascii():
            if re.search(r"\b" + re.escape(w) + r"\b", n):
                return True
        elif w in n:
            return True
    # 规则3: 弱配件词只看标题前6个词
    head = " ".join(re.findall(r"[a-z0-9]+", n)[:6])
    for w in WEAK_ACCESSORY_WORDS:
        if w not in k and re.search(r"\b" + re.escape(w) + r"\b", head):
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
                          "list_price": (b.get("price_min") or b.get("price") or 0) / 100000,
                          "was": ((b.get("price_max_before_discount") or b.get("price_before_discount") or 0) / 100000) or None,
                          "url": f"https://{SHOPEE_DOMAIN}/product/{b['shopid']}/{b['itemid']}",
                          "platform": "Shopee MY"})
    return items


def models_real_price(models):
    """多规格商品: 返回 (真实可买的最低价, 备注)；规格混着不同商品(如相机+防水壳+电池)时直接跳过。
    models: [{name, price, stock}]，包含售罄规格——售罄规格用来判断这个链接「主商品」的价格水平。"""
    valid = [m for m in models if m["price"] is not None and m["price"] > 0]
    if not valid:
        return None, "无规格数据"
    in_stock = [m for m in valid if m["stock"] > 0]
    if not in_stock:
        return None, "全部规格售罄"
    main = [m for m in in_stock if not is_accessory(m["name"])]
    if not main:
        return None, "在售规格均疑似配件"
    ref_max = max(m["price"] for m in valid)  # 含售罄规格
    lo = min(m["price"] for m in main)
    summary = ""
    if len(valid) > 1:
        summary = " | ".join(f"{m['name'][:16]} {m['price']:.0f}" + ("(售罄)" if m["stock"] <= 0 else "")
                             for m in valid[:6])
    if len(valid) >= 2 and lo < ref_max / 3:
        return None, f"规格混合(价差>3倍，疑似含配件/不同商品)，已跳过: {summary}"
    note = ("已排除疑似配件规格; " if len(main) < len(in_stock) else "") + (f"规格: {summary}" if summary else "")
    return lo, note.strip()


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
                          "list_price": float(it.get("price") or 0), "was": to_price(str(it.get("originalPrice") or "")),
                          "url": ("https:" + link) if link.startswith("//") else link,
                          "platform": "Lazada MY"})
        except Exception:
            continue
    return items


def sale_price(pr):
    """Lazada sku 的 price 字段: 优先 salePrice(售价)；取不到就取里面所有数值的最小值(售价 <= 原价)"""
    if not isinstance(pr, dict):
        return None
    sp = pr.get("salePrice")
    if isinstance(sp, dict):
        v = sp.get("value")
        if v is None:
            v = to_price(str(sp.get("text") or ""))
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    vals = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "value":
                    try:
                        vals.append(float(v))
                    except (TypeError, ValueError):
                        pass
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(pr)
    return min(vals) if vals else None


def lazada_detail(page, item):
    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        skus = find_key(page.evaluate("window.__moduleData__"), "skuInfos") or {}
        out = []
        for sid, s in skus.items():
            if sid == "0" and len(skus) > 1:
                continue
            out.append({"name": s.get("skuName") or s.get("name") or "",
                        "price": sale_price(s.get("price")),
                        "stock": s.get("stock") if s.get("stock") is not None else 1})
        return models_real_price(out)
    except Exception as e:
        return None, f"详情失败: {e}"


ADAPTERS = {"amazon": (amazon_search, amazon_detail),
            "shopee": (shopee_search, shopee_detail),
            "lazada": (lazada_search, lazada_detail)}


# ---------------- 主流程 ----------------
def load_seen():
    """seen: {商品id: {"t": 时间戳, "a": 是否已推送}}"""
    if not os.path.exists(SEEN_FILE):
        return {}
    data = json.load(open(SEEN_FILE))
    if isinstance(data, list):  # 兼容旧格式
        return {i: {"t": time.time(), "a": False} for i in data}
    return data


def is_seen(seen, item_id):
    e = seen.get(item_id)
    if not e:
        return False
    return time.time() - e["t"] < (ALERT_TTL if e["a"] else CHECK_TTL)


def mark(seen, item_id, alerted):
    seen[item_id] = {"t": time.time(), "a": alerted}


def save_seen(seen):
    cutoff = time.time() - max(ALERT_TTL, CHECK_TTL) * 2
    for k in [k for k, e in seen.items() if e["t"] < cutoff]:
        del seen[k]
    json.dump(seen, open(SEEN_FILE, "w"))


HISTORY = {}  # {商品id: {"min": 最低价, "min_t": 时间, "first": 首次记录时间, "last": 最近记录时间, "n": 记录次数}}


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            return json.load(open(HISTORY_FILE))
        except Exception:
            return {}
    return {}


def record_price(item_id, price):
    if price is None or price < 0:
        return
    now = time.time()
    e = HISTORY.get(item_id)
    if not e:
        HISTORY[item_id] = {"min": price, "min_t": now, "first": now, "last": now, "n": 1}
        return
    e["n"] += 1
    e["last"] = now
    if price < e["min"]:
        e["min"], e["min_t"] = price, now


def save_history():
    cutoff = time.time() - 60 * 86400  # 60天没出现过的商品清掉
    for k in [k for k, e in HISTORY.items() if e["last"] < cutoff]:
        del HISTORY[k]
    json.dump(HISTORY, open(HISTORY_FILE, "w"))


def history_line(info, price):
    """info 是本轮扫描前的历史快照"""
    if not info:
        return "历史最低: 首次记录，暂无历史"
    days = int((time.time() - info["first"]) / 86400)
    txt = f"历史最低(自记录{days}天/{info['n']}次): {info['min']:.2f}"
    if price < info["min"]:
        txt += " ← 🆕这次是新低"
    return txt


def save_deal(row):
    if CI:
        return
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "platform", "name", "price", "market_median", "discount", "note", "url"])
        w.writerow(row)


def ref_check(item, market, cfg):
    """价格错误模式: 商品自己的原价应该接近市场水平；原价本身就远低于市场中位价 = 另一类商品/配件，不是错价"""
    ratio = cfg.get("ref_ratio", 0)
    if not ratio:
        return True, ""
    was = item.get("was")
    if not was:
        return True, "无原价佐证，请人工确认是否真为错价"
    if was < market * ratio:
        return False, f"原价{was:.0f}远低于市场中位价{market:.0f}，属另一类商品/配件，非价格错误"
    return True, ""


def scan(page, platform, kw, seen, cfg):
    search, detail = ADAPTERS[platform]
    items = [i for i in search(page, kw) if i["list_price"] is not None]
    prior = {i["id"]: dict(HISTORY[i["id"]]) for i in items if i["id"] in HISTORY}  # 本轮前的历史快照
    if platform == "amazon":  # Amazon 搜索页价格可靠，全部记录；Shopee/Lazada 只记录进详情页核实过的价格
        for i in items:
            record_price(i["id"], i["list_price"])
    # 市场中位价只用主商品算: 去掉配件(以及 Amazon 上标题不含关键词的无关商品)
    core = [i for i in items if not i.get("extra") and not is_accessory(i["name"], kw)
            and (platform != "amazon" or relevant(i["name"], kw))]
    if len(core) < 5:
        print(f"[{platform}] '{kw}' 结果太少({len(core)})，跳过(可能被拦截)")
        return
    market = statistics.median([i["list_price"] for i in core if i["list_price"] > 0] or [0])
    if market < cfg["min_market"]:
        print(f"[{platform}] '{kw}' 市场中位价 {market:.2f} 太低，跳过")
        return
    cands = []
    for i in items:
        if is_seen(seen, i["id"]) or is_accessory(i["name"], kw):
            continue
        if not ref_check(i, market, cfg)[0]:
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
        mark(seen, it["id"], False)
        if price is None:
            print("  跳过:", it["name"][:40], "-", note)
            continue
        if it["id"] not in prior and it["id"] in HISTORY and platform != "amazon":
            prior[it["id"]] = dict(HISTORY[it["id"]])
        record_price(it["id"], price)
        disc = 1 - price / market
        if price <= cfg["abs_low"] or disc >= cfg["min_discount"]:
            ok, ref_note = ref_check(it, market, cfg)
            if not ok:
                print("  跳过:", it["name"][:40], "-", ref_note)
                continue
            if ref_note:
                note = (note + "; " if note else "") + ref_note
            notify(f"{cfg['icon']} {cfg['title']} | {it['platform']}\n{it['name'][:80]}\n"
                   f"价格 {price:.2f} (市场中位 {market:.2f}，便宜 {disc:.0%})\n"
                   f"{history_line(prior.get(it['id']), price)}\n"
                   f"{note}\n{it['url']}")
            mark(seen, it["id"], True)
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
                save_seen(seen)
                save_history()
                time.sleep(4)


def main():
    seen = load_seen()
    HISTORY.update(load_history())
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
