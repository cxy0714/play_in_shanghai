#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全国范围（上海以外）公开活动数据源采集模块。

设计约束（与 update.py 的契约）：
* 只依赖 requests / beautifulsoup4 / 标准库，不 import 项目内其它模块（避免循环导入）。
* 每个 collector 签名 `collector(source: dict) -> list[dict]`，返回「原始数据」dict 列表，
  字段尽量填全（缺的用空字符串），交给 update.py 的 normalize_event() 统一标准化。
* 任何单个子请求失败只跳过该子请求；整体失败返回 [] 并打印日志，绝不向上抛异常。

返回的 raw dict 字段：
    title        必填
    category     "展览" / "音乐剧" / "话剧"
    venue        场馆名
    address      地址
    city         城市中文名（新增字段，必须给）
    start_date   "YYYY-MM-DD" 或 ""
    end_date     "YYYY-MM-DD" 或 ""
    price_text   票价描述
    ticket_url   详情/购票链接
    source_url   来源页
    image_url    海报图，没有就 ""
    summary      一句话描述
    status       可选，如 "已售罄"

暴露的适配器：
    NATIONAL_ADAPTERS["maoyan_city"]  猫眼/大众点评 myshow ajax，按 cityId 抓城市
    NATIONAL_ADAPTERS["douban_city"]  豆瓣同城城市子域
    NATIONAL_ADAPTERS["museum_cn"]    全国博物馆/美术馆官网展览（site 字段选择站点）

实测记录（2026-11，详见交付说明）：
    * 猫眼 cityId 必须用「猫眼演出城市 id」而非大众点评城市 id：
      北京1 / 广州20 / 深圳30 / 天津40 / 西安42 / 重庆45 / 杭州50 / 南京55 /
      武汉57 / 成都59 / 苏州80 / 长沙70 / 上海10。
      传其它值会被服务端静默回落成上海（所以必须显式配置 city_id）。
    * 豆瓣只有 beijing/shanghai/guangzhou/shenzhen 四个城市子域能解析，其它城市域名
      直接 SSL/连接被重置。
    * 博物馆站点：国博、南博、湖南博物院、陕历博、故宫可解析；中国美术馆/浙博/湖北省博/
      川博/苏博/首博/广东省博/天津博物馆等因 SSL、超时、JS 渲染或需鉴权不可用。
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

MAOYAN_API = (
    "https://m.dianping.com/myshow/ajax/performances/"
    "{category};st=0;p={page};s={page_size};tft=0?cityId={city_id}&sellChannel=7"
)
MAOYAN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://show.maoyan.com/",
    "Accept": "application/json, text/plain, */*",
}
MAOYAN_DETAIL = "https://www.gewara.com/detail/{performance_id}"

DOUBAN_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douban.com/",
}

MUSEUM_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 猫眼演出 categoryId：2=体育 3=戏曲 4=话剧/舞台剧(音乐剧混在这里) 9=展览
# 16=相声曲艺 17=演唱会 1/6/10=音乐会 5=舞蹈 15=脱口秀
MAOYAN_CATEGORY_NAMES = {4: "话剧", 9: "展览"}

# 猫眼「展览」类目里混着车展/漫展/消费节/招聘会等，不属于博物馆/历史文物展览，
# 默认过滤；可在 source 里用 "exhibition_blacklist": false 关闭，
# 或用 "exhibition_exclude": ["关键词", ...] 覆盖。
EXHIBITION_BLACKLIST = [
    "车展", "汽车", "消费节", "机器人大会", "招聘", "家博会", "婚博会",
    "茶博会", "糖酒会", "漫展", "动漫展", "游戏展", "同人展", "嘉年华",
    "音乐节", "演唱会", "见面会", "签售", "市集", "集市", "美食节",
]

SLEEP = 0.25  # 子请求间隔，避免打太快

# 城市默认地址（用于博物馆源的 address 字段兜底）
MUSEUM_DEFAULT_ADDRESS = {
    "中国国家博物馆": "北京市东城区东长安街16号",
    "故宫博物院": "北京市东城区景山前街4号",
    "南京博物院": "南京市玄武区中山东路321号",
    "湖南博物院": "长沙市开福区东风路50号",
    "陕西历史博物馆": "西安市雁塔区小寨东路91号",
    "良渚博物院": "杭州市余杭区良渚街道美丽洲路1号",
    "中国丝绸博物馆": "杭州市西湖区玉皇山路73-1号",
    "苏州博物馆": "苏州市姑苏区东北街204号",
}

TODAY = datetime.now().date()


# ---------------------------------------------------------------------------
# 小工具（不依赖 update.py，自己实现）
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value if item)
    text = str(value)
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_tags(value: Any) -> str:
    """把富文本字段（如 describe 里的 HTML）压成纯文本。"""
    return clean_text(value)


def to_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


_FULL_DATE_RE = re.compile(r"(20\d{2})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?")
_SHORT_DATE_RE = re.compile(r"(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?")
_RANGE_SEP_RE = re.compile(r"[~～至到\-—–－]{1,2}")
_MONTH_SPAN_RE = re.compile(r"展期\s*(\d+)\s*个?\s*月")
_DAY_SPAN_RE = re.compile(r"展期\s*(\d+)\s*天")
_PERMANENT_RE = re.compile(r"常设|永久|长期|基本陈列|常驻")


def parse_date_range(text: Any) -> tuple[date | None, date | None]:
    """从各种中文/数字日期描述里解析 (start, end)。

    覆盖实测到的格式：
        "2026.11.02 - 11.29"            "2026.09.24 - 2027.03.31"
        "2026.11.06 / 11.07"            "2026.11.02 周一"
        "2026年10月1日—11月30日"         "2026/10/31—2027/3/15"
        "2026年9月23日—2026年12月20日（暂定）"
        "2026年9月29日开幕，展期3个月"    "2026年9月4日开展"
        "2025年5月18日——11月17日"
    """
    value = clean_text(text)
    if not value:
        return None, None

    match = _FULL_DATE_RE.search(value)
    if not match:
        return None, None
    start = to_date(match.group(1), match.group(2), match.group(3))
    if start is None:
        return None, None

    rest = value[match.end():]
    # 1) 后半段先找分隔符 + 缺年份的短日期（"- 11.29" / "—11月30日"）
    sep = _RANGE_SEP_RE.search(rest)
    if sep:
        tail = rest[sep.end():]
        short = _SHORT_DATE_RE.match(tail.lstrip())
        if short:
            end = to_date(start.year, short.group(1), short.group(2))
            if end and end < start:
                end = to_date(start.year + 1, short.group(1), short.group(2))
            if end:
                return start, end
    # 2) 再找后半段里完整的第二个日期
    second = _FULL_DATE_RE.search(rest)
    if second:
        end = to_date(second.group(1), second.group(2), second.group(3))
        if end and end < start:
            end = None
        if end:
            return start, end
    # 3) "展期 3 个月" / "展期 90 天"
    span = _MONTH_SPAN_RE.search(value)
    if span:
        months = int(span.group(1))
        return start, start + timedelta(days=30 * months)
    span = _DAY_SPAN_RE.search(value)
    if span:
        return start, start + timedelta(days=int(span.group(1)))
    return start, None


def is_permanent(text: Any) -> bool:
    return bool(_PERMANENT_RE.search(clean_text(text)))


def http_get(url: str, headers: dict | None = None, timeout: int = 25, retries: int = 2) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers=headers or MUSEUM_HEADERS, timeout=timeout)
            if response.status_code >= 400:
                raise requests.HTTPError(f"HTTP {response.status_code}")
            # 中文站点常见：没声明 charset -> requests 默认 ISO-8859-1，需要猜一次；
            # 已声明 utf-8 的 JSON/HTML 不要覆盖，避免把接口数据猜坏。
            if not response.encoding or response.encoding.lower() in ("iso-8859-1", "ascii"):
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except Exception as exc:  # noqa: BLE001 - 子请求失败只跳过
            last_error = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"GET 失败 {url}: {last_error}")


def http_get_json(url: str, headers: dict | None = None, timeout: int = 25, retries: int = 2) -> Any:
    text = http_get(url, headers=headers, timeout=timeout, retries=retries)
    return json.loads(text)


def future_or_ongoing(start: date | None, end: date | None, today: date | None = None) -> bool:
    """是否属于「未来 / 仍在展」：结束日期已过则丢弃；起止都未知则保守丢弃。"""
    today = today or TODAY
    if end is not None and end < today:
        return False
    if start is None and end is None:
        return False
    return True


def _result(**kwargs: Any) -> dict:
    """保证契约字段齐全、值为字符串。"""
    base = {
        "title": "",
        "category": "展览",
        "venue": "",
        "address": "",
        "city": "",
        "start_date": "",
        "end_date": "",
        "price_text": "",
        "ticket_url": "",
        "source_url": "",
        "image_url": "",
        "summary": "",
        "status": "",
    }
    for key, value in kwargs.items():
        if key not in base:
            continue
        if key in ("start_date", "end_date") and isinstance(value, date):
            base[key] = value.isoformat()
        else:
            base[key] = clean_text(value)
    return base


# ---------------------------------------------------------------------------
# A. 猫眼演出（按城市）
# ---------------------------------------------------------------------------

def _maoyan_category(category_id: int, title: str) -> str:
    base = MAOYAN_CATEGORY_NAMES.get(category_id, "")
    if base == "话剧" and re.search(r"音乐剧|musical", title, re.I):
        return "音乐剧"
    return base


def _collect_maoyan_city(source: dict) -> list[dict]:
    city = clean_text(source.get("city"))
    city_id = source.get("city_id")
    if not city or not city_id:
        log("  [maoyan_city] 缺少 city / city_id，跳过")
        return []

    categories = [int(c) for c in (source.get("categories") or [9, 4])]
    page_size = int(source.get("page_size", 30))
    max_pages = max(1, int(source.get("max_pages", 1)))
    source_url = clean_text(source.get("url")) or "https://show.maoyan.com/"

    if source.get("exhibition_blacklist", True):
        exclude = source.get("exhibition_exclude") or EXHIBITION_BLACKLIST
    else:
        exclude = source.get("exhibition_exclude") or []
    exclude = [clean_text(word) for word in exclude if clean_text(word)]

    results: list[dict] = []
    for category in categories:
        if category not in MAOYAN_CATEGORY_NAMES:
            continue
        for page in range(1, max_pages + 1):
            url = MAOYAN_API.format(
                category=category, page=page, page_size=page_size, city_id=city_id
            )
            try:
                data = http_get_json(url, headers=MAOYAN_HEADERS, timeout=20, retries=1)
            except Exception as exc:  # noqa: BLE001
                log(f"  [maoyan_city] {city} cat={category} p={page} 请求失败：{exc}")
                break

            items = data.get("data") or []
            if not items:
                break
            for item in items:
                title = clean_text(item.get("name") or item.get("shortName"))
                if not title:
                    continue
                # 只保留本城市的场次：猫眼会带回周边城市（isCurrentCity=0）。
                # 但部分城市 id 的「本地」条目 isCurrentCity 也是 0，
                # 所以按城市名兜底：item.cityName 与配置城市一致时保留。
                item_city = clean_text(item.get("cityName"))
                if item_city and item_city != city and item.get("isCurrentCity") in (0, False):
                    continue
                category_id = int(item.get("categoryId") or category)
                event_category = _maoyan_category(category_id, title)
                if event_category not in ("展览", "音乐剧", "话剧"):
                    continue
                if event_category == "展览" and exclude:
                    haystack = f"{title} {clean_text(item.get('shopName'))}"
                    if any(word in haystack for word in exclude):
                        continue

                start, end = parse_date_range(item.get("showTimeRange"))
                if not future_or_ongoing(start, end):
                    continue

                performance_id = (
                    item.get("performanceId")
                    or item.get("projectId")
                    or item.get("project_id")
                )
                jump = clean_text(item.get("jumpDetailUrl"))
                if performance_id:
                    ticket_url = MAOYAN_DETAIL.format(performance_id=performance_id)
                elif jump:
                    ticket_url = urljoin("https://show.maoyan.com", jump)
                else:
                    ticket_url = source_url

                price = ""
                price_display = item.get("sellPriceDisplay")
                if isinstance(price_display, dict):
                    price = clean_text(price_display.get("priceDisplayText"))
                price = price or clean_text(item.get("priceRange"))

                score = clean_text(item.get("score"))
                summary = f"猫眼评分 {score}" if score and score not in ("0.0", "0") else ""

                results.append(
                    _result(
                        title=title,
                        category=event_category,
                        venue=item.get("shopName"),
                        address=item.get("address"),
                        city=clean_text(item.get("cityName")) or city,
                        start_date=start,
                        end_date=end,
                        price_text=price,
                        ticket_url=ticket_url,
                        source_url=source_url,
                        image_url=item.get("posterUrl"),
                        summary=summary,
                        status="已售罄" if item.get("stockOut") else "",
                    )
                )

            paging = data.get("paging") or {}
            if not paging.get("hasMore"):
                break
            time.sleep(SLEEP)
    return results


def collect_maoyan_city(source: dict) -> list[dict]:
    """按 cityId 抓猫眼演出的展览 / 话剧 / 音乐剧。

    读取字段：city（中文名，必填）、city_id（猫眼演出城市 id，必填）、
    categories（默认 [9, 4]）、page_size（默认 30）、max_pages（默认 1）、url。
    """
    try:
        items = _collect_maoyan_city(source)
        log(f"  [maoyan_city] {source.get('city', '')} 抓到 {len(items)} 条")
        return items
    except Exception as exc:  # noqa: BLE001 - 契约要求整体失败返回 []
        log(f"  [maoyan_city] 采集异常：{exc}")
        return []


# ---------------------------------------------------------------------------
# B. 豆瓣同城（按城市子域）
# ---------------------------------------------------------------------------

DOUBAN_PAGE_CATEGORY = {
    "week-drama": "话剧",
    "week-exhibition": "展览",
}


def _clean_douban_location(text: str, city: str) -> str:
    value = clean_text(text)
    if city and value.startswith(city):
        value = value[len(city):].strip()
    # 去掉首尾的城市/行政区前缀，只保留场馆名
    value = re.sub(
        r"^(北京|上海|广州|深圳|杭州|成都|南京|西安|武汉|苏州|天津|重庆|长沙)\s*",
        "",
        value,
    )
    value = re.sub(r"^[\u4e00-\u9fa5]{2,4}区\s*", "", value)
    return value or ""


def _collect_douban_city(source: dict) -> list[dict]:
    city = clean_text(source.get("city")) or "北京"
    pages = source.get("pages") or ["week-drama", "week-exhibition"]
    base = clean_text(source.get("base_url")) or f"https://beijing.douban.com/events"

    results: list[dict] = []
    for page in pages:
        page_category = DOUBAN_PAGE_CATEGORY.get(page)
        if not page_category:
            continue
        url = f"{base.rstrip('/')}/{page}"
        try:
            html_text = http_get(url, headers=DOUBAN_HEADERS, timeout=25, retries=1)
        except Exception as exc:  # noqa: BLE001
            log(f"  [douban_city] {city} {page} 请求失败：{exc}")
            continue

        soup = BeautifulSoup(html_text, "html.parser")
        entries = soup.select("li.list-entry")
        if not entries:
            log(f"  [douban_city] {city} {page} 页面结构变更或风控（无 list-entry）")
            continue

        for item in entries:
            title_link = item.select_one(".title a[href]")
            if not title_link:
                continue
            title = clean_text(title_link.get("title") or title_link.get_text(" ", strip=True))
            if len(title) < 2:
                continue
            detail_url = title_link.get("href") or url

            start_el = item.select_one("time[itemprop='startDate']")
            end_el = item.select_one("time[itemprop='endDate']")
            start, end = None, None
            if start_el and start_el.get("datetime"):
                start, _ = parse_date_range(start_el.get("datetime"))
            if end_el and end_el.get("datetime"):
                _, end = parse_date_range(end_el.get("datetime"))
            if not start:
                start, end = parse_date_range(item.get_text(" ", strip=True))
            if not future_or_ongoing(start, end):
                continue

            location_el = item.select_one("li[title]")
            location = clean_text(location_el.get("title")) if location_el else ""

            fee_el = item.select_one(".fee strong")
            price = clean_text(fee_el.get_text(" ", strip=True)) if fee_el else ""
            summary_el = item.select_one("p")
            summary = clean_text(summary_el.get_text(" ", strip=True)) if summary_el else ""

            category = page_category
            if category == "话剧" and re.search(r"音乐剧|musical", title, re.I):
                category = "音乐剧"

            results.append(
                _result(
                    title=title,
                    category=category,
                    venue=_clean_douban_location(location, city) or city,
                    address=location,
                    city=city,
                    start_date=start,
                    end_date=end,
                    price_text=price,
                    ticket_url=detail_url,
                    source_url=url,
                    image_url="",
                    summary=summary,
                )
            )
        time.sleep(0.6)
    return results


def collect_douban_city(source: dict) -> list[dict]:
    """按城市子域抓豆瓣同城的话剧 / 展览。

    读取字段：city（中文名，必填）、base_url（如 https://beijing.douban.com/events，
    默认按 city 猜）、pages（默认 ["week-drama", "week-exhibition"]）、url。
    注意：实测只有北京/上海/广州/深圳四个子域可用，其它城市域名连不上。
    """
    try:
        items = _collect_douban_city(source)
        log(f"  [douban_city] {source.get('city', '')} 抓到 {len(items)} 条")
        return items
    except Exception as exc:  # noqa: BLE001
        log(f"  [douban_city] 采集异常：{exc}")
        return []


# ---------------------------------------------------------------------------
# C. 博物馆 / 美术馆官网展览
# ---------------------------------------------------------------------------

def _museum_chnmuseum(source: dict) -> list[dict]:
    """中国国家博物馆「正在展出」页：标题/展期/展厅都在 li 里，结构稳定。"""
    url = clean_text(source.get("url")) or "https://www.chnmuseum.cn/zl/zzzc/"
    html_text = http_get(url, timeout=25)
    soup = BeautifulSoup(html_text, "html.parser")

    results: list[dict] = []
    for li in soup.select("div.clum_img_list ul li"):
        link = li.select_one("a[href]")
        title_el = li.select_one(".hide_title")
        if not link or not title_el:
            continue
        title = clean_text(title_el.get_text(" ", strip=True))
        if len(title) < 2:
            continue

        boxes = li.select(".hide_box")
        date_text = ""
        hall_text = ""
        for box in boxes:
            paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in box.select("p")]
            if not paragraphs:
                continue
            label = paragraphs[0]
            value = " ".join(paragraphs[1:]) if len(paragraphs) > 1 else ""
            if "展期" in label or "时间" in label:
                date_text = value
            elif "地点" in label or "展厅" in label:
                hall_text = value
        if not hall_text:
            hall_text = clean_text(li.get_text(" ", strip=True))[:40]

        start, end = parse_date_range(date_text)
        permanent = is_permanent(date_text)
        if permanent:
            start, end = TODAY, TODAY + timedelta(days=365)
        elif start is None:
            continue
        if not future_or_ongoing(start, end):
            continue

        image_el = li.select_one("img")
        image_url = ""
        if image_el and image_el.get("src"):
            image_url = urljoin(url, image_el["src"])

        if end is None:
            end = start + timedelta(days=180)

        results.append(
            _result(
                title=title,
                category="展览",
                venue=f"中国国家博物馆 {hall_text}".strip(),
                address=MUSEUM_DEFAULT_ADDRESS["中国国家博物馆"],
                city="北京",
                start_date=start,
                end_date=end,
                price_text="详见官网",
                ticket_url=urljoin(url, link["href"]),
                source_url=url,
                image_url=image_url,
                summary=f"展期：{date_text}" if date_text else "中国国家博物馆正在展出",
            )
        )

    # 列表页的长标题会被截成 "..."，用详情页 .title 补全（最多补 8 条，避免拖慢）
    for item in results[:8]:
        if not item["title"].endswith(("...", "…")):
            continue
        try:
            detail_html = http_get(item["ticket_url"], timeout=8, retries=1)
            detail_soup = BeautifulSoup(detail_html, "html.parser")
            title_el = detail_soup.select_one(".title") or detail_soup.select_one("h1")
            if title_el:
                full_title = clean_text(title_el.get_text(" ", strip=True))
                if full_title and full_title != item["title"] and not full_title.endswith(("...", "…")):
                    item["title"] = full_title
        except Exception:  # noqa: BLE001
            continue
        time.sleep(SLEEP)
    return results


def _museum_njmuseum(source: dict) -> list[dict]:
    """南京博物院展览列表 JSON 接口（GET 也返回数据，POST 表单更稳）。"""
    url = clean_text(source.get("url")) or "https://www.njmuseum.com/api/exhibition/list"
    payload = {"properties": "当前展览", "pageNum": 1, "pageSize": 50}
    data: Any = None
    try:
        response = requests.post(
            url,
            data=payload,
            headers={
                **MUSEUM_HEADERS,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=25,
        )
        if response.status_code < 400:
            data = response.json()
    except Exception:  # noqa: BLE001
        data = None
    if not data or not isinstance(data, dict):
        data = http_get_json(url, headers={**MUSEUM_HEADERS, "Accept": "application/json"}, timeout=25)
    data_block = (data or {}).get("data") if isinstance(data, dict) else None
    if isinstance(data_block, dict):
        items = data_block.get("list") or []
    elif isinstance(data_block, list):
        items = data_block
    else:
        items = []

    results: list[dict] = []
    for item in items:
        title = clean_text(item.get("title"))
        if len(title) < 2:
            continue
        time_desc = clean_text(item.get("timedesc"))
        start, end = parse_date_range(time_desc)
        permanent = is_permanent(time_desc)
        if permanent:
            start, end = TODAY, TODAY + timedelta(days=365)
        elif start is None:
            continue
        if not future_or_ongoing(start, end):
            continue
        if end is None:
            # "2026年6月19日开幕" 这类只有开幕日：按在展处理
            end = TODAY + timedelta(days=180) if start <= TODAY else start + timedelta(days=90)

        position = clean_text(item.get("position"))
        image_url = ""
        img_src = item.get("imgSrc") or item.get("banner")
        if isinstance(img_src, list) and img_src:
            img_src = img_src[0]
        if isinstance(img_src, str) and img_src:
            image_url = img_src if img_src.startswith("http") else "https://www.njmuseum.com" + img_src

        price = clean_text(item.get("price"))
        summary = strip_tags(item.get("describe"))[:120]

        results.append(
            _result(
                title=title,
                category="展览",
                venue=f"南京博物院 {position}".strip(),
                address=MUSEUM_DEFAULT_ADDRESS["南京博物院"],
                city="南京",
                start_date=start,
                end_date=end,
                price_text=price or "详见官网",
                ticket_url=f"https://www.njmuseum.com/zh/generalDetails?id={item.get('id')}",
                source_url=url,
                image_url=image_url,
                summary=summary or f"展期：{time_desc}",
            )
        )
    return results


def _museum_hnmuseum(source: dict) -> list[dict]:
    """湖南博物院「当前展览」页（Drupal views）。"""
    url = clean_text(source.get("url")) or (
        "https://www.hnmuseum.com/zh-hans/content/"
        "%E5%BD%93%E5%89%8D%E5%B1%95%E8%A7%88%EF%BC%8D%E5%9F%BA%E6%9C%AC%E9%99%88%E5%88%97"
    )
    html_text = http_get(url, timeout=25)
    soup = BeautifulSoup(html_text, "html.parser")

    results: list[dict] = []
    for row in soup.select("div.views-row"):
        title_link = row.select_one("h3 a[href]") or row.select_one("a[href]")
        if not title_link:
            continue
        title = clean_text(title_link.get_text(" ", strip=True))
        if len(title) < 2:
            continue

        hall = ""
        start_text = ""
        end_text = ""
        notes = ""
        summary = ""
        hall_el = row.select_one("[class*='views-field-field-zhanting']")
        if hall_el:
            hall = clean_text(hall_el.get_text(" ", strip=True))
        start_el = row.select_one("[class*='views-field-field-zhanlan-start-time']")
        if start_el:
            start_text = clean_text(start_el.get_text(" ", strip=True))
        end_el = row.select_one("[class*='views-field-field-zhanlan-end-time']")
        if end_el:
            end_text = clean_text(end_el.get_text(" ", strip=True))
        notes_el = row.select_one("[class*='views-field-field-notes']")
        if notes_el:
            notes = clean_text(notes_el.get_text(" ", strip=True))
        summary_el = row.select_one("[class*='views-field-field-zhuanti-details']")
        if summary_el:
            summary = clean_text(summary_el.get_text(" ", strip=True))[:120]

        start, end = parse_date_range(start_text)
        if parse_date_range(end_text)[0]:
            end = parse_date_range(end_text)[0]
        if is_permanent(start_text) or is_permanent(end_text):
            start, end = TODAY, TODAY + timedelta(days=365)
        if start is None:
            # 页面没给日期（特展常见）：按在展处理
            start, end = TODAY, TODAY + timedelta(days=120)
        if not future_or_ongoing(start, end):
            continue
        if end is None:
            end = TODAY + timedelta(days=120)

        image_el = row.select_one("img")
        image_url = ""
        if image_el and image_el.get("src"):
            image_url = urljoin(url, image_el["src"])

        results.append(
            _result(
                title=title,
                category="展览",
                venue=(hall if ("博物院" in hall or "博物馆" in hall) else f"湖南博物院 {hall}".strip()),
                address=MUSEUM_DEFAULT_ADDRESS["湖南博物院"],
                city="长沙",
                start_date=start,
                end_date=end,
                price_text=notes or "详见官网",
                ticket_url=urljoin(url, title_link["href"]),
                source_url=url,
                image_url=image_url,
                summary=summary or notes or "湖南博物院当前展览",
            )
        )
    return results


def _museum_sxhm(source: dict) -> list[dict]:
    """陕西历史博物馆「临时展览」列表（含分页）。

    列表页标题会被截断成 "……"，只对通过日期过滤的少量条目再抓一次详情页补全标题。
    """
    base_url = clean_text(source.get("url")) or "https://www.sxhm.com/Temporary.html"
    max_pages = max(1, int(source.get("max_pages", 2)))

    results: list[dict] = []
    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else re.sub(r"\.html.*$", f"/p/{page}.html", base_url)
        try:
            html_text = http_get(url, timeout=25, retries=1)
        except Exception as exc:  # noqa: BLE001
            log(f"  [museum_cn] 陕历博第 {page} 页失败：{exc}")
            continue

        soup = BeautifulSoup(html_text, "html.parser")
        for card in soup.select("div#datalist div.item, div.temporary_exhibition2 div.item"):
            link = card.select_one("a.t1[href]") or card.select_one("a[href]")
            if not link:
                continue
            title = clean_text(link.get_text(" ", strip=True))
            if len(title) < 2:
                continue
            detail_url = urljoin(url, link["href"])

            paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in card.select("div.t2 .p")]
            date_text = ""
            hall_text = ""
            for paragraph in paragraphs:
                if "时间" in paragraph:
                    date_text = re.sub(r"^(时间|展期)[:：]\s*", "", paragraph)
                elif "地点" in paragraph:
                    hall_text = re.sub(r"^(地点|展厅)[:：]\s*", "", paragraph)

            start, end = parse_date_range(date_text)
            if start is None:
                continue
            # 列表页混着历年展览：过期直接丢，只有开幕日的也要求不太旧
            if end is not None:
                if end < TODAY:
                    continue
            elif start < TODAY - timedelta(days=120):
                continue
            if end is None:
                end = TODAY + timedelta(days=120) if start <= TODAY else start + timedelta(days=90)

            image_el = card.select_one("img")
            image_url = ""
            if image_el and image_el.get("src"):
                image_url = urljoin(url, image_el["src"])

            results.append(
                _result(
                    title=title,
                    category="展览",
                    venue=f"陕西历史博物馆 {hall_text}".strip(),
                    address=MUSEUM_DEFAULT_ADDRESS["陕西历史博物馆"],
                    city="西安",
                    start_date=start,
                    end_date=end,
                    price_text="详见官网",
                    ticket_url=detail_url,
                    source_url=url,
                    image_url=image_url,
                    summary=f"展期：{date_text}" if date_text else "陕西历史博物馆临时展览",
                )
            )
        time.sleep(SLEEP)

    # 补全被截断的标题（最多 5 条，避免拖慢）
    for item in results[:5]:
        if not item["title"].endswith(("...", "…")):
            continue
        try:
            detail_html = http_get(item["ticket_url"], timeout=12, retries=1)
            match = re.search(r"<h1[^>]*>(.*?)</h1>", detail_html, re.S) or re.search(
                r"<title>(.*?)</title>", detail_html, re.S
            )
            if match:
                full_title = clean_text(re.sub(r"<[^>]+>", "", match.group(1)))
                full_title = re.sub(r"\s*[-_|].*$", "", full_title).strip()
                if full_title and full_title != item["title"] and not full_title.endswith(("...", "…")):
                    item["title"] = full_title
        except Exception:  # noqa: BLE001
            continue
        time.sleep(SLEEP)
    return results


def _museum_dpm(source: dict) -> list[dict]:
    """故宫博物院「近期展览」焦点图（shows.html .temporary1）。

    注意：该区块只给标题/海报/详情入口，不公开展期，故按「正在展出」收录，
    start_date 记为今天，end_date 记为 +120 天，summary 里注明以官网为准。
    """
    url = clean_text(source.get("url")) or "https://www.dpm.org.cn/shows.html"
    html_text = http_get(url, timeout=25)
    soup = BeautifulSoup(html_text, "html.parser")

    results: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.select("div.temporary1 div.child a[href]"):
        title = clean_text(anchor.get("title") or "")
        if len(title) < 2 or title in seen:
            continue
        # 过滤掉宣传性/回顾性/大纲类条目
        if re.search(r"回顾|公告|讲座|活动|大纲|预告|海报", title):
            continue
        seen.add(title)
        image_el = anchor.select_one("img")
        image_url = ""
        if image_el and image_el.get("src"):
            image_url = urljoin(url, image_el["src"])
        results.append(
            _result(
                title=title,
                category="展览",
                venue="故宫博物院",
                address=MUSEUM_DEFAULT_ADDRESS["故宫博物院"],
                city="北京",
                start_date=TODAY,
                end_date=TODAY + timedelta(days=120),
                price_text="需预约/购票，详见官网",
                ticket_url=urljoin(url, anchor["href"]),
                source_url=url,
                image_url=image_url,
                summary="故宫博物院近期展览（展期以官网为准）",
            )
        )
    return results


# ---------------------------------------------------------------------------
# 第三轮补充：良渚博物院 / 中国丝绸博物馆 / 苏州博物馆（免费预约型特展）
# ---------------------------------------------------------------------------

SZ_HEADERS = {**MUSEUM_HEADERS, "Connection": "close"}

_LZ_RANGE_RE = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*"
    r"[—–\-~～至到]{1,2}\s*(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日?"
)


def _article_dates(text: str) -> tuple[date | None, date | None, bool]:
    """从「展讯」文章正文里抽展期，返回 (start, end, 是否找到明确展期)。"""
    match = _LZ_RANGE_RE.search(text)
    if match:
        start = to_date(match.group(1), match.group(2), match.group(3))
        year = int(match.group(4) or match.group(1))
        end = to_date(year, match.group(5), match.group(6))
        if start and end and end < start:
            end = None
        if start:
            return start, end, True

    end_match = re.search(r"展期(?:将)?至\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    start_match = re.search(
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^。；]{0,24}?(?:开幕|开展|展出|亮相)", text
    )
    start = to_date(start_match.group(1), start_match.group(2), start_match.group(3)) if start_match else None
    end = to_date(end_match.group(1), end_match.group(2), end_match.group(3)) if end_match else None
    if start and end and end < start:
        end = None
    return start, end, bool(end_match)


def _article_title(soup: BeautifulSoup, text: str) -> str:
    """展讯文章标题往往写成「展讯 | xxx 开幕」，优先从正文里取正式展名。"""
    # 文章末尾常带「展览名称：“xxx”」信息块，最可靠
    for pattern in (r"展览名称[：:]\s*[“\"]([^”\"]{3,60})[”\"]", r"展览名称[：:]\s*([^\s。；]{3,60})"):
        match = re.search(pattern, text)
        if match:
            return clean_text(match.group(1))
    for quoted in re.findall(r"[“\"]([^”\"]{3,60})[”\"]", text):
        if "展" in quoted and len(quoted) >= 4:
            return quoted
    title_tag = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    title_tag = re.sub(r"\s*[-_|]\s*.{0,12}博物院.*$", "", title_tag).strip()
    title_tag = re.sub(
        r"^(展讯|展览预告|新展开幕|展览开幕|展览|国际展讯|外展展讯)\s*[|｜丨]\s*", "", title_tag
    )
    return title_tag


def _museum_lzmuseum(source: dict) -> list[dict]:
    """良渚博物院「引进展览」：每篇展讯是一篇文章，展期写在正文里。"""
    url = clean_text(source.get("url")) or "https://www.lzmuseum.cn/YinJinZhanLan/index.html"
    html_text = http_get(url, timeout=25)
    soup = BeautifulSoup(html_text, "html.parser")

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if not re.search(r"/YinJinZhanLan/\d+\.html$", href):
            continue
        full = urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        image_url = ""
        node = anchor
        for _ in range(3):
            node = node.parent
            if node is None:
                break
            img = node.select_one("img[src]")
            if img and img.get("src"):
                image_url = urljoin(url, img["src"])
                break
        entries.append((full, image_url))

    # 文章 id 以年份开头，按 id 倒序 = 新的在前；只回抓前 max_articles 篇
    def _article_id(pair: tuple[str, str]) -> int:
        match = re.search(r"/(\d+)\.html$", pair[0])
        return int(match.group(1)) if match else 0

    entries.sort(key=_article_id, reverse=True)
    max_articles = max(1, int(source.get("max_articles", 12)))

    results: list[dict] = []
    for full, image_url in entries[:max_articles]:
        try:
            detail_html = http_get(full, timeout=25)
        except Exception:  # noqa: BLE001
            continue
        detail_soup = BeautifulSoup(detail_html, "html.parser")
        text = clean_text(detail_soup.get_text(" ", strip=True))
        title = _article_title(detail_soup, text)
        if len(title) < 2:
            continue
        start, end, explicit = _article_dates(text)
        if start is None and end is None:
            continue  # 没有展期的宣传稿无法判断是否在展
        if not future_or_ongoing(start, end):
            continue
        if start is None:
            start = end
        if not image_url:
            upload = [img.get("src") for img in detail_soup.select("img[src]") if "/upload/" in (img.get("src") or "")]
            if upload:
                image_url = urljoin(url, upload[-1])

        raw_range = ""
        range_match = re.search(
            r"展期[：:]\s*((?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日?"
            r"(?:\s*[至到—–\-~～]\s*(?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日)?)",
            text,
        )
        if not range_match:
            range_match = re.search(
                r"展期(?:将)?至\s*(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)", text
            )
        if range_match:
            raw_range = clean_text(range_match.group(1))

        note = ""
        if end is None:
            end = start + timedelta(days=120)
            note = "（展期以官网为准）"
        summary = f"展期：{raw_range.lstrip('展期：: ')}" if raw_range else "展期以官网为准"
        if note:
            summary += note

        venue = "良渚博物院 临展厅" if "临展厅" in text else "良渚博物院"
        results.append(
            _result(
                title=title.strip("“”\""),
                category="展览",
                venue=venue,
                address=MUSEUM_DEFAULT_ADDRESS["良渚博物院"],
                city="杭州",
                start_date=start,
                end_date=end,
                price_text="免费（需预约）",
                ticket_url=full,
                source_url=url,
                image_url=image_url,
                summary=summary,
            )
        )
        time.sleep(SLEEP)
    return results


def _museum_silkmuseum(source: dict) -> list[dict]:
    """中国丝绸博物馆「在展」列表：列表给起始日，详情页给完整展期。"""
    url = clean_text(source.get("url")) or "https://www.chinasilkmuseum.com/zz/list_17.aspx"
    html_text = http_get(url, headers={**MUSEUM_HEADERS, "Connection": "close"}, timeout=25)
    soup = BeautifulSoup(html_text, "html.parser")

    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if not re.search(r"info_1[78]\.aspx\?itemid=\d+", href):
            continue
        full = urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        title = clean_text(anchor.get_text(" ", strip=True))
        parent = anchor.find_parent(["li", "div", "tr"])
        block_text = clean_text(parent.get_text(" ", strip=True)) if parent else ""
        if not title:
            title = block_text.split("展览时间")[0].replace("查看更多>", "").strip()
        img = parent.select_one("img[src]") if parent else None
        image_url = urljoin(url, img["src"]) if img and img.get("src") else ""
        entries.append((full, title, image_url))

    max_articles = max(1, int(source.get("max_articles", 10)))
    results: list[dict] = []
    for full, title, image_url in entries[:max_articles]:
        try:
            detail_html = http_get(full, headers={**MUSEUM_HEADERS, "Connection": "close"}, timeout=25)
        except Exception:  # noqa: BLE001
            continue
        detail_soup = BeautifulSoup(detail_html, "html.parser")
        text = clean_text(detail_soup.get_text(" ", strip=True))
        if not title:
            title = _article_title(detail_soup, text)
        title = title.strip("“”\"")
        if len(title) < 2:
            continue

        date_text = ""
        date_match = re.search(
            r"展览时间[：:]\s*((?:20\d{2}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*日?"
            r"(?:\s*[-—–~至到]\s*(?:20\d{2}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*日)?)",
            text,
        )
        if date_match:
            date_text = date_match.group(1)
        start, end = parse_date_range(date_text)
        if end is None:
            span_match = re.search(r"展期(?:持续)?到\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
            if span_match and start:
                end = to_date(start.year, span_match.group(1), span_match.group(2))
                if end and end < start:
                    end = to_date(start.year + 1, span_match.group(1), span_match.group(2))
        if start is None and end is None:
            continue

        permanent = False
        if start and end and (end - start).days > 730:
            permanent = True  # 源站把常设陈列标成 2016-...-2028 这种超长展期
        elif end is None and start and (TODAY - start).days > 365:
            permanent = True
        if permanent:
            # 常设陈列不是「上新」，源站也没给真实展期，跳过以免页面出现假日期
            continue
        if end is None:
            end = start + timedelta(days=120)
        if not future_or_ongoing(start, end):
            continue

        venue_text = ""
        venue_match = re.search(r"展览地点[：:]\s*([^\s。；]{2,30})", text)
        if venue_match:
            venue_text = clean_text(venue_match.group(1))
        venue = f"中国丝绸博物馆 {venue_text}".strip()

        summary = "常设陈列，展期以官网为准" if permanent else f"展期：{date_text}" if date_text else "展期以官网为准"
        results.append(
            _result(
                title=title,
                category="展览",
                venue=venue,
                address=MUSEUM_DEFAULT_ADDRESS["中国丝绸博物馆"],
                city="杭州",
                start_date=start,
                end_date=end,
                price_text="免费（需预约）",
                ticket_url=full,
                source_url=url,
                image_url=image_url,
                summary=summary,
            )
        )
        time.sleep(SLEEP)
    return results


def _museum_szmuseum(source: dict) -> list[dict]:
    """苏州博物馆「临时展览」当前展览列表（其 443 端口 keep-alive 会 hang，需 Connection: close）。"""
    url = clean_text(source.get("url")) or "https://www.szmuseum.com/Exhibition/Temporary"
    html_text = http_get(url, headers=SZ_HEADERS, timeout=25)
    soup = BeautifulSoup(html_text, "html.parser")

    results: list[dict] = []
    for li in soup.select("div.extemwrap ul li"):
        link = li.select_one('a[href*="TemporaryDetails"]')
        if not link:
            continue
        title_el = li.select_one("h1 a") or link
        title = clean_text(title_el.get("title") or title_el.get_text(" ", strip=True))
        if len(title) < 2:
            continue

        date_text = ""
        venue_text = ""
        intro = ""
        for paragraph in li.select("div.activity_r_detail p"):
            spans = [clean_text(span.get_text(" ", strip=True)) for span in paragraph.select("span")]
            if spans and "展览时间" in spans[0]:
                date_text = " ".join(spans[1:])
            elif spans and "展览地点" in spans[0]:
                venue_text = " ".join(spans[1:])
            elif not spans:
                text = clean_text(paragraph.get_text(" ", strip=True))
                if text and len(text) > len(intro):
                    intro = text

        range_match = re.search(
            r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^0-9]{0,20}?"
            r"[-—–~至到]\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
            date_text,
        )
        if range_match:
            start = to_date(range_match.group(1), range_match.group(2), range_match.group(3))
            end = to_date(range_match.group(4), range_match.group(5), range_match.group(6))
        else:
            start, end = parse_date_range(date_text)
        if not future_or_ongoing(start, end):
            continue
        if end is None:
            end = start + timedelta(days=120)

        img = li.select_one("img[src]")
        image_url = img["src"] if img and img.get("src") else ""
        if image_url.startswith("//"):
            image_url = "https:" + image_url

        venue = venue_text
        if venue and "苏州博物馆" not in venue:
            venue = f"苏州博物馆 {venue}"
        results.append(
            _result(
                title=title,
                category="展览",
                venue=venue or "苏州博物馆",
                address=MUSEUM_DEFAULT_ADDRESS["苏州博物馆"],
                city="苏州",
                start_date=start,
                end_date=end,
                price_text="免费（需预约）",
                ticket_url=urljoin(url, link["href"]),
                source_url=url,
                image_url=image_url,
                summary=f"展期：{date_text}" if date_text else "展期以官网为准",
            )
        )
    return results


MUSEUM_COLLECTORS: dict[str, Callable[[dict], list[dict]]] = {
    "chnmuseum": _museum_chnmuseum,
    "njmuseum": _museum_njmuseum,
    "hnmuseum": _museum_hnmuseum,
    "sxhm": _museum_sxhm,
    "dpm": _museum_dpm,
    "lzmuseum": _museum_lzmuseum,
    "silkmuseum": _museum_silkmuseum,
    "szmuseum": _museum_szmuseum,
}


def collect_museum_cn(source: dict) -> list[dict]:
    """全国博物馆/美术馆官网展览。

    读取字段：site（必填，见 MUSEUM_COLLECTORS，可用 chnmuseum / njmuseum /
    hnmuseum / sxhm / dpm / lzmuseum / silkmuseum / szmuseum）、city（中文名，可选）、
    url（可覆盖默认列表页）、max_pages（陕历博分页用）、max_articles（展讯文章型站点用）。
    """
    try:
        site = clean_text(source.get("site")).lower()
        collector = MUSEUM_COLLECTORS.get(site)
        if not collector:
            log(f"  [museum_cn] 未知 site：{site}")
            return []
        items = collector(source)
        # 统一兜底：city / category / source_url
        city = clean_text(source.get("city"))
        source_url = clean_text(source.get("url"))
        for item in items:
            item["category"] = "展览"
            if city and not item.get("city"):
                item["city"] = city
            if source_url and not item.get("source_url"):
                item["source_url"] = source_url
        log(f"  [museum_cn] {source.get('name', site)} 抓到 {len(items)} 条")
        return items
    except Exception as exc:  # noqa: BLE001
        log(f"  [museum_cn] 采集异常：{exc}")
        return []


# ---------------------------------------------------------------------------
# 适配器注册表（update.py 只需要 import 这一个 dict）
# ---------------------------------------------------------------------------

NATIONAL_ADAPTERS: dict[str, Callable[[dict], list[dict]]] = {
    "maoyan_city": collect_maoyan_city,
    "douban_city": collect_douban_city,
    "museum_cn": collect_museum_cn,
}


def _smoke_test() -> None:
    """本地自测：跑 config/sources.json 里所有新适配器的源，打印条数与耗时。"""
    config_path = Path(__file__).resolve().parent / "config" / "sources.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    for source in payload.get("sources", []):
        adapter = source.get("adapter", "")
        if adapter not in NATIONAL_ADAPTERS:
            continue
        started = time.time()
        items = NATIONAL_ADAPTERS[adapter](source)
        elapsed = round(time.time() - started, 2)
        cities = sorted({item.get("city", "") for item in items})
        print(
            f"{source.get('name')}: {len(items)} 条 / {elapsed}s / cities={cities}",
            flush=True,
        )


if __name__ == "__main__":
    _smoke_test()