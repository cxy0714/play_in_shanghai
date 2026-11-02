# -*- coding: utf-8 -*-
"""Round 3 probe: lzmuseum.cn structure discovery (write UTF-8 json for GBK console)."""
import json
import re

import requests
from bs4 import BeautifulSoup

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
out = {}


def get(url):
    r = requests.get(url, headers=H, timeout=25)
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    return r


def snap(url, name):
    rec = {"url": url}
    try:
        r = get(url)
        rec["http"] = r.status_code
        rec["final"] = r.url
        rec["len"] = len(r.text)
        soup = BeautifulSoup(r.text, "html.parser")
        rec["title"] = soup.title.get_text(strip=True) if soup.title else ""
        links = []
        for a in soup.select("a[href]"):
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:40]
            href = a.get("href")
            if not text and not a.get("title"):
                continue
            links.append({"t": text or a.get("title"), "h": href})
        rec["links"] = links
        # 关键容器
        rec["classes"] = sorted({c for tag in soup.find_all(True) for c in (tag.get("class") or [])})[:0]
        rec["html_head"] = r.text[:1500]
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)[:200]
    out[name] = rec
    return rec


snap("https://www.lzmuseum.cn/", "home")
snap("https://www.lzmuseum.cn/YinJinZhanLan/2026695233690.html", "detail_congpu")

with open("scratch_probe_lz1.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done", {k: (v.get("http"), v.get("len")) for k, v in out.items()})