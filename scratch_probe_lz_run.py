# -*- coding: utf-8 -*-
"""Round 3 full run: all national sources incl. the 3 new museums."""
import json
import time
from collections import Counter
from pathlib import Path

import national_sources as ns

cfg = json.loads(Path("config/sources.json").read_text(encoding="utf-8"))
rows = []
all_items = []
t0_all = time.time()
for source in cfg["sources"]:
    fn = ns.NATIONAL_ADAPTERS.get(source.get("adapter"))
    if not fn:
        continue
    t0 = time.time()
    try:
        items = fn(source) or []
        err = ""
    except Exception as exc:  # noqa: BLE001
        items, err = [], repr(exc)[:120]
    sec = round(time.time() - t0, 2)
    for item in items:
        item["_src"] = source["name"]
    all_items.extend(items)
    rows.append(
        {
            "name": source["name"],
            "adapter": source.get("adapter"),
            "site": source.get("site", ""),
            "city": source.get("city", ""),
            "n": len(items),
            "sec": sec,
            "cities": dict(Counter(i.get("city") for i in items)),
            "error": err,
        }
    )
    print(f"{source['name']}\t{len(items)}\t{sec}s\t{dict(Counter(i.get('city') for i in items))}", flush=True)
total_sec = round(time.time() - t0_all, 1)
summary = {
    "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "sources": len(rows),
    "items": len(all_items),
    "seconds": total_sec,
    "by_category": dict(Counter(i["category"] for i in all_items)),
    "rows": rows,
}
Path("scratch_probe_lz_run.json").write_text(
    json.dumps({"summary": summary, "items": all_items}, ensure_ascii=False), encoding="utf-8"
)
sh = [i for i in all_items if i.get("city") == "上海"]
print("TOTAL", len(all_items), "in", total_sec, "s; 上海条目", len(sh))
for r in rows:
    if r["site"] in ("lzmuseum", "silkmuseum", "szmuseum"):
        print("NEW", r["name"], r["n"], r["sec"], r["cities"])