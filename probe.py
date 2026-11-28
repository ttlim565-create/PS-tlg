"""一次性探测: 从 GitHub Actions 机器打开各网站搜索页，看能不能读到价格(只打印摘要，不保存页面)"""
import re, urllib.parse
from playwright.sync_api import sync_playwright

SITES = {
 "senheng":    ["https://www.senheng.com.my/catalogsearch/result/?q=playstation+5", "https://www.senheng.com.my/search?q=playstation+5"],
 "harveynorman": ["https://www.harveynorman.com.my/catalogsearch/result/?q=playstation+5", "https://www.harveynorman.com.my/search?q=playstation+5"],
 "qoo10_my":   ["https://www.qoo10.my/gmkt.inc/Search/Default.aspx?keyword=playstation+5"],
 "qoo10_sg":   ["https://www.qoo10.sg/gmkt.inc/Search/Default.aspx?keyword=playstation+5"],
 "mudah":      ["https://www.mudah.my/malaysia/all?q=playstation+5"],
 "switch":     ["https://switch.com.my/search?q=playstation+5", "https://www.switch.com.my/search?q=playstation+5"],
 "machines":   ["https://machines.com.my/search?q=iphone", "https://www.machines.com.my/catalogsearch/result/?q=iphone"],
 "urbanrepublic": ["https://www.urbanrepublic.com.my/search?q=iphone", "https://www.urbanrepublic.com.my/catalogsearch/result/?q=iphone"],
 "superbuy_my": ["https://www.superbuy.com.my/search?q=iphone", "https://superbuy.com.my/"],
 "zalora":     ["https://www.zalora.com.my/catalog/?q=gucci"],
 "ebay_my":    ["https://www.ebay.com.my/sch/i.html?_nkw=playstation+5"],
 "ebay_com":   ["https://www.ebay.com/sch/i.html?_nkw=playstation+5"],
}
BLOCK = re.compile(r"captcha|access denied|just a moment|verify you are human|unusual traffic|robot|forbidden|blocked", re.I)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(locale="en-MY", viewport={"width": 1280, "height": 900})
    for name, urls in SITES.items():
        for u in urls:
            page = ctx.new_page()
            try:
                resp = page.goto(u, wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(5000)
                html = page.content()
                txt = page.inner_text("body")[:6000]
                prices = re.findall(r"(?:RM|MYR|S\$|US\s?\$|\$)\s?\d[\d,]*\.?\d*", txt)
                ld = len(re.findall(r'application/ld\+json', html))
                prod_ld = len(re.findall(r'"@type"\s*:\s*"Product"', html))
                print(f"[{name}] status={resp.status if resp else None} final={page.url[:90]}")
                print(f"   title={page.title()[:70]!r} html={len(html)} price_texts={len(prices)} sample={prices[:4]} ld+json={ld} product_ld={prod_ld}")
                print(f"   block_marker={bool(BLOCK.search(txt))} text={re.sub(r'\\s+',' ',txt)[:160]!r}")
                if resp and resp.status < 400 and (prices or prod_ld):
                    page.close(); break
            except Exception as e:
                print(f"[{name}] ERROR {u[:70]} -> {str(e)[:100]}")
            page.close()
    b.close()
