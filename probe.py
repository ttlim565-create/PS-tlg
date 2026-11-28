"""站点探测: 对每个候选站点打开一个搜索页，记录是否被拦、有没有价格、页面结构，并保存 HTML。
只在 GitHub Actions 手动运行，用来决定哪些站点能写进监控脚本。"""
import json, os, re, time, urllib.parse
from playwright.sync_api import sync_playwright

OUT = "probe_out"
os.makedirs(OUT, exist_ok=True)
SITES = {
    "senheng": ("iphone 16", ["https://www.senheng.com.my/catalogsearch/result/?q={q}",
                              "https://www.senheng.com.my/search?q={q}"]),
    "harveynorman": ("iphone 16", ["https://www.harveynorman.com.my/catalogsearch/result/?q={q}",
                                   "https://www.harveynorman.com.my/search?q={q}",
                                   "https://www.harveynorman.com.my/search/?q={q}"]),
    "qoo10": ("iphone 16", ["https://www.qoo10.my/gmkt.inc/Search/?keyword={q}",
                            "https://www.qoo10.my/s/{q}",
                            "https://www.qoo10.sg/gmkt.inc/Search/?keyword={q}"]),
    "mudah": ("iphone 16", ["https://www.mudah.my/malaysia/all-ads?q={q}",
                            "https://www.mudah.my/malaysia?q={q}"]),
    "switch": ("iphone 16", ["https://www.switch.com.my/search?q={q}",
                             "https://www.switch.com.my/catalogsearch/result/?q={q}"]),
    "machines": ("iphone 16", ["https://www.machines.com.my/search?q={q}",
                               "https://www.machines.com.my/catalogsearch/result/?q={q}"]),
    "urbanrepublic": ("iphone 16", ["https://www.urbanrepublic.com.my/search?q={q}",
                                    "https://www.urbanrepublic.com.my/catalogsearch/result/?q={q}"]),
    "superbuy": ("iphone 16", ["https://www.superbuy.com.my/search?q={q}",
                               "https://www.superbuy.com/en/page/search?keyword={q}"]),
    "zalora": ("coach bag", ["https://www.zalora.com.my/catalog/?q={q}",
                             "https://www.zalora.com.my/search?q={q}"]),
    "ebay": ("iphone 16", ["https://www.ebay.com/sch/i.html?_nkw={q}&LH_BIN=1",
                           "https://www.ebay.com.my/sch/i.html?_nkw={q}"]),
}
BLOCK = ["captcha", "access denied", "verify you are human", "unusual traffic", "cf-challenge",
         "just a moment", "pardon our interruption", "forbidden", "robot"]
PRICE = re.compile(r"(RM|MYR|S\$|US\s?\$|\$)\s?\d[\d,]*(\.\d{1,2})?")

JS = """() => {
  const re = /(RM|MYR|S\\$|US\\s?\\$|\\$)\\s?\\d[\\d,]*(\\.\\d{1,2})?/;
  const out = []; const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 3) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 40 || !re.test(t)) continue;
    let p = el; let a = null;
    for (let i = 0; i < 6 && p; i++) { p = p.parentElement; if (p && p.querySelector('a[href]')) { a = p.querySelector('a[href]'); break; } }
    if (!p) continue;
    const key = p.className + '|' + (a ? a.href : '');
    if (seen.has(key)) continue; seen.add(key);
    out.push({price_text: t, price_cls: el.className, card_tag: p.tagName, card_cls: String(p.className).slice(0, 80),
              href: a ? a.href : '', card_text: (p.innerText || '').replace(/\\s+/g, ' ').slice(0, 140)});
    if (out.length >= 6) break;
  }
  return out;
}"""

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(locale="en-MY", viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    for site, (kw, templates) in SITES.items():
        for idx, tpl in enumerate(templates):
            url = tpl.format(q=urllib.parse.quote_plus(kw))
            info = {"site": site, "url": url}
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4500)
                for _ in range(2):
                    page.mouse.wheel(0, 2000); page.wait_for_timeout(800)
                html = page.content()
                open(f"{OUT}/{site}_{idx}.html", "w", encoding="utf-8").write(html)
                low = html.lower()
                ld = []
                for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
                    try:
                        j = json.loads(m.group(1)); ld.append(j.get("@type") if isinstance(j, dict) else "list")
                    except Exception:
                        ld.append("bad")
                info.update(status=resp.status if resp else None, final=page.url[:120], title=page.title()[:80],
                            html_len=len(html), block=[b for b in BLOCK if b in low],
                            price_mentions=len(PRICE.findall(html)), jsonld=ld[:8],
                            cards=page.evaluate(JS))
            except Exception as e:
                info["error"] = str(e)[:150]
            print("PROBE " + json.dumps(info, ensure_ascii=False), flush=True)
            time.sleep(2)
    browser.close()
