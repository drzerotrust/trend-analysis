"""Exercise the local viewer without opening a port or reading real reports."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dirtyServer import DirtyHandler


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        # Test response generation directly, without creating a socket server.
        self.handler = object.__new__(DirtyHandler)
        self.handler.path = "/"
        self.handler.directory = str(self.root)
        for method in ("send_response", "send_header", "end_headers"):
            mock_method = patch.object(self.handler, method)
            self.enterContext(mock_method)

    def test_listing_skips_directory_names_and_file_patterns(self):
        for name in ("static", "2026-10-01", "static-report"):
            directory = self.root / name
            directory.mkdir()

        hidden_files = ("report.json", ".report.md.swp", ".report.md.swo", ".gitkeep")
        for name in (*hidden_files, "report.md"):
            path = self.root / name
            path.touch()

        with self.handler.list_directory(self.root) as response:
            page = response.read().decode("utf-8")

        self.assertNotIn('href="static/"', page)
        for name in hidden_files:
            self.assertNotIn(name, page)

        self.assertIn('href="2026-10-01/"', page)
        self.assertIn('href="static-report/"', page)
        self.assertIn('href="report.md"', page)
        self.assertNotIn('target="_blank"', page)

    def test_file_patterns_can_be_customized_with_the_correct_attribute_name(self):
        self.handler.file_patterns_to_skip = [".txt"]
        for name in ("notes.txt", "report.json"):
            path = self.root / name
            path.touch()

        with self.handler.list_directory(self.root) as response:
            page = response.read().decode("utf-8")

        self.assertNotIn("notes.txt", page)
        self.assertIn('href="report.json"', page)

    def test_report_links_default_to_a_new_tab_without_changing_markdown(self):
        path = self.root / "report.md"
        text = "# Report\n\n[Story](https://example.org/story) and [Local](other.md)."
        path.write_text(text, encoding="utf-8")
        self.handler.path = "/report.md"

        with self.handler.send_head() as response:
            body = response.read()

        page = body.decode("utf-8")
        self.assertIn('<base target="_blank">', page)
        self.assertIn('href="https://example.org/story"', page)
        self.assertIn('href="other.md"', page)
        self.assertIn('href="/static/reports.css"', page)
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.handler.send_header.assert_any_call("Content-Length", str(len(body)))

    def test_hidden_stylesheet_is_still_served_directly(self):
        directory = self.root / "static"
        directory.mkdir()
        stylesheet = directory / "reports.css"
        stylesheet.write_text("body { color: teal; }", encoding="utf-8")
        self.handler.path = "/static/reports.css"
        self.handler.headers = {}
        self.handler.command = "GET"

        with self.handler.send_head() as response:
            body = response.read()

        self.assertEqual(body, b"body { color: teal; }")
        self.handler.send_header.assert_any_call("Content-type", "text/css")
