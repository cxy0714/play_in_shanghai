#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性回填 data/updates.json：用 git 历史里的 docs/events.json 还原历次「上新」。

用法（在仓库根目录）：python tools/backfill_updates.py [max_revisions]
生成的数据结构与 update.py 的 record_update() 完全一致，后续 CI 会继续往后追加。
注意：会整体覆盖 data/updates.json，只适合在需要重建历史时手动运行。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import update

TZ = ZoneInfo("Asia/Shanghai")
MAX_REV = int(sys.argv[1]) if len(sys.argv) > 1 else 16


def git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], capture_output=True, encoding="utf-8", cwd=str(update.ROOT)
    )
    if out.returncode != 0:
        raise SystemExit(f"git failed: {out.stderr[:300]}")
    return out.stdout


def revisions() -> list[tuple[str, datetime]]:
    raw = git("log", "--format=%H %cI", f"-{MAX_REV}", "--", "docs/events.json")
    result = []
    for line in raw.strip().splitlines():
        sha, stamp = line.split(" ", 1)
        when = datetime.fromisoformat(stamp).astimezone(TZ)
        result.append((sha, when))
    result.reverse()  # 旧 → 新
    return result


def load_events(sha: str) -> list[dict]:
    try:
        text = git("show", f"{sha}:docs/events.json")
    except SystemExit:
        return []
    payload = json.loads(text)
    events = payload.get("events") if isinstance(payload, dict) else payload
    return [event for event in (events or []) if isinstance(event, dict)]


def main() -> int:
    runs: list[dict] = []
    previous: list[dict] = []
    for index, (sha, when) in enumerate(revisions()):
        events = load_events(sha)
        if not events:
            continue
        previous_keys = {update.title_key(event) for event in previous if update.title_key(event)}
        current_keys = {update.title_key(event) for event in events if update.title_key(event)}
        added = [
            update.compact_event(event)
            for event in events
            if update.title_key(event)
            and update.title_key(event) not in previous_keys
            and update.national_allowed(event)
        ]
        added.sort(
            key=lambda event: (
                update.event_city(event) != update.SHANGHAI_CITY,
                event.get("start_date") or "9999-99-99",
                event.get("title", ""),
            )
        )
        runs.append(
            {
                "at": when.isoformat(timespec="seconds"),
                "added": added,
                "added_count": len(added),
                "added_national": sum(1 for event in added if not update.is_shanghai(event)),
                "removed": len(previous_keys - current_keys),
                "total": len(events),
                "baseline": index == 0,
            }
        )
        previous = events

    updates = {
        "version": 1,
        "runs": runs[-update.UPDATES_KEEP_RUNS:],
    }
    update.save_updates(updates)
    print(
        f"回填 {len(runs)} 次更新，最近一次 {runs[-1]['at']} 新增 {runs[-1]['added_count']} 个"
        f"（历史总条数 {sum(r['added_count'] for r in runs)}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())