"""Hacker News API, global reporting, and partial failures without live requests."""

import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from cli import main
from collect import collect
from config import DEFAULTS, load_settings
from fetch import SourceError
from reporting import build_report, write_report
from scoring import rank_topics
from social import HN_API, hacker_news_ids, parse_hacker_news
from storage import save_snapshot


class HackerNewsTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / "fixtures" / "hacker_news.json"
        text = path.read_text()
        self.payload = json.loads(text)
        self.settings = DEFAULTS.copy()
        self.settings["sources"] = ["hacker_news"]

    def response(self, url, **kwargs):
        if url == HN_API + "/topstories.json":
            return self.payload["topstories"]
        parts = url.rsplit("/", 1)
        item_id = parts[-1].removesuffix(".json")
        item = self.payload["items"][item_id]
        if isinstance(item, Exception):
            raise item
        return item

    def hacker_news_health(self, batch):
        for row in batch["health"]:
            if row["source"] == "hacker_news":
                return row

        self.fail("The collected snapshot has no Hacker News health row")

    def test_story_metadata_and_discussion_fallback(self):
        rows = parse_hacker_news(self.payload["items"]["101"], 101, 1)
        row = rows[0]

        self.assertEqual(row["title"], "Open source & fast databases")
        self.assertEqual(row["url"], "https://example.org/databases")
        self.assertEqual(row["metric_value"], 120)
        self.assertEqual(row["metric_name"], "points")
        self.assertEqual(row["comment_count"], 35)
        self.assertEqual(row["author"], "example_author")
        published_at = datetime.fromisoformat(row["published_at"])

        self.assertEqual(published_at.timestamp(), 1175714200)

        item = self.payload["items"]["103"]

        for url in (None, "", "javascript:alert(1)"):
            with self.subTest(url=url):
                sample = item.copy()
                sample["url"] = url
                rows = parse_hacker_news(sample, 103, 3)
                row = rows[0]

                self.assertEqual(row["url"], "https://news.ycombinator.com/item?id=103")
                self.assertEqual(row["discussion_url"], row["url"])

    def test_missing_metrics_stay_unknown(self):
        item = {"id": 1, "type": "story", "title": "A tech story"}
        rows = parse_hacker_news(item, 1, 1)
        row = rows[0]

        self.assertIsNone(row["metric_value"])
        self.assertIsNone(row["comment_count"])
        self.assertIsNone(row["published_at"])
        json.dumps(row, allow_nan=False)

    def test_malformed_stories_raise_source_errors(self):
        invalid_title = self.payload["items"]["101"].copy()
        invalid_title["title"] = 123

        for item in (
            [],
            {},
            {"id": 101},
            invalid_title,
        ):
            with self.subTest(item=item), self.assertRaises(SourceError):
                parse_hacker_news(item, 101, 1)

    def test_bad_top_lists_are_rejected(self):
        for payload in (None, {}, ["101"], [True], [0], [-1], [101, 101]):
            with (
                self.subTest(payload=payload),
                patch("social.fetch_json", return_value=payload),
                self.assertRaises(SourceError),
            ):
                hacker_news_ids(5, 25)

    def test_collects_once_globally_in_one_bounded_pool(self):
        # All eight configured countries must still produce only one HN sample.
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("social.fetch_json", side_effect=self.response) as request,
            patch("collect.ThreadPoolExecutor", wraps=ThreadPoolExecutor) as pool,
        ):
            batch = collect(self.settings)

        self.assertEqual(request.call_count, 7)
        pool.assert_called_once_with(max_workers=self.settings["workers"])
        rows = batch["observations"]
        ranks = []
        identifiers = []

        for row in rows:
            ranks.append(row["rank"])
            identifiers.append(row["external_id"])

            self.assertIsNone(row["country"])
            self.assertEqual(row["region"], "global")
            self.assertEqual(row["list_size"], 6)

        self.assertEqual(ranks, [1, 3])
        self.assertEqual(identifiers, ["101", "103"])

        health = []

        for row in batch["health"]:
            if row["source"] == "hacker_news":
                health.append(row)

        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["status"], "ok")
        self.assertEqual(health[0]["item_count"], 2)
        self.assertIn("4 skipped", health[0]["message"])

    def test_limit_caps_requests_and_unselected_source_makes_none(self):
        settings = self.settings.copy()
        settings.update(limit=1, timeout=3)

        with patch("social.fetch_json", side_effect=self.response) as request:
            collect(settings)

        self.assertEqual(request.call_count, 2)
        urls = []

        for call in request.call_args_list:
            urls.append(call.args[0])

            self.assertEqual(call.kwargs, {"timeout": 3})

        self.assertEqual(
            urls,
            [HN_API + "/topstories.json", HN_API + "/item/101.json"],
        )

        settings = self.settings.copy()
        settings["sources"] = ["google_trends"]

        with (
            patch("social.fetch_json") as request,
            patch("social.google_trends", return_value=[]),
        ):
            collect(settings)

        request.assert_not_called()

    def test_partial_item_failure_preserves_other_stories(self):
        self.payload["items"]["101"] = SourceError("HTTP 503")

        with patch("social.fetch_json", side_effect=self.response):
            batch = collect(self.settings)

        identifiers = []

        for row in batch["observations"]:
            identifiers.append(row["external_id"])

        self.assertEqual(identifiers, ["103"])
        health = self.hacker_news_health(batch)

        self.assertEqual(health["status"], "partial")
        self.assertEqual(health["item_count"], 1)
        self.assertIn("1 failed (HTTP 503)", health["message"])

    def test_all_item_failures_are_failed_not_empty(self):
        self.payload["topstories"] = [101, 103]
        self.payload["items"].update({"101": {}, "103": SourceError("HTTP 503")})

        with patch("social.fetch_json", side_effect=self.response):
            batch = collect(self.settings)

        self.assertEqual(batch["observations"], [])
        health = self.hacker_news_health(batch)

        self.assertEqual(health["status"], "failed")
        self.assertIn("2 failed", health["message"])

    def test_top_list_failure_does_not_cancel_another_source(self):
        settings = self.settings.copy()
        settings.update(sources=["hacker_news", "google_trends"], countries=["US"])
        google = [{"external_id": "news", "title": "News", "rank": 1}]

        with (
            patch("social.fetch_json", side_effect=SourceError("HTTP 503")),
            patch("social.google_trends", return_value=google),
        ):
            batch = collect(settings)

        self.assertEqual(batch["observations"][0]["source"], "google_trends")
        health = self.hacker_news_health(batch)

        self.assertEqual(health["status"], "failed")
        self.assertEqual(health["kind"], "topstories")

    def test_empty_and_fully_skipped_feeds_are_empty(self):
        for ids in ([], [102, 104, 105, 106]):
            self.payload["topstories"] = ids

            with (
                self.subTest(ids=ids),
                patch("social.fetch_json", side_effect=self.response),
            ):
                batch = collect(self.settings)

            self.assertEqual(batch["observations"], [])
            health = self.hacker_news_health(batch)

            self.assertEqual(health["status"], "empty")

    def test_global_rank_changes_and_country_lists(self):
        with patch("social.fetch_json", side_effect=self.response):
            batch = collect(self.settings)

        row = batch["observations"][0]
        previous = row.copy()
        previous["rank"] = 8
        topics = rank_topics([row], [previous])
        topic = topics[0]

        self.assertEqual(topic["countries"], [])
        self.assertEqual(topic["region"], "global")
        self.assertEqual(topic["evidence"][0]["rank_change"], 7)

    def test_default_source_cli_json_storage_and_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('countries = ["US", "JP"]\n')
            settings = load_settings(config)

            self.assertIn("hacker_news", settings["sources"])
            default_path = Path("config.toml")
            defaults = load_settings(default_path)

            self.assertIn("hacker_news", defaults["sources"])
            output, error = io.StringIO(), io.StringIO()
            env_file = root / "test.env"
            env_file.write_text("", encoding="utf-8")
            variables = {"TREND_ENGINE_ENV_FILE": str(env_file)}

            with (
                patch.dict("os.environ", variables, clear=True),
                patch("social.fetch_json", side_effect=self.response),
                redirect_stdout(output),
                redirect_stderr(error),
            ):
                result = main(
                    [
                        "run",
                        "--config",
                        str(config),
                        "--sources",
                        "hacker_news",
                        "--json",
                    ]
                )

            self.assertEqual(result, 0, error.getvalue())
            report = json.loads(output.getvalue())

            self.assertEqual(len(report["global_trends"]), 2)
            self.assertEqual(
                report["regional_trends"], {"north_america": [], "asia": []}
            )
            self.assertEqual(len(report["platform_trends"]["hacker_news"]["global"]), 2)
            self.assertTrue(settings["database"].exists())
            self.assertFalse(settings["reports_dir"].exists())
            markdown, _ = write_report(report, settings["reports_dir"])
            text = markdown.read_text()

            self.assertIn("## Top 50 trends across platforms", text)
            self.assertIn(" — Global; score ", text)
            self.assertNotIn("Platform highlights", text)
            self.assertIn("35.0 comments", text)
            self.assertIn("https://news.ycombinator.com/item?id=101", text)
            self.assertNotIn("hacker_news/None", text)

            # A failed later collection must not keep showing old successful stories.
            settings["sources"] = ["hacker_news"]

            with patch("social.fetch_json", side_effect=SourceError("HTTP 503")):
                failed = collect(settings)

            save_snapshot(settings["database"], failed)
            report = build_report(settings)

            self.assertEqual(report["global_trends"], [])
