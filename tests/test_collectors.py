"""External response fixtures and failure behavior; no live network in tests."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from collect import collect
from config import DEFAULTS
from fetch import SourceError, fetch_json, fetch_text, number
from social import parse_google_rss, parse_tiktok, parse_youtube

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    path = FIXTURES / name
    text = path.read_text()
    if name.endswith(".json"):
        return json.loads(text)

    return text


class CollectorTests(unittest.TestCase):
    def test_google_rss(self):
        text = fixture("google_trends.xml")
        rows = parse_google_rss(text, "US")

        self.assertEqual(rows[0]["title"], "Solar eclipse")
        self.assertEqual(rows[0]["metric_value"], 200_000)
        self.assertIn("q=Solar+eclipse", rows[0]["url"])

        # Invalid XML and a valid non-RSS page both count as source failures.
        for text in ("<rss>", "<html>blocked</html>"):
            with self.assertRaises(SourceError):
                parse_google_rss(text, "US")

    def test_youtube(self):
        payload = fixture("youtube.json")
        rows = parse_youtube(payload)

        self.assertEqual(rows[0]["external_id"], "abc123")
        self.assertEqual(rows[0]["metric_value"], 1_000_000)

        with self.assertRaises(SourceError):
            parse_youtube({"error": {"message": "quota"}})

    def test_tiktok_public_samples(self):
        hashtag_payload = fixture("tiktok_hashtags.json")
        video_payload = fixture("tiktok_videos.json")
        hashtags = parse_tiktok(hashtag_payload, "hashtag")
        videos = parse_tiktok(video_payload, "video")

        self.assertEqual(hashtags[0]["title"], "#solareclipse")
        self.assertEqual(hashtags[0]["raw"]["publishCnt"], 5000)
        self.assertEqual(videos[0]["url"], "https://www.tiktok.com/@science/video/99")
        self.assertEqual(videos[0]["source_window_days"], 7)

        with self.assertRaises(SourceError):
            parse_tiktok({"BaseResp": {"StatusCode": 403}, "items": []}, "hashtag")

    def test_bad_numbers_remain_unknown(self):
        for value in (None, "invalid", "NaN", float("inf")):
            self.assertIsNone(number(value))

        self.assertEqual(number("-2.5"), -2.5)

    def test_failures_and_missing_keys_do_not_discard_success(self):
        settings = DEFAULTS.copy()
        settings.update(
            {
                "countries": ["US"],
                "sources": ["google_trends", "youtube", "tiktok"],
            }
        )
        text = fixture("google_trends.xml")
        google = parse_google_rss(text, "US")

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("social.google_trends", return_value=google),
            patch(
                "social.tiktok_hashtags",
                side_effect=SourceError("HTTP 403"),
            ),
            patch("social.tiktok_videos", return_value=[]),
            patch("social.youtube") as youtube,
        ):
            snapshot = collect(settings)

        self.assertEqual(len(snapshot["observations"]), 2)
        statuses = []

        for row in snapshot["health"]:
            statuses.append(row["status"])

        self.assertIn("failed", statuses)
        self.assertIn("missing_credentials", statuses)
        youtube.assert_not_called()

    def test_india_skips_both_tiktok_calls(self):
        settings = DEFAULTS.copy()
        settings.update(countries=["IN"], sources=["tiktok"])

        with (
            patch("social.tiktok_hashtags") as hashtags,
            patch("social.tiktok_videos") as videos,
        ):
            collect(settings)

        hashtags.assert_not_called()
        videos.assert_not_called()


class HttpTests(unittest.TestCase):
    def test_retry_after_and_success(self):
        from io import BytesIO

        error = HTTPError(
            "https://test.invalid", 429, "limited", {"Retry-After": "5"}, None
        )

        with (
            patch("fetch.urlopen", side_effect=[error, BytesIO(b"{}")]) as request,
            patch("fetch.time.sleep") as sleep,
        ):
            response = fetch_json("https://test.invalid", timeout=3)

        self.assertEqual(response, {})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["timeout"], 3)
        sleep.assert_called_once_with(5)

    def test_forbidden_never_retries_or_exposes_key(self):
        error = HTTPError("https://test.invalid?key=SECRET", 403, "SECRET", {}, None)

        with (
            patch("fetch.urlopen", side_effect=error) as request,
            patch("fetch.time.sleep") as sleep,
            self.assertRaises(SourceError) as caught,
        ):
            fetch_text("https://test.invalid", params={"key": "SECRET"})

        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_network_retries_are_bounded(self):
        with (
            patch("fetch.urlopen", side_effect=URLError("secret URL")) as request,
            patch("fetch.time.sleep"),
            self.assertRaises(SourceError),
        ):
            fetch_text("https://test.invalid")

        self.assertEqual(request.call_count, 3)

    def test_invalid_json_is_a_source_failure(self):
        with (
            patch("fetch.fetch_text", return_value="<html>blocked</html>"),
            self.assertRaises(SourceError),
        ):
            fetch_json("https://test.invalid")

    def test_nonfinite_json_cannot_break_snapshot_serialization(self):
        with patch(
            "fetch.fetch_text",
            return_value='{"price": NaN, "volume": 1e999}',
        ):
            payload = fetch_json("https://test.invalid")

        self.assertEqual(payload, {"price": None, "volume": None})
        json.dumps(payload, allow_nan=False)
