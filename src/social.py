"""Each collector is a request followed by a small, fixture-tested parser."""

import html
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import quote, urlencode, urlsplit

from fetch import SourceError, fetch_json, fetch_text, items, number

__all__ = [
    "hacker_news_ids",
    "hacker_news_story",
    "google_trends",
    "youtube",
    "tiktok_hashtags",
    "tiktok_videos",
]

HN_API = "https://hacker-news.firebaseio.com/v0"


def hacker_news_ids(timeout, limit):
    # The public API returns ranked IDs first, not full stories. No key needed.
    ids = fetch_json(HN_API + "/topstories.json", timeout=timeout)
    if not isinstance(ids, list):
        raise SourceError("Hacker News top stories must be a list of positive IDs")

    for item in ids:
        # A boolean is not an item ID, even though Python treats it as an int.
        if type(item) is not int or item <= 0:
            raise SourceError("Hacker News top stories must be a list of positive IDs")

    unique_ids = set(ids)
    if len(unique_ids) != len(ids):
        raise SourceError("Hacker News top stories contained duplicate IDs")

    return ids[:limit]


def hacker_news_story(item_id, rank, timeout):
    url = HN_API + "/item/%s.json" % item_id
    item = fetch_json(url, timeout=timeout)
    return parse_hacker_news(item, item_id, rank)


def parse_hacker_news(item, item_id, rank):
    # Entries can disappear or be jobs; preserve original ranks when skipping them.
    if item is None:
        return []
    if not isinstance(item, dict) or item.get("id") != item_id:
        raise SourceError("Hacker News item %s has an invalid response" % item_id)
    if item.get("deleted") or item.get("dead"):
        return []
    if item.get("type") in {"job", "comment", "poll", "pollopt"}:
        return []

    title = item.get("title")
    if item.get("type") != "story" or not isinstance(title, str) or not title.strip():
        raise SourceError("Hacker News item %s is missing a story title/type" % item_id)
    # Remove markup before decoding entities, then reject empty display titles.
    title = re.sub(r"<[^>]*>", "", title)
    title = html.unescape(title)
    title = title.strip()
    if not title:
        raise SourceError("Hacker News item %s has an empty title" % item_id)

    # Ask HN and other text posts have no article URL; link to their discussion.
    discussion = "https://news.ycombinator.com/item?id=%s" % item_id
    url = item.get("url")
    if not isinstance(url, str):
        url = discussion
    else:
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in {"http", "https"}:
            url = discussion

    published_at = None
    timestamp = item.get("time")
    timestamp = number(timestamp)
    if timestamp is not None:
        try:
            published_time = datetime.fromtimestamp(timestamp, UTC)
            published_at = published_time.isoformat()
        except (ValueError, OverflowError, OSError) as exc:
            raise SourceError(
                "Hacker News item %s has an invalid time" % item_id
            ) from exc

    points = item.get("score")
    points = number(points)
    comments = item.get("descendants")
    comments = number(comments)

    return [
        {
            "external_id": str(item_id),
            "title": title,
            "rank": rank,
            "url": url,
            "discussion_url": discussion,
            "metric_value": points,
            "metric_name": "points",
            "comment_count": comments,
            "author": item.get("by"),
            "published_at": published_at,
            "raw": item,
        }
    ]


def google_trends(country, timeout):
    text = fetch_text(
        "https://trends.google.com/trending/rss",
        params={"geo": country},
        timeout=timeout,
    )
    return parse_google_rss(text, country)


def parse_google_rss(text, country):
    # Read the official feed and turn each entry into a simple observation.
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SourceError("Google Trends returned malformed RSS") from exc
    if root.tag != "rss":
        raise SourceError("Google Trends response was not RSS")

    result = []
    entries = root.findall("./channel/item")
    for rank, item in enumerate(entries, 1):
        title = item.findtext("title") or ""
        title = title.strip()
        if not title:
            continue

        traffic = item.findtext("{*}approx_traffic", "")
        searches = _search_count(traffic)
        query = urlencode({"geo": country, "q": title})
        url = "https://trends.google.com/trends/explore?" + query
        observation = {
            "external_id": title.casefold(),
            "title": title,
            "rank": rank,
            "url": url,
            "metric_value": searches,
            "metric_name": "approx_searches",
            "published_at": item.findtext("pubDate"),
            "raw": {"traffic": traffic},
        }
        result.append(observation)

    return result


def _search_count(traffic):
    # Expand counts such as 200K+ while keeping unknown values as None.
    match = re.fullmatch(r"([\d,.]+)\s*([KMB])?\+?", traffic, re.I)
    if not match:
        return None

    digits = match[1].replace(",", "")
    searches = number(digits)
    if searches is None:
        return None

    suffix = match[2] or ""
    suffix = suffix.upper()
    multipliers = {"K": 1e3, "M": 1e6, "B": 1e9}
    multiplier = multipliers.get(suffix, 1)
    return searches * multiplier


def youtube(country, timeout, key):
    payload = fetch_json(
        "https://www.googleapis.com/youtube/v3/videos",
        timeout=timeout,
        params={
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "maxResults": 50,
            "regionCode": country,
            "key": key,
        },
    )
    return parse_youtube(payload)


def parse_youtube(payload):
    result = []
    videos = items(payload, "items")
    for rank, item in enumerate(videos, 1):
        snippet = item.get("snippet", {})
        title = snippet.get("title")
        video_id = item.get("id")
        if not title or not video_id:
            continue

        # View counts are optional; missing statistics must not look like zero views.
        statistics = item.get("statistics", {})
        views = statistics.get("viewCount")
        views = number(views)
        query = urlencode({"v": video_id})
        url = "https://www.youtube.com/watch?" + query
        observation = {
            "external_id": str(video_id),
            "title": title,
            "rank": rank,
            "url": url,
            "metric_value": views,
            "metric_name": "views",
            "raw": item,
        }
        result.append(observation)

    return result


def tiktok_hashtags(country, timeout):
    payload = fetch_json(
        "https://ads.tiktok.com/CreativeOne/KnowledgeAPI/GetHashtagList",
        timeout=timeout,
        data={"timeRange": 7, "countryCode": country, "page": 1, "limit": 20},
    )
    return parse_tiktok(payload, "hashtag")


def tiktok_videos(country, timeout):
    # The video list needs the latest date supplied by the overview endpoint.
    host = "https://ads.tiktok.com"
    if country == "US":
        host = "https://ads.us.tiktok.com"

    base = host + "/CreativeOne/Report/"
    overview = fetch_json(base + "GetTopContentsOverview", timeout=timeout)
    if not isinstance(overview, dict) or not overview.get("lastDailyEndTimestamp"):
        raise SourceError("TikTok video overview lacks a daily timestamp")

    payload = fetch_json(
        base + "CreativeCenterGetTopContentsList",
        timeout=timeout,
        params={
            "periodDimension": 3,
            "periodEndTimestamp": overview["lastDailyEndTimestamp"],
            "orderByMetric": 1,
            "countryCode": country,
            "contentLabelIDs": "",
            "organicOnly": "false",
            "limit": 20,
            "page": 1,
        },
    )
    return parse_tiktok(payload, "video")


def parse_tiktok(payload, kind):
    # Hashtags and videos use different response keys but share the output shape.
    response_status = payload.get("BaseResp", {})
    if response_status.get("StatusCode", 0) != 0:
        raise SourceError("TikTok returned a nonzero status")

    key = "entityInfos"
    if kind == "hashtag":
        key = "items"

    entries = items(payload, key)
    result = []
    for rank, item in enumerate(entries, 1):
        if kind == "hashtag":
            title = item.get("hashtagName") or ""
            title = str(title)
            title = title.strip()
            title = title.lstrip("#")
            identifier = item.get("hashtagID")
            tag = quote(title, safe="")
            url = "https://www.tiktok.com/tag/" + tag
            views = item.get("vv")
            if title:
                title = "#" + title
        else:
            info = item.get("itemInfo", {})
            title = info.get("title")
            identifier = info.get("itemID")

            # Include the author handle when available; escape URL path segments.
            author = item.get("itemAuthorInfo", {})
            handle = author.get("handlerName")
            prefix = ""
            if handle:
                handle = quote(handle, safe="")
                prefix = "@%s/" % handle

            video_id = str(identifier)
            video_id = quote(video_id, safe="")
            url = "https://www.tiktok.com/%svideo/%s" % (prefix, video_id)
            metrics = item.get("itemMetrics", {})
            views = metrics.get("videoViews")

        if not title or not identifier:
            continue

        observation = {
            "external_id": str(identifier),
            "title": title,
            "rank": rank,
            "url": url,
            "metric_value": number(views),
            "metric_name": "views",
            "source_window_days": 7,
            "raw": item,
        }
        result.append(observation)

    return result
