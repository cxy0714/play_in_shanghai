#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用博物馆展览采集器：一个馆 = `config/museums.json` 里一条配置，不用写代码。

    {
      "name": "湖北省博物馆",
      "city": "武汉",
      "adapter": "museum_generic",
      "list_url": "https://www.hbww.org/...",       # 必填，临展列表页
      "detail": true,                                # 是否进详情页取展期（默认 true）
      "item_selector": "ul.newslist li a",           # 可选，CSS 选择器
      "link_regex": "zhanlan|exhibit",               # 可选，href 正则（没有 item_selector 时用）
      "title_selector": "h1",                        # 可选，详情页标题选择器
      "date_regex": "展览时间[：:]\\s*([^。<]{4,40})",  # 可选，展期串（取第 1 组）
      "date_regex_list": ["..."],                    # 可选，多组按顺序尝试
      "exclude_regex": "常设|讲座|活动",              # 可选，标题过滤
      "venue_default": "湖北省博物馆",
      "price_text": "免费（需预约）",
      "max_articles": 8
    }

流程：列表页 → 链接 → 详情页正文 → 抽展期 → 解析日期 → 丢掉已结束/常设 → 统一契约返回。
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from national_sources import (
    MUSEUM_DEFAULT_ADDRESS,
    MUSEUM_HEADERS,
    SLEEP,
    _article_dates,
    _article_title,
    _result,
    clean_text,
    future_or_ongoing,
    http_get,
    is_permanent,
    log,
    parse_date_range,
)

# 通用展期正则：配置没给 date_regex 时按顺序试
GENERIC_HEADERS = {**MUSEUM_HEADERS, "Connection": "close"}

DEFAULT_DATE_PATTERNS = [
    r"展览时间[：:]\s*([^。；\n]{4,60}?(?:至|到|—|–|-|~|～)[^。；\n]{0,30})",
    r"展期[：:]\s*([^。；\n]{4,60}?(?:至|到|—|–|-|~|～)[^。；\n]{0,30})",
    r"展览时间[：:]\s*([^。；\n]{4,50})",
    r"展览日期[：:]\s*([^。；\n]{4,50})",
    r"展期[：:]\s*([^。；\n]{4,50})",
    r"时间[：:]\s*(20\d{2}[^。；\n]{0,30}?(?:至|到|—|–|-|~|～)[^。；\n]{0,30})",
    r"((?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日?\s*"
    r"(?:至|到|—|–|-|~|～)\s*"
    r"(?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日?)",
    # 兜底：正文里任意一处的「年-月-日 ~ 年-月-日」区间
    r"(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^。；\n]{0,10}?"
    r"(?:至|到|—|–|-|~|～)[^。；\n]{0,10}?(?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日)",
    r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}[^。；\n]{0,6}?"
    r"(?:至|到|—|–|-|~|～)[^。；\n]{0,6}?20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})",
]


def _config_patterns(source: dict) -> list[str]:
    patterns: list[str] = []
    for key in ("date_regex_list", "date_regex"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            patterns.append(value.strip())
        elif isinstance(value, (list, tuple)):
            patterns.extend(str(item).strip() for item in value if str(item).strip())
    return patterns


def _extract_date_text(text: str, source: dict) -> str:
    for pattern in _config_patterns(source) + DEFAULT_DATE_PATTERNS:
        try:
            match = re.search(pattern, text)
        except re.error:
            continue
        if not match:
            continue
        value = clean_text(match.group(1) if match.groups() else match.group(0))
        if len(value) < 4:
            continue
        # 抓到的必须是真能解析出日期的串，避免把「点击率/发布时间」当展期
        start, end = parse_date_range(value)
        if start and end and end > start:
            return value
        if (
            start
            and not end
            and len(value) <= 24
            and not re.search(r"点击率|发布|浏览|来源|编辑|作者", value)
        ):
            return value
    return ""


def _dates_from_text(text: str, date_text: str):
    if date_text:
        start, end = parse_date_range(date_text)
        if start or end:
            return start, end
    start, end, _found = _article_dates(text)
    if start or end:
        return start, end
    return None, None


def _pick_title(soup: BeautifulSoup, source: dict, fallback: str) -> str:
    selector = clean_text(source.get("title_selector"))
    if selector:
        node = soup.select_one(selector)
        if node is not None:
            title = clean_text(node.get_text(" ", strip=True))
            if len(title) >= 2:
                return title
    heading = soup.find("h1")
    if heading is not None:
        title = clean_text(heading.get_text(" ", strip=True))
        if 2 <= len(title) <= 80:
            return title
    page_text = clean_text(soup.get_text(" ", strip=True))
    return _article_title(soup, page_text) or fallback


_NAV_TITLE_RE = re.compile(
    r"^(?:首页|展览|当前展览|正在展出|即将展出|即将开展|历史展览|全部展览|全部|更多|查看|详情|发布|"
    r"展览资讯|展览预告|展讯|常设展览|临时展览|特别展览|虚拟|公共教育|参观|服务|研究|收藏|"
    r"新闻|动态|活动|资讯|回顾|上一页|下一页|返回|English|EN|中文)$",
    re.I,
)


def _pick_block_title(block: Tag) -> str:
    """列表项里挑最像展名的链接文本：优先含「展」、长度适中、不是导航词。"""
    best = ""
    best_score = -1
    for candidate in block.find_all("a", href=True):
        text = clean_text(candidate.get_text(" ", strip=True))
        if not (2 <= len(text) <= 80) or _NAV_TITLE_RE.match(text):
            continue
        score = 0
        if "展" in text:
            score += 3
        if 6 <= len(text) <= 40:
            score += 2
        if re.search(r"[，,、：:—\-]", text):
            score += 1
        if score > best_score or (score == best_score and len(text) > len(best)):
            best, best_score = text, score
    return best


def _list_entries(soup: BeautifulSoup, source: dict, base_url: str) -> list[dict]:
    """从列表页提取条目：优先 item_selector，其次 link_regex；没有详情链接的列表项也支持。"""
    selector = clean_text(source.get("item_selector"))
    link_pattern = clean_text(source.get("link_regex"))
    title_selector = clean_text(source.get("title_selector"))
    nodes = soup.select(selector) if selector else soup.find_all("a", href=True)
    entries: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        anchor = node if getattr(node, "name", "") == "a" else node.find("a", href=True)
        href = clean_text(anchor.get("href")) if anchor is not None else ""
        if href.startswith(("javascript:", "#", "mailto:")):
            href = ""
        if not href and hasattr(node, "get"):  # 列表项本身就是一条展讯，没有 <a>
            for attr in ("data-url", "data-href", "data-link"):
                href = clean_text(node.get(attr)) or href
            if not href:
                found = re.search(
                    r"(?:https?://|/)[\w./\-]+\.(?:html?|aspx)", node.get("onclick") or ""
                )
                href = found.group(0) if found else ""
        if link_pattern and not re.search(link_pattern, href or ""):
            continue
        url = urljoin(base_url, href) if href else ""
        block = node if selector else (anchor.find_parent(["li", "div", "tr", "p"]) or anchor)
        block_text = clean_text(block.get_text(" ", strip=True))
        title = ""
        if title_selector and hasattr(block, "select_one"):
            title_node = block.select_one(title_selector)
            if title_node is not None:
                title = clean_text(title_node.get_text(" ", strip=True))
        if not title and hasattr(block, "get"):
            title = clean_text(block.get("title") or "")
        if not title and hasattr(block, "find_all"):
            title = _pick_block_title(block) or ""
        if not title and anchor is not None:
            title = clean_text(anchor.get_text(" ", strip=True))
        if not title:
            title = block_text[:60]
        dedupe_key = f"{url}|{title}" if url else title
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        image = ""
        if hasattr(block, "select_one"):
            image_node = block.select_one("img[src]")
            if image_node is not None:
                image = urljoin(base_url, image_node.get("src") or "")
        entries.append({"url": url, "title": title, "block_text": block_text, "image_url": image})
    return entries


_TITLE_PREFIX_RE = re.compile(
    r"^(?:展览上新|展讯|新展开幕|展览开幕|展览预告|开展|即将开展|公告|新馆|本馆|分馆)"
    r"\s*[|｜丨:：]\s*"
)
_TITLE_TAIL_RE = re.compile(r"\s*(?:地点|展期|展览时间|开展时间|展厅|展览日期|发布日期|发布时间|时间)\s*[：:].*$")


def _clean_title(title: str) -> str:
    """列表项常把「地点/展期」拼进展名里，这里切干净。"""
    title = clean_text(title)
    title = _TITLE_PREFIX_RE.sub("", title)
    title = _TITLE_TAIL_RE.sub("", title)
    title = re.split(r"\s+20\d{2}\s*[年.\-/]\s*\d{1,2}\s*[月.\-/]", title)[0]
    title = re.sub(r"\s+", " ", title).strip(" ·|—-、,，")
    return title


def _extract_venue(text: str, source: dict, city: str) -> str:
    base = clean_text(source.get("venue_default")) or clean_text(source.get("name"))
    match = re.search(r"(?:展览|展出)?地点[：:]\s*([^\s。；，,]{2,30})", text)
    hall = clean_text(match.group(1)) if match else ""
    if base and hall:
        if base in hall:
            return hall
        if hall in base:
            return base
        return f"{base} {hall}"
    return base or hall or city


def collect_museum_generic(source: dict) -> list[dict]:
    """按 config/museums.json 的一条配置抓取某个博物馆在展的临展。"""
    name = clean_text(source.get("name")) or "博物馆"
    try:
        list_url = clean_text(source.get("list_url") or source.get("url"))
        if not list_url:
            log(f"  [museum_generic] {name} 缺少 list_url")
            return []
        city = clean_text(source.get("city")) or "上海"
        html_text = http_get(list_url, headers=GENERIC_HEADERS, timeout=20, retries=1)
        soup = BeautifulSoup(html_text, "html.parser")
        entries = _list_entries(soup, source, list_url)
        if not entries:
            log(f"  [museum_generic] {name} 列表页没解析到链接：{list_url}")
            return []

        max_articles = max(1, int(source.get("max_articles", 8) or 8))
        budget = float(source.get("max_seconds", 45) or 45)
        deadline = time.monotonic() + budget
        exclude = clean_text(source.get("exclude_regex"))
        detail = bool(source.get("detail", True))
        price = clean_text(source.get("price_text")) or "以官网为准"
        address = clean_text(source.get("address")) or MUSEUM_DEFAULT_ADDRESS.get(city, "")
        results: list[dict] = []

        for entry in entries:
            if len(results) >= max_articles or time.monotonic() > deadline:
                break
            title = entry["title"]
            if exclude and re.search(exclude, title):
                continue
            text = entry["block_text"]
            image = entry["image_url"]
            detail_url = entry["url"]
            if detail:
                try:
                    detail_html = http_get(detail_url, headers=GENERIC_HEADERS, timeout=20, retries=1)
                except Exception as exc:  # noqa: BLE001
                    log(f"    [museum_generic] 详情页失败 {detail_url}：{exc}")
                    continue
                detail_soup = BeautifulSoup(detail_html, "html.parser")
                text = clean_text(detail_soup.get_text(" ", strip=True))
                title = _pick_title(detail_soup, source, title)
                image_node = detail_soup.select_one("img[src]")
                if image_node is not None:
                    image = urljoin(detail_url, image_node.get("src") or "") or image
                time.sleep(SLEEP)

            title = _clean_title(title)
            site_name = clean_text(source.get("name"))
            if site_name and title.endswith(site_name) and len(title) > len(site_name):
                title = title[: -len(site_name)].strip(" -–—|_·、,，")
            if not title or len(title) < 2:
                continue

            date_text = _extract_date_text(text, source)
            if is_permanent(title) or is_permanent(date_text):
                continue
            start, end = _dates_from_text(text, date_text)
            if start is None and end is None:
                continue
            derived_end = False
            if end is None and start is not None:
                end = start + timedelta(days=90)
                derived_end = True
            if not future_or_ongoing(start, end):
                continue

            if derived_end:
                summary = (
                    f"展期：{date_text}（官网未给结束日，按开幕日 +90 天暂存，以官网为准）"
                    if date_text
                    else "官网未给结束日，按开幕日 +90 天暂存，以官网为准"
                )
            elif date_text:
                summary = f"展期：{date_text}"
            else:
                summary = "展期以官网为准"
            results.append(
                _result(
                    title=title,
                    category="展览",
                    venue=_extract_venue(text, source, city),
                    address=address,
                    city=city,
                    start_date=start,
                    end_date=end,
                    price_text=price,
                    ticket_url=detail_url or list_url,
                    source_url=list_url,
                    image_url=image,
                    summary=summary,
                )
            )

        log(f"  [museum_generic] {name} 抓到 {len(results)} 条")
        return results
    except Exception as exc:  # noqa: BLE001
        log(f"  [museum_generic] {name} 采集异常：{exc}")
        return []


if __name__ == "__main__":  # 手动试一条：python museum_generic.py '<json>'
    import json
    import sys

    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    emit = collect_museum_generic(payload)
    print(json.dumps(emit, ensure_ascii=False, indent=2))