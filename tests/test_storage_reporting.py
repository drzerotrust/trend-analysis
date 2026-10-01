"""CLI, persistence, legacy compatibility, and report integration."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import markdown as markdown_renderer
from peewee import IntegrityError

from cli import main
from config import load_settings
from reporting import build_report, write_report
from storage import load_snapshots, save_snapshot

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


def snapshot(title="A useful trend", when=NOW):
    return {
        "collected_at": when.isoformat(),
        "observations": [
            {
                "source": "google_trends",
                "country": "US",
                "region": "north_america",
                "kind": "search",
                "external_id": title,
                "title": title,
                "rank": 1,
                "observed_at": when.isoformat(),
                "url": "https://example.org/a(b)|x",
            }
        ],
        "health": [
            {
                "source": "google_trends",
                "country": "US",
                "kind": "search",
                "status": "ok",
                "item_count": 1,
                "checked_at": when.isoformat(),
                "message": "",
            }
        ],
    }


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text('countries = ["US"]\nsources = ["google_trends"]\n')
        self.settings = load_settings(self.config)

        # Subprocess tests inherit only a temporary, empty credential file.
        env_file = self.root / "test.env"
        env_file.write_text("", encoding="utf-8")
        variables = {"TREND_ENGINE_ENV_FILE": str(env_file)}
        environment = patch.dict(os.environ, variables, clear=True)
        self.enterContext(environment)

    def test_round_trip_and_report_without_replacing_same_minute_files(self):
        original = snapshot("Trend | [bad](javascript:alert(1)) <script>")
        save_snapshot(self.settings["database"], original)
        stored = load_snapshots(self.settings["database"], NOW, NOW)

        self.assertEqual(stored, [original])

        report = build_report(self.settings, now=NOW)
        markdown, data = write_report(report, self.settings["reports_dir"])
        json_text = data.read_text()
        saved_report = json.loads(json_text)

        self.assertEqual(saved_report["schema_version"], 3)
        text = markdown.read_text()

        self.assertNotIn("<script>", text)
        self.assertIn("%28b%29%7Cx", text)

        with self.assertRaises(FileExistsError):
            write_report(report, self.settings["reports_dir"])

        json_text = data.read_text()
        saved_report = json.loads(json_text)

        self.assertEqual(saved_report["collected_at"], NOW.isoformat())

    def test_latest_run_replaces_old_items_even_after_failure(self):
        previous_time = NOW - timedelta(hours=6)
        previous = snapshot("Old", previous_time)
        save_snapshot(self.settings["database"], previous)

        latest = snapshot()
        latest["observations"] = []
        latest["health"][0].update(status="failed", item_count=0, message="HTTP 429")
        save_snapshot(self.settings["database"], latest)
        report = build_report(self.settings, now=NOW)

        self.assertEqual(report["regional_trends"]["north_america"], [])
        self.assertEqual(report["source_health"][0]["status"], "failed")

    def test_markdown_titles_do_not_become_headings_inside_lists(self):
        # Python-Markdown treats even hashtags without a space as headings.
        titles = [
            "#citytastetest",
            "# Heading one",
            "## Heading two",
            "### Heading three",
            "#### Heading four",
            "##### Heading five",
            "###### Heading six",
            "#추석",
            "  # Leading whitespace\n## Still the same title",
        ]
        batch = snapshot()
        template = batch["observations"][0]
        batch["observations"] = []

        for country, region in (("US", "north_america"), ("JP", "asia")):
            for rank, title in enumerate(titles, start=1):
                row = template.copy()
                row.update(
                    source="youtube",
                    kind="video",
                    country=country,
                    region=region,
                    title=title,
                    external_id=str(rank),
                    rank=rank,
                )
                # Exercise both linked titles and the plain-text URL fallback.
                if country == "JP":
                    row["url"] = None

                batch["observations"].append(row)

        batch["health"] = []
        save_snapshot(self.settings["database"], batch)

        report = build_report(self.settings, now=NOW)
        markdown_path, json_path = write_report(report, self.settings["reports_dir"])
        text = markdown_path.read_text(encoding="utf-8")
        rendered = markdown_renderer.markdown(text, extensions=["extra"])
        document = ElementTree.fromstring("<report>%s</report>" % rendered)

        # Preserve real report headings, but never promote a collected title.
        headings = document.findall(".//h1")
        self.assertEqual(len(headings), 1)
        self.assertEqual(headings[0].text, "Trend Engine")
        self.assertEqual(len(document.findall("./h2")), 2)
        self.assertEqual(len(document.findall("./h3")), 0)
        for level in range(1, 7):
            selector = ".//li//h%s" % level
            self.assertEqual(document.findall(selector), [])

        rendered_text = "".join(document.itertext())
        for title in titles:
            words = title.split()
            visible_title = " ".join(words)
            self.assertIn(visible_title, rendered_text)

        # Escaping belongs to Markdown output, never the saved evidence or JSON.
        json_text = json_path.read_text(encoding="utf-8")
        saved_report = json.loads(json_text)
        self.assertEqual(saved_report, report)
        self.assertEqual(load_snapshots(self.settings["database"], NOW, NOW), [batch])

        for country in ("US", "JP"):
            saved_titles = []
            for row in saved_report["platform_trends"]["youtube"][country]:
                saved_titles.append(row["title"])

            self.assertEqual(saved_titles, titles)

    def test_markdown_escaping_preserves_literal_title_text(self):
        title = "# What's new: <h2>news</h2> & &#35;literal **not bold**"
        batch = snapshot(title)
        save_snapshot(self.settings["database"], batch)

        report = build_report(self.settings, now=NOW)
        markdown_path, _ = write_report(report, self.settings["reports_dir"])
        text = markdown_path.read_text(encoding="utf-8")
        rendered = markdown_renderer.markdown(text, extensions=["extra"])
        document = ElementTree.fromstring("<report>%s</report>" % rendered)
        rendered_text = "".join(document.itertext())

        self.assertIn(title, rendered_text)
        self.assertEqual(document.findall(".//li//h1"), [])
        self.assertEqual(document.findall(".//li//h2"), [])
        self.assertEqual(document.findall(".//li//strong"), [])

    def test_markdown_evidence_is_nested_under_its_trend(self):
        # Two topics in each region reveal evidence flattened into sibling items.
        batch = snapshot()
        template = batch["observations"][0]
        batch["observations"] = []
        batch["health"] = []
        locations = (
            ("US", "north_america"),
            ("CA", "north_america"),
            ("JP", "asia"),
            ("KR", "asia"),
            (None, "global"),
        )

        for country, region in locations:
            for rank, title in enumerate(("First trend", "Second trend"), start=1):
                row = template.copy()
                row.update(
                    source="youtube",
                    kind="video",
                    country=country,
                    region=region,
                    external_id=title,
                    title=title,
                    rank=rank,
                )
                if region == "global":
                    row.update(
                        source="hacker_news",
                        kind="story",
                        metric_value=17,
                        comment_count=4,
                        discussion_url="https://news.ycombinator.com/item?id=1",
                        published_at=NOW.isoformat(),
                    )

                batch["observations"].append(row)

        save_snapshot(self.settings["database"], batch)

        report = build_report(self.settings, now=NOW)
        markdown_path, _ = write_report(report, self.settings["reports_dir"])
        text = markdown_path.read_text(encoding="utf-8")
        rendered = markdown_renderer.markdown(text, extensions=["extra"])
        document = ElementTree.fromstring("<report>%s</report>" % rendered)

        # One combined list still keeps each topic's regional evidence nested.
        topics = document.findall("./ul/li")
        self.assertEqual(len(topics), 6)
        expected_sources = {
            "Global": ["hacker_news/global"],
            "North America": ["youtube/US", "youtube/CA"],
            "Asia": ["youtube/JP", "youtube/KR"],
        }
        for topic in topics:
            summary = topic.text
            evidence = topic.findall("./ul/li")
            identities = []
            for item in evidence:
                content = "".join(item.itertext())
                words = content.split()
                identities.append(words[0])

                # HN's continuation must stay inside the evidence item.
                if words[0] == "hacker_news/global":
                    self.assertIn("17 points; 4 comments;", content)
                    selector = "./a[@href='https://news.ycombinator.com/item?id=1']"
                    discussion = item.find(selector)
                    self.assertIsNotNone(discussion)

            for region, sources in expected_sources.items():
                if " — %s;" % region in summary:
                    self.assertCountEqual(identities, sources)
                    break
            else:
                self.fail("Topic is missing its region: %s" % summary)

    def test_markdown_shows_top_50_combined_topics_without_platform_duplicates(self):
        batch = snapshot()
        template = batch["observations"][0]
        batch["observations"] = []
        batch["health"] = []
        feeds = (
            ("hacker_news", None, "global", "story"),
            ("youtube", "US", "north_america", "video"),
            ("google_trends", "JP", "asia", "search"),
            ("tiktok", "KR", "asia", "hashtag"),
        )
        self.settings["limit"] = 100

        # Interleave platforms so a per-platform cap or insertion order is wrong.
        for position in range(80, 0, -1):
            source, country, region, kind = feeds[(position - 1) % 4]
            title = "Trend %03d" % position
            row = template.copy()
            row.update(
                source=source,
                country=country,
                region=region,
                kind=kind,
                title=title,
                external_id=title,
                rank=position,
            )
            batch["observations"].append(row)

        save_snapshot(self.settings["database"], batch)
        report = build_report(self.settings, now=NOW)
        markdown_path, json_path = write_report(report, self.settings["reports_dir"])
        text = markdown_path.read_text(encoding="utf-8")
        rendered = markdown_renderer.markdown(text, extensions=["extra"])
        document = ElementTree.fromstring("<report>%s</report>" % rendered)

        topics = document.findall("./ul/li")
        self.assertEqual(len(topics), 50)
        for position, topic in enumerate(topics, start=1):
            self.assertTrue(topic.text.startswith("Trend %03d — " % position))
            self.assertEqual(len(topic.findall("./ul/li")), 1)

        self.assertNotIn("Trend 051", text)
        self.assertNotIn("Platform highlights", text)
        self.assertIn("## Source health", text)

        # The visual cap must not trim the machine-readable report or history.
        json_text = json_path.read_text(encoding="utf-8")
        saved_report = json.loads(json_text)
        self.assertEqual(saved_report, report)
        row_count = 0
        for countries in saved_report["platform_trends"].values():
            for rows in countries.values():
                row_count += len(rows)

        self.assertEqual(row_count, 80)
        self.assertEqual(load_snapshots(self.settings["database"], NOW, NOW), [batch])

    def test_old_market_payloads_are_ignored_without_changing_history(self):
        old = snapshot()
        old["assets"] = [{"asset_id": "old-token", "universe": "solana"}]
        # Also ignore obsolete providers stored as observations or health rows.
        market_observation = old["observations"][0].copy()
        market_observation.update(source="robinhood", title="Old market mover")
        old["observations"].append(market_observation)
        market_health = old["health"][0].copy()
        market_health.update(source="solana", kind="market")
        old["health"].append(market_health)

        path = self.settings["database"]
        save_snapshot(path, old)
        before = path.read_bytes()
        report = build_report(self.settings, now=NOW)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(load_snapshots(path, NOW, NOW), [old])
        self.assertEqual(set(report["platform_trends"]), {"google_trends"})
        health_sources = []

        for row in report["source_health"]:
            health_sources.append(row["source"])

        self.assertEqual(health_sources, ["google_trends"])
        self.assertEqual(len(report["regional_trends"]["north_america"]), 1)
        self.assertNotIn("old-token", json.dumps(report))
        markdown, _ = write_report(report, self.settings["reports_dir"])
        text = markdown.read_text()

        for removed in (
            "market movers",
            "asset matches",
            "Robinhood",
            "Solana",
            "CoinGecko",
        ):
            self.assertNotIn(removed, text)

    def test_market_only_latest_snapshot_does_not_reuse_older_news(self):
        now = datetime.now(UTC)
        previous_time = now - timedelta(hours=1)
        previous = snapshot("Older headline", previous_time)
        save_snapshot(self.settings["database"], previous)

        latest = snapshot(when=now)
        latest["observations"] = []
        latest["assets"] = [{"asset_id": "old-token", "universe": "robinhood"}]
        latest["health"][0].update(source="robinhood", kind="market")
        save_snapshot(self.settings["database"], latest)
        before = self.settings["database"].read_bytes()
        output, error = io.StringIO(), io.StringIO()

        with redirect_stdout(output), redirect_stderr(error):
            status = main(["report", "--config", str(self.config), "--json"])

        self.assertEqual(status, 1)
        report = json.loads(output.getvalue())

        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["collected_at"], now.isoformat())
        self.assertEqual(report["global_trends"], [])
        self.assertEqual(report["platform_trends"], {})
        self.assertEqual(report["source_health"], [])
        self.assertNotIn("Older headline", output.getvalue())
        self.assertIn("No news or social observations", error.getvalue())
        self.assertEqual(self.settings["database"].read_bytes(), before)
        self.assertFalse(self.settings["reports_dir"].exists())

    def test_collection_with_no_news_observations_returns_one(self):
        now = datetime.now(UTC)
        batch = snapshot(when=now)
        batch["observations"] = []
        batch["health"][0].update(status="empty", item_count=0)

        with patch("cli.collect", return_value=batch), redirect_stdout(io.StringIO()):
            status = main(["collect", "--config", str(self.config)])

        self.assertEqual(status, 1)

    def test_future_and_outside_window_are_excluded(self):
        expired_time = NOW - timedelta(hours=25)
        future_time = NOW + timedelta(hours=1)
        expired = snapshot("Expired", expired_time)
        future = snapshot("Future", future_time)
        save_snapshot(self.settings["database"], expired)
        save_snapshot(self.settings["database"], future)

        report = build_report(self.settings, now=NOW)

        self.assertIsNone(report["collected_at"])

    def test_stale_snapshot_is_labeled(self):
        collected_at = NOW - timedelta(hours=7)
        batch = snapshot(when=collected_at)
        save_snapshot(self.settings["database"], batch)

        report = build_report(self.settings, now=NOW)

        self.assertTrue(report["stale"])

    def test_reading_missing_database_does_not_create_it(self):
        self.assertEqual(load_snapshots(self.settings["database"], NOW, NOW), [])
        self.assertFalse(self.settings["database"].exists())

    def test_legacy_tables_remain_untouched_and_combine_with_new_history(self):
        path = self.settings["database"]

        # A minimal instance of the old SQLAlchemy schema, with its naive UTC dates.
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE collection_runs (id INTEGER, started_at TEXT);
                INSERT INTO collection_runs VALUES (1, '2026-09-25 06:00:00');
                CREATE TABLE observations (
                    id INTEGER, run_id INTEGER, source TEXT, country TEXT, region TEXT,
                    kind TEXT, external_id TEXT, title TEXT, rank INTEGER,
                    observed_at TEXT, raw_json TEXT);
                INSERT INTO observations VALUES (
                    1, 1, 'google_trends', 'US', 'north_america', 'search',
                    'old', 'Old topic', 1, '2026-09-25 06:00:00', '{}');
                CREATE TABLE asset_snapshots (id INTEGER, run_id INTEGER);
                INSERT INTO asset_snapshots VALUES (1, 1);
                CREATE TABLE source_health (id INTEGER, run_id INTEGER);
            """)

        before = path.read_bytes()
        window_start = NOW - timedelta(days=1)
        loaded = load_snapshots(path, window_start, NOW)

        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(loaded[0]["legacy"])
        self.assertNotIn("assets", loaded[0])
        self.assertEqual(
            loaded[0]["observations"][0]["observed_at"], "2026-09-25T06:00:00+00:00"
        )
        latest = snapshot()
        save_snapshot(path, latest)
        loaded = load_snapshots(path, window_start, NOW)

        self.assertEqual(len(loaded), 2)

        with sqlite3.connect(path) as db:
            observations_query = db.execute("SELECT COUNT(*) FROM observations")
            observations_count = observations_query.fetchone()
            assets_query = db.execute("SELECT COUNT(*) FROM asset_snapshots")
            assets_count = assets_query.fetchone()

        self.assertEqual(observations_count[0], 1)
        self.assertEqual(assets_count[0], 1)

    def test_bad_config_fails_before_any_collection(self):
        for invalid in (
            'countries = ["US", "US"]',
            "workers = true",
            "workers = 1.5",
            "timeout = nan",
            "unexpected = 1",
            'sources = ["instagram"]',
            'sources = ["robinhood"]',
            'sources = ["solana"]',
            "solana_min_liquidity_usd = 25000",
            "solana_min_volume_24h_usd = 50000",
        ):
            self.config.write_text(invalid)

            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                load_settings(self.config)

    def test_doctor_and_argument_errors_do_not_touch_database(self):
        with (
            redirect_stdout(io.StringIO()),
            patch("cli.collect") as collect,
        ):
            status = main(["doctor", "--config", str(self.config)])

        self.assertEqual(status, 0)
        collect.assert_not_called()

        self.assertFalse(self.settings["database"].exists())

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["report", "--window", "0"])

        self.assertEqual(caught.exception.code, 2)

    def test_run_persists_partial_failure_and_returns_nonzero(self):
        batch = snapshot()
        batch["health"][0].update(status="failed", message="HTTP 429")

        with (
            patch("cli.collect", return_value=batch),
            redirect_stdout(io.StringIO()),
        ):
            result = main(["run", "--config", str(self.config), "--window", "168"])

        self.assertEqual(result, 1)
        self.assertTrue(self.settings["database"].exists())
        report_files = self.settings["reports_dir"].rglob("report.json")
        report_files = list(report_files)

        self.assertEqual(len(report_files), 1)

    def test_report_json_prints_only_json_without_writing_files(self):
        now = datetime.now(UTC)
        batch = snapshot("Tendencias 日本", when=now)
        save_snapshot(self.settings["database"], batch)
        before = self.settings["database"].read_bytes()
        output, error = io.StringIO(), io.StringIO()

        with redirect_stdout(output), redirect_stderr(error):
            result = main(
                ["report", "--config", str(self.config), "--window", "12", "--json"]
            )

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        # Compare with the saved-file format, including non-ASCII source titles.
        settings = self.settings.copy()
        settings["window"] = 12
        generated_at = datetime.fromisoformat(report["generated_at"])
        expected = build_report(settings, now=generated_at)

        self.assertEqual(report, expected)
        self.assertIn("Tendencias 日本", output.getvalue())
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(self.settings["database"].read_bytes(), before)
        self.assertFalse(self.settings["reports_dir"].exists())

    def test_run_json_keeps_collection_messages_on_stderr(self):
        for status, exit_code in (("ok", 0), ("failed", 1)):
            with self.subTest(status=status):
                now = datetime.now(UTC)
                batch = snapshot(when=now)
                batch["health"][0].update(status=status, message="HTTP 429")
                output, error = io.StringIO(), io.StringIO()

                with (
                    patch("cli.collect", return_value=batch),
                    redirect_stdout(output),
                    redirect_stderr(error),
                ):
                    result = main(["run", "--config", str(self.config), "--json"])

                self.assertEqual(result, exit_code)
                report = json.loads(output.getvalue())

                self.assertEqual(report["collected_at"], batch["collected_at"])
                self.assertEqual(report["source_health"][0]["status"], status)
                self.assertIn("Snapshot", error.getvalue())
                if status == "failed":
                    self.assertIn("HTTP 429", error.getvalue())
                when = datetime.fromisoformat(batch["collected_at"])

                self.assertEqual(
                    load_snapshots(self.settings["database"], when, when), [batch]
                )
                self.assertFalse(self.settings["reports_dir"].exists())

    def test_empty_report_json_preserves_warning_and_exit_code(self):
        output, error = io.StringIO(), io.StringIO()

        with redirect_stdout(output), redirect_stderr(error):
            result = main(["report", "--config", str(self.config), "--json"])

        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())

        self.assertIsNone(report["collected_at"])
        self.assertTrue(report["stale"])
        self.assertIn("No stored snapshot", error.getvalue())
        self.assertFalse(self.settings["database"].exists())
        self.assertFalse(self.settings["reports_dir"].exists())

    def test_report_without_json_still_prints_paths_and_writes_both_files(self):
        now = datetime.now(UTC)
        batch = snapshot(when=now)
        save_snapshot(self.settings["database"], batch)

        output = io.StringIO()

        with redirect_stdout(output):
            result = main(["report", "--config", str(self.config)])

        self.assertEqual(result, 0)
        text = output.getvalue()
        lines = text.splitlines()
        names = []

        for line in lines:
            path = Path(line)
            names.append(path.name)

            self.assertTrue(path.is_file())

        self.assertEqual(names, ["report.md", "report.json"])

    def test_cli_runs_as_a_script_and_installed_module(self):
        # Exercise both entry points after removing the nested package.
        for entry in (["src/cli.py"], ["-m", "cli"]):
            with self.subTest(entry=entry):
                result = subprocess.run(
                    [sys.executable] + entry + ["doctor", "--config", str(self.config)],
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("SQLite storage uses Peewee", result.stdout)

    def test_existing_snapshot_schema_accepts_peewee_reads_and_writes(self):
        path = self.settings["database"]
        original = snapshot("Before Peewee")

        # SQL fixtures deliberately describe the pre-Peewee on-disk format.
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE snapshots (
                    id INTEGER PRIMARY KEY, collected_at TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX snapshots_time ON snapshots(collected_at);
            """)
            payload = json.dumps(original)
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?)",
                (42, original["collected_at"], payload),
            )

        before = path.read_bytes()

        self.assertEqual(load_snapshots(path, NOW, NOW), [original])
        self.assertEqual(path.read_bytes(), before)
        latest = snapshot("After Peewee")
        run_id = save_snapshot(path, latest)

        self.assertEqual(run_id, 43)
        loaded = load_snapshots(path, NOW, NOW)

        self.assertEqual(loaded, [original, latest])

        with sqlite3.connect(path) as db:
            query = db.execute("PRAGMA index_list(snapshots)")
            indexes = query.fetchall()

        names = []

        for row in indexes:
            names.append(row[1])

        self.assertEqual(names, ["snapshots_time"])

    def test_failed_insert_rolls_back_and_does_not_leave_a_lock(self):
        path = self.settings["database"]
        original = snapshot()
        save_snapshot(path, original)
        invalid = snapshot()
        invalid["collected_at"] = None

        with self.assertRaises(IntegrityError):
            save_snapshot(path, invalid)

        latest = snapshot("Next")
        run_id = save_snapshot(path, latest)
        loaded = load_snapshots(path, NOW, NOW)

        self.assertEqual(run_id, 2)
        self.assertEqual(len(loaded), 2)

    def test_connections_are_isolated_between_database_paths(self):
        def write_and_read(index):
            path = self.root / ("isolated-%s.db" % index)
            rows = []

            for number in range(3):
                title = "%s-%s" % (index, number)
                row = snapshot(title)
                rows.append(row)
                save_snapshot(path, row)

            loaded = load_snapshots(path, NOW, NOW)
            return loaded, rows

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = pool.map(write_and_read, range(2))

            for actual, expected in results:
                self.assertEqual(actual, expected)

    def test_cli_reports_peewee_errors_without_a_traceback(self):
        self.settings["database"].write_bytes(b"not a SQLite database")

        for flags in ([], ["--json"]):
            with self.subTest(flags=flags):
                output, error = io.StringIO(), io.StringIO()

                with redirect_stdout(output), redirect_stderr(error):
                    result = main(["report", "--config", str(self.config)] + flags)

                self.assertEqual(result, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("Error:", error.getvalue())
                self.assertNotIn("Traceback", error.getvalue())
