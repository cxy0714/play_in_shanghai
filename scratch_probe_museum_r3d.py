# -*- coding: utf-8 -*-
"""Round 3 probe 4: silk 在展 list anchors + zhejiangmuseum api guesses."""
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


# silk 在展 list
for path in ("/cs/list_164.aspx", "/zz/list_17.aspx"):
    key = path.strip("/").replace("/", "_")
    try:
        r = get("https://www.chinasilkmuseum.com" + path)
        soup = BeautifulSoup(r.text, "html.parser")
        anchors = []
        for a in soup.select("a[href]"):
            anchors.append({"t": re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:60], "h": a.get("href")})
        out[key] = {"http": r.status_code, "title": soup.title.get_text(strip=True) if soup.title else "", "anchors": anchors[:60]}
    except Exception as exc:  # noqa: BLE001
        out[key] = {"error": repr(exc)[:160]}
    print(key, out[key].get("http"), len(out[key].get("anchors") or []), flush=True)

# 浙博 api 猜测
guesses = [
    "https://www.zhejiangmuseum.com/api/exhibition/list",
    "https://www.zhejiangmuseum.com/api/exhibit/list",
    "https://www.zhejiangmuseum.com/prod-api/exhibition/list",
    "https://www.zhejiangmuseum.com/api/site/exhibition",
    "https://www.zhejiangmuseum.com/static/js/app.3c0c0d72.js",
]
zres = []
for url in guesses:
    rec = {"url": url}
    try:
        r = get(url, timeout=15)
        rec["http"] = r.status_code
        rec["len"] = len(r.text)
        rec["head"] = r.text[:180]
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)[:120]
    zres.append(rec)
    print(url, rec.get("http"), rec.get("len"), rec.get("error", ""), flush=True)
out["zj"] = zres

# 苏博再试一次（不同 UA / http2 无关）
try:
    r = requests.get("https://www.szmuseum.com/", headers={**H, "Connection": "close"}, timeout=25, verify=False)
    out["szmuseum_home"] = {"http": r.status_code, "len": len(r.text)}
except Exception as exc:  # noqa: BLE001
    out["szmuseum_home"] = {"error": repr(exc)[:160]}

with open("scratch_probe_museum_r3d.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done")