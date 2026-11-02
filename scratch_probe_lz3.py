# -*- coding: utf-8 -*-
"""Round 3 probe: dump all lzmuseum 引进展览 article pages (title/body/dates/img)."""
import json
import re

import requests
from bs4 import BeautifulSoup

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
BASE = "https://www.lzmuseum.cn"


def get(url):
    r = requests.get(url, headers=H, timeout=25)
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    return r


r = get(BASE + "/YinJinZhanLan/index.html")
soup = BeautifulSoup(r.text, "html.parser")
links = []
for a in soup.select("a[href]"):
    href = a.get("href") or ""
    if not re.search(r"/YinJinZhanLan/\d+\.html$", href):
        continue
    if href in links:
        continue
    links.append(href)

out = {"links": links, "articles": []}
for href in links:
    rec = {"href": href}
    try:
        page = get(BASE + href)
        ps = BeautifulSoup(page.text, "html.parser")
        rec["http"] = page.status_code
        rec["title_tag"] = ps.title.get_text(strip=True) if ps.title else ""
        imgs = [i.get("src") for i in ps.select("img[src]") if not (i.get("src") or "").startswith("/images/")]
        rec["imgs"] = imgs[-4:]
        text = re.sub(r"[ \t\u3000]+", " ", ps.get_text("\n", strip=True))
        rec["text"] = re.sub(r"\n{2,}", "\n", text)[:4000]
        rec["date_tokens"] = re.findall(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[./-]\d{1,2}[./-]\d{1,2}|展期[^\n。]{0,40}", rec["text"])[:20]
        rec["quoted"] = re.findall(r"[“\"]([^”\"]{3,40})[”\"]", rec["text"])[:8]
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)[:160]
    out["articles"].append(rec)
    print(href, rec.get("http"), rec.get("title_tag"), (rec.get("date_tokens") or [])[:4], flush=True)

with open("scratch_probe_lz3.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done", len(links))