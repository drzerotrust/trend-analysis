"""One bounded thread pool, one snapshot, independent source failures."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial

import social
from config import COUNTRIES
from fetch import SourceError

__all__ = ["collect"]


def _run_job(job):
    # A failed source gets its own health row without losing other results.
    source, country, kind, operation = job
    now = datetime.now(UTC)
    checked_at = now.isoformat()
    health = {
        "source": source,
        "country": country,
        "kind": kind,
        "checked_at": checked_at,
        "status": "ok",
        "item_count": 0,
        "message": "",
    }

    try:
        rows = operation()
        item_count = len(rows)
        health["item_count"] = item_count
        if not rows:
            health.update(status="empty", message="No usable items returned")
        elif source == "youtube":
            health["message"] = "mostPopular chart: music, movies and gaming"
        elif source == "tiktok":
            health.update(
                status="partial", message="Limited public 7-day sample; may include ads"
            )

        # Add shared fields here so each parser can stick to its source's data.
        region = "global"
        if country is not None:
            region = COUNTRIES[country]

        for row in rows:
            row["observed_at"] = checked_at
            row.update(
                source=source,
                country=country,
                region=region,
                kind=kind,
                list_size=item_count,
            )

        return rows, health
    except SourceError as exc:
        message = str(exc)
        health.update(status="failed", message=message)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        # Schema errors must not discard data from other countries/sources.
        error_type = type(exc).__name__
        message = "Unexpected response shape (%s)" % error_type
        health.update(status="failed", message=message)

    return [], health


def _make_jobs(settings, snapshot):
    # Build jobs; missing keys and unsupported markets are skipped.
    jobs = []
    timeout = settings["timeout"]
    sources = settings["sources"]
    youtube_key = os.getenv("YOUTUBE_API_KEY")

    def skip(source, country, kind, status, message):
        health = {
            "source": source,
            "country": country,
            "kind": kind,
            "status": status,
            "message": message,
            "item_count": 0,
            "checked_at": snapshot["collected_at"],
        }
        snapshot["health"].append(health)

    # Fetch this global list once, then let the shared pool fetch its story details.
    if "hacker_news" in sources:
        try:
            ids = social.hacker_news_ids(timeout, settings["limit"])
        except SourceError as exc:
            skip("hacker_news", None, "topstories", "failed", str(exc))
        else:
            if not ids:
                skip(
                    "hacker_news",
                    None,
                    "topstories",
                    "empty",
                    "No top stories returned",
                )
            for rank, item_id in enumerate(ids, 1):
                operation = partial(social.hacker_news_story, item_id, rank, timeout)
                job = ("hacker_news", None, "story", operation)
                jobs.append(job)

    # Bind each request's arguments now; the shared pool will call it later.
    for country in settings["countries"]:
        if "google_trends" in sources:
            operation = partial(social.google_trends, country, timeout)
            job = ("google_trends", country, "search", operation)
            jobs.append(job)

        if "youtube" in sources:
            if youtube_key:
                operation = partial(social.youtube, country, timeout, youtube_key)
                job = ("youtube", country, "video", operation)
                jobs.append(job)
            else:
                skip(
                    "youtube",
                    country,
                    "video",
                    "missing_credentials",
                    "Set YOUTUBE_API_KEY (free)",
                )

        if "tiktok" in sources:
            if country == "IN":
                skip(
                    "tiktok", country, "hashtag", "unavailable", "Market not supported"
                )
            else:
                operation = partial(social.tiktok_hashtags, country, timeout)
                job = ("tiktok", country, "hashtag", operation)
                jobs.append(job)

            if country in {"US", "JP", "ID"}:
                operation = partial(social.tiktok_videos, country, timeout)
                job = ("tiktok", country, "video", operation)
                jobs.append(job)
            else:
                skip("tiktok", country, "video", "unavailable", "Market not supported")

    skip(
        "instagram",
        None,
        "all",
        "disabled",
        "No general public regional trend feed integrated",
    )
    return jobs


def _summarize_hacker_news(snapshot):
    # One health row for the feed, not one per story; retain usable partial results.
    checks = []
    other_health = []
    for row in snapshot["health"]:
        if row["source"] != "hacker_news":
            other_health.append(row)
        elif row["kind"] == "story":
            checks.append(row)

    if not checks:
        return

    count = 0
    failures = []
    for row in checks:
        count += row["item_count"]
        if row["status"] == "failed":
            failures.append(row)

    # Deleted/non-story entries are skips, not failed requests.
    sample_size = len(checks)
    failure_count = len(failures)
    skipped = sample_size - count - failure_count
    summary = checks[0].copy()
    summary["item_count"] = count
    summary["status"] = "empty"
    if count:
        summary["status"] = "ok"

    summary["message"] = "Global HN feed: %s/%s sampled entries usable; %s skipped" % (
        count,
        sample_size,
        skipped,
    )
    if failures:
        summary["status"] = "failed"
        if count:
            summary["status"] = "partial"

        summary["message"] += "; %s failed (%s)" % (
            failure_count,
            failures[0]["message"],
        )

    snapshot["health"] = other_health
    snapshot["health"].append(summary)
    for row in snapshot["observations"]:
        if row["source"] == "hacker_news":
            row["list_size"] = sample_size


def _health_order(row):
    # Global feeds sort before named countries within each source.
    country = row["country"] or ""
    return row["source"], country, row["kind"]


def collect(settings):
    now = datetime.now(UTC)
    snapshot = {
        "collected_at": now.isoformat(),
        "observations": [],
        "health": [],
    }
    jobs = _make_jobs(settings, snapshot)

    # Run independent requests in one small pool, then combine their results.
    with ThreadPoolExecutor(max_workers=settings["workers"]) as pool:
        results = pool.map(_run_job, jobs)
        for rows, health in results:
            snapshot["observations"].extend(rows)
            snapshot["health"].append(health)

    _summarize_hacker_news(snapshot)
    snapshot["health"].sort(key=_health_order)
    return snapshot
