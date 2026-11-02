# -*- coding: utf-8 -*-
"""博物馆官方 JSON 接口采集器。

和 museum_generic.py 的分工：
- museum_generic：从 HTML 列表页 / 详情页里抠展讯（兜底，适合没有接口、或者接口要签名/登录的馆）
- museum_api    ：直接打官方 JSON 接口（字段全、不受页面改版影响，优先用这个）

新增一个馆的步骤：
1. 在本文件 MUSEUM_API_COLLECTORS 里注册一个 site 函数，返回 list[dict]（用 _event 组装）；
2. config/museums.json 里写 {"adapter": "museum_api", "site": "<key>", ...}。

接口结构相似、只是路径/字段名不同的馆，可以不写代码：
config 里用 "adapter": "museum_json"，填 api_url / items_path / fields 等映射即可，
见 collect_museum_json 的文档字符串。
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin

import requests

from national_sources import (
    MUSEUM_DEFAULT_ADDRESS,
    MUSEUM_HEADERS,
    TODAY,
    _result,
    clean_text,
    future_or_ongoing,
    http_get_json,
    is_permanent,
    log,
    parse_date_range,
    strip_tags,
)

JSON_HEADERS = {
    **MUSEUM_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _event(source: dict, **kwargs: Any) -> dict:
    """补上城市 / 场馆 / 地址等公共字段，再交给 national_sources._result 收口。"""
    city = clean_text(source.get("city")) or kwargs.pop("city", "")
    venue = clean_text(kwargs.pop("venue", "")) or clean_text(source.get("venue_default"))
    kwargs.setdefault("category", "展览")
    kwargs["city"] = city
    kwargs["venue"] = venue
    kwargs.setdefault("address", MUSEUM_DEFAULT_ADDRESS.get(venue, "") if venue else "")
    kwargs.setdefault("source_url", clean_text(source.get("list_url") or source.get("url")))
    return _result(**kwargs)


def _pick(item: dict, *names: str) -> str:
    """接口字段名五花八门，按候选取第一个非空的。"""
    for name in names:
        value = item.get(name)
        if value not in (None, "", [], {}):
            return clean_text(value)
    return ""


def _apply_dates(source: dict, text: str) -> tuple[Any, Any]:
    """从接口给的展期文本里解析起止（复用 museum_generic 的兜底正则）。"""
    from museum_generic import _dates_from_text, _extract_date_text

    raw = clean_text(text)
    if not raw:
        return None, None
    start, end = parse_date_range(raw)
    if start is None and end is None:
        date_text = _extract_date_text(raw, source)
        start, end = _dates_from_text(raw, date_text)
    if is_permanent(raw):
        start, end = TODAY, TODAY + timedelta(days=365)
    return start, end


def _wrap_dates(start: Any, end: Any) -> tuple[Any, Any]:
    if start is None and end is None:
        return None, None
    if start is None:
        start = end
    if end is None:
        end = TODAY + timedelta(days=180) if start <= TODAY else start + timedelta(days=90)
    return start, end


def _resolve(path: str, payload: Any) -> Any:
    """按 "data.list" 这种点路径取值；支持数字下标。"""
    node = payload
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            index = int(part)
            node = node[index] if index < len(node) else None
        else:
            return None
    return node


# ---------------------------------------------------------------------------
# 具体馆：南京博物院
# ---------------------------------------------------------------------------

def _api_njmuseum(source: dict) -> list[dict]:
    """南京博物院展览列表接口（GET/POST 都行，字段稳定）。

    返回结构：{"code":0,"data":{"list":[{"id","title","position","timedesc","price","describe","imgSrc"}]}}
    """
    from national_sources import _museum_njmuseum

    return _museum_njmuseum(source)


# ---------------------------------------------------------------------------
# 具体馆：四川博物院
# ---------------------------------------------------------------------------

def _api_scmuseum(source: dict) -> list[dict]:
    """四川博物院 CMS 接口（sw-cms-cloud 网关）。

    接口本身可用，但要求登录态（未带 token 时返回 {"code":310,"msg":"请重新登录"}）。
    config 里配 "token" / "cookie" 即可启用；拿不到时安静返回 0 条，不影响整轮。
    """
    base = clean_text(source.get("api_url")) or "https://www.scmuseum.cn/japi/sw-cms-cloud/api/queryArchivesList"
    headers = dict(JSON_HEADERS)
    token = clean_text(source.get("token"))
    if token:
        headers["Authorization"] = token
        headers["token"] = token
    cookie = clean_text(source.get("cookie"))
    if cookie:
        headers["Cookie"] = cookie

    body = {"entity": {"channelNo": clean_text(source.get("channel_no"))},
            "param": {"pageNum": 1, "pageSize": int(source.get("page_size") or 30)}}
    try:
        response = requests.post(base, headers=headers, json=body, timeout=25)
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log(f"    [museum_api] 四川博物院接口失败：{exc}")
        return []
    if isinstance(payload, dict) and payload.get("code") not in (0, None, "0"):
        log(f"    [museum_api] 四川博物院接口返回：{payload.get('msg') or payload.get('code')}")
        return []

    records = _resolve("data.records", payload) or _resolve("data.list", payload) or []
    results: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = _pick(record, "title", "name", "archivesTitle")
        if len(title) < 2:
            continue
        date_text = _pick(record, "exhibitionTime", "showTime", "dateRange", "time", "publishTime")
        start, end = _wrap_dates(*_apply_dates(source, date_text))
        if not future_or_ongoing(start, end):
            continue
        record_id = _pick(record, "id", "archivesId", "code")
        results.append(_event(
            source,
            title=title,
            start_date=start,
            end_date=end,
            venue=_pick(record, "position", "address") or clean_text(source.get("venue_default")),
            price_text=_pick(record, "price") or "以官网为准",
            ticket_url=f"https://www.scmuseum.cn/detail/{record_id}" if record_id else base,
            summary=_pick(record, "describe", "summary")[:120] or f"展期：{date_text}",
        ))
    return results


# ---------------------------------------------------------------------------
# 通用 JSON 接口采集器（config 驱动，不写代码也能接新馆）
# ---------------------------------------------------------------------------

def collect_museum_json(source: dict) -> list[dict]:
    """config 驱动的 JSON 接口采集器。

    config 示例（字段名、路径按目标接口改）：
    {
      "name": "某某博物馆", "city": "某市", "adapter": "museum_json",
      "api_url": "https://example.com/api/exhibition/list",
      "method": "post",
      "params": {"pageNum": 1, "pageSize": 30},
      "body": {"properties": "当前展览"},
      "items_path": "data.list",
      "fields": {
        "title": ["title", "name"],
        "date": ["timedesc", "showTime"],
        "venue": ["position"],
        "price": ["price"],
        "summary": ["describe"],
        "url": ["id"],
        "image": ["imgSrc"]
      },
      "url_template": "https://example.com/detail?id={url}",
      "image_base": "https://example.com",
      "venue_default": "某某博物馆"
    }
    """
    api_url = clean_text(source.get("api_url"))
    if not api_url:
        log("    [museum_json] 缺少 api_url")
        return []
    headers = dict(JSON_HEADERS)
    headers.update({str(k): str(v) for k, v in (source.get("headers") or {}).items()})
    method = clean_text(source.get("method") or "get").lower()
    try:
        if method == "post":
            response = requests.post(
                api_url,
                headers=headers,
                params=source.get("params") or None,
                json=source.get("body") or None,
                timeout=30,
            )
            payload = response.json()
        else:
            response = requests.get(api_url, headers=headers, params=source.get("params") or None, timeout=30)
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log(f"    [museum_json] 接口失败：{exc}")
        return []

    items = _resolve(source.get("items_path") or "data.list", payload)
    if not isinstance(items, list):
        log("    [museum_json] items_path 没取到列表，检查配置")
        return []

    fields = {str(k): (v if isinstance(v, list) else [v]) for k, v in (source.get("fields") or {}).items()}
    url_template = clean_text(source.get("url_template"))
    image_base = clean_text(source.get("image_base"))
    results: list[dict] = []
    for record in items:
        if not isinstance(record, dict):
            continue
        title = _pick(record, *fields.get("title", ["title", "name"]))
        if len(title) < 2:
            continue
        date_text = _pick(record, *fields.get("date", ["timedesc", "time", "date"]))
        start, end = _wrap_dates(*_apply_dates(source, date_text))
        if not future_or_ongoing(start, end):
            continue
        url_value = _pick(record, *fields.get("url", ["id", "url", "link"]))
        ticket_url = url_template.format(url=url_value, id=url_value) if url_template else (url_value or api_url)
        if ticket_url and not ticket_url.startswith("http") and api_url:
            ticket_url = urljoin(api_url, ticket_url)
        image_value = _pick(record, *fields.get("image", ["imgSrc", "image", "cover"]))
        if image_value and not image_value.startswith("http") and image_base:
            image_value = image_base.rstrip("/") + "/" + image_value.lstrip("/")
        results.append(_event(
            source,
            title=title,
            start_date=start,
            end_date=end,
            venue=_pick(record, *fields.get("venue", ["position", "venue", "address"])) or clean_text(source.get("venue_default")),
            price_text=_pick(record, *fields.get("price", ["price"])) or clean_text(source.get("price_text")) or "以官网为准",
            ticket_url=ticket_url,
            image_url=image_value,
            summary=_pick(record, *fields.get("summary", ["describe", "summary", "description"]))[:120] or f"展期：{date_text}",
        ))
    return results


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

MUSEUM_API_COLLECTORS = {
    "njmuseum": _api_njmuseum,
    "scmuseum": _api_scmuseum,
}


def collect_museum_api(source: dict) -> list[dict]:
    site = clean_text(source.get("site")).lower()
    collector = MUSEUM_API_COLLECTORS.get(site)
    if collector is None:
        log(f"    [museum_api] 未登记的 site：{site or '(空)'}")
        return []
    items = collector(source)
    if items:
        log(f"    [museum_api] {source.get('name') or site} 抓到 {len(items)} 条")
    return items