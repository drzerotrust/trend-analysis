"""Offline discovery must work without collection, storage, or exposed secrets."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from cli import main
from compatibility import CONTRACT_VERSIONS, __version__, load_schema, version_parts

ROOT = Path(__file__).resolve().parents[1]


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        working_directory = chdir(self.directory)
        environment = patch.dict(os.environ, {}, clear=True)
        self.enterContext(working_directory)
        self.enterContext(environment)

        # Keep optional credential discovery away from the real checkout and home.
        checkout_root = patch("environment.CHECKOUT_ROOT", self.directory)
        self.enterContext(checkout_root)
        os.environ["XDG_CONFIG_HOME"] = str(self.directory / "user-config")

        # Discovery commands must not collect, read reports, or open storage.
        for target in (
            "cli.collect",
            "cli.save_snapshot",
            "cli.build_report",
            "fetch.urlopen",
        ):
            error = AssertionError("Unexpected I/O")
            guard = patch(target, side_effect=error)
            self.enterContext(guard)

    def invoke(self, *argv):
        output, error = io.StringIO(), io.StringIO()
        args = list(argv)

        with redirect_stdout(output), redirect_stderr(error):
            status = main(args)

        return status, output.getvalue(), error.getvalue()

    def doctor(self, *argv):
        status, output, error = self.invoke("doctor", "--json", *argv)
        document = json.loads(output)
        schema = load_schema("doctor")
        validator = Draft202012Validator(schema)
        validator.validate(document)

        self.assertEqual(error, "")
        self.assertFalse(document["network_checked"])
        return status, document

    def test_version_needs_no_config(self):
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main(["--version"])

        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue(), "trend-engine %s\n" % __version__)

    def test_schemas_are_offline_and_match_published_files(self):
        self.assertEqual(set(CONTRACT_VERSIONS), {"report", "doctor"})

        for name in CONTRACT_VERSIONS:
            with self.subTest(name=name):
                status, output, error = self.invoke("schema", name)

                self.assertEqual(status, 0)
                self.assertEqual(error, "")
                schema = json.loads(output)
                Draft202012Validator.check_schema(schema)

                self.assertEqual(
                    schema["$id"],
                    "urn:trend-engine:%s:%s" % (name, CONTRACT_VERSIONS[name]),
                )
                expected = ROOT / "contracts" / ("%s.schema.json" % name)
                text = expected.read_text()
                expected_schema = json.loads(text)

                self.assertEqual(schema, expected_schema)

        status, output, error = self.invoke("schema")
        default_schema = json.loads(output)
        expected_schema = load_schema("report")

        self.assertEqual(default_schema, expected_schema)
        files = list(self.directory.iterdir())

        self.assertEqual(files, [])

    def test_doctor_can_check_an_install_without_config(self):
        status, document = self.doctor()

        self.assertEqual(status, 0)
        self.assertEqual(document["version"], __version__)
        self.assertEqual(document["contracts"], CONTRACT_VERSIONS)
        self.assertEqual(document["contracts"], {"report": 3, "doctor": 2})
        self.assertEqual(document["doctor_version"], 2)
        self.assertEqual(document["errors"], [])
        self.assertEqual(document["status"], "success")
        self.assertEqual(document["configuration"]["status"], "not_checked")
        self.assertFalse(document["configuration"]["youtube_key_present"])
        self.assertNotIn("coingecko_key_present", document["configuration"])
        files = list(self.directory.iterdir())

        self.assertEqual(files, [])

    def test_version_bounds_are_inclusive_minimum_and_exclusive_maximum(self):
        status, document = self.doctor("--min-version", __version__)

        self.assertEqual(status, 0)

        status, document = self.doctor("--max-version", __version__)

        self.assertEqual(status, 1)
        self.assertEqual(document["status"], "error")
        self.assertEqual(document["errors"], ["version_too_new"])
        status, document = self.doctor("--min-version", "999.0.0")

        self.assertEqual(status, 1)
        self.assertEqual(document["errors"], ["version_too_old"])
        self.assertLess(version_parts("0.9.0"), version_parts("0.10.0"))

    def test_contract_checks_collect_all_mismatches(self):
        flags = []

        for name, version in CONTRACT_VERSIONS.items():
            flags += ["--require-%s-schema" % name, str(version)]

        status, document = self.doctor(*flags)

        self.assertEqual(status, 0)

        flags = []

        for name, version in CONTRACT_VERSIONS.items():
            flags += ["--require-%s-schema" % name, str(version + 1)]

        status, document = self.doctor(*flags)

        self.assertEqual(status, 1)
        expected_errors = []

        for name in CONTRACT_VERSIONS:
            expected_errors.append("%s_schema_mismatch" % name)

        self.assertEqual(document["errors"], expected_errors)

    def test_pre_news_only_contracts_fail_compatibility_checks(self):
        status, document = self.doctor(
            "--require-report-schema", "2", "--require-doctor-schema", "1"
        )

        self.assertEqual(status, 1)
        self.assertEqual(
            document["errors"], ["report_schema_mismatch", "doctor_schema_mismatch"]
        )

    def test_doctor_checks_config_without_touching_storage(self):
        config = self.directory / "config.toml"
        config.write_text('sources = ["hacker_news"]\n')

        for flags in ([], ["--config", str(config)]):
            status, document = self.doctor(*flags)

            self.assertEqual(status, 0)
            self.assertEqual(document["configuration"]["status"], "ok")
            self.assertEqual(
                document["configuration"]["enabled_sources"], ["hacker_news"]
            )

        files = list(self.directory.iterdir())

        self.assertEqual(files, [config])

    def test_bad_or_missing_explicit_config_returns_redacted_json(self):
        secret = "test-private-setting-never-print"
        config = self.directory / (secret + ".toml")

        for content in (None, secret + " = 1", "not valid toml", 'database = ""'):
            if content is not None:
                config.write_text(content)
            status, document = self.doctor("--config", str(config))

            self.assertEqual(status, 2)
            self.assertEqual(document["configuration"]["status"], "error")
            self.assertEqual(document["errors"], ["configuration_invalid"])
            text = json.dumps(document)
            directory = str(self.directory)

            self.assertNotIn(secret, text)
            self.assertNotIn(directory, text)

    def test_doctor_ignores_cwd_dotenv_and_reports_exported_key(self):
        dotenv_path = self.directory / ".env"
        dotenv_path.write_text("YOUTUBE_API_KEY=test-dotenv-not-loaded\n")
        status, document = self.doctor()

        self.assertFalse(document["configuration"]["youtube_key_present"])

        with patch.dict(
            os.environ,
            {
                "YOUTUBE_API_KEY": "test-private-youtube",
                "COINGECKO_API_KEY": "test-private-coingecko",
            },
        ):
            status, document = self.doctor()

        self.assertEqual(status, 0)
        self.assertTrue(document["configuration"]["youtube_key_present"])
        self.assertNotIn("coingecko_key_present", document["configuration"])
        self.assertNotIn("test-private", json.dumps(document))

    def test_invalid_discovery_arguments_exit_two_without_json(self):
        for argv in (
            ["doctor", "--min-version", "0.3"],
            ["doctor", "--max-version", "0.4.0rc1"],
            ["doctor", "--min-version", "0.4.0", "--max-version", "0.3.0"],
            ["doctor", "--min-version", "0.3.0", "--max-version", "0.3.0"],
            ["doctor", "--require-report-schema", "two"],
            ["doctor", "--json", "--require-invocation-schema", "1"],
            ["schema", "invocation"],
            ["schema", "../../.env"],
            ["schema", "--config", "config.toml"],
        ):
            with self.subTest(argv=argv):
                output = io.StringIO()

                with (
                    redirect_stdout(output),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as caught,
                ):
                    main(argv)

                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(output.getvalue(), "")

        for name in ("invocation", "../../.env"):
            with self.assertRaises(ValueError):
                load_schema(name)

    def test_script_discovery_outside_checkout_ignores_broken_config(self):
        (self.directory / "config.toml").write_text("broken config")
        result = subprocess.run(
            [sys.executable, str(ROOT / "src" / "cli.py"), "schema"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        schema = json.loads(result.stdout)
        expected = load_schema("report")

        self.assertEqual(schema, expected)
