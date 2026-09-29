"""Transparent news/topic ranking. No embeddings or model downloads."""

import unicodedata
from collections import defaultdict

__all__ = ["rank_topics"]


def _normalize(text):
    # Match titles despite casing, punctuation, and Unicode display variants.
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()

    characters = []
    for character in text:
        if character.isalnum():
            characters.append(character)

    return "".join(characters)


def _identity(row):
    return row["source"], row["country"], row["kind"], row["external_id"]


def _evidence_order(row):
    # Lower feed positions come first; titles break ties consistently.
    return row["rank"], row["title"]


def _topic_order(topic):
    # Higher scores come first; titles break ties consistently.
    return -topic["score"], topic["title"]


def _build_topic(region, key, rows, previous_by_id):
    ordered_rows = sorted(rows, key=_evidence_order)
    evidence = []
    contributions = {}
    countries = set()
    sources = set()

    for row in ordered_rows:
        # Copy public evidence so scoring cannot change the stored observation.
        item = row.copy()
        item.pop("raw", None)
        item["rank_change"] = None
        item["compared_at"] = None

        identity = _identity(row)
        before = previous_by_id.get(identity)
        if before:
            item["rank_change"] = before["rank"] - row["rank"]
            item["compared_at"] = before["observed_at"]

        evidence.append(item)

        # Only the best position in each source/country feed contributes points.
        feed = (row["source"], row["country"])
        contribution = 1 / row["rank"]
        best_contribution = contributions.get(feed, 0)
        contributions[feed] = max(best_contribution, contribution)

        sources.add(row["source"])
        if row["country"]:
            countries.add(row["country"])

    total_score = sum(contributions.values())
    score = round(total_score, 4)
    ordered_countries = sorted(countries)
    ordered_sources = sorted(sources)

    return {
        "topic_key": key,
        "title": evidence[0]["title"],
        "region": region,
        "score": score,
        "countries": ordered_countries,
        "sources": ordered_sources,
        "evidence": evidence,
    }


def rank_topics(current, previous):
    # Index the previous snapshot for exact source/country/kind/item comparisons.
    previous_by_id = {}
    for row in previous:
        identity = _identity(row)
        previous_by_id[identity] = row

    # Similar titles can share a topic, but never across different regions.
    groups = defaultdict(list)
    for row in current:
        key = _normalize(row["title"])
        if not key:
            continue

        group = (row["region"], key)
        groups[group].append(row)

    topics = []
    for (region, key), rows in groups.items():
        topic = _build_topic(region, key, rows, previous_by_id)
        topics.append(topic)

    topics.sort(key=_topic_order)
    return topics
