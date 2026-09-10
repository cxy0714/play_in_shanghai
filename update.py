#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上海活动自动更新器

从 config/sources.json 里的公开网页抓取/抽取活动，
生成 docs/index.html、docs/events.json、docs/events.ics。

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
    15: "其他",
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
    "week-comedy": "话剧",
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
                jump = clean_text(item.get("jumpDetailUrl"))
                ticket_url = (
                    "https://show.maoyan.com" + jump
                    if jump.startswith("/")
                    else (jump or clean_text(source.get("url")))
                )
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
        title = clean_text(item.get("title"))
        if not title:
            continue
        listing_url = clean_text(item.get("listing_url")) or clean_text(item.get("tickets_url"))
        start = ""
        match = re.search(r"(20\d{2}-\d{2}-\d{2})", listing_url)
        if match:
            start = match.group(1)

        results.append(
            {
                "title": title,
                "category": guess_smart_category(item),
                "venue": clean_text(item.get("venue_name") or item.get("venue_label")),
                "address": "",
                "start_date": start,
                "end_date": start,
                "price_text": clean_text(item.get("price")),
                "ticket_url": listing_url or clean_text(source.get("url")),
                "source_url": clean_text(source.get("url")),
                "image_url": clean_text(item.get("thumbnail_url") or item.get("compressed_thumbnail_url")),
                "summary": clean_text(item.get("brief_description")),
                "status": clean_text((item.get("listing_status") or {}).get("title")),
            }
        )

    return results


def clean_douban_location(text: str) -> str:
    value = clean_text(text)
    value = re.sub(r"^上海\s*", "", value)
    value = re.sub(r"^(黄浦区|徐汇区|长宁区|静安区|普陀区|虹口区|杨浦区|闵行区|宝山区|嘉定区|浦东新区|金山区|松江区|青浦区|奉贤区|崇明区)\s*", "", value)
    return value or "上海"


def collect_douban(source: dict) -> list[dict]:
    pages = source.get("pages") or ["week-drama", "week-comedy", "week-exhibition", "week-music"]
    base = source.get("base_url") or "https://shanghai.douban.com/events"
    results: list[dict] = []

    for page in pages:
        url = f"{base}/{page}"
        html_text = http_get(url, timeout=30)
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
        detail_url = urljoin(url, detail_link["href"]) if detail_link else url

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
        path = "exhibition" if item_type == "exhibition" else "event"
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
                detail_url = f"https://www.powerstationofart.com/cn/whats-on/exhibitions/{slug}"
            elif "activity" in endpoint:
                detail_url = f"https://www.powerstationofart.com/cn/whats-on/activities/{slug}"
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
    ("话剧", ["话剧", "舞台剧", "戏剧", "默剧", "喜剧"]),
    ("舞蹈", ["舞剧", "舞蹈", "芭蕾"]),
    ("戏曲", ["京剧", "昆曲", "越剧", "沪剧", "评弹", "戏曲"]),
    ("展览", ["展览", "美术馆", "博物馆", "艺术展", "特展", "双年展", "画廊", "艺术中心"]),
    ("音乐会", [
        "音乐会", "交响", "室内乐", "合唱", "钢琴", "小提琴", "大提琴",
        "爵士", "演唱会", "livehouse", "音乐现场", "音乐节",
    ]),
]
CATEGORIES = ["展览", "话剧", "音乐剧", "音乐会", "舞蹈", "戏曲", "其他"]


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
            normalized_key(event.get("title", "")),
            normalized_key(event.get("venue", "")),
            event.get("start_date", ""),
        ]
    )


def merge_events(primary: dict, other: dict) -> dict:
    preferred_status = ["已取消", "已延期", "已售罄", "", "在售"]
    for field in (
        "summary", "venue", "district", "address", "start_date", "end_date",
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
.badge.other { background: #f0f0f2; color: #555; }
.date { color: var(--muted); font-size: 13px; text-align: right; }
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
footer { max-width: 980px; margin: 0 auto; padding: 0 18px 40px; color: #999; font-size: 12px; }
@media (max-width: 720px) {
  main { grid-template-columns: 1fr; padding-top: 12px; }
  h1 { font-size: 25px; }
  .toolbar-inner { gap: 8px; }
}
"""


def build_card(event: dict, months: str) -> str:
    group = event.get("category") or "其他"
    category = event.get("display_category") or group
    category_class = CATEGORY_CLASS.get(group, "other")
    title = html_lib.escape(event.get("title", ""))
    venue = html_lib.escape(event.get("venue", ""))
    district = event.get("district", "")
    meta = venue + (f" · {district}" if district else "")
    summary = html_lib.escape(event.get("summary", ""))
    price = html_lib.escape(event.get("price_text", "以页面为准"))
    date_text = html_lib.escape(format_event_date(event))
    status = event.get("status", "")
    status_html = f'<span class="status">{html_lib.escape(status)}</span>' if status else ""
    link = event.get("ticket_url") or event.get("source_url") or "#"
    link = html_lib.escape(link, quote=True)
    search_text = html_lib.escape(
        " ".join([
            event.get("title", ""),
            event.get("venue", ""),
            district,
            event.get("summary", ""),
            category,
            group,
        ]),
        quote=True,
    )
    return f"""
      <article class="card" data-month="{months}" data-category="{html_lib.escape(category, quote=True)}" data-group="{html_lib.escape(group, quote=True)}" data-search="{search_text.lower()}">
        <div class="card-top">
          <span class="badge {category_class}">{html_lib.escape(category)}</span>
          <span class="date">{date_text}</span>
        </div>
        <h2>{title}</h2>
        <p class="meta">{meta}</p>
        <p class="summary">{summary}</p>
        <div class="card-bottom">
          <span>{status_html}<span class="price">{price}</span></span>
          <a class="button" href="{link}" target="_blank" rel="noopener noreferrer">查看详情 →</a>
        </div>
      </article>"""


def build_html(events: list[dict], generated_at: datetime) -> str:
    today = today_cn()
    window_end = today + timedelta(days=DATE_WINDOW_DAYS)
    visible = [event for event in events if event_overlaps(event, today - timedelta(days=31), window_end)]
    visible.sort(key=lambda e: (e.get("start_date") or "9999-99-99", e.get("title", "")))

    cards = "\n".join(build_card(event, compute_months(event, today)) for event in visible)
    filter_order = ["展览"] + EXHIBITION_SUBCATEGORIES + [c for c in CATEGORIES if c != "展览"]
    present: set[str] = set()
    for event in visible:
        group = event.get("category", "其他")
        present.add(group)
        if group == "展览":
            present.add(event.get("display_category") or "其他展览")
    categories = [category for category in filter_order if category in present]

    chips = ['<button class="chip active" data-cat="all">全部</button>']
    chips.extend(f'<button class="chip" data-cat="{html_lib.escape(cat, quote=True)}">{html_lib.escape(cat)}</button>' for cat in categories)

    updated = generated_at.strftime("%Y-%m-%d %H:%M")
    count = len(visible)
    if count:
        subtitle = f"更新于 {updated} · 近期 {count} 个活动"
    else:
        subtitle = f"更新于 {updated} · 暂时没有抓到活动"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>上海本月去哪玩</title>
  <meta name="description" content="上海展览、话剧、音乐剧、演出自动汇总">
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header class="hero">
    <p class="eyebrow">周末去哪玩 · 上海</p>
    <h1>本月展览 / 话剧 / 音乐剧</h1>
    <p class="updated">{subtitle}</p>
  </header>
  <div class="toolbar">
    <div class="toolbar-inner">
      <div class="tabs" role="tablist">
        <button class="tab active" data-tab="current">本月</button>
        <button class="tab" data-tab="next">下月</button>
        <button class="tab" data-tab="all">全部</button>
      </div>
      <input id="search" type="search" placeholder="搜名称 / 场馆 / 区域">
    </div>
  </div>
  <div class="chips" id="chips">
    {''.join(chips)}
  </div>
  <main id="event-list">
    {cards}
  </main>
  <div id="empty" class="empty" hidden>没有符合条件的活动，换个筛选试试。</div>
  <footer>
    信息来自公开网页，以官方购票/预约页面为准。如有延期、取消或售罄，请以官方最新信息为准。
  </footer>
  <script>
    const state = {{ tab: 'current', cat: 'all', q: '' }};
    const cards = Array.from(document.querySelectorAll('.card'));
    const empty = document.getElementById('empty');
    const search = document.getElementById('search');

    function applyFilters() {{
      let visible = 0;
      cards.forEach(card => {{
        const months = (card.dataset.month || '').split(' ');
        const category = card.dataset.category || '';
        const group = card.dataset.group || category;
        const text = card.dataset.search || '';
        const monthOk = state.tab === 'all' || months.includes(state.tab);
        const catOk = state.cat === 'all' || category === state.cat || group === state.cat;
        const searchOk = !state.q || text.includes(state.q);
        const show = monthOk && catOk && searchOk;
        card.hidden = !show;
        if (show) visible += 1;
      }});
      empty.hidden = visible > 0;
    }}

    document.querySelectorAll('.tab').forEach(button => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('.tab').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        state.tab = button.dataset.tab;
        applyFilters();
      }});
    }});

    document.querySelectorAll('.chip').forEach(button => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('.chip').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        state.cat = button.dataset.cat;
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

    if LLM_CONFIG:
        log(f"LLM 已启用：{LLM_CONFIG[0]} / {LLM_CONFIG[2]}")
    else:
        log("未检测到 LLM 配置，只使用 JSON-LD / RSS")

    config = load_json(config_path)
    sources = config.get("sources", [])
    log(f"开始抓取 {len(sources)} 个来源")

    raw_with_source: list[tuple[dict, dict]] = []
    for source in sources:
        name = source.get("name", source.get("url", "未命名"))
        log(f"→ {name}")
        try:
            items = collect_from_source(source)
        except Exception as exc:
            log(f"  抓取异常：{exc}")
            items = []
        log(f"  获得 {len(items)} 条原始数据")
        for item in items:
            raw_with_source.append((item, source))

    if not raw_with_source and (DATA_DIR / "events.json").exists():
        log("本次没有抓到任何原始数据，保留上一次结果，不覆盖页面")
        return

    events: list[dict] = []
    for raw, source in raw_with_source:
        event = normalize_event(raw, source)
        if event:
            events.append(event)

    events = dedupe_events(events)

    generated_at = datetime.now(SHANGHAI_TZ)
    today = today_cn()
    window_start = today - timedelta(days=31)
    window_end = today + timedelta(days=DATE_WINDOW_DAYS)
    upcoming = [event for event in events if event_overlaps(event, window_start, window_end)]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "updated": generated_at.isoformat(timespec="seconds"),
        "count": len(upcoming),
        "events": events,
    }
    save_json(DATA_DIR / "events.json", payload)
    save_json(DOCS_DIR / "events.json", payload)

    html_text = build_html(upcoming, generated_at)
    (DOCS_DIR / "index.html").write_text(html_text, encoding="utf-8")
    (DOCS_DIR / "style.css").write_text(STYLE, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    (DOCS_DIR / "events.ics").write_text(build_ics(upcoming, generated_at), encoding="utf-8")

    by_category: dict[str, int] = {}
    for event in upcoming:
        by_category[event["category"]] = by_category.get(event["category"], 0) + 1

    log(f"完成：标准化 {len(events)} 条，近期 {len(upcoming)} 条")
    log(f"分类统计：{json.dumps(by_category, ensure_ascii=False)}")
    log(f"页面：{(DOCS_DIR / 'index.html').resolve()}")
    log(f"日历：{(DOCS_DIR / 'events.ics').resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="上海活动自动更新器")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="来源配置路径")
    parser.add_argument("--no-llm", action="store_true", help="禁用 LLM 抽取")
    args = parser.parse_args()

    try:
        run(Path(args.config), no_llm=args.no_llm)
    except Exception as exc:
        log(f"运行失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
