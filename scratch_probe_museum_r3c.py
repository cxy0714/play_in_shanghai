# -*- coding: utf-8 -*-
"""Round 3 probe 3: silk museum detail dates, zhejiangmuseum SPA api, szmuseum retry."""
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


def get(url, timeout=25, verify=False):
    r = requests.get(url, headers=H, timeout=timeout, verify=verify)
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    return r


# 1) 丝绸博物馆详情页
for itemid in (31766, 31764, 31765, 31735):
    rec = {}
    url = f"https://www.chinasilkmuseum.com/zz/info_17.aspx?itemid={itemid}"
    try:
        r = get(url)
        soup = BeautifulSoup(r.text, "html.parser")
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        rec = {
            "url": url,
            "http": r.status_code,
            "title": soup.title.get_text(strip=True) if soup.title else "",
            "h1": re.sub(r"\s+", " ", soup.select_one("h1,h2,.title,.bt").get_text(" ", strip=True))
            if soup.select_one("h1,h2,.title,.bt")
            else "",
            "date_matches": re.findall(r"(?:展览时间|展期|开幕)[：: ]*[^\s，。；]{4,40}", text)[:6],
            "text": text[:1200],
            "imgs": [i.get("src") for i in soup.select("img[src]")][:6],
        }
    except Exception as exc:  # noqa: BLE001
        rec = {"url": url, "error": repr(exc)[:160]}
    out[f"silk_{itemid}"] = rec
    print("silk", itemid, rec.get("http"), rec.get("date_matches"), flush=True)

# 2) 浙博 SPA: 找 API
try:
    r = get("https://www.zhejiangmuseum.com/")
    js = re.findall(r"static/js/[^\"]+\.js", r.text)
    out["zj_js"] = js
    app = next((j for j in js if "app." in j), js[0] if js else None)
    if app:
        r2 = get("https://www.zhejiangmuseum.com/" + app)
        out["zj_app_len"] = len(r2.text)
        out["zj_api_hints"] = sorted(set(re.findall(r"[\"'](/[a-zA-Z0-9_/-]*(?:api|exhibit|list|portal)[a-zA-Z0-9_/-]*)[\"']", r2.text)))[:40]
        out["zj_domains"] = sorted(set(re.findall(r"https?://[a-zA-Z0-9.-]+", r2.text)))[:20]
except Exception as exc:  # noqa: BLE001
    out["zj_error"] = repr(exc)[:200]

# 3) 苏博重试
try:
    r = get("https://www.szmuseum.com/", timeout=25)
    out["szmuseum"] = {"http": r.status_code, "len": len(r.text), "title": BeautifulSoup(r.text, "html.parser").title.get_text(strip=True) if "<title" in r.text else ""}
except Exception as exc:  # noqa: BLE001
    out["szmuseum"] = {"error": repr(exc)[:200]}

with open("scratch_probe_museum_r3c.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done")