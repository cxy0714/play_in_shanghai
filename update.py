#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上海活动自动更新器

从 config/sources.json 里的公开网页抓取/抽取活动，
生成 docs/index.html、docs/events.json、docs/events.ics，
并调用 mapbuild.py 生成 docs/map.html（上海公共建筑地图）。

优先使用 JSON-LD 结构化数据；没有的话，如果配置了 LLM，
会把网页正文交给 LLM 抽取成结构化活动。

环境变量：
  LLM_API_KEY    OpenAI 兼容接口的 key（可选）
  LLM_BASE_URL   例如 https://api.deepseek.com/v1
  LLM_MODEL      例如 deepseek-chat
  GITHUB_TOKEN   GitHub Actions 内置 token，可作为 GitHub Models 的 key
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

try:  # 全国范围的数据源（可选模块，缺失时退化为纯上海模式）
    from national_sources import NATIONAL_ADAPTERS
except Exception as exc:  # pragma: no cover - 只在模块缺失/损坏时触发
    NATIONAL_ADAPTERS = {}
    print(f"[warn] national_sources 加载失败，仅抓取上海数据：{exc}", flush=True)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "sources.json"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 PlayInShanghai/1.0"
)
DATE_WINDOW_DAYS = 75
MAX_LLM_TEXT = 28000
LLM_TIMEOUT = 180

# 全国范围（外地）只收录下面这几类活动；其余类别仍然只看上海
SHANGHAI_CITY = "上海"
NATIONAL_GROUPS = {"音乐剧", "话剧"}
NATIONAL_EXHIBITION_SUBS = {"历史文物"}

# 「本周上新」记录
UPDATES_PATH = DATA_DIR / "updates.json"
UPDATES_KEEP_RUNS = 60
NEW_DAYS = 7
NEW_PAGE_RUN_CAP = 40  # 单次更新在时间线页面最多展示多少张卡片
FETCH_BUDGET_SECONDS = 18 * 60  # 抓取总时间预算，超时后跳过剩余来源（配合 GitHub Actions 30 分钟超时）

LLM_CONFIG: tuple[str, str, str] | None = None


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    now = datetime.now(SHANGHAI_TZ).strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def today_cn() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html_lib.unescape(str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def as_text(value: Any) -> str:
    """把 JSON-LD 里可能出现的字符串/列表/字典统一成文本。"""
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return clean_text(value)
    if isinstance(value, list):
        parts = [as_text(item) for item in value]
        return clean_text(" ".join(part for part in parts if part))
    if isinstance(value, dict):
        for key in ("name", "text", "@value", "url"):
            if key in value:
                return as_text(value[key])
        return ""
    return clean_text(value)


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 英文翻译
# ---------------------------------------------------------------------------

TRANSLATION_CACHE_PATH = DATA_DIR / "translations.json"
TRANSLATION_CACHE: dict[str, str] = {}


def load_translation_cache() -> dict[str, str]:
    global TRANSLATION_CACHE
    if TRANSLATION_CACHE_PATH.exists():
        try:
            TRANSLATION_CACHE = json.loads(TRANSLATION_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            TRANSLATION_CACHE = {}
    return TRANSLATION_CACHE


def save_translation_cache() -> None:
    if TRANSLATION_CACHE:
        save_json(TRANSLATION_CACHE_PATH, TRANSLATION_CACHE)


def needs_translation(text: str) -> bool:
    text = clean_text(text)
    return bool(text) and bool(re.search(r"[A-Za-z]", text)) and not bool(re.search(r"[\u4e00-\u9fff]", text))


def translate_with_mymemory(value: str) -> str:
    query = value if len(value) <= 450 else value[:450]
    try:
        response = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": query, "langpair": "en|zh-CN"},
            timeout=20,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        translated = clean_text(response.json().get("responseData", {}).get("translatedText", ""))
    except Exception:
        return ""
    if not translated or "QUERY LENGTH LIMIT EXCEEDED" in translated.upper():
        return ""
    if len(value) > 450:
        translated = translated.rstrip("。.!！?？ ") + "……"
    return translated


def translate_with_youdao(value: str) -> str:
    try:
        response = requests.get(
            "https://aidemo.youdao.com/trans",
            params={"q": value, "from": "en", "to": "zh-CHS"},
            timeout=20,
            headers={"User-Agent": USER_AGENT, "Referer": "https://fanyi.youdao.com/"},
        )
        response.raise_for_status()
        translated = response.json().get("translation")
        if isinstance(translated, list):
            translated = "".join(str(item) for item in translated)
        return clean_text(translated)
    except Exception:
        return ""


def translate_to_zh(text: str) -> str:
    value = clean_text(text)
    if not needs_translation(value):
        return value
    if value in TRANSLATION_CACHE:
        return TRANSLATION_CACHE[value]

    time.sleep(0.3)
    translated = translate_with_mymemory(value)
    if not translated or translated == value:
        translated = translate_with_youdao(value)
    if translated and translated != value:
        TRANSLATION_CACHE[value] = translated
        return translated

    # 翻译失败时不缓存原句，下次还会重试
    return value


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 40) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        timeout=timeout,
    )
    response.raise_for_status()
    # 有些页面编码没写对，交给 requests 猜，但兜底用 utf-8
    if response.encoding and response.encoding.lower() in ("iso-8859-1", "latin-1"):
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def http_get_json(url: str, headers: dict | None = None, timeout: int = 40) -> Any:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if headers:
        request_headers.update(headers)
    response = requests.get(url, headers=request_headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_jina(url: str) -> str:
    """用 Jina Reader 把动态网页转成可读文本。"""
    target = "https://r.jina.ai/" + url
    response = requests.get(
        target,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain",
        },
        timeout=90,
    )
    response.raise_for_status()
    text = response.text.strip()
    if len(text) < 80:
        raise ValueError("Jina Reader 返回内容过短")
    return text


def html_to_text(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_like_feed(text: str) -> bool:
    head = text[:500].lower()
    return "<rss" in head or "<feed" in head or "<?xml" in head


# ---------------------------------------------------------------------------
# JSON-LD 抽取
# ---------------------------------------------------------------------------

EVENT_TYPE_KEYS = {
    "event",
    "exhibitionevent",
    "theaterevent",
    "musicevent",
    "comedyevent",
    "danceevent",
    "festivalevent",
    "screeningevent",
    "visualartsevent",
    "businessevent",
    "educationevent",
    "socialevent",
    "festival",
}


def is_event_type(value: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    for item in values:
        text = str(item).lower().strip()
        text = text.rsplit("/", 1)[-1]
        if text in EVENT_TYPE_KEYS:
            return True
    return False


def walk_jsonld(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        if is_event_type(value.get("@type")):
            yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from walk_jsonld(child)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from walk_jsonld(item)


def extract_time_text(text: str) -> str:
    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", text or "")
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    return f"{hour:02d}:{minute}"


def parse_jsonld_event(item: dict, base_url: str) -> dict:
    title = as_text(item.get("name"))
    description = as_text(item.get("description"))
    start = item.get("startDate") or item.get("startDateText") or ""
    end = item.get("endDate") or item.get("endDateText") or ""

    location = item.get("location")
    venue = ""
    address = ""
    if isinstance(location, dict):
        venue = as_text(location.get("name"))
        addr = location.get("address")
        if isinstance(addr, dict):
            address = clean_text(
                " ".join(
                    as_text(addr.get(key))
                    for key in ("addressRegion", "addressLocality", "streetAddress")
                    if addr.get(key)
                )
            )
        else:
            address = as_text(addr)
    elif isinstance(location, str):
        venue = clean_text(location)

    price_text = ""
    ticket_url = ""
    offers = item.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if isinstance(offers, list):
        prices: list[str] = []
        urls: list[str] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
            if price not in (None, ""):
                prices.append(str(price))
            if offer.get("url"):
                urls.append(urljoin(base_url, str(offer["url"])))
        if prices:
            price_text = " / ".join(dict.fromkeys(prices))
        if urls:
            ticket_url = urls[0]

    url = item.get("url") or ticket_url or base_url
    detail_url = urljoin(base_url, str(url)) if url else base_url

    image = item.get("image")
    if isinstance(image, dict):
        image = image.get("url")
    elif isinstance(image, list) and image:
        image = image[0] if isinstance(image[0], str) else as_text(image[0])

    status = as_text(item.get("eventStatus")).lower()
    status_text = ""
    if "cancelled" in status:
        status_text = "已取消"
    elif "postponed" in status or "movedonline" in status:
        status_text = "已延期"
    elif "soldout" in status:
        status_text = "已售罄"

    return {
        "title": title,
        "summary": description,
        "venue": venue,
        "address": address,
        "start_date": start,
        "end_date": end,
        "time_text": extract_time_text(" ".join([str(start), str(end), description, title])),
        "price_text": price_text,
        "ticket_url": detail_url,
        "source_url": base_url,
        "image_url": image or "",
        "status": status_text,
    }


def extract_jsonld_events(html_text: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    scripts = soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})
    results: list[dict] = []
    for script in scripts:
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for item in walk_jsonld(data):
            event = parse_jsonld_event(item, base_url)
            if event.get("title"):
                results.append(event)
    return results


# ---------------------------------------------------------------------------
# RSS 抽取（备选）
# ---------------------------------------------------------------------------

def extract_rss_events(xml_text: str, base_url: str) -> list[dict]:
    try:
        import feedparser
    except ImportError:
        return []

    feed = feedparser.parse(xml_text)
    results: list[dict] = []
    for entry in feed.entries[:80]:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link") or base_url
        summary_html = entry.get("summary") or entry.get("description") or ""
        summary = clean_text(BeautifulSoup(summary_html, "html.parser").get_text(" "))
        published = entry.get("published") or entry.get("updated") or ""
        if not title:
            continue
        results.append(
            {
                "title": title,
                "summary": summary,
                "date_text": f"{published} {title} {summary}",
                "ticket_url": link,
                "source_url": base_url,
            }
        )
    return results


# ---------------------------------------------------------------------------
# 站点适配器
# ---------------------------------------------------------------------------

MAOYAN_BASE = "https://m.dianping.com/myshow"
MAOYAN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "Referer": "https://show.maoyan.com/",
}
MAOYAN_CATEGORY_MAP = {
    1: "音乐会",
    2: "其他",
    3: "戏曲",
    4: "话剧",
    5: "舞蹈",
    6: "音乐会",
    7: "其他",
    8: "其他",
    9: "展览",
    10: "音乐会",
    12: "其他",
    13: "其他",
    14: "话剧",
    15: "脱口秀",
    16: "其他",
    17: "音乐会",
}

SMART_HEADERS = {
    "User-Agent": USER_AGENT,
    "X-API-KEY": "oisidoosdkouiimnkcjhisdfui393jskdfu23jsdf",
    "Authorization": "Bearer oisidoosdkouiimnkcjhisdfui393jskdfu23jsdf",
    "Referer": "https://www.smartshanghai.com/events",
}

DOUBAN_PAGE_CATEGORY = {
    "week-drama": "话剧",
    "week-comedy": "脱口秀",
    "week-exhibition": "展览",
    "week-music": "音乐会",
}


def locale_text(value: Any) -> str:
    """处理 Sanity/PSA 常见的 {cn: ...} / [{children: ...}] 结构。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return clean_text(" ".join(locale_text(item) for item in value))
    if isinstance(value, dict):
        for key in ("cn", "zh", "zh-Hans", "en"):
            if key in value:
                return locale_text(value[key])
        if "text" in value:
            return clean_text(value["text"])
        if "children" in value:
            return locale_text(value["children"])
        if "title" in value:
            return locale_text(value["title"])
    return clean_text(value)


def extract_image_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("url"):
            return str(value["url"])
        srcs = value.get("srcs")
        if isinstance(srcs, list) and srcs:
            first = srcs[0]
            if isinstance(first, dict) and first.get("url"):
                return str(first["url"])
    return ""


def parse_maoyan_time_range(text: Any) -> tuple[date | None, date | None]:
    start, end = infer_dates(text)
    return start, end


def collect_maoyan(source: dict) -> list[dict]:
    results: list[dict] = []
    categories = source.get("categories") or [4, 9, 6, 3, 5]
    page_size = int(source.get("page_size", 30))
    max_pages = int(source.get("max_pages", 1))

    for category in categories:
        for page in range(1, max_pages + 1):
            url = (
                f"{MAOYAN_BASE}/ajax/performances/{category};st=0"
                f";p={page};s={page_size};tft=0?cityId=10&sellChannel=7"
            )
            data = http_get_json(url, headers=MAOYAN_HEADERS)
            items = data.get("data") or []
            if not items:
                break

            for item in items:
                title = clean_text(item.get("name") or item.get("shortName"))
                if not title:
                    continue
                category_id = int(item.get("categoryId") or category)
                event_category = MAOYAN_CATEGORY_MAP.get(category_id, "其他")
                if event_category == "话剧" and "音乐剧" in title:
                    event_category = "音乐剧"

                start, end = parse_maoyan_time_range(item.get("showTimeRange", ""))
                performance_id = (
                    item.get("performanceId")
                    or item.get("project_id")
                    or item.get("projectId")
                )
                jump = clean_text(item.get("jumpDetailUrl"))
                if performance_id:
                    # 格瓦拉/猫眼的统一网页详情页，能直接看到活动标题、场馆和日期
                    ticket_url = f"https://www.gewara.com/detail/{performance_id}"
                elif jump:
                    ticket_url = urljoin("https://show.maoyan.com", jump)
                else:
                    ticket_url = clean_text(source.get("url"))

                price = ""
                price_display = item.get("sellPriceDisplay")
                if isinstance(price_display, dict):
                    price = clean_text(price_display.get("priceDisplayText"))
                price = price or clean_text(item.get("priceRange"))

                score = clean_text(item.get("score"))
                summary = f"猫眼评分 {score}" if score else ""

                results.append(
                    {
                        "title": title,
                        "category": event_category,
                        "venue": clean_text(item.get("shopName")),
                        "address": clean_text(item.get("address")),
                        "start_date": start.isoformat() if start else "",
                        "end_date": end.isoformat() if end else "",
                        "price_text": price,
                        "ticket_url": ticket_url,
                        "source_url": clean_text(source.get("url")),
                        "image_url": clean_text(item.get("posterUrl")),
                        "summary": summary,
                        "status": "已售罄" if item.get("stockOut") else "",
                    }
                )

            paging = data.get("paging") or {}
            if not paging.get("hasMore"):
                break
        time.sleep(0.15)

    return results


def guess_smart_category(item: dict) -> str:
    tags = item.get("main_category_tag_name") or ""
    if isinstance(tags, list):
        tags = " ".join(str(tag) for tag in tags)
    text = f"{tags} {item.get('title', '')}".lower()
    if any(keyword in text for keyword in ["musical"]):
        return "音乐剧"
    if any(keyword in text for keyword in ["exhibition", "museum", "gallery", "art"]):
        return "展览"
    if any(keyword in text for keyword in ["theater", "theatre", "drama", "stage", "comedy", "performance"]):
        return "话剧"
    if any(keyword in text for keyword in ["dance", "ballet"]):
        return "舞蹈"
    if any(keyword in text for keyword in ["music", "concert", "live"]):
        return "音乐会"
    return "其他"


def collect_smartshanghai(source: dict) -> list[dict]:
    url = source.get("api_url") or "https://www.smartshanghai.com/api2/events"
    data = http_get_json(url, headers=SMART_HEADERS)
    items = data.get("data") or []
    results: list[dict] = []

    for item in items:
        category = guess_smart_category(item)
        if category == "其他":
            continue
        raw_title = clean_text(item.get("title"))
        if not raw_title:
            continue
        title = translate_to_zh(raw_title)
        raw_summary = clean_text(item.get("brief_description"))
        summary = translate_to_zh(raw_summary) if raw_summary else ""
        if raw_title and raw_title != title:
            summary = f"原名：{raw_title}。{summary}".strip()

        listing_url = clean_text(item.get("listing_url")) or clean_text(item.get("tickets_url"))
        start = ""
        match = re.search(r"(20\d{2}-\d{2}-\d{2})", listing_url)
        if match:
            start = match.group(1)

        results.append(
            {
                "title": title,
                "category": category,
                "venue": clean_text(item.get("venue_name") or item.get("venue_label")),
                "address": "",
                "start_date": start,
                "end_date": start,
                "price_text": clean_text(item.get("price")),
                "ticket_url": listing_url or clean_text(source.get("url")),
                "source_url": clean_text(source.get("url")),
                "image_url": clean_text(item.get("thumbnail_url") or item.get("compressed_thumbnail_url")),
                "summary": summary,
                "status": clean_text((item.get("listing_status") or {}).get("title")),
            }
        )

    return results


def clean_douban_location(text: str) -> str:
    value = clean_text(text)
    value = re.sub(r"^上海\s*", "", value)
    value = re.sub(r"^(黄浦区|徐汇区|长宁区|静安区|普陀区|虹口区|杨浦区|闵行区|宝山区|嘉定区|浦东新区|金山区|松江区|青浦区|奉贤区|崇明区)\s*", "", value)
    return value or "上海"


DOUBAN_CACHE_PATH = DATA_DIR / "douban_cache.json"


def collect_douban(source: dict) -> list[dict]:
    pages = source.get("pages") or ["week-drama", "week-comedy", "week-exhibition", "week-music"]
    base = source.get("base_url") or "https://shanghai.douban.com/events"
    results: list[dict] = []

    for page in pages:
        url = f"{base}/{page}"
        try:
            html_text = http_get(url, timeout=30)
        except Exception:
            continue
        soup = BeautifulSoup(html_text, "html.parser")
        page_category = DOUBAN_PAGE_CATEGORY.get(page, "其他")

        for item in soup.select("li.list-entry"):
            title_link = item.select_one(".title a[href]")
            if not title_link:
                continue
            title = clean_text(title_link.get("title") or title_link.get_text(" ", strip=True))
            if not title:
                continue

            detail_url = title_link.get("href") or url
            start_el = item.select_one("time[itemprop='startDate']")
            end_el = item.select_one("time[itemprop='endDate']")
            start = start_el.get("datetime") if start_el else ""
            end = end_el.get("datetime") if end_el else ""

            location_el = item.select_one("li[title]")
            location = clean_text(location_el.get("title")) if location_el else ""
            if not location:
                location = clean_text(item.select_one("li[title]").get_text(" ", strip=True)) if item.select_one("li[title]") else ""

            fee_el = item.select_one(".fee strong")
            price = clean_text(fee_el.get_text(" ", strip=True)) if fee_el else ""
            summary_el = item.select_one("p")
            summary = clean_text(summary_el.get_text(" ", strip=True)) if summary_el else ""

            category = page_category
            if category == "话剧" and "音乐剧" in title:
                category = "音乐剧"
            if category == "其他":
                category = guess_category(title, "")

            results.append(
                {
                    "title": title,
                    "category": category,
                    "venue": clean_douban_location(location),
                    "address": location,
                    "start_date": start,
                    "end_date": end,
                    "price_text": price,
                    "ticket_url": detail_url,
                    "source_url": url,
                    "image_url": "",
                    "summary": summary,
                    "status": "",
                }
            )
        time.sleep(0.35)

    if results:
        # 缓存最后一次成功结果，防止豆瓣偶发风控导致整块数据消失
        save_json(DOUBAN_CACHE_PATH, results)
        return results

    if DOUBAN_CACHE_PATH.exists():
        try:
            cached = json.loads(DOUBAN_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                log(f"  豆瓣直连失败，使用缓存 {len(cached)} 条")
                return cached
        except Exception:
            pass
    return results


def collect_shmuseum(source: dict) -> list[dict]:
    url = source.get("url") or "https://www.shanghaimuseum.net/"
    html_text = http_get(url)
    soup = BeautifulSoup(html_text, "html.parser")
    results: list[dict] = []

    for slide in soup.select("div.swiper-slide"):
        title_el = slide.select_one("p.title")
        if not title_el:
            continue
        paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in slide.select("p")]
        title = clean_text(title_el.get_text(" ", strip=True))
        if not title or len(paragraphs) < 2:
            continue

        date_text = paragraphs[1]
        if not re.search(r"20\d{2}", date_text):
            continue
        if "典藏精选" in title or "开展！" in title or "重磅开展" in title:
            continue
        venue = paragraphs[2] if len(paragraphs) > 2 else "上海博物馆"
        detail_link = slide.select_one('a[href*="article"], a[href*="exhibit"]')
        if detail_link:
            detail_href = detail_link["href"]
            if detail_href.startswith("http"):
                detail_url = detail_href
            elif detail_href.startswith("/"):
                detail_url = "https://www.shanghaimuseum.cn" + detail_href
            else:
                detail_url = "https://www.shanghaimuseum.cn/mu/" + detail_href
        else:
            detail_url = url

        results.append(
            {
                "title": title,
                "category": "展览",
                "venue": venue,
                "address": venue,
                "start_date": "",
                "end_date": "",
                "date_text": date_text,
                "price_text": "以官方预约页面为准",
                "ticket_url": detail_url,
                "source_url": url,
                "image_url": "",
                "summary": date_text,
                "status": "",
            }
        )

    return results


def collect_pudong(source: dict) -> list[dict]:
    url = source.get("url") or "https://www.museumofartpd.org.cn/"
    html_text = http_get(url)
    soup = BeautifulSoup(html_text, "html.parser")
    results: list[dict] = []

    for item in soup.select(".exhibitioniqlist .item"):
        title_el = item.select_one(".tit")
        date_el = item.select_one(".txt")
        link_el = item.select_one("a[href]")
        if not title_el or not link_el:
            continue
        title = clean_text(title_el.get_text(" ", strip=True))
        date_text = clean_text(date_el.get_text(" ", strip=True)) if date_el else ""
        if not title or not re.search(r"20\d{2}", date_text):
            continue
        detail_url = urljoin(url, link_el["href"])
        results.append(
            {
                "title": title,
                "category": "展览",
                "venue": "浦东美术馆",
                "address": "上海市浦东新区滨江大道2777号",
                "start_date": "",
                "end_date": "",
                "date_text": date_text,
                "price_text": "以官方预约页面为准",
                "ticket_url": detail_url,
                "source_url": url,
                "image_url": "",
                "summary": date_text,
                "status": "",
            }
        )

    return results


def collect_rockbund(source: dict) -> list[dict]:
    url = source.get("url") or "https://www.rockbundartmuseum.org/"
    html_text = http_get(url)
    soup = BeautifulSoup(html_text, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return []
    data = json.loads(script.string)
    items = data.get("props", {}).get("pageProps", {}).get("data", [])
    results: list[dict] = []

    for item in items:
        item_type = item.get("_type")
        if item_type not in ("exhibition", "event"):
            continue
        title = locale_text(item.get("title"))
        if not title:
            continue
        slug = clean_text(item.get("slug"))
        path = "exhibition" if item_type == "exhibition" else "calendar"
        detail_url = f"https://www.rockbundartmuseum.org/{path}/{slug}" if slug else url
        summary = locale_text(item.get("description"))
        category = "展览" if item_type == "exhibition" else "其他"
        results.append(
            {
                "title": title,
                "category": category,
                "venue": "上海外滩美术馆",
                "address": "上海市黄浦区虎丘路20号",
                "start_date": item.get("startDate") or "",
                "end_date": item.get("endDate") or "",
                "price_text": "以官方预约页面为准",
                "ticket_url": detail_url,
                "source_url": url,
                "image_url": extract_image_url(item.get("mainImage")),
                "summary": summary,
                "status": "",
            }
        )

    return results


def collect_psa(source: dict) -> list[dict]:
    base = source.get("api_base") or "https://www.powerstationofart.com/campus/api/feed/public/psa"
    endpoints = source.get("endpoints") or ["/whats-on/exhibitions"]
    url = source.get("url") or "https://www.powerstationofart.com/"
    results: list[dict] = []

    for endpoint in endpoints:
        data = http_get_json(
            base + endpoint,
            headers={"X-Language": "zh-Hans", "Referer": "https://www.powerstationofart.com/"},
        )
        items = data.get("items") or []
        category = "展览" if "exhibition" in endpoint else "其他"
        for item in items:
            title = clean_text(item.get("title"))
            if not title:
                continue
            slug = clean_text(item.get("slug"))
            if "exhibition" in endpoint:
                detail_url = f"https://www.powerstationofart.com/whats-on/exhibitions/{slug}"
            elif "activity" in endpoint:
                detail_url = f"https://www.powerstationofart.com/whats-on/activities/{slug}"
            else:
                detail_url = url
            results.append(
                {
                    "title": title,
                    "category": category,
                    "venue": "上海当代艺术博物馆",
                    "address": "上海市黄浦区苗江路678号",
                    "start_date": item.get("startDate") or "",
                    "end_date": item.get("endDate") or "",
                    "price_text": "以官方页面为准",
                    "ticket_url": detail_url,
                    "source_url": url,
                    "image_url": extract_image_url(item.get("image")),
                    "summary": "",
                    "status": "",
                }
            )

    return results


def collect_longmuseum(source: dict) -> list[dict]:
    url = source.get("url") or "http://www.thelongmuseum.org/exhibition-current.html"
    html_text = http_get(url)
    soup = BeautifulSoup(html_text, "html.parser")
    results: list[dict] = []

    for item in soup.select("li"):
        link_el = item.select_one('a[href*="detail-"]')
        if not link_el:
            continue
        title_el = item.select_one("h1")
        info_el = item.select_one("h2")
        summary_el = item.select_one("h3")
        if not title_el or not info_el:
            continue

        title = clean_text(title_el.get_text(" ", strip=True))
        info = clean_text(info_el.get_text(" ", strip=True))
        if "重庆" in info and "上海" not in info and "西岸" not in info and "浦东" not in info:
            continue
        if not any(keyword in info for keyword in ["西岸", "浦东", "上海"]):
            continue

        date_match = re.search(r"20\d{2}\.\d{1,2}\.\d{1,2}\s*[－\-—~至到]\s*20\d{2}\.\d{1,2}\.\d{1,2}", info)
        date_text = date_match.group(0) if date_match else ""
        if not date_text:
            continue

        venue = "龙美术馆（西岸馆）" if "西岸" in info else "龙美术馆（浦东馆）" if "浦东" in info else "龙美术馆"
        detail_url = urljoin("http://www.thelongmuseum.org/", link_el["href"])
        summary = clean_text(summary_el.get_text(" ", strip=True)) if summary_el else ""
        results.append(
            {
                "title": title,
                "category": "展览",
                "venue": venue,
                "address": "上海市徐汇区龙腾大道3398号" if "西岸" in info else "上海市浦东新区罗山路2255弄210号",
                "start_date": "",
                "end_date": "",
                "date_text": date_text,
                "price_text": "以官方页面为准",
                "ticket_url": detail_url,
                "source_url": url,
                "image_url": "",
                "summary": summary,
                "status": "",
            }
        )

    return results


# ---------------------------------------------------------------------------
# LLM 抽取
# ---------------------------------------------------------------------------

def load_llm_config(no_llm: bool = False) -> tuple[str, str, str] | None:
    if no_llm:
        return None

    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()

    if not api_key:
        github_token = os.getenv("GITHUB_TOKEN", "").strip()
        if github_token:
            api_key = github_token
            base_url = base_url or "https://models.inference.ai.azure.com"
            model = model or "gpt-4o-mini"

    if not api_key:
        return None

    base_url = base_url or "https://api.deepseek.com/v1"
    model = model or "deepseek-chat"
    return base_url.rstrip("/"), api_key, model


def call_llm(messages: list[dict[str, str]]) -> dict:
    if LLM_CONFIG is None:
        raise RuntimeError("没有配置 LLM")

    base_url, api_key, model = LLM_CONFIG
    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 4096,
    }

    response = requests.post(
        endpoint,
        headers=headers,
        json={**payload, "response_format": {"type": "json_object"}},
        timeout=LLM_TIMEOUT,
    )
    if response.status_code >= 400:
        # 有些 OpenAI 兼容接口不支持 response_format
        response = requests.post(endpoint, headers=headers, json=payload, timeout=LLM_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM 返回内容为空")

    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
    content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


def extract_with_llm(text: str, source: dict) -> list[dict]:
    if LLM_CONFIG is None:
        return []

    today = today_cn()
    window_end = today + timedelta(days=DATE_WINDOW_DAYS)
    source_name = source.get("name", "")
    hint = source.get("category_hint", "")

    system_prompt = (
        "你是一个上海线下活动信息抽取助手。"
        "只输出 JSON，不要输出解释、Markdown 或代码块。"
    )
    user_prompt = f"""请从下面的网页文本中提取所有在上海举办的线下活动，重点是展览、话剧、音乐剧、音乐会、舞蹈和戏曲。

当前日期：{today.isoformat()}
只保留从 {today.isoformat()} 到 {window_end.isoformat()} 之间可参加的活动。

输出 JSON 格式：
{{
  "events": [
    {{
      "title": "活动名称",
      "category": "展览|话剧|音乐剧|音乐会|舞蹈|戏曲|其他",
      "venue": "场馆名称",
      "address": "地址或区域，可为空字符串",
      "start_date": "YYYY-MM-DD 或空字符串",
      "end_date": "YYYY-MM-DD 或空字符串",
      "time_text": "如 19:30，可为空字符串",
      "price_text": "票价信息，可为空字符串",
      "ticket_url": "页面中出现的真实购票/预约链接，没有就空字符串",
      "summary": "一句话推荐，不超过 60 字"
    }}
  ]
}}

规则：
1. 只提取名称明确、地点在上海的活动。
2. 不要编造日期、票价、链接；不确定就留空。
3. 长期展览如果能确定展期，就填 start_date 和 end_date。
4. 同一活动只输出一次。
5. 没有符合条件的活动就返回 {{"events": []}}。

来源名称：{source_name}
类别提示：{hint}
网页文本：
{text[:MAX_LLM_TEXT]}
"""

    data = call_llm(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    events = data.get("events", []) if isinstance(data, dict) else data
    if not isinstance(events, list):
        return []
    return [item for item in events if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# 日期与分类标准化
# ---------------------------------------------------------------------------

DATE_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")


def normalize_date_text(text: Any) -> str:
    value = str(text or "").translate(DATE_TRANS)
    value = (
        value.replace("．", ".")
        .replace("。", ".")
        .replace("／", "/")
        .replace("－", "-")
        .replace("—", "-")
        .replace("–", "-")
        .replace("～", "~")
    )
    value = value.replace("年", "-").replace("月", "-").replace("日", "").replace("号", "")
    value = value.replace("/", "-").replace(".", "-")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None

    text = normalize_date_text(value)
    match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = map(int, match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)", text)
    if match:
        month, day = map(int, match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        current = today_cn()
        try:
            result = date(current.year, month, day)
        except ValueError:
            return None
        # 已经过去超过一个半月的月日，大概率是明年的
        if result < current - timedelta(days=45):
            try:
                result = date(current.year + 1, month, day)
            except ValueError:
                return None
        return result

    match = re.search(r"(20\d{2})-(\d{1,2})(?!\d)", text)
    if match:
        year, month = map(int, match.groups())
        if 1 <= month <= 12:
            try:
                return date(year, month, 1)
            except ValueError:
                return None
    return None


def extract_date_tokens(text: Any) -> list[date]:
    value = normalize_date_text(text)
    tokens: list[date] = []
    spans: list[tuple[int, int]] = []

    for match in re.finditer(r"(20\d{2})-(\d{1,2})-(\d{1,2})", value):
        try:
            tokens.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
            spans.append(match.span())
        except ValueError:
            pass

    # 去掉完整日期后，再找 "09-12" 这种缺少年份的日期。
    if spans:
        pieces: list[str] = []
        last = 0
        for start, end in spans:
            pieces.append(value[last:start])
            last = end
        pieces.append(value[last:])
        value = " ".join(pieces)

    current = today_cn()
    for month, day in re.findall(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)", value):
        month_i, day_i = int(month), int(day)
        if not (1 <= month_i <= 12 and 1 <= day_i <= 31):
            continue
        try:
            token = date(current.year, month_i, day_i)
        except ValueError:
            continue
        if token < current - timedelta(days=45):
            try:
                token = date(current.year + 1, month_i, day_i)
            except ValueError:
                continue
        tokens.append(token)

    return sorted(set(tokens))


def infer_dates(text: Any) -> tuple[date | None, date | None]:
    value = normalize_date_text(text)
    tokens = extract_date_tokens(value)
    if not tokens:
        return None, None
    if len(tokens) == 1:
        return tokens[0], tokens[0]
    if re.search(r"[~至到\-]", value):
        return tokens[0], tokens[-1]
    return tokens[0], tokens[0]


CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("音乐剧", ["音乐剧", "musical"]),
    ("脱口秀", ["脱口秀", "单口", "开放麦", "喜剧大会", "talk show"]),
    ("话剧", ["话剧", "舞台剧", "戏剧", "默剧", "喜剧"]),
    ("舞蹈", ["舞剧", "舞蹈", "芭蕾"]),
    ("戏曲", ["京剧", "昆曲", "越剧", "沪剧", "评弹", "戏曲"]),
    ("展览", ["展览", "美术馆", "博物馆", "艺术展", "特展", "双年展", "画廊", "艺术中心"]),
    ("音乐会", [
        "音乐会", "交响", "室内乐", "合唱", "钢琴", "小提琴", "大提琴",
        "爵士", "演唱会", "livehouse", "音乐现场", "音乐节",
    ]),
]
CATEGORIES = ["展览", "话剧", "音乐剧", "音乐会", "舞蹈", "戏曲", "脱口秀", "其他"]


def guess_category(text: str, hint: str = "") -> str:
    haystack = f"{text} {hint}".lower()
    scores: dict[str, int] = {}
    for category, keywords in CATEGORY_KEYWORDS:
        score = sum(1 for keyword in keywords if keyword.lower() in haystack)
        if score:
            scores[category] = score
    if hint in scores:
        scores[hint] += 2

    if scores:
        return max(scores, key=scores.get)

    for category in CATEGORIES:
        if category and (category in hint or hint in category):
            return category
    return "其他"


EXHIBITION_CATEGORIES: list[tuple[str, list[str]]] = [
    ("历史文物", [
        "历史", "文物", "考古", "古代", "文明", "汉代", "唐代", "宋代",
        "元代", "明代", "清代", "明清", "马王堆", "青铜", "陶瓷", "玉器",
        "书画", "碑帖", "遗址", "海派", "红色", "革命", "文献",
    ]),
    ("现代艺术", [
        "现代艺术", "当代艺术", "当代", "抽象", "装置", "观念", "行为艺术",
        "实验艺术", "雕塑", "绘画", "油画", "版画", "水墨",
    ]),
    ("艺术家特展", [
        "个展", "回顾展", "回顾", "作品展", "首展", "艺术家",
    ]),
    ("数字沉浸", [
        "沉浸", "数字", "VR", "AR", "元宇宙", "投影", "互动体验", "巨幕", "全景", "光影",
    ]),
    ("摄影影像", [
        "摄影", "影像", "图片", "电影", "纪录片", "胶片",
    ]),
    ("设计工艺", [
        "设计", "工艺", "珠宝", "时尚", "服饰", "家具", "器物", "匠艺",
    ]),
    ("亲子动漫", [
        "亲子", "儿童", "少儿", "家庭", "恐龙", "动漫", "国漫", "卡通",
        "童话", "乐园", "游戏", "二次元", "潮玩", "手办",
    ]),
    ("建筑城市", [
        "建筑", "城市", "规划", "景观", "公共艺术",
    ]),
    ("自然科技", [
        "自然", "科学", "科技", "极地", "天文", "生物", "海洋", "深海", "地球", "生态",
    ]),
    ("其他展览", [
        "展览", "美术馆", "艺术中心", "画廊", "艺术",
    ]),
]
EXHIBITION_SUBCATEGORIES = [category for category, _ in EXHIBITION_CATEGORIES]


def classify_exhibition(text: str) -> str:
    haystack = clean_text(text).lower()
    scores: dict[str, int] = {}
    for category, keywords in EXHIBITION_CATEGORIES:
        if category == "其他展览":
            continue
        score = sum(1 for keyword in keywords if keyword.lower() in haystack)
        if score:
            scores[category] = score
    if not scores:
        return "其他展览"
    return max(scores, key=scores.get)


DISTRICTS = [
    "黄浦", "徐汇", "长宁", "静安", "普陀", "虹口", "杨浦", "闵行",
    "宝山", "嘉定", "浦东", "金山", "松江", "青浦", "奉贤", "崇明",
]


def find_district(*texts: Any) -> str:
    haystack = " ".join(clean_text(text) for text in texts if text)
    for district in DISTRICTS:
        if district in haystack:
            return district
    return ""


def first_present(raw: dict, *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def normalize_event(raw: dict, source: dict) -> dict | None:
    title = clean_text(first_present(raw, "title", "name"))
    if len(title) < 2:
        return None

    summary = clean_text(first_present(raw, "summary", "description", "desc"))
    venue = clean_text(first_present(raw, "venue", "location", "place"))
    address = clean_text(first_present(raw, "address", "addr"))
    if not venue and address:
        venue = address
    if not venue:
        venue = "场馆待确认"

    category = clean_text(first_present(raw, "category", "type"))
    if category not in CATEGORIES:
        category = guess_category(
            " ".join([title, summary, venue, address]),
            clean_text(source.get("category_hint", "")),
        )

    raw_start = first_present(raw, "start_date", "startDate", "start")
    raw_end = first_present(raw, "end_date", "endDate", "end")
    start = parse_date(raw_start)
    end = parse_date(raw_end)

    time_text = clean_text(first_present(raw, "time_text", "time", "show_time"))
    if not time_text:
        time_text = extract_time_text(" ".join([str(raw_start), str(raw_end), summary, title]))
    date_text = clean_text(first_present(raw, "date_text", "date"))

    if not start and not end:
        start, end = infer_dates(" ".join([date_text, summary, title, address]))

    if not start and end:
        start = end
    if start and not end:
        end = start

    price_text = clean_text(first_present(raw, "price_text", "price", "prices"))
    if not price_text:
        price_text = "以页面为准"

    ticket_url = clean_text(first_present(raw, "ticket_url", "url", "link"))
    source_url = clean_text(first_present(raw, "source_url")) or source.get("url", "")
    if not ticket_url:
        ticket_url = source_url

    status = clean_text(first_present(raw, "status", "event_status"))
    text_for_status = f"{title} {summary}"
    if not status:
        if "取消" in text_for_status:
            status = "已取消"
        elif "延期" in text_for_status:
            status = "已延期"
        elif "售罄" in text_for_status:
            status = "已售罄"

    image_url = clean_text(first_present(raw, "image_url", "image"))
    district = clean_text(first_present(raw, "district")) or find_district(venue, address, title)
    # 城市：优先用采集模块给的城市名，其次用 source 配置，最后默认上海
    city = (
        clean_text(first_present(raw, "city", "city_name", "cityName"))
        or clean_text(source.get("city"))
        or SHANGHAI_CITY
    )

    display_category = category
    sub_category = ""
    if category == "展览":
        sub_category = classify_exhibition(" ".join([title, summary, venue, address]))
        display_category = sub_category

    unique = f"{title}|{venue}|{start.isoformat() if start else ''}"
    event_id = hashlib.sha1(unique.encode("utf-8")).hexdigest()[:16]

    return {
        "id": event_id,
        "title": title,
        "category": category,
        "display_category": display_category,
        "sub_category": sub_category,
        "venue": venue,
        "district": district,
        "city": city,
        "address": address,
        "start_date": start.isoformat() if start else "",
        "end_date": end.isoformat() if end else "",
        "time_text": time_text,
        "price_text": price_text,
        "ticket_url": ticket_url,
        "source_url": source_url,
        "source_name": source.get("name", ""),
        "summary": summary,
        "image_url": image_url,
        "status": status,
        "last_updated": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 去重与合并
# ---------------------------------------------------------------------------

def normalized_key(text: str) -> str:
    return re.sub(r"[\s\-—_·,，。.!！?？:：;；/\\()（）\[\]【】《》\"'“”‘’]+", "", text or "").lower()


def event_key(event: dict) -> str:
    return "|".join(
        [
            normalized_key(event.get("city", "")),
            normalized_key(event.get("title", "")),
            normalized_key(event.get("venue", "")),
            event.get("start_date", ""),
        ]
    )


def merge_events(primary: dict, other: dict) -> dict:
    preferred_status = ["已取消", "已延期", "已售罄", "", "在售"]
    for field in (
        "summary", "venue", "district", "city", "address", "start_date", "end_date",
        "time_text", "price_text", "ticket_url", "source_url", "image_url", "status",
        "display_category", "sub_category",
    ):
        if not primary.get(field) and other.get(field):
            primary[field] = other[field]
        elif field == "summary" and len(other.get(field, "")) > len(primary.get(field, "")):
            primary[field] = other[field]

    if other.get("status"):
        def status_rank(value: str) -> int:
            try:
                return preferred_status.index(value)
            except ValueError:
                return len(preferred_status)
        if status_rank(other["status"]) < status_rank(primary.get("status", "")):
            primary["status"] = other["status"]

    names = set(filter(None, [primary.get("source_name", ""), other.get("source_name", "")]))
    primary["source_name"] = " / ".join(sorted(names))
    return primary


def enrich_time_from_title(events: list[dict]) -> list[dict]:
    """同名活动可能来自不同平台，把有场次时间的一边补到另一边。"""
    time_by_title: dict[str, str] = {}
    for event in events:
        if event.get("time_text"):
            key = normalized_key(event.get("title", ""))
            if key and key not in time_by_title:
                time_by_title[key] = event["time_text"]

    for event in events:
        if event.get("time_text"):
            continue
        key = normalized_key(event.get("title", ""))
        if key in time_by_title:
            event["time_text"] = time_by_title[key]
    return events


def dedupe_events(events: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for event in events:
        key = event_key(event)
        if key in merged:
            merged[key] = merge_events(merged[key], event)
        else:
            merged[key] = event

    def sort_key(event: dict):
        return (event.get("start_date") or "9999-99-99", event.get("title", ""))

    return sorted(merged.values(), key=sort_key)


# ---------------------------------------------------------------------------
# 窗口过滤与渲染数据
# ---------------------------------------------------------------------------

def event_city(event: dict) -> str:
    return clean_text(event.get("city")) or SHANGHAI_CITY


def is_shanghai(event: dict) -> bool:
    return event_city(event) == SHANGHAI_CITY


def national_allowed(event: dict) -> bool:
    """外地活动只收录：历史文物类展览、音乐剧、话剧。"""
    if is_shanghai(event):
        return True
    group = event.get("category", "")
    if group in NATIONAL_GROUPS:
        return True
    if group == "展览" and (event.get("display_category") or "") in NATIONAL_EXHIBITION_SUBS:
        return True
    return False


def title_key(event: dict) -> str:
    """「上新」的身份键：只看归一化标题。

    日期/场馆在每次抓取里都可能抖动（滚动场次、场馆名多写一个字），
    用 id 或 title+venue+date 判断会把老活动误报成新增。
    """
    return normalized_key(event.get("title", ""))


def compact_event(event: dict) -> dict:
    fields = (
        "id", "title", "category", "display_category", "venue", "district", "city",
        "start_date", "end_date", "time_text", "price_text", "ticket_url",
        "source_url", "source_name", "summary", "status",
    )
    return {field: event.get(field, "") for field in fields}

def event_overlaps(event: dict, start: date, end: date) -> bool:
    event_start = parse_date(event.get("start_date"))
    event_end = parse_date(event.get("end_date")) or event_start
    if event_start is None:
        return False
    return event_start <= end and event_end >= start


def compute_months(event: dict, today: date) -> str:
    months: list[str] = ["all"]
    event_start = parse_date(event.get("start_date"))
    event_end = parse_date(event.get("end_date")) or event_start
    if event_start is None:
        return "unknown all"

    current_start = date(today.year, today.month, 1)
    if today.month == 12:
        next_start = date(today.year + 1, 1, 1)
    else:
        next_start = date(today.year, today.month + 1, 1)
    if next_start.month == 12:
        next_end = date(next_start.year + 1, 1, 1) - timedelta(days=1)
    else:
        next_end = date(next_start.year, next_start.month + 1, 1) - timedelta(days=1)

    if event_start <= next_end and event_end >= current_start:
        if event_start < next_start and event_end >= current_start:
            months.append("current")
        if event_start <= next_end and event_end >= next_start:
            months.append("next")
    return " ".join(dict.fromkeys(months))


def format_event_date(event: dict) -> str:
    start = parse_date(event.get("start_date"))
    end = parse_date(event.get("end_date"))
    if start and end:
        if start == end:
            text = f"{start.year}.{start.month:02d}.{start.day:02d}"
        elif start.year == end.year:
            text = (
                f"{start.year}.{start.month:02d}.{start.day:02d}"
                f" - {end.month:02d}.{end.day:02d}"
            )
        else:
            text = (
                f"{start.year}.{start.month:02d}.{start.day:02d}"
                f" - {end.year}.{end.month:02d}.{end.day:02d}"
            )
    elif start:
        text = f"{start.year}.{start.month:02d}.{start.day:02d} 起"
    elif end:
        text = f"至 {end.year}.{end.month:02d}.{end.day:02d}"
    else:
        text = event.get("time_text") or "日期待确认"
    if event.get("time_text") and event.get("time_text") not in text:
        text = f"{text} {event['time_text']}"
    return text


CATEGORY_CLASS = {
    "展览": "exhibit",
    "话剧": "drama",
    "音乐剧": "musical",
    "音乐会": "concert",
    "舞蹈": "dance",
    "戏曲": "opera",
    "脱口秀": "talk",
    "其他": "other",
}


STYLE = """
:root {
  --bg: #f7f7f5;
  --card: #ffffff;
  --text: #1d1d1f;
  --muted: #6e6e73;
  --line: #e5e5e7;
  --accent: #ff5a5f;
  --accent-soft: #fff0f0;
  --shadow: 0 8px 28px rgba(0, 0, 0, .07);
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  line-height: 1.55;
}
.hero { padding: 26px 18px 14px; max-width: 980px; margin: 0 auto; }
.eyebrow { margin: 0 0 4px; color: var(--accent); font-weight: 700; font-size: 13px; letter-spacing: .08em; }
h1 { margin: 0; font-size: 30px; line-height: 1.2; }
.updated { margin: 8px 0 0; color: var(--muted); font-size: 13px; }
.toolbar {
  position: sticky; top: 0; z-index: 10;
  background: rgba(247, 247, 245, .94);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--line);
  padding: 10px 18px;
}
.toolbar-inner { max-width: 980px; margin: 0 auto; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.tabs { display: flex; gap: 6px; background: #ececeb; padding: 4px; border-radius: 12px; }
.tab {
  border: 0; background: transparent; color: var(--muted);
  padding: 7px 14px; border-radius: 9px; font-size: 14px; cursor: pointer;
}
.tab.active { background: #fff; color: var(--text); box-shadow: 0 1px 5px rgba(0,0,0,.08); font-weight: 600; }
#search {
  flex: 1; min-width: 190px; border: 1px solid var(--line); background: #fff;
  border-radius: 12px; padding: 9px 13px; font-size: 14px; outline: none;
}
#search:focus { border-color: #bbb; }
.chips { max-width: 980px; margin: 12px auto 0; padding: 0 18px; display: flex; gap: 8px; flex-wrap: wrap; }
.chip {
  border: 1px solid var(--line); background: #fff; color: var(--muted);
  border-radius: 999px; padding: 6px 12px; font-size: 13px; cursor: pointer;
}
.chip.active { background: var(--text); border-color: var(--text); color: #fff; }
main { max-width: 980px; margin: 0 auto; padding: 16px 18px 40px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.card {
  background: var(--card); border-radius: 18px; padding: 16px;
  box-shadow: var(--shadow); display: flex; flex-direction: column; gap: 9px;
  border: 1px solid rgba(0,0,0,.02);
}
.card-top { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
.badge { display: inline-block; border-radius: 999px; padding: 3px 9px; font-size: 12px; font-weight: 700; white-space: nowrap; }
.badge.exhibit { background: #e8f2ff; color: #1565c0; }
.badge.drama { background: #fff4e5; color: #b35c00; }
.badge.musical { background: #ffe9f3; color: #c2185b; }
.badge.concert { background: #eef7ea; color: #2e7d32; }
.badge.dance { background: #f1eaff; color: #6a35c2; }
.badge.opera { background: #fff0f0; color: #c62828; }
.badge.talk { background: #e8f7f0; color: #00796b; }
.badge.other { background: #f0f0f2; color: #555; }
.date { color: var(--muted); font-size: 12px; text-align: right; }
.when {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  background: #fff4e5; color: #9a4b00; border-radius: 12px;
  padding: 8px 10px; font-size: 14px; font-weight: 700;
}
.when.exhibit { background: #eef6ff; color: #1565c0; }
.when.musical, .when.concert, .when.drama, .when.dance, .when.opera, .when.talk { background: #fff4e5; color: #9a4b00; }
.when .when-icon { font-size: 13px; }
.when .when-date { letter-spacing: .02em; }
.when .when-time {
  background: var(--accent); color: #fff; border-radius: 999px;
  padding: 3px 9px; font-size: 13px; font-weight: 800;
}
.card h2 { margin: 0; font-size: 18px; line-height: 1.35; }
.meta { margin: 0; color: var(--muted); font-size: 13px; }
.summary { margin: 0; color: #444; font-size: 14px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.card-bottom { margin-top: auto; display: flex; align-items: center; justify-content: space-between; gap: 10px; padding-top: 4px; }
.price { color: var(--accent); font-weight: 700; font-size: 13px; }
.button {
  display: inline-block; text-decoration: none; background: var(--accent); color: #fff;
  padding: 7px 12px; border-radius: 10px; font-size: 13px; font-weight: 700; white-space: nowrap;
}
.status { color: #b35c00; font-size: 12px; font-weight: 700; }
.empty { max-width: 980px; margin: 20px auto; padding: 30px 18px; color: var(--muted); text-align: center; }
.site-nav { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.site-nav a {
  display: inline-flex; align-items: center; gap: 5px;
  text-decoration: none; color: var(--muted); background: #fff;
  border: 1px solid var(--line); border-radius: 999px;
  padding: 6px 12px; font-size: 13px; font-weight: 600;
}
.site-nav a:hover { color: var(--text); border-color: #d0d0d4; }
.site-nav a.active { background: var(--text); border-color: var(--text); color: #fff; }
footer { max-width: 980px; margin: 0 auto; padding: 0 18px 40px; color: #999; font-size: 12px; }
.updates-panel { max-width: 980px; margin: 16px auto 0; padding: 0 18px; }
.updates-inner {
  background: linear-gradient(135deg, #ffffff 0%, #fff7f7 100%);
  border: 1px solid #ffe3e3; border-radius: 18px; padding: 16px;
  box-shadow: var(--shadow);
  display: grid; grid-template-columns: 1fr auto; gap: 6px 14px; align-items: start;
}
.updates-main { min-width: 0; }
.updates-kicker { margin: 0 0 2px; color: var(--accent); font-size: 12px; font-weight: 800; letter-spacing: .06em; }
.updates-count { margin: 0; font-size: 28px; font-weight: 800; line-height: 1.15; }
.updates-count span { font-size: 14px; font-weight: 600; color: var(--muted); }
.updates-meta { margin: 4px 0 0; color: var(--muted); font-size: 13px; }
.updates-link {
  align-self: start; text-decoration: none; color: var(--accent); font-weight: 700; font-size: 13px;
  background: #fff; border: 1px solid #ffd6d6; border-radius: 999px; padding: 7px 13px; white-space: nowrap;
}
.updates-list {
  grid-column: 1 / -1; list-style: none; margin: 6px 0 0; padding: 0;
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 16px;
}
.updates-list li { display: flex; align-items: baseline; gap: 6px; min-width: 0; font-size: 13px; }
.updates-list a { color: var(--text); text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.updates-list a:hover { color: var(--accent); }
.updates-list .up-city { color: var(--muted); font-size: 12px; white-space: nowrap; }
.updates-list .up-more { color: var(--muted); grid-column: 1 / -1; }
.badge-row { display: flex; gap: 6px; flex-wrap: wrap; }
.badge.new { background: var(--accent); color: #fff; }
.card.is-new { border-color: #ffd9d9; }
.chips.city-chips { margin-top: 8px; }
.timeline { max-width: 980px; margin: 0 auto; padding: 6px 18px 0; }
.run { margin-top: 20px; }
.run-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.run-head h2 { margin: 0; font-size: 18px; }
.run-stat { color: var(--accent); font-weight: 700; font-size: 13px; }
.run-note { margin: 0 0 10px; color: var(--muted); font-size: 13px; }
.run-cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.timeline-empty { color: var(--muted); text-align: center; padding: 30px 0; }
details.older { max-width: 980px; margin: 26px auto 0; padding: 0 18px 40px; }
details.older summary { cursor: pointer; color: var(--muted); font-weight: 700; font-size: 14px; padding: 10px 0; }
@media (max-width: 720px) {
  .updates-list, .run-cards { grid-template-columns: 1fr; }
  .updates-inner { grid-template-columns: 1fr; }
}
@media (max-width: 720px) {
  main { grid-template-columns: 1fr; padding-top: 12px; }
  h1 { font-size: 25px; }
  .toolbar-inner { gap: 8px; }
}
"""


def format_event_date_parts(event: dict) -> tuple[str, str]:
    full = format_event_date(event)
    time_text = clean_text(event.get("time_text", ""))
    if time_text and full.endswith(time_text):
        return full[: -len(time_text)].strip(), time_text
    return full, ""


def build_card(event: dict, months: str, is_new: bool = False) -> str:
    group = event.get("category") or "其他"
    category = event.get("display_category") or group
    category_class = CATEGORY_CLASS.get(group, "other")
    title = html_lib.escape(event.get("title", ""))
    venue = html_lib.escape(event.get("venue", ""))
    city = event_city(event)
    national = city != SHANGHAI_CITY
    district = event.get("district", "")
    if national:
        meta = f"{html_lib.escape(city)} · {venue}"
    else:
        meta = venue + (f" · {html_lib.escape(district)}" if district else "")
    scope = "national" if national else "sh"
    summary = html_lib.escape(event.get("summary", ""))
    price = html_lib.escape(event.get("price_text", "以页面为准"))
    date_text, time_text = format_event_date_parts(event)
    date_html = html_lib.escape(date_text)
    when_icon = "📅" if group == "展览" else "🕒"
    time_html = (
        f'<span class="when-time">{html_lib.escape(time_text)}</span>'
        if time_text
        else ""
    )
    status = event.get("status", "")
    status_html = f'<span class="status">{html_lib.escape(status)}</span>' if status else ""
    link = event.get("ticket_url") or event.get("source_url") or "#"
    link = html_lib.escape(link, quote=True)
    search_text = html_lib.escape(
        " ".join([
            event.get("title", ""),
            event.get("venue", ""),
            city,
            district,
            event.get("summary", ""),
            category,
            group,
            time_text,
        ]),
        quote=True,
    )
    new_badge = '<span class="badge new">NEW</span>' if is_new else ""
    card_class = "card is-new" if is_new else "card"
    return f"""
      <article class="{card_class}" data-month="{months}" data-scope="{scope}" data-city="{html_lib.escape(city, quote=True)}" data-category="{html_lib.escape(category, quote=True)}" data-group="{html_lib.escape(group, quote=True)}" data-search="{search_text.lower()}">
        <div class="card-top">
          <span class="badge-row"><span class="badge {category_class}">{html_lib.escape(category)}</span>{new_badge}</span>
        </div>
        <h2>{title}</h2>
        <div class="when {category_class}">
          <span class="when-icon">{when_icon}</span>
          <span class="when-date">{date_html}</span>
          {time_html}
        </div>
        <p class="meta">{meta}</p>
        <p class="summary">{summary}</p>
        <div class="card-bottom">
          <span>{status_html}<span class="price">{price}</span></span>
          <a class="button" href="{link}" target="_blank" rel="noopener noreferrer">查看详情 →</a>
        </div>
      </article>"""


def build_html(
    events: list[dict],
    generated_at: datetime,
    updates: dict | None = None,
    new_titles: set[str] | None = None,
) -> str:
    today = today_cn()
    window_end = today + timedelta(days=DATE_WINDOW_DAYS)
    new_titles = new_titles or set()
    visible = [
        event
        for event in events
        if national_allowed(event) and event_overlaps(event, today - timedelta(days=31), window_end)
    ]
    visible.sort(
        key=lambda event: (
            event_city(event) != SHANGHAI_CITY,
            event.get("start_date") or "9999-99-99",
            event.get("title", ""),
        )
    )

    cards = "\n".join(
        build_card(event, compute_months(event, today), is_new=title_key(event) in new_titles)
        for event in visible
    )
    filter_order = EXHIBITION_SUBCATEGORIES + [c for c in CATEGORIES if c != "展览"]
    present: set[str] = set()
    for event in visible:
        group = event.get("category", "其他")
        present.add(group)
        if group == "展览":
            present.add(event.get("display_category") or "其他展览")
    categories = [category for category in filter_order if category in present]

    chips = ['<button class="chip active" data-cat="all">全部</button>']
    chips.extend(f'<button class="chip" data-cat="{html_lib.escape(cat, quote=True)}">{html_lib.escape(cat)}</button>' for cat in categories)

    city_counts: dict[str, int] = {}
    for event in visible:
        city = event_city(event)
        if city != SHANGHAI_CITY:
            city_counts[city] = city_counts.get(city, 0) + 1
    city_chips = ['<button class="chip active" data-city="all">全部城市</button>']
    city_chips.extend(
        f'<button class="chip" data-city="{html_lib.escape(city, quote=True)}">{html_lib.escape(city)} {num}</button>'
        for city, num in sorted(city_counts.items(), key=lambda item: (-item[1], item[0]))
    )

    updated = generated_at.strftime("%Y-%m-%d %H:%M")
    count = len(visible)
    sh_count = sum(1 for event in visible if is_shanghai(event))
    national_count = count - sh_count
    if count:
        subtitle = f"更新于 {updated} · 近期 {count} 个活动（上海 {sh_count}"
        subtitle += f" · 外地 {national_count}）" if national_count else "）"
    else:
        subtitle = f"更新于 {updated} · 暂时没有抓到活动"

    updates_panel = updates_panel_html(updates) if updates else ""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>上海 &amp; 全国本月去哪玩</title>
  <meta name="description" content="上海展览、话剧、音乐剧自动汇总，历史文物/音乐剧/话剧覆盖全国主要城市">
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header class="hero">
    <p class="eyebrow">周末去哪玩 · 上海 &amp; 全国</p>
    <h1>本月展览 / 话剧 / 音乐剧</h1>
    <p class="updated">{subtitle}</p>
    <nav class="site-nav" aria-label="站点导航">
      <a class="active" href="index.html" aria-current="page">🎫 活动清单</a>
      <a href="map.html">🗺 建筑地图</a>
      <a href="new.html">🆕 本周上新</a>
    </nav>
  </header>
  {updates_panel}
  <div class="toolbar">
    <div class="toolbar-inner">
      <div class="tabs tabs-range" role="group" aria-label="范围">
        <button class="tab active" data-scope="sh">上海</button>
        <button class="tab" data-scope="all">全国</button>
      </div>
      <div class="tabs" role="tablist" aria-label="时间">
        <button class="tab active" data-tab="current">本月</button>
        <button class="tab" data-tab="next">下月</button>
        <button class="tab" data-tab="all">全部</button>
      </div>
      <input id="search" type="search" placeholder="搜名称 / 场馆 / 城市">
    </div>
  </div>
  <div class="chips" id="chips">
    {''.join(chips)}
  </div>
  <div class="chips city-chips" id="city-chips" hidden>
    {''.join(city_chips)}
  </div>
  <main id="event-list">
    {cards}
  </main>
  <div id="empty" class="empty" hidden>没有符合条件的活动，换个筛选试试。</div>
  <footer>
    信息来自公开网页，以官方购票/预约页面为准。如有延期、取消或售罄，请以官方最新信息为准。
  </footer>
  <script>
    const state = {{ tab: 'current', cat: 'all', q: '', scope: 'sh', city: 'all' }};
    const cards = Array.from(document.querySelectorAll('#event-list .card'));
    const empty = document.getElementById('empty');
    const search = document.getElementById('search');
    const cityChips = document.getElementById('city-chips');

    function applyFilters() {{
      let visible = 0;
      cards.forEach(card => {{
        const months = (card.dataset.month || '').split(' ');
        const category = card.dataset.category || '';
        const group = card.dataset.group || category;
        const text = card.dataset.search || '';
        const scope = card.dataset.scope || 'sh';
        const city = card.dataset.city || '';
        const monthOk = state.tab === 'all' || months.includes(state.tab);
        const catOk = state.cat === 'all' || category === state.cat || group === state.cat;
        const searchOk = !state.q || text.includes(state.q);
        const scopeOk = state.scope === 'all' || scope === 'sh';
        const cityOk = state.scope !== 'all' || state.city === 'all' || city === state.city;
        const show = monthOk && catOk && searchOk && scopeOk && cityOk;
        card.hidden = !show;
        if (show) visible += 1;
      }});
      empty.hidden = visible > 0;
      if (cityChips) cityChips.hidden = state.scope !== 'all';
    }}

    document.querySelectorAll('.tabs-range .tab').forEach(button => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('.tabs-range .tab').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        state.scope = button.dataset.scope;
        if (state.scope !== 'all') {{
          state.city = 'all';
          document.querySelectorAll('#city-chips .chip').forEach(item => item.classList.remove('active'));
          const first = document.querySelector('#city-chips .chip[data-city="all"]');
          if (first) first.classList.add('active');
        }}
        applyFilters();
      }});
    }});

    document.querySelectorAll('[data-tab]').forEach(button => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('[data-tab]').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        state.tab = button.dataset.tab;
        applyFilters();
      }});
    }});

    document.querySelectorAll('#chips .chip').forEach(button => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('#chips .chip').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        state.cat = button.dataset.cat;
        applyFilters();
      }});
    }});

    document.querySelectorAll('#city-chips .chip').forEach(button => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('#city-chips .chip').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        state.city = button.dataset.city;
        applyFilters();
      }});
    }});

    search.addEventListener('input', () => {{
      state.q = search.value.trim().toLowerCase();
      applyFilters();
    }});

    applyFilters();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 「本周上新」：每次运行的新增记录 / 时间线页面
# ---------------------------------------------------------------------------

def load_updates() -> dict:
    if not UPDATES_PATH.exists():
        return {"version": 1, "runs": []}
    try:
        payload = json.loads(UPDATES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "runs": []}
    if not isinstance(payload, dict):
        return {"version": 1, "runs": []}
    runs = payload.get("runs")
    if not isinstance(runs, list):
        runs = []
    return {"version": 1, "runs": [run for run in runs if isinstance(run, dict)]}


def load_previous_events() -> list[dict]:
    """读上一轮生成的 data/events.json（在覆盖之前调用）。"""
    path = DATA_DIR / "events.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    events = payload.get("events") if isinstance(payload, dict) else payload
    return [event for event in (events or []) if isinstance(event, dict)]


def record_update(
    previous_events: list[dict],
    current_events: list[dict],
    generated_at: datetime,
) -> dict:
    """把本轮相对上一轮的新增活动追加进 data/updates.json。"""
    updates = load_updates()
    previous_keys = {title_key(event) for event in previous_events if title_key(event)}
    current_keys = {title_key(event) for event in current_events if title_key(event)}

    added = [
        compact_event(event)
        for event in current_events
        if title_key(event)
        and title_key(event) not in previous_keys
        and national_allowed(event)
    ]
    added.sort(
        key=lambda event: (
            event_city(event) != SHANGHAI_CITY,
            event.get("start_date") or "9999-99-99",
            event.get("title", ""),
        )
    )

    run_entry = {
        "at": generated_at.isoformat(timespec="seconds"),
        "added": added,
        "added_count": len(added),
        "added_national": sum(1 for event in added if not is_shanghai(event)),
        "removed": len(previous_keys - current_keys),
        "total": len(current_events),
        "baseline": not previous_events,
    }
    runs = [run for run in updates["runs"] if run.get("at") != run_entry["at"]]
    runs.append(run_entry)
    runs.sort(key=lambda run: run.get("at", ""))
    updates["runs"] = runs[-UPDATES_KEEP_RUNS:]

    # 滚出 7 天窗口的旧记录只保留前几十条，避免 updates.json 越来越大
    cutoff = generated_at - timedelta(days=NEW_DAYS)
    for run in updates["runs"]:
        try:
            when = datetime.fromisoformat(str(run.get("at", "")))
        except Exception:
            continue
        added = run.get("added")
        if when < cutoff and isinstance(added, list) and len(added) > NEW_PAGE_RUN_CAP:
            run["added"] = added[:NEW_PAGE_RUN_CAP]
            run["trimmed"] = True
    return updates


def save_updates(updates: dict) -> None:
    save_json(UPDATES_PATH, updates)


def recent_runs(updates: dict, days: int = NEW_DAYS, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(SHANGHAI_TZ)
    threshold = now - timedelta(days=days)
    result: list[dict] = []
    for run in updates.get("runs", []):
        try:
            when = datetime.fromisoformat(str(run.get("at", "")))
        except Exception:
            continue
        if when >= threshold:
            result.append(run)
    return result


def week_new_events(updates: dict, now: datetime | None = None) -> list[dict]:
    """近 7 天新增的活动，按标题去重，新的在前。"""
    seen: set[str] = set()
    collected: list[dict] = []
    for run in sorted(recent_runs(updates, now=now), key=lambda run: run.get("at", ""), reverse=True):
        for event in run.get("added", []):
            key = title_key(event)
            if key and key not in seen:
                seen.add(key)
                collected.append(event)
    return collected


def recent_new_titles(updates: dict, now: datetime | None = None) -> set[str]:
    return {title_key(event) for event in week_new_events(updates, now=now) if title_key(event)}


def fmt_run_time(value: str) -> str:
    try:
        when = datetime.fromisoformat(str(value))
    except Exception:
        return ""
    return f"{when.year}年{when.month}月{when.day}日 {when.strftime('%H:%M')}"


def updates_panel_html(updates: dict) -> str:
    """主页顶部的「本周上新」区块。"""
    if not updates.get("runs"):
        return ""
    week_events = week_new_events(updates)
    week_count = len(week_events)
    week_sh = sum(1 for event in week_events if is_shanghai(event))
    week_nat = week_count - week_sh
    last = updates["runs"][-1]
    last_time = fmt_run_time(last.get("at", ""))

    items: list[str] = []
    for event in week_events[:6]:
        title = html_lib.escape(event.get("title", ""))
        link = html_lib.escape(
            event.get("ticket_url") or event.get("source_url") or "new.html", quote=True
        )
        city = html_lib.escape(event_city(event))
        items.append(
            f'<li><a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a>'
            f'<span class="up-city">{city}</span></li>'
        )
    if week_count > 6:
        items.append(f'<li class="up-more">还有 {week_count - 6} 个…</li>')
    if not week_count:
        items.append('<li class="up-more">这 7 天还没有新活动，等下一次自动更新。</li>')

    scope_note = f"上海 {week_sh} · 外地 {week_nat}" if week_nat else f"上海 {week_sh}"
    return f"""
  <section class="updates-panel">
    <div class="updates-inner">
      <div class="updates-main">
        <p class="updates-kicker">本周上新 · 近 {NEW_DAYS} 天</p>
        <p class="updates-count">+{week_count}<span> 个新活动</span></p>
        <p class="updates-meta">{scope_note} · 共 {len(updates.get('runs', []))} 次更新记录 · 最近一次 {last_time}（新增 {last.get('added_count', 0)} 个）</p>
      </div>
      <a class="updates-link" href="new.html">看时间线 →</a>
      <ul class="updates-list">
        {''.join(items)}
      </ul>
    </div>
  </section>"""


BULK_RUN_LIMIT = 150  # 单轮新增超过这个数（比如第一次接入全国源）就走汇总模式


def render_run(run: dict, cap: int = NEW_PAGE_RUN_CAP) -> str:
    added = [event for event in run.get("added", []) if isinstance(event, dict)]
    local_events = [event for event in added if is_shanghai(event)]
    national_events = [event for event in added if not is_shanghai(event)]
    bulk = len(added) > BULK_RUN_LIMIT

    if bulk and not run.get("baseline"):
        cards = local_events[:cap] + national_events[:12]
    else:
        cards = added[:cap]
    hidden = len(added) - len(cards)
    cards_html = "\n".join(build_card(event, "all") for event in cards)

    stat = f"新增 {len(added)} 个"
    if national_events:
        stat += f"（外地 {len(national_events)} 个）"

    note = ""
    if run.get("baseline"):
        note = '<p class="run-note">这是第一次记录，全部在架活动都算新增。</p>'
    elif bulk:
        city_counts: dict[str, int] = {}
        for event in national_events:
            city = event_city(event)
            city_counts[city] = city_counts.get(city, 0) + 1
        top_cities = sorted(city_counts.items(), key=lambda item: (-item[1], item[0]))
        breakdown = " · ".join(f"{city} {num}" for city, num in top_cities[:12])
        if len(top_cities) > 12:
            breakdown += f" 等 {len(top_cities)} 个城市"
        note = (
            f'<p class="run-note">这一轮第一次大规模收录全国场次：外地 {len(national_events)} 个'
            f"（{breakdown}）在这里只做汇总，下面列的是上海新增 {len(local_events)} 个和少量外地示例；"
            "完整外地清单可以在「活动清单」里切到全国查看。</p>"
        )
    elif hidden > 0:
        note = (
            f'<p class="run-note">本轮新增 {len(added)} 个，这里只展示最早开始的 {len(cards)} 个；'
            "完整清单可以在活动清单里切到「全国」按城市查看。</p>"
        )

    return f"""
    <section class="run" data-at="{html_lib.escape(str(run.get('at', '')), quote=True)}">
      <header class="run-head">
        <h2>{fmt_run_time(run.get('at', ''))}</h2>
        <span class="run-stat">{stat}</span>
      </header>
      {note}
      <div class="run-cards">
        {cards_html}
      </div>
    </section>"""


NEW_PAGE_SCRIPT = """
<script>
  const scopeChips = Array.from(document.querySelectorAll('.scope-chip'));
  const cards = Array.from(document.querySelectorAll('.run-cards .card'));
  const runs = Array.from(document.querySelectorAll('.run'));
  let scope = 'all';

  function applyScope() {
    let shown = 0;
    cards.forEach(card => {
      const ok = scope === 'all' || card.dataset.scope === scope;
      card.hidden = !ok;
      if (ok) shown += 1;
    });
    runs.forEach(run => {
      run.hidden = run.querySelectorAll('.card:not([hidden])').length === 0;
    });
    const empty = document.getElementById('timeline-empty');
    if (empty) empty.hidden = shown > 0;
  }

  scopeChips.forEach(btn => {
    btn.addEventListener('click', () => {
      scopeChips.forEach(item => item.classList.remove('active'));
      btn.classList.add('active');
      scope = btn.dataset.scope;
      applyScope();
    });
  });

  applyScope();
</script>
"""


def build_new_page(updates: dict, generated_at: datetime) -> str:
    all_runs = [run for run in updates.get("runs", []) if run.get("added")]
    all_runs.sort(key=lambda run: run.get("at", ""), reverse=True)
    recent_at = {run.get("at") for run in recent_runs(updates)}
    recent = [run for run in all_runs if run.get("at") in recent_at]
    older = [run for run in all_runs if run.get("at") not in recent_at]

    week_events = week_new_events(updates)
    week_count = len(week_events)
    week_sh = sum(1 for event in week_events if is_shanghai(event))
    week_nat = week_count - week_sh

    recent_html = "".join(render_run(run) for run in recent)
    older_html = "".join(render_run(run, cap=12) for run in older)
    older_block = ""
    if older_html:
        older_block = f"""
  <details class="older">
    <summary>更早的更新（{len(older)} 次）</summary>
    {older_html}
  </details>"""

    scope_chips = """
    <button class="chip scope-chip active" data-scope="all">全部</button>
    <button class="chip scope-chip" data-scope="sh">上海</button>
    <button class="chip scope-chip" data-scope="national">外地</button>"""

    if recent_html:
        timeline = f'<div class="timeline">{recent_html}</div>'
    else:
        timeline = (
            '<div class="timeline"><p class="timeline-empty" id="timeline-empty">'
            f"最近 {NEW_DAYS} 天还没有新增活动，等下一次自动更新。</p></div>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>本周上新 · 上海 &amp; 全国展演</title>
  <meta name="description" content="每次自动更新新增了哪些展览、话剧和音乐剧">
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header class="hero">
    <p class="eyebrow">更新日志 · 上海 &amp; 全国</p>
    <h1>本周上新</h1>
    <p class="updated">近 {NEW_DAYS} 天新增 {week_count} 个活动（上海 {week_sh} · 外地 {week_nat}）· 页面更新于 {generated_at.strftime("%Y-%m-%d %H:%M")}</p>
    <nav class="site-nav" aria-label="站点导航">
      <a href="index.html">🎫 活动清单</a>
      <a href="map.html">🗺 建筑地图</a>
      <a class="active" href="new.html" aria-current="page">🆕 本周上新</a>
    </nav>
  </header>
  <div class="chips" id="scope-chips">{scope_chips}
  </div>
  {timeline}
  {older_block}
  <footer>
    记录口径：每轮自动更新后，与上一轮的活动清单按「标题」比对，出现的新活动记到这里；
    日期、场馆的日常抖动不算新增。外地活动只收录历史文物类展览、音乐剧和话剧。
  </footer>
  {NEW_PAGE_SCRIPT}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# ICS 日历
# ---------------------------------------------------------------------------

def ics_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def fold_ics_line(line: str) -> str:
    if len(line) <= 74:
        return line
    chunks = [line[:74]]
    rest = line[74:]
    while rest:
        chunks.append(" " + rest[:73])
        rest = rest[73:]
    return "\r\n".join(chunks)


def parse_time_text(time_text: str) -> tuple[int, int] | None:
    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", time_text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def build_ics(events: list[dict], generated_at: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//play-in-shanghai//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:上海活动清单",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    stamp = generated_at.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    for event in events:
        start = parse_date(event.get("start_date")) or parse_date(event.get("end_date"))
        if not start:
            continue
        end = parse_date(event.get("end_date")) or start
        timed = parse_time_text(event.get("time_text", ""))
        if timed:
            start_dt = datetime(start.year, start.month, start.day, timed[0], timed[1], tzinfo=SHANGHAI_TZ)
            if end > start:
                end_dt = datetime(end.year, end.month, end.day, timed[0], timed[1], tzinfo=SHANGHAI_TZ) + timedelta(hours=2)
            else:
                end_dt = start_dt + timedelta(hours=2)
            dtstart = f"DTSTART;TZID=Asia/Shanghai:{start_dt.strftime('%Y%m%dT%H%M%S')}"
            dtend = f"DTEND;TZID=Asia/Shanghai:{end_dt.strftime('%Y%m%dT%H%M%S')}"
        else:
            dtstart = f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}"
            dtend = f"DTEND;VALUE=DATE:{(end + timedelta(days=1)).strftime('%Y%m%d')}"

        summary = event.get("title", "上海活动")
        description_parts = [
            event.get("category", ""),
            f"时间：{format_event_date(event)}",
            f"场馆：{event.get('venue', '')}",
            f"票价：{event.get('price_text', '')}",
            event.get("summary", ""),
        ]
        description = " | ".join(part for part in description_parts if part)

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{event.get('id', '')}@play-in-shanghai",
                f"DTSTAMP:{stamp}",
                dtstart,
                dtend,
                f"SUMMARY:{ics_escape(summary)}",
                f"LOCATION:{ics_escape(event.get('venue', ''))}",
                f"DESCRIPTION:{ics_escape(description)}",
                f"URL:{ics_escape(event.get('ticket_url') or event.get('source_url', ''))}",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def collect_from_source(source: dict) -> list[dict]:
    if not source.get("enabled", True):
        return []

    adapter = source.get("adapter", "").strip()
    if adapter:
        adapters = {
            "maoyan": collect_maoyan,
            "smartshanghai": collect_smartshanghai,
            "douban": collect_douban,
            "shmuseum": collect_shmuseum,
            "pudong": collect_pudong,
            "rockbund": collect_rockbund,
            "psa": collect_psa,
            "longmuseum": collect_longmuseum,
            **NATIONAL_ADAPTERS,
        }
        collector = adapters.get(adapter)
        if collector:
            return collector(source)
        log(f"  未知适配器：{adapter}")
        return []

    url = source.get("url", "").strip()
    if not url:
        return []

    html_text: str | None = None
    direct_error: Exception | None = None
    try:
        html_text = http_get(url)
    except Exception as exc:
        direct_error = exc

    items: list[dict] = []
    extractor = source.get("extractor", "auto")

    if html_text:
        if extractor in ("auto", "jsonld"):
            items.extend(extract_jsonld_events(html_text, url))
        if extractor in ("auto", "rss") and looks_like_feed(html_text):
            items.extend(extract_rss_events(html_text, url))

    min_before_llm = int(source.get("min_events_before_llm", 3))
    need_llm = len(items) < min_before_llm
    if need_llm and LLM_CONFIG is not None:
        text: str | None = None
        if source.get("reader", "jina") == "jina":
            try:
                text = fetch_jina(url)
            except Exception as exc:
                log(f"  Jina Reader 失败：{exc}")
        if not text and html_text:
            text = html_to_text(html_text)
        if not text and direct_error:
            log(f"  直接抓取失败：{direct_error}")
        if text:
            try:
                llm_items = extract_with_llm(text, source)
                log(f"  LLM 抽取到 {len(llm_items)} 条")
                items.extend(llm_items)
            except Exception as exc:
                log(f"  LLM 抽取失败：{exc}")
    elif need_llm:
        log("  未配置 LLM，跳过正文抽取")

    return items


def run(config_path: Path, no_llm: bool = False) -> None:
    global LLM_CONFIG
    LLM_CONFIG = load_llm_config(no_llm=no_llm)
    load_translation_cache()

    if LLM_CONFIG:
        log(f"LLM 已启用：{LLM_CONFIG[0]} / {LLM_CONFIG[2]}")
    else:
        log("未检测到 LLM 配置，只使用 JSON-LD / RSS")

    config = load_json(config_path)
    sources = config.get("sources", [])
    log(f"开始抓取 {len(sources)} 个来源")

    raw_with_source: list[tuple[dict, dict]] = []
    source_counts: dict[str, int] = {}
    fetch_started = time.monotonic()
    for source in sources:
        if time.monotonic() - fetch_started > FETCH_BUDGET_SECONDS:
            log(f"抓取已超过 {FETCH_BUDGET_SECONDS // 60} 分钟，跳过剩余来源（它们会沿用上一轮数据）")
            break
        name = source.get("name", source.get("url", "未命名"))
        log(f"→ {name}")
        source_started = time.monotonic()
        try:
            items = collect_from_source(source)
        except Exception as exc:
            log(f"  抓取异常：{exc}")
            items = []
        log(f"  获得 {len(items)} 条原始数据（{time.monotonic() - source_started:.1f}s）")
        source_counts[name] = len(items)
        for item in items:
            raw_with_source.append((item, source))

    if not raw_with_source and (DATA_DIR / "events.json").exists():
        log("本次没有抓到任何原始数据，保留上一次结果，不覆盖页面")
        return

    previous_events = load_previous_events()

    events: list[dict] = []
    for raw, source in raw_with_source:
        event = normalize_event(raw, source)
        if event:
            events.append(event)

    # 某个源整轮没抓到（限流/被墙/改版）时，沿用上一轮该源的数据，避免那一块直接消失
    missing_sources = {name for name, count in source_counts.items() if count == 0}
    if missing_sources and previous_events:
        carried = [
            event
            for event in previous_events
            if {
                part.strip()
                for part in str(event.get("source_name", "")).split("/")
            } & missing_sources
        ]
        if carried:
            events.extend(carried)
            log(
                f"沿用上一轮 {len(carried)} 条（这轮没抓到：{('、'.join(sorted(missing_sources)))}）"
            )

    events = enrich_time_from_title(events)
    events = dedupe_events(events)

    generated_at = datetime.now(SHANGHAI_TZ)
    today = today_cn()
    window_start = today - timedelta(days=31)
    window_end = today + timedelta(days=DATE_WINDOW_DAYS)
    upcoming = [event for event in events if event_overlaps(event, window_start, window_end)]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    save_translation_cache()

    # 「本周上新」：先读上一轮清单算差集，再覆盖 events.json
    updates = record_update(previous_events, events, generated_at)
    save_updates(updates)
    new_titles = recent_new_titles(updates)
    latest = updates["runs"][-1] if updates.get("runs") else {}
    log(
        f"本轮新增 {latest.get('added_count', 0)} 个活动"
        f"（外地 {latest.get('added_national', 0)} 个），消失 {latest.get('removed', 0)} 个"
    )

    payload = {
        "updated": generated_at.isoformat(timespec="seconds"),
        "count": len(upcoming),
        "events": events,
    }
    save_json(DATA_DIR / "events.json", payload)
    save_json(DOCS_DIR / "events.json", payload)

    html_text = build_html(upcoming, generated_at, updates=updates, new_titles=new_titles)
    (DOCS_DIR / "index.html").write_text(html_text, encoding="utf-8")
    (DOCS_DIR / "style.css").write_text(STYLE, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    (DOCS_DIR / "events.ics").write_text(
        build_ics([event for event in upcoming if is_shanghai(event)], generated_at),
        encoding="utf-8",
    )
    (DOCS_DIR / "new.html").write_text(build_new_page(updates, generated_at), encoding="utf-8")

    try:
        from mapbuild import build_places_outputs

        places_result = build_places_outputs(ROOT)
        log(f"建筑地图：{places_result['count']} 处 → {(DOCS_DIR / 'map.html').resolve()}")
        if places_result.get("skipped"):
            log("建筑地图缺少坐标已跳过：" + "、".join(places_result["skipped"]))
    except Exception as exc:
        log(f"建筑地图生成失败：{exc}")

    by_category: dict[str, int] = {}
    for event in upcoming:
        by_category[event["category"]] = by_category.get(event["category"], 0) + 1

    city_counts: dict[str, int] = {}
    for event in upcoming:
        if not is_shanghai(event):
            city = event_city(event)
            city_counts[city] = city_counts.get(city, 0) + 1

    log(f"完成：标准化 {len(events)} 条，近期 {len(upcoming)} 条")
    log(f"分类统计：{json.dumps(by_category, ensure_ascii=False)}")
    if city_counts:
        log(f"外地场次：{json.dumps(city_counts, ensure_ascii=False, sort_keys=True)}")
    log(f"页面：{(DOCS_DIR / 'index.html').resolve()}")
    log(f"上新：{(DOCS_DIR / 'new.html').resolve()}")
    log(f"日历：{(DOCS_DIR / 'events.ics').resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="上海活动自动更新器")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="来源配置路径")
    parser.add_argument("--no-llm", action="store_true", help="禁用 LLM 抽取")
    parser.add_argument(
        "--places-only", action="store_true", help="只根据 config/places.json 重新生成建筑地图"
    )
    args = parser.parse_args()

    try:
        if args.places_only:
            from mapbuild import build_places_outputs

            result = build_places_outputs(ROOT)
            log(f"建筑地图：{result['count']} 处 → {(DOCS_DIR / 'map.html').resolve()}")
            if result.get("skipped"):
                log("建筑地图缺少坐标已跳过：" + "、".join(result["skipped"]))
        else:
            run(Path(args.config), no_llm=args.no_llm)
    except Exception as exc:
        log(f"运行失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
