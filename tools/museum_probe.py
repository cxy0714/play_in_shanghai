#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""试跑博物馆条目：验证 config/museums.json 里某条配置能不能抓到展。

用法：
  python tools/museum_probe.py --all                    # 跑 config/museums.json 全部
  python tools/museum_probe.py --file scratch_x.json    # 跑任意 JSON（{"museums": [...]} 或 [...]）
  python tools/museum_probe.py --name 湖北省博物馆      # 只跑某个馆（在 museums.json 里找）
  python tools/museum_probe.py --entry '<json 字符串>'  # 直接试一条配置

输出：每馆条数、耗时、前几条的 title / 展期 / 场馆，方便判断配置对不对。
只发网络请求，不写 data/ 和 docs/。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import update  # noqa: E402  （只为了拿适配器映射，不会跑抓取）
from museum_generic import collect_museum_generic  # noqa: E402
from national_sources import NATIONAL_ADAPTERS  # noqa: E402

API_ADAPTERS = {}
for _adapter_name, _factory in (
    ("museum_api", "collect_museum_api"),
    ("museum_json", "collect_museum_json"),
):
    try:
        _module = __import__("museum_api", fromlist=[_factory])
        API_ADAPTERS[_adapter_name] = getattr(_module, _factory)
    except Exception as _exc:  # noqa: BLE001
        print(f"[warn] {_adapter_name} 不可用：{_exc}")


def load_entries(file_path: Path) -> list[dict]:
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    entries = payload.get("museums", []) if isinstance(payload, dict) else payload
    return [entry for entry in entries or [] if isinstance(entry, dict)]


def collect(entry: dict) -> tuple[list[dict], float]:
    started = time.time()
    adapter = entry.get("adapter") or "museum_generic"
    if adapter == "museum_generic":
        items = collect_museum_generic(entry)
    elif adapter in API_ADAPTERS:
        items = API_ADAPTERS[adapter](entry)
    else:
        collector = NATIONAL_ADAPTERS.get(adapter)
        items = collector(entry) if collector else []
    return items, time.time() - started


def show(entry: dict, limit: int) -> int:
    items, elapsed = collect(entry)
    label = f"{entry.get('name', '?')}（{entry.get('city', '?')}）"
    print(f"\n=== {label}  {len(items)} 条 / {elapsed:.1f}s")
    for item in items[:limit]:
        print(
            f"  - {item.get('title', '')[:40]} | {item.get('start_date', '')} ~ {item.get('end_date', '')}"
            f" | {item.get('venue', '')[:24]} | {item.get('ticket_url', '')}"
        )
    return len(items)


def main() -> int:
    parser = argparse.ArgumentParser(description="试跑博物馆展览配置")
    parser.add_argument("--all", action="store_true", help="跑 config/museums.json 里的全部条目")
    parser.add_argument("--file", help="跑指定 JSON 文件里的条目")
    parser.add_argument("--name", help="只跑名字包含该字符串的馆")
    parser.add_argument("--entry", help="直接试一条 JSON 配置")
    parser.add_argument("--limit", type=int, default=5, help="每馆打印前几条，默认 5")
    args = parser.parse_args()

    if args.entry:
        entries = [json.loads(args.entry)]
    else:
        file_path = ROOT / (args.file or "config/museums.json")
        entries = load_entries(file_path)
        if args.name:
            entries = [e for e in entries if args.name in str(e.get("name", ""))]
    if not entries:
        print("没有要跑的条目")
        return 1

    total = 0
    ok = 0
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        count = show(entry, args.limit)
        total += count
        ok += 1 if count else 0
    print(f"\n合计：{ok}/{len(entries)} 个馆抓到展，共 {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())