# -*- coding: utf-8 -*-
"""Round 3 probe: lzmuseum list pages + detail page structure."""
import json
import re

import requests
from bs4 import BeautifulSoup

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
BASE = "https://www.lzmuseum.cn"
out = {}


def get(url):
    r = requests.get(url, headers=H, timeout=25)
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    return r


def dump_list(path):
    key = path.strip("/").replace("/", "_") or "home"
    rec = {}
    url = BASE + path
    try:
        r = get(url)
        rec["url"] = url
        rec["http"] = r.status_code
        rec["len"] = len(r.text)
        soup = BeautifulSoup(r.text, "html.parser")
        rec["title"] = soup.title.get_text(strip=True) if soup.title else ""
        items = []
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if not re.search(r"/\d+\.html$", href):
                continue
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            parent = a.find_parent(["li", "div", "tr"])
            pcls = " ".join(parent.get("class") or []) if parent else ""
            img = parent.select_one("img") if parent else None
            img_src = img.get("src") if img and img.get("src") else ""
            items.append({"href": href, "text": text[:120], "pclass": pcls, "img": img_src})
        rec["items"] = items
        # 分页链接
        rec["pages"] = [
            (a.get_text(strip=True), a.get("href"))
            for a in soup.select("a[href]")
            if re.search(r"(index_\d+|list|page)", a.get("href") or "") and a.get_text(strip=True).isdigit()
        ][:10]
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)[:200]
    out[key] = rec
    print(key, rec.get("http"), rec.get("len"), rec.get("title"), "items", len(rec.get("items") or []))


for path in ("/YinJinZhanLan/index.html", "/ZhanLanXinXi/index.html", "/ZhanLanHuiGu/index.html", "/DuiWaiZhanLan/index.html"):
    dump_list(path)

# 详情页结构
try:
    r = get(BASE + "/YinJinZhanLan/2026695233690.html")
    soup = BeautifulSoup(r.text, "html.parser")
    det = {"http": r.status_code, "title": soup.title.get_text(strip=True) if soup.title else ""}
    content = soup.select_one(".content") or soup.select_one(".article") or soup.select_one("#content")
    det["main_text"] = re.sub(r"\n{2,}", "\n", soup.get_text("\n", strip=True))[:3000]
    det["imgs"] = [img.get("src") for img in soup.select("img[src]")][:10]
    det["candidates"] = []
    for sel in (".xqnr", ".xq_title", ".title", ".news_title", ".content", ".article_con", ".detaile", ".xq"):
        for el in soup.select(sel)[:3]:
            det["candidates"].append(
                {"sel": sel, "text": re.sub(r"\s+", " ", el.get_text(" ", strip=True))[:200]}
            )
    out["detail"] = det
except Exception as exc:  # noqa: BLE001
    out["detail"] = {"error": repr(exc)[:200]}

with open("scratch_probe_lz2.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done")