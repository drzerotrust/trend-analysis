"""Agent contracts must describe the CLI's actual output, including partial data."""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from test_collectors import fixture

from cli import main, read_args
from collect import collect
from config import SOURCES, load_settings
from reporting import build_report
from social import parse_google_rss, parse_hacker_news, parse_tiktok, parse_youtube
from storage import save_snapshot

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def load_contract(name):
    path = CONTRACTS / (name + ".schema.json")
    text = path.read_text()
    schema = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# Parse fixtures for every mocked request so countries never share mutable rows.
def google_response(country, timeout):
    text = fixture("google_trends.xml")
    return parse_google_rss(text, country)


def youtube_response(*args):
    payload = fixture("youtube.json")
    return parse_youtube(payload)


def hashtag_response(*args):
    payload = fixture("tiktok_hashtags.json")
    return parse_tiktok(payload, "hashtag")


def video_response(*args):
    payload = fixture("tiktok_videos.json")
    return parse_tiktok(payload, "video")


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.report_contract = load_contract("report")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.toml"
        self.config.write_text('countries = ["US", "JP"]\n')
        self.settings = load_settings(self.config)

        # Real operator credentials must never be loaded during contract tests.
        env_file = self.config.parent / "test.env"
        env_file.write_text("", encoding="utf-8")
        variables = {"TREND_ENGINE_ENV_FILE": str(env_file)}
        environment = patch.dict(os.environ, variables, clear=True)
        self.enterContext(environment)

    def sample_collection(self):
        # Exercise every current source without network calls or real credentials.
        payload = fixture("hacker_news.json")
        hn = payload["items"]["101"]
        stories = parse_hacker_news(hn, 101, 1)

        with (
            patch(
                "fetch.urlopen",
                side_effect=AssertionError("Unexpected network request"),
            ),
            patch.dict("os.environ", {"YOUTUBE_API_KEY": "test-only"}, clear=True),
            patch("social.hacker_news_ids", return_value=[101]),
            patch("social.hacker_news_story", return_value=stories),
            patch("social.google_trends", side_effect=google_response),
            patch("social.youtube", side_effect=youtube_response),
            patch("social.tiktok_hashtags", side_effect=hashtag_response),
            patch("social.tiktok_videos", side_effect=video_response),
        ):
            return collect(self.settings)

    def test_cli_options_preserve_config_defaults(self):
        for command in ("report", "run"):
            args = read_args([command, "--json", "--config", str(self.config)])

            self.assertEqual(args.command, command)
            self.assertTrue(args.json)
            self.assertEqual(args.config, self.config)
            self.assertIsNone(args.window)
            self.assertIsNone(getattr(args, "sources", None))

        args = read_args(["run", "--json", "--window", "24", "--sources", *SOURCES])

        self.assertEqual(args.sources, list(SOURCES))
        self.assertEqual(args.window, 24)

    def test_argparse_rejects_unsupported_options(self):
        for argv in (
            [],
            ["report", "--sources", "hacker_news"],
            ["run", "--sources", "instagram"],
            ["run", "--sources", "robinhood"],
            ["run", "--sources", "solana"],
            ["run", "--sources"],
            ["report", "--window", "0"],
            ["report", "--window", "169"],
            ["report", "--window", "true"],
            ["run", "--api-key", "must-not-be-an-argument"],
            ["run", "--countries", "US"],
            ["run", "--limit", "10"],
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                read_args(argv)

            self.assertEqual(caught.exception.code, 2)

    def test_full_report_and_source_fields_validate(self):
        batch = self.sample_collection()

        self.assertEqual(
            set(SOURCES), {"hacker_news", "google_trends", "tiktok", "youtube"}
        )
        self.assertNotIn("assets", batch)
        allowed_sources = (*SOURCES, "instagram")

        for row in batch["health"]:
            self.assertIn(row["source"], allowed_sources)

        save_snapshot(self.settings["database"], batch)
        report = build_report(self.settings)
        self.report_contract.validate(report)

        self.assertTrue(report["global_trends"])
        self.assertTrue(report["regional_trends"]["north_america"])
        self.assertTrue(report["regional_trends"]["asia"])
        self.assertEqual(report["schema_version"], 3)

        for field in (
            "hot_robinhood_crypto",
            "hot_solana_tokens",
            "social_asset_matches",
        ):
            self.assertNotIn(field, report)

        self.assertEqual(
            set(report["platform_trends"]),
            {"hacker_news", "google_trends", "tiktok", "youtube"},
        )
        json.dumps(report, allow_nan=False)

    def test_json_exit_codes_keep_partial_and_empty_reports_parseable(self):
        for failed in (False, True):
            batch = self.sample_collection()
            if failed:
                batch["health"][0].update(status="failed", message="HTTP 503")
            output, error = io.StringIO(), io.StringIO()

            with (
                self.subTest(failed=failed),
                patch("cli.collect", return_value=batch),
                redirect_stdout(output),
                redirect_stderr(error),
            ):
                result = main(["run", "--config", str(self.config), "--json"])

            self.assertEqual(result, int(failed))
            text = output.getvalue()
            report = json.loads(text)
            self.report_contract.validate(report)

            self.assertIn("Snapshot", error.getvalue())
            self.assertFalse(self.settings["reports_dir"].exists())

    def test_missing_snapshot_is_valid_json_on_exit_one(self):
        output, error = io.StringIO(), io.StringIO()

        with redirect_stdout(output), redirect_stderr(error):
            result = main(["report", "--config", str(self.config), "--json"])

        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.report_contract.validate(report)

        self.assertIsNone(report["collected_at"])
        self.assertTrue(report["stale"])
        self.assertFalse(self.settings["database"].exists())

    def test_local_error_and_bad_arguments_do_not_emit_a_report(self):
        self.config.write_text("not TOML")
        output, error = io.StringIO(), io.StringIO()

        with redirect_stdout(output), redirect_stderr(error):
            result = main(["report", "--config", str(self.config), "--json"])

        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Error:", error.getvalue())

        with (
            redirect_stdout(output),
            redirect_stderr(error),
            self.assertRaises(SystemExit) as caught,
        ):
            main(["report", "--window", "999", "--json"])

        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(output.getvalue(), "")

    def test_legacy_missing_fields_and_compatible_additions(self):
        batch = self.sample_collection()
        batch["legacy"] = True

        for health in batch["health"]:
            health.pop("kind")
            health["message"] = None

        save_snapshot(self.settings["database"], batch)
        report = build_report(self.settings)
        report["future_compatible_field"] = {"value": 1}
        self.report_contract.validate(report)

        self.assertTrue(report["legacy_snapshot"])

    def test_breaking_output_changes_are_rejected(self):
        batch = self.sample_collection()
        save_snapshot(self.settings["database"], batch)
        report = build_report(self.settings)

        for key, invalid in (
            ("schema_version", 2),
            ("schema_version", 4),
            ("window_hours", 0),
            ("stale", "false"),
            ("source_health", {}),
            ("regional_trends", {"asia": []}),
        ):
            with self.subTest(key=key):
                invalid_report = report.copy()
                invalid_report[key] = invalid

                self.assertFalse(self.report_contract.is_valid(invalid_report))

        invalid = copy.deepcopy(report)
        invalid["global_trends"][0]["evidence"][0]["rank"] = "first"

        self.assertFalse(self.report_contract.is_valid(invalid))
        invalid = copy.deepcopy(report)
        invalid["global_trends"][0]["evidence"][0]["metric_value"] = "unknown"

        self.assertFalse(self.report_contract.is_valid(invalid))
        invalid = copy.deepcopy(report)
        del invalid["global_trends"]

        self.assertFalse(self.report_contract.is_valid(invalid))

    def test_doctor_never_prints_environment_secrets(self):
        output = io.StringIO()

        with (
            patch.dict(
                "os.environ",
                {
                    "YOUTUBE_API_KEY": "test-youtube-secret",
                    "COINGECKO_API_KEY": "test-coingecko-secret",
                },
            ),
            redirect_stdout(output),
        ):
            result = main(["doctor", "--config", str(self.config)])

        self.assertEqual(result, 0)
        self.assertIn("YouTube: key present", output.getvalue())
        self.assertNotIn("CoinGecko", output.getvalue())
        self.assertNotIn("test-youtube-secret", output.getvalue())
        self.assertNotIn("test-coingecko-secret", output.getvalue())

    def test_collection_timestamps_are_iso8601(self):
        batch = self.sample_collection()
        save_snapshot(self.settings["database"], batch)
        report = build_report(self.settings)

        for key in ("generated_at", "window_start", "collected_at"):
            timestamp = datetime.fromisoformat(report[key])

            self.assertEqual(timestamp.tzinfo, UTC)

    def test_skill_command_works_outside_the_analyzer_directory(self):
        root = CONTRACTS.parent
        result = subprocess.run(
            [
                sys.executable,
                str(root / "src" / "cli.py"),
                "report",
                "--config",
                str(self.config),
                "--json",
            ],
            cwd=self.temp.name,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.report_contract.validate(report)

        self.assertIn("No stored snapshot", result.stderr)
        self.assertFalse(self.settings["database"].exists())
