# -*- coding: utf-8 -*-
"""Round 3 probe: 浙博 / 杭州博物馆 / 苏博 / 中国丝绸博物馆 当前展览页."""
import json
import re
import warnings

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
SITES = {
    "zhejiangmuseum": [
        "https://www.zhejiangmuseum.com/",
        "http://www.zhejiangmuseum.com/",
        "https://www.zhejiangmuseum.com/Exhibition",
        "https://www.zhejiangmuseum.com/exhibition",
    ],
    "hzmuseum": [
        "https://www.hzmuseum.com/",
        "http://www.hzmuseum.com/",
        "https://www.hangzhoumuseum.cn/",
        "http://www.hangzhoumuseum.cn/",
    ],
    "szmuseum": [
        "https://www.szmuseum.com/",
        "http://www.szmuseum.com/",
        "https://www.szmuseum.com/Exhibition",
    ],
    "chinasilk": [
        "https://www.chinasilkmuseum.com/",
        "http://www.chinasilkmuseum.com/",
        "https://www.chinasilkmuseum.com/exhibition",
        "https://www.chinasilkmuseum.com/Exhibition",
    ],
}
out = {}
for key, urls in SITES.items():
    recs = []
    for url in urls:
        rec = {"url": url}
        try:
            r = requests.get(url, headers=H, timeout=20, verify=False, allow_redirects=True)
            rec["http"] = r.status_code
            rec["final"] = r.url
            rec["len"] = len(r.text)
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            rec["title"] = soup.title.get_text(strip=True) if soup.title else ""
            links = []
            for a in soup.select("a[href]"):
                text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
                if "展" in text and len(text) >= 4:
                    links.append({"t": text[:60], "h": a.get("href")})
            rec["exhibit_links"] = links[:25]
            rec["api_hints"] = sorted(set(re.findall(r"[/\w.-]*(?:api|json|list|exhibition)[/\w.?=&-]*", r.text, re.I)))[:15]
        except Exception as exc:  # noqa: BLE001
            rec["error"] = repr(exc)[:160]
        recs.append(rec)
        print(key, url, rec.get("http"), rec.get("len"), rec.get("title"), rec.get("error", ""), flush=True)
    out[key] = recs

with open("scratch_probe_museum_r3.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done")