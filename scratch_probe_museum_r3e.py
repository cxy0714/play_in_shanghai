# -*- coding: utf-8 -*-
"""Round 3 probe 5: silk 在展/将展 links + szmuseum 当前展览 page structure."""
import json
import re
import warnings

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "close",
}
out = {}


def get(url, timeout=25):
    r = requests.get(url, headers=H, timeout=timeout, verify=False)
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    return r


# 丝绸：找 在展/已展/将展 链接
r = get("https://www.chinasilkmuseum.com/")
soup = BeautifulSoup(r.text, "html.parser")
nav = []
for a in soup.select("a[href]"):
    t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
    if t in ("在展", "已展", "将展", "云上展览") or "在展" in t[:6]:
        nav.append({"t": t, "h": a.get("href")})
out["silk_nav"] = nav
print("silk nav", nav, flush=True)

# 丝绸：抓 list_18 全部锚点里带 info 的
try:
    r2 = get("https://www.chinasilkmuseum.com/yz/list_18.aspx")
    s2 = BeautifulSoup(r2.text, "html.parser")
    links = []
    for a in s2.select("a[href]"):
        h = a.get("href") or ""
        if "info_" in h:
            links.append({"t": re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:80], "h": h})
    out["silk_list18"] = links[:30]
    out["silk_list18_len"] = len(r2.text)
except Exception as exc:  # noqa: BLE001
    out["silk_list18_err"] = repr(exc)[:160]
print("silk list18", len(out.get("silk_list18") or []), flush=True)

# 苏博：主页 + 找展览栏目
try:
    r3 = get("https://www.szmuseum.com/")
    s3 = BeautifulSoup(r3.text, "html.parser")
    out["sz_home_title"] = s3.title.get_text(strip=True) if s3.title else ""
    cand = []
    for a in s3.select("a[href]"):
        t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        h = a.get("href") or ""
        if "展" in t or "exhibit" in h.lower():
            cand.append({"t": t[:40], "h": h})
    out["sz_links"] = cand[:40]
    out["sz_html_len"] = len(r3.text)
except Exception as exc:  # noqa: BLE001
    out["sz_home_err"] = repr(exc)[:200]

with open("scratch_probe_museum_r3e.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done")