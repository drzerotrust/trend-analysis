"""News/topic ranking should be explainable and conservative."""

import copy
import unittest

from scoring import rank_topics


def observation(title="Solar eclipse", **values):
    row = {
        "title": title,
        "source": "google_trends",
        "country": "US",
        "region": "north_america",
        "kind": "search",
        "external_id": "eclipse",
        "rank": 1,
        "observed_at": "2026-09-25T12:00:00+00:00",
        "url": "https://example.org",
    }
    row.update(values)
    return row


class RankingTests(unittest.TestCase):
    def test_grouping_movement_and_no_double_count_for_same_source_country(self):
        old = [observation(rank=8)]
        current = [
            observation(),
            observation("#solareclipse", external_id="duplicate"),
            observation(
                "#solareclipse", source="tiktok", country="CA", kind="hashtag", rank=2
            ),
        ]
        topics = rank_topics(current, old)
        topic = topics[0]

        self.assertEqual(topic["score"], 1.5)
        self.assertEqual(topic["countries"], ["CA", "US"])
        self.assertEqual(len(topic["evidence"]), 3)
        changes = []

        for row in topic["evidence"]:
            changes.append(row["rank_change"])

        self.assertIn(7, changes)

    def test_old_items_do_not_become_current_and_first_sample_has_no_momentum(self):
        current = [observation()]
        previous = [observation("Expired", external_id="old")]
        topics = rank_topics(current, previous)
        topic = topics[0]

        self.assertEqual(topic["title"], "Solar eclipse")
        self.assertIsNone(topic["evidence"][0]["rank_change"])

    def test_best_feed_position_wins_and_tied_scores_sort_by_title(self):
        # A repeated title contributes only its best rank, even when it arrives last.
        rows = [
            observation("Zulu", rank=2),
            observation("Alpha", rank=9),
            observation("Alpha", rank=2, external_id="best"),
            observation("Bravo", rank=1),
        ]
        topics = rank_topics(rows, [])

        titles = []

        for topic in topics:
            titles.append(topic["title"])

        self.assertEqual(titles, ["Bravo", "Alpha", "Zulu"])
        self.assertEqual(topics[1]["score"], 0.5)
        self.assertEqual(topics[1]["evidence"][0]["external_id"], "best")

    def test_unicode_grouping_stays_separate_between_regions(self):
        rows = [
            observation("ＡＩ News", rank=2),
            observation("AI news", rank=1, source="youtube"),
            observation("AI news", country="JP", region="asia"),
            observation("!!!"),
        ]
        topics = rank_topics(rows, [])

        self.assertEqual(len(topics), 2)
        self.assertEqual(topics[0]["topic_key"], "ainews")
        self.assertEqual(topics[0]["region"], "north_america")
        self.assertEqual(topics[0]["score"], 1.5)
        self.assertEqual(topics[1]["region"], "asia")

    def test_evidence_ties_use_title_order_without_changing_inputs(self):
        rows = [
            observation("solar eclipse", raw={"sample": [1]}),
            observation("Solar eclipse", external_id="second"),
        ]
        previous = [observation(rank=4)]
        original_rows = copy.deepcopy(rows)
        original_previous = copy.deepcopy(previous)

        topics = rank_topics(rows, previous)
        evidence = topics[0]["evidence"]

        self.assertEqual(evidence[0]["title"], "Solar eclipse")
        self.assertIsNone(evidence[0]["compared_at"])
        self.assertEqual(evidence[1]["rank_change"], 3)
        self.assertEqual(evidence[1]["compared_at"], previous[0]["observed_at"])

        for row in evidence:
            self.assertNotIn("raw", row)

        self.assertEqual(rows, original_rows)
        self.assertEqual(previous, original_previous)
