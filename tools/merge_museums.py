#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一批博物馆条目合并进 config/museums.json（按 name 覆盖同名条目，其余追加）。

用法：
  python tools/merge_museums.py scratch_museums_east.json scratch_museums_north.json
  python tools/merge_museums.py scratch_x.json --dry-run    # 只看会加什么，不写文件

每个输入文件可以是 {"museums": [...]} 或纯数组；条目字段同 config/museums.json。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "config" / "museums.json"
NOTE = (
    "博物馆官网展览来源清单：一个馆一条配置。adapter=museum_cn 用 national_sources.py 里的专用采集器"
    "（site 指定站点）；adapter=museum_generic 用 museum_generic.py 的通用采集器"
    "（list_url + 可选选择器/正则）。改完跑 python update.py 即生效。"
)


def load_entries(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("museums", []) if isinstance(payload, dict) else payload
    return [entry for entry in entries or [] if isinstance(entry, dict) and entry.get("name")]


def main() -> int:
    parser = argparse.ArgumentParser(description="合并博物馆条目到 config/museums.json")
    parser.add_argument("files", nargs="+", help="要合并的 JSON 文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args()

    current = load_entries(TARGET) if TARGET.exists() else []
    index = {entry["name"]: position for position, entry in enumerate(current)}

    added: list[str] = []
    replaced: list[str] = []
    for name in args.files:
        for entry in load_entries(ROOT / name):
            if entry["name"] in index:
                current[index[entry["name"]]] = entry
                replaced.append(entry["name"])
            else:
                index[entry["name"]] = len(current)
                current.append(entry)
                added.append(entry["name"])

    print(f"新增 {len(added)}：{'、'.join(added) or '无'}")
    print(f"覆盖 {len(replaced)}：{'、'.join(replaced) or '无'}")
    print(f"合计 {len(current)} 条")

    if args.dry_run:
        return 0
    TARGET.write_text(
        json.dumps({"_note": NOTE, "museums": current}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())