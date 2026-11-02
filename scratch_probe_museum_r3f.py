# -*- coding: utf-8 -*-
"""Round 3 probe 6: szmuseum 临时展览/展览 pages structure + silk list_17 items."""
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


def dump(url, key):
    rec = {"url": url}
    try:
        r = get(url)
        rec["http"] = r.status_code
        rec["len"] = len(r.text)
        soup = BeautifulSoup(r.text, "html.parser")
        rec["title"] = soup.title.get_text(strip=True) if soup.title else ""
        rec["text"] = re.sub(r"\n{2,}", "\n", soup.get_text("\n", strip=True))[:2500]
        blocks = []
        for a in soup.select("a[href]"):
            h = a.get("href") or ""
            t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:80]
            if re.search(r"/Exhibition/|infoid|Details", h):
                blocks.append({"h": h, "t": t})
        rec["blocks"] = blocks[:40]
        rec["imgs"] = [i.get("src") for i in soup.select("img[src]")][:8]
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)[:180]
    out[key] = rec
    print(key, rec.get("http"), rec.get("len"), rec.get("title"), len(rec.get("blocks") or []), flush=True)


dump("https://www.szmuseum.com/Exhibition/Temporary", "sz_temporary")
dump("https://www.szmuseum.com/Exhibition/Index", "sz_index")

# 丝绸在展 list_17 的条目文本（含展览时间/地点）
try:
    r = get("https://www.chinasilkmuseum.com/zz/list_17.aspx")
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    seen = set()
    for a in soup.select("a[href]"):
        h = a.get("href") or ""
        if "info_17" not in h and "info_18" not in h:
            continue
        if h in seen:
            continue
        seen.add(h)
        parent = a.find_parent(["li", "div", "tr"])
        ptext = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)) if parent else ""
        img = parent.select_one("img") if parent else None
        items.append({"h": h, "ptext": ptext[:220], "img": (img.get("src") if img else "")})
    out["silk_current"] = items[:20]
    print("silk_current", len(items), flush=True)
except Exception as exc:  # noqa: BLE001
    out["silk_current_err"] = repr(exc)[:160]

with open("scratch_probe_museum_r3f.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("done")