"""Credential loading uses temporary files, never the operator's real keys."""

import io
import json
import os
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cli import main
from environment import load_environment


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        metadata = self.checkout / "pyproject.toml"
        metadata.write_text('[project]\nname = "trend-engine"\n', encoding="utf-8")
        self.env_file = self.checkout / ".env"
        self.config = self.checkout / "config.toml"
        self.config.write_text('sources = ["youtube"]\n', encoding="utf-8")

        # Prevent both checkout and user-config discovery from touching real files.
        variables = {"XDG_CONFIG_HOME": str(self.root / "user-config")}
        process_environment = patch.dict(os.environ, variables, clear=True)
        checkout_root = patch("environment.CHECKOUT_ROOT", self.checkout)
        self.enterContext(process_environment)
        self.enterContext(checkout_root)

    def invoke(self, *args):
        output = io.StringIO()
        error = io.StringIO()
        arguments = list(args)

        with redirect_stdout(output), redirect_stderr(error):
            status = main(arguments)

        return status, output.getvalue(), error.getvalue()

    def test_checkout_loads_quotes_comments_and_export_without_changing_file(self):
        contents = '# Optional key\nexport YOUTUBE_API_KEY="test#key" # comment\n'
        self.env_file.write_text(contents, encoding="utf-8")

        load_environment()

        self.assertEqual(os.environ["YOUTUBE_API_KEY"], "test#key")
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), contents)

    def test_existing_environment_wins_even_when_explicitly_empty(self):
        self.env_file.write_text("YOUTUBE_API_KEY=test-file\n", encoding="utf-8")

        for value in ("test-exported", ""):
            with self.subTest(value=value):
                os.environ["YOUTUBE_API_KEY"] = value
                load_environment()

                self.assertEqual(os.environ["YOUTUBE_API_KEY"], value)

    def test_explicit_file_wins_over_checkout_and_other_working_directory(self):
        self.env_file.write_text("YOUTUBE_API_KEY=test-checkout\n", encoding="utf-8")
        selected = self.root / "private keys.env"
        selected.write_text("YOUTUBE_API_KEY=test-selected\n", encoding="utf-8")
        os.environ["TREND_ENGINE_ENV_FILE"] = str(selected)

        with chdir(self.root):
            load_environment()

        self.assertEqual(os.environ["YOUTUBE_API_KEY"], "test-selected")

    def test_user_config_is_the_fallback(self):
        directory = self.root / "user-config" / "trend-engine"
        directory.mkdir(parents=True)
        selected = directory / ".env"
        selected.write_text("YOUTUBE_API_KEY=test-user\n", encoding="utf-8")

        load_environment()

        self.assertEqual(os.environ["YOUTUBE_API_KEY"], "test-user")

    def test_home_config_is_used_without_xdg(self):
        os.environ.pop("XDG_CONFIG_HOME")
        home_directory = self.root / "test-user"
        directory = home_directory / ".config" / "trend-engine"
        directory.mkdir(parents=True)
        selected = directory / ".env"
        selected.write_text("YOUTUBE_API_KEY=test-home\n", encoding="utf-8")

        with patch("environment.Path.home", return_value=home_directory):
            load_environment()

        self.assertEqual(os.environ["YOUTUBE_API_KEY"], "test-home")

    def test_arbitrary_working_directory_is_not_searched(self):
        untrusted = self.root / ".env"
        untrusted.write_text("YOUTUBE_API_KEY=test-untrusted\n", encoding="utf-8")

        with chdir(self.root):
            load_environment()

        self.assertNotIn("YOUTUBE_API_KEY", os.environ)

    def test_wheel_does_not_load_an_installation_directory_env(self):
        self.env_file.write_text("YOUTUBE_API_KEY=test-installed\n", encoding="utf-8")

        with patch("environment.CHECKOUT_ROOT", None), chdir(self.checkout):
            load_environment()

        self.assertNotIn("YOUTUBE_API_KEY", os.environ)

    def test_missing_explicit_file_fails_without_falling_back(self):
        self.env_file.write_text("YOUTUBE_API_KEY=test-checkout\n", encoding="utf-8")
        selected = self.root / "test-private-missing.env"
        os.environ["TREND_ENGINE_ENV_FILE"] = str(selected)

        with self.assertRaises(ValueError) as caught:
            load_environment()

        self.assertNotIn("test-private", str(caught.exception))
        self.assertNotIn("YOUTUBE_API_KEY", os.environ)

    def test_invalid_files_are_redacted_and_never_partially_applied(self):
        invalid_contents = [
            b'YOUTUBE_API_KEY=test-private\nBROKEN="test-private\n',
            b"YOUTUBE_API_KEY=test-private\nBAD=has\x00null\n",
            b"YOUTUBE_API_KEY=test-private\nBAD=\xff\n",
            b"YOUTUBE_API_KEY=test-private\n'BAD=NAME'=test-private\n",
        ]

        for contents in invalid_contents:
            with self.subTest(contents=contents):
                self.env_file.write_bytes(contents)
                output = io.StringIO()
                error = io.StringIO()

                with (
                    redirect_stdout(output),
                    redirect_stderr(error),
                    self.assertRaises(ValueError) as caught,
                ):
                    load_environment()

                self.assertNotIn("test-private", str(caught.exception))
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(error.getvalue(), "")
                self.assertNotIn("YOUTUBE_API_KEY", os.environ)

    def test_unreadable_file_error_does_not_expose_its_path(self):
        self.env_file.write_text("YOUTUBE_API_KEY=test-private\n", encoding="utf-8")
        failure = PermissionError("test-private-path")

        with (
            patch("environment.Path.read_text", side_effect=failure),
            self.assertRaises(ValueError) as caught,
        ):
            load_environment()

        self.assertNotIn("test-private", str(caught.exception))

    def test_dotenv_values_are_data_not_shell_commands(self):
        contents = "YOUTUBE_API_KEY=$(echo test-not-executed)\n"
        self.env_file.write_text(contents, encoding="utf-8")

        load_environment()

        self.assertEqual(os.environ["YOUTUBE_API_KEY"], "$(echo test-not-executed)")

    def test_doctor_reports_loaded_key_presence_without_exposing_value(self):
        self.env_file.write_text("YOUTUBE_API_KEY=test-private\n", encoding="utf-8")

        status, output, error = self.invoke(
            "doctor", "--json", "--config", str(self.config)
        )
        document = json.loads(output)

        self.assertEqual(status, 0)
        self.assertTrue(document["configuration"]["youtube_key_present"])
        self.assertFalse(document["network_checked"])
        self.assertNotIn("test-private", output + error)

    def test_bad_env_keeps_doctor_json_parseable_on_exit_two(self):
        self.env_file.write_text('YOUTUBE_API_KEY="test-private\n', encoding="utf-8")

        status, output, error = self.invoke(
            "doctor", "--json", "--config", str(self.config)
        )
        document = json.loads(output)

        self.assertEqual(status, 2)
        self.assertEqual(document["configuration"]["status"], "error")
        self.assertIn("environment_invalid", document["errors"])
        self.assertNotIn("test-private", output + error)
        self.assertEqual(error, "")

    def test_collection_loads_credentials_before_calling_sources(self):
        self.env_file.write_text("YOUTUBE_API_KEY=test-private\n", encoding="utf-8")
        seen_keys = []

        def collect_sample(settings):
            key = os.getenv("YOUTUBE_API_KEY")
            seen_keys.append(key)
            return {"observations": [{}], "health": []}

        # No network or storage is needed to verify CLI startup order.
        for command in ("collect", "run"):
            with (
                self.subTest(command=command),
                patch("cli.collect", side_effect=collect_sample),
                patch("cli.save_snapshot", return_value=1),
                patch("cli.report_once", return_value=0),
            ):
                os.environ.pop("YOUTUBE_API_KEY", None)
                status, output, error = self.invoke(
                    command, "--config", str(self.config)
                )

                self.assertEqual(status, 0)
                self.assertNotIn("test-private", output + error)

        self.assertEqual(seen_keys, ["test-private", "test-private"])

    def test_bad_env_stops_collection_before_requests_or_storage(self):
        self.env_file.write_text('YOUTUBE_API_KEY="test-private\n', encoding="utf-8")

        with patch("cli.collect") as collect, patch("cli.save_snapshot") as save:
            status, output, error = self.invoke(
                "run", "--config", str(self.config), "--json"
            )

        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        self.assertNotIn("test-private", error)
        collect.assert_not_called()
        save.assert_not_called()

    def test_stored_reports_and_discovery_do_not_load_credentials(self):
        self.env_file.write_text('YOUTUBE_API_KEY="test-private\n', encoding="utf-8")

        status, output, error = self.invoke(
            "report", "--config", str(self.config), "--json"
        )
        report = json.loads(output)

        self.assertEqual(status, 1)
        self.assertIsNone(report["collected_at"])
        self.assertIn("No stored snapshot", error)

        status, output, error = self.invoke("schema")
        schema = json.loads(output)

        self.assertEqual(status, 0)
        self.assertEqual(schema["$id"], "urn:trend-engine:report:3")

        for option in ("--version", "--help"):
            with self.subTest(option=option), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main([option])

                self.assertEqual(caught.exception.code, 0)
