#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上海公共建筑地图生成器

读取 config/places.json（人工整理的公共建筑清单 + WGS-84 坐标），
生成：
  docs/map.html     可直接打开的交互地图页面（数据内嵌，无需额外请求）
  docs/places.json  结构化数据（WGS-84 坐标）

高德底图使用 GCJ-02 坐标，因此页面里的每处建筑会同时带上 WGS-84 与 GCJ-02
两套坐标，切换底图时不需要再请求任何接口。

可以单独运行：
    python mapbuild.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_CENTER = (31.2304, 121.4737)  # 人民广场附近


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02（高德/腾讯底图使用的火星坐标）。"""
    if not (73.66 < lng < 135.05 and 3.86 < lat < 53.55):
        return lat, lng
    a = 6378245.0
    ee = 0.00669342162296594323
    pi = 3.1415926535897932384626

    def transform_lat(x: float, y: float) -> float:
        ret = (
            -100.0
            + 2.0 * x
            + 3.0 * y
            + 0.2 * y * y
            + 0.1 * x * y
            + 0.2 * math.sqrt(abs(x))
        )
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * pi) + 320.0 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
        return ret

    def transform_lng(x: float, y: float) -> float:
        ret = (
            300.0
            + x
            + 2.0 * y
            + 0.1 * x * x
            + 0.1 * x * y
            + 0.1 * math.sqrt(abs(x))
        )
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0
        return ret

    dlat = transform_lat(lng - 105.0, lat - 35.0)
    dlng = transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * pi
    magic = 1.0 - ee * (math.sin(radlat) ** 2)
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1.0 - ee)) / (magic * sqrtmagic) * pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * pi)
    return lat + dlat, lng + dlng


PLACES_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>上海公共建筑地图 · 周末去哪玩</title>
  <meta name="description" content="上海值得参观的公共建筑地图：博物馆、美术馆、剧院、教堂、历史建筑、园林景点与文化场馆，共 __COUNT__ 处。">
  <link rel="stylesheet" href="style.css">
  <link rel="stylesheet" href="vendor/leaflet/leaflet.css">
  <link rel="stylesheet" href="vendor/markercluster/MarkerCluster.css">
  <link rel="stylesheet" href="vendor/markercluster/MarkerCluster.Default.css">
  <link rel="stylesheet" href="map.css">
</head>
<body class="map-page">
  <header class="map-header">
    <div>
      <p class="eyebrow">上海城市漫步 · 公共建筑</p>
      <h1>值得进去看看的上海建筑</h1>
      <p class="updated">__SUMMARY__ · 更新于 __UPDATED__</p>
    </div>
    <nav class="site-nav" aria-label="站点导航">
      <a href="index.html">🎫 活动清单</a>
      <a class="active" href="map.html" aria-current="page">🗺 建筑地图</a>
    </nav>
  </header>

  <div class="filterbar">
    <div class="filterbar-inner">
      <div class="place-chips" id="place-chips" role="group" aria-label="按类别筛选">
        __CHIPS__
      </div>
      <div class="filter-tools">
        <input id="place-search" type="search" placeholder="搜名称 / 区域 / 地址" aria-label="搜索建筑">
        <button type="button" class="view-btn" id="fit-core">市区</button>
        <button type="button" class="view-btn" id="fit-city">全城</button>
      </div>
    </div>
  </div>

  <div class="map-layout">
    <aside class="place-panel" aria-label="建筑列表">
      <div class="panel-head">
        <span id="result-count">__COUNT__ 处</span>
        <span class="panel-hint">点击定位到地图</span>
      </div>
      <div id="place-list" class="place-list"></div>
      <div id="place-empty" class="panel-empty" hidden>没有匹配的建筑，换个关键词或分类试试。</div>
    </aside>
    <div class="map-wrap">
      <div id="map" role="application" aria-label="上海公共建筑地图"></div>
      <div id="map-status" class="map-status" hidden>地图组件加载失败，左侧列表仍可浏览。</div>
      <div class="map-tip">底图
        <button type="button" class="base-btn active" data-base="amap">高德</button>
        <button type="button" class="base-btn" data-base="osm">OpenStreetMap</button>
      </div>
    </div>
  </div>

  <footer class="map-footer">
    <p>共收录 __COUNT__ 处公共建筑与景点。坐标整理自 OpenStreetMap，底图 © 高德地图 / © OpenStreetMap contributors。</p>
    <p>开放时间、预约方式和临时闭馆请以官方公告为准；也欢迎补充还没收录的建筑。</p>
    <p><a href="index.html">← 回到本月展览 / 话剧 / 音乐剧</a></p>
  </footer>

  <script id="places-data" type="application/json">__DATA_JSON__</script>
  <script src="vendor/leaflet/leaflet.js"></script>
  <script src="vendor/markercluster/leaflet.markercluster.js"></script>
  <script src="map.js"></script>
</body>
</html>
"""


def build_places_payload(places_path: Path) -> dict:
    """读取并整理建筑数据；没有坐标的条目会被跳过。"""
    config = json.loads(places_path.read_text(encoding="utf-8"))
    raw_categories = list(config.get("categories", []))
    cat_order: dict[str, int] = {}
    categories: list[dict] = []
    for index, cat in enumerate(raw_categories):
        key = str(cat.get("key") or "").strip()
        if not key or key in cat_order:
            continue
        cat_order[key] = index
        categories.append(
            {
                "key": key,
                "label": str(cat.get("label") or key),
                "color": str(cat.get("color") or "#666"),
                "emoji": str(cat.get("emoji") or "📍"),
                "count": 0,
            }
        )

    places: list[dict] = []
    skipped: list[str] = []
    for raw in config.get("places", []):
        name = str(raw.get("name") or "").strip()
        lat, lng = raw.get("lat"), raw.get("lng")
        if lat is None or lng is None:
            skipped.append(name)
            continue
        try:
            lat = float(lat)
            lng = float(lng)
        except (TypeError, ValueError):
            skipped.append(name)
            continue
        if not (30.6 <= lat <= 31.9 and 120.7 <= lng <= 122.2):
            skipped.append(name)
            continue
        category = str(raw.get("category") or "")
        if category not in cat_order:
            category = categories[0]["key"] if categories else "other"
        gcj_lat, gcj_lng = wgs84_to_gcj02(lat, lng)
        slug = str(raw.get("slug") or name).strip()
        places.append(
            {
                "slug": slug,
                "name": name,
                "category": category,
                "district": str(raw.get("district") or ""),
                "address": str(raw.get("address") or ""),
                "intro": str(raw.get("intro") or ""),
                "site": str(raw.get("site") or ""),
                "wgs": [round(lat, 6), round(lng, 6)],
                "gcj": [round(gcj_lat, 6), round(gcj_lng, 6)],
            }
        )

    places.sort(key=lambda item: (cat_order.get(item["category"], 999), item["name"]))
    counts: dict[str, int] = {}
    for place in places:
        counts[place["category"]] = counts.get(place["category"], 0) + 1
    for cat in categories:
        cat["count"] = counts.get(cat["key"], 0)

    center_wgs = DEFAULT_CENTER
    center_gcj = wgs84_to_gcj02(*center_wgs)
    return {
        "updated": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M"),
        "count": len(places),
        "categories": categories,
        "center": {
            "wgs": [round(center_wgs[0], 6), round(center_wgs[1], 6)],
            "gcj": [round(center_gcj[0], 6), round(center_gcj[1], 6)],
        },
        "places": places,
        "skipped": skipped,
    }


def build_places_html(payload: dict) -> str:
    categories = payload.get("categories", [])
    places = payload.get("places", [])
    total = len(places)

    summary_bits = [
        f"{cat['label']} {cat['count']}" for cat in categories if cat.get("count")
    ]
    summary = f"共 {total} 处 · " + " · ".join(summary_bits) if total else "暂未添加建筑"

    chips = [
        '<button type="button" class="place-chip active" data-cat="all" aria-pressed="true">'
        f"全部 <b>{total}</b></button>"
    ]
    for cat in categories:
        if not cat.get("count"):
            continue
        chips.append(
            '<button type="button" class="place-chip" data-cat="{key}" aria-pressed="false">'
            '<span class="chip-dot" style="background:{color}"></span>{label} <b>{count}</b></button>'.format(
                key=cat["key"],
                color=cat["color"],
                label=cat["label"],
                count=cat["count"],
            )
        )

    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data_json = data_json.replace("</", "<\\/")

    html_text = PLACES_PAGE_TEMPLATE
    replacements = {
        "__SUMMARY__": summary,
        "__CHIPS__": "".join(chips),
        "__UPDATED__": str(payload.get("updated") or ""),
        "__DATA_JSON__": data_json,
        "__COUNT__": str(total),
    }
    for placeholder, value in replacements.items():
        html_text = html_text.replace(placeholder, value)
    return html_text


def build_places_outputs(root: Path) -> dict:
    """生成 docs/map.html 与 docs/places.json，返回统计信息。"""
    places_path = root / "config" / "places.json"
    docs_dir = root / "docs"
    if not places_path.exists():
        raise FileNotFoundError(places_path)

    payload = build_places_payload(places_path)
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "map.html").write_text(build_places_html(payload), encoding="utf-8")

    public_payload = {
        "updated": payload["updated"],
        "count": payload["count"],
        "categories": [
            {key: cat[key] for key in ("key", "label", "color", "emoji", "count")}
            for cat in payload["categories"]
        ],
        "places": [
            {key: place[key] for key in ("slug", "name", "category", "district", "address", "intro", "site", "wgs")}
            for place in payload["places"]
        ],
    }
    (docs_dir / "places.json").write_text(
        json.dumps(public_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def main() -> int:
    root = Path(__file__).resolve().parent
    try:
        payload = build_places_outputs(root)
    except Exception as exc:  # noqa: BLE001
        print(f"生成建筑地图失败：{exc}", file=sys.stderr)
        return 1
    print(f"建筑地图：{payload['count']} 处 → {root / 'docs' / 'map.html'}")
    if payload.get("skipped"):
        print("缺少坐标已跳过：" + "、".join(payload["skipped"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
