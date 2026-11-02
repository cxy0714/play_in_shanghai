# -*- coding: utf-8 -*-
"""Round 3 probe 2: zhejiangmuseum raw shell + chinasilk exhibition list pages."""
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
out = {}


def get(url, timeout=25):
    r = requests.get(url, headers=H, timeout=timeout, verify=False)
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    return r


# 1) 浙博原始 HTML
try:
    r = get("https://www.zhejiangmuseum.com/")
    out["zhejiang_raw"] = {
        "http": r.status_code,
        "headers": dict(r.headers),
        "text": r.text[:2500],
    }
except Exception as exc:  # noqa: BLE001
    out["zhejiang_raw"] = {"error": repr(exc)[:200]}

# 2) 丝绸博物馆列表页
for path in ("/cs/list_164.aspx", "/Cloudzl/list_367.aspx", "/yz/list_18.aspx", "/zz/list_17.aspx"):
    key = path.strip("/").replace("/", "_")
    rec = {"url": "https://www.chinasilkmuseum.com" + path}
    try:
        r = get(rec["url"])
        rec["http"] = r.status_code
        rec["len"] = len(r.text)
        soup = BeautifulSoup(r.text, "html.parser")
        rec["title"] = soup.title.get_text(strip=True) if soup.title else ""
        items = []
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if not re.search(r"info_\d+\.aspx", href):
                continue
            t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            parent = a.find_parent(["li", "div", "tr"])
            ptext = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))[:200] if parent else ""
            items.append({"href": href, "t": t[:80], "ptext": ptext})
        rec["items"] = items[:20]
        rec["text"] = re.sub(r"\n{2,}", "\n", soup.get_text("\n", strip=True))[:1500]
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)[:160]
    out[key] = rec
    print(key, rec.get("http"), rec.get("len"), rec.get("title"), len(rec.get("items") or []), flush=True)

with open("scratch_probe_museum_r3b.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done")