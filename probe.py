"""站点探测: 对每个候选站点打开一个搜索页，记录是否被拦、有没有价格、页面结构，并保存 HTML。
只在 GitHub Actions 手动运行，用来决定哪些站点能写进监控脚本。"""
import json, os, re, time, urllib.parse
from playwright.sync_api import sync_playwright

OUT = "probe_out"
os.makedirs(OUT, exist_ok=True)
SITES = {
    "switch": ("iphone 16", ["https://shop.switch.com.my/search?q={q}"]),
    "machines": ("iphone 16", ["https://www.machines.com.my/search?q={q}"]),
    "urbanrepublic": ("iphone 16", ["https://shop.urbanrepublic.com.my/search?q={q}"]),
    "mudah": ("iphone 16", ["https://www.mudah.my/malaysia/all?q={q}"]),
    "zalora": ("coach", ["https://www.zalora.com.my/c?q={q}"]),
    "senheng": ("iphone 16", ["https://www.senheng.com.my/?s={q}&post_type=product",
                              "https://www.senheng.com.my/shop/?s={q}&post_type=product"]),
}
LINKSEL = {"switch": "a[href*='/products/']", "machines": "a[href*='/products/']", "urbanrepublic": "a[href*='/products/']",
           "mudah": "a[href$='.htm']", "zalora": "a[href*='/p/']", "senheng": "li.product a, .product a[href*='/shop/'], a[href*='/product/']"}
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
                ldp = []
                for m2 in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
                    if '"Product"' in m2.group(1) and len(ldp) < 2:
                        ldp.append(re.sub(r"\s+", " ", m2.group(1))[:900])
                card_html = page.evaluate("""(sel) => {
                  const links = [...document.querySelectorAll(sel)].filter(a => /RM|\\$/.test((a.closest('li,div,article')||a).innerText||''));
                  if (!links.length) return '';
                  let el = links[0];
                  for (let i = 0; i < 6 && el.parentElement; i++) {
                    const p = el.parentElement; const hs = new Set([...p.querySelectorAll(sel)].map(x => x.getAttribute('href')));
                    if (hs.size > 1) break; el = p;
                  }
                  return el.outerHTML.replace(/\\s+/g, ' ').slice(0, 1800);
                }""", LINKSEL.get(site, "a"))
                info.update(ldp=ldp, card_html=card_html, status=resp.status if resp else None, final=page.url[:120], title=page.title()[:80],
                            html_len=len(html), block=[b for b in BLOCK if b in low],
                            price_mentions=len(PRICE.findall(html)), jsonld=ld[:8],
                            cards=page.evaluate(JS))
            except Exception as e:
                info["error"] = str(e)[:150]
            print("PROBE " + json.dumps(info, ensure_ascii=False), flush=True)
            time.sleep(2)
    browser.close()
