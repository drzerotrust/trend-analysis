"""Build JSON evidence and a Markdown report using ordinary Python strings."""

import html
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

from compatibility import CONTRACT_VERSIONS
from config import SOURCES
from scoring import rank_topics
from storage import load_snapshots

__all__ = ["build_report", "write_report"]


def _news_observations(snapshot):
    # Old snapshots remain untouched; only supported news/social feeds reach reports.
    observations = []
    for row in snapshot.get("observations", []):
        if row["source"] in SOURCES:
            observations.append(row)

    return observations


def _platform_order(row):
    # Keep feed types together, then use their original feed positions.
    return row["kind"], row["rank"]


def _platform_trends(observations, limit):
    # Group copied evidence by source and country without exposing raw payloads.
    platforms = {}
    for row in observations:
        source = row["source"]
        country = row["country"] or "global"
        if source not in platforms:
            platforms[source] = {}
        if country not in platforms[source]:
            platforms[source][country] = []

        item = row.copy()
        item.pop("raw", None)
        platforms[source][country].append(item)

    # Sort names and feed positions before applying each country's display limit.
    result = {}
    sources = sorted(platforms)
    for source in sources:
        result[source] = {}
        countries = sorted(platforms[source])
        for country in countries:
            rows = platforms[source][country]
            rows.sort(key=_platform_order)
            result[source][country] = rows[:limit]

    return result


def build_report(settings, *, now=None):
    now = now or datetime.now(UTC)
    window = timedelta(hours=settings["window"])
    since = now - window
    snapshots = load_snapshots(settings["database"], since, now)

    # Use whole snapshots, including failed ones; never backfill an older success.
    latest = {}
    previous = {}
    if snapshots:
        latest = snapshots[-1]
    if len(snapshots) > 1:
        previous = snapshots[-2]

    observations = _news_observations(latest)
    old_observations = _news_observations(previous)
    topics = rank_topics(observations, old_observations)

    # Missing data and snapshots older than six hours are both marked stale.
    collected_at = latest.get("collected_at")
    stale = True
    if collected_at is not None:
        collected_time = datetime.fromisoformat(collected_at)
        age = now - collected_time
        stale = age > timedelta(hours=6)

    source_health = []
    supported_health_sources = (*SOURCES, "instagram")
    for row in latest.get("health", []):
        if row["source"] in supported_health_sources:
            source_health.append(row)

    # Topics already have score order. Keep that order within each region.
    regional_topics = {"global": [], "north_america": [], "asia": []}
    limit = settings["limit"]
    for topic in topics:
        region = topic["region"]
        if region in regional_topics:
            regional_topics[region].append(topic)

    for region, rows in regional_topics.items():
        regional_topics[region] = rows[:limit]

    global_trends = regional_topics.pop("global")
    platform_trends = _platform_trends(observations, limit)

    return {
        "schema_version": CONTRACT_VERSIONS["report"],
        "generated_at": now.isoformat(),
        "window_hours": settings["window"],
        "window_start": since.isoformat(),
        "collected_at": collected_at,
        "stale": stale,
        "previous_collected_at": previous.get("collected_at"),
        "legacy_snapshot": latest.get("legacy", False),
        "source_health": source_health,
        "global_trends": global_trends,
        "regional_trends": regional_topics,
        "platform_trends": platform_trends,
    }


def _cell(value):
    if value is None:
        value = "—"

    text = str(value)
    text = html.escape(text)

    # Neutralize links, formatting, HTML, and table breaks in public source text.
    for character in ("\\", "|", "[", "]", "*", "_", "`"):
        text = text.replace(character, "\\" + character)

    # Collapse source whitespace so a value cannot span multiple table rows.
    words = text.split()
    return " ".join(words)


def _link(label, url):
    label = _cell(label)
    if not url:
        return label

    # Source URLs may be untrusted; only HTTP(S) URLs become clickable links.
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"https", "http"}:
        return label

    safe_url = quote(url, safe=":/?=&%#@+,-._~")
    return "[%s](%s)" % (label, safe_url)


def _table(headers, rows):
    heading = " | ".join(headers)
    separators = ["---"] * len(headers)
    divider = " | ".join(separators)
    lines = ["| %s |" % heading, "| %s |" % divider]

    # Escape each cell before joining it into a Markdown table row.
    for row in rows:
        cells = []
        for value in row:
            cell = _cell(value)
            cells.append(cell)

        content = " | ".join(cells)
        lines.append("| %s |" % content)

    lines.append("")
    return lines


def _report_intro(report):
    # Explain the snapshot age and show which sources returned usable data.
    latest_snapshot = report["collected_at"] or "none in this window"
    lines = [
        "# Trend Engine",
        "",
        ("Generated: %s" % report["generated_at"]),
        ("Latest snapshot: %s" % latest_snapshot),
        ("Window: %s hours" % report["window_hours"]),
        "",
        "News and attention signals for research; not trade recommendations. "
        "Regional lists describe sampled search/social feeds, not verified news.",
        "Hacker News is a global community feed for information and tech trends, "
        "not evidence of popularity in a particular country.",
        "",
        "Rank score = sum of 1 / feed position, at most once per source/country. "
        "Feed order is not a platform-wide popularity measure.",
        "",
    ]

    if report["stale"]:
        lines += [
            "**Stale or missing data:** no snapshot within the last six hours.",
            "",
        ]
    if report["previous_collected_at"] is None:
        lines += ["Cold start: no earlier snapshot in the window to compare.", ""]
    if report["legacy_snapshot"]:
        lines += [
            "Legacy snapshot: collected before the current source parsers.",
            "",
        ]

    # Older snapshots may not have a country, kind, or diagnostic message.
    rows = []
    for row in report["source_health"]:
        country = row.get("country") or "all"
        kind = row.get("kind", "all")
        coverage = "%s / %s" % (country, kind)
        cells = [
            row["source"],
            coverage,
            row["status"],
            row["item_count"],
            row["checked_at"],
            row.get("message"),
        ]
        rows.append(cells)

    headers = ["Source", "Country / kind", "Status", "Items", "Checked", "Detail"]
    table = _table(headers, rows)
    lines += ["## Source health", ""]
    lines.extend(table)
    return lines


def _evidence_lines(row):
    # No previous matching observation means unknown movement, not zero change.
    movement = "unknown"
    change = row["rank_change"]
    if change is not None:
        movement = "%+d" % change

    source = _cell(row["source"])
    country = row["country"] or "global"
    kind = _cell(row["kind"])
    source_url = row.get("url")
    link = _link("source", source_url)
    observed_at = _cell(row["observed_at"])
    line = "  - %s/%s %s, position %s, change %s: %s (observed %s)" % (
        source,
        country,
        kind,
        row["rank"],
        movement,
        link,
        observed_at,
    )
    lines = [line]

    # HN provides discussion details in addition to the article evidence.
    if row["source"] == "hacker_news":
        points = _cell(row.get("metric_value"))
        comments = _cell(row.get("comment_count"))
        discussion_url = row.get("discussion_url")
        discussion = _link("Discussion", discussion_url)
        published_at = _cell(row.get("published_at"))
        line = "    %s points; %s comments; %s; published %s." % (
            points,
            comments,
            discussion,
            published_at,
        )
        lines.append(line)

    return lines


def _trend_sections(report):
    # Lead with the global information/tech channel; keep regional evidence separate.
    lines = []
    sections = {"global": report["global_trends"]}
    sections.update(report["regional_trends"])
    for region, topics in sections.items():
        title = region.replace("_", " ")
        title = title.title()
        if region == "global":
            title = "Global information and tech trends — Hacker News"
        lines += [("## %s" % title), ""]
        if not topics:
            lines += ["No observations in the latest snapshot.", ""]

        for topic in topics:
            title = _cell(topic["title"])
            countries = ", ".join(topic["countries"]) or "global"
            summary = "- %s — score %s; %s" % (title, topic["score"], countries)
            lines.append(summary)

            for row in topic["evidence"]:
                evidence = _evidence_lines(row)
                lines.extend(evidence)

        lines.append("")

    highlights = _platform_sections(report)
    lines.extend(highlights)
    return lines


def _platform_sections(report):
    # Display the original feed positions separately from grouped topic scores.
    lines = ["## Platform highlights", ""]
    for source, countries in report["platform_trends"].items():
        source_label = _cell(source)
        for country, rows in countries.items():
            lines += ["### %s / %s" % (source_label, country), ""]
            for row in rows:
                url = row.get("url")
                link = _link(row["title"], url)
                kind = _cell(row["kind"])
                metric_value = _cell(row.get("metric_value"))
                metric_name = _cell(row.get("metric_name"))
                line = "- %s — %s, position %s; %s %s." % (
                    link,
                    kind,
                    row["rank"],
                    metric_value,
                    metric_name,
                )
                lines.append(line)

            lines.append("")

    return lines


def write_report(report, directory):
    # Microseconds plus exist_ok=False avoid silently replacing earlier reports.
    generated_at = datetime.fromisoformat(report["generated_at"])
    stamp = generated_at.strftime("%Y-%m-%d/%H%M%S-%f")
    destination = directory / stamp
    destination.mkdir(parents=True, exist_ok=False)

    lines = _report_intro(report)
    trends = _trend_sections(report)
    lines.extend(trends)

    # JSON keeps the structured evidence; Markdown is the human-readable report.
    json_path = destination / "report.json"
    markdown_path = destination / "report.md"
    json_text = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    markdown_text = "\n".join(lines)
    json_path.write_text(json_text + "\n", encoding="utf-8")
    markdown_path.write_text(markdown_text, encoding="utf-8")

    return markdown_path, json_path
