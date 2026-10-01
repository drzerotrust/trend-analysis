"""Command-line script: parse options, collect data, and write reports."""

import argparse
import json
import sys
from pathlib import Path

from peewee import PeeweeException

from collect import collect
from compatibility import (
    CONTRACT_VERSIONS,
    __version__,
    check_installation,
    load_schema,
    version_parts,
)
from config import SOURCES, load_settings
from environment import load_environment
from reporting import build_report, write_report
from storage import save_snapshot


def _window(value):
    # Reject bad windows before any requests or database writes.
    try:
        hours = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("window must be an integer") from exc

    if not 1 <= hours <= 168:
        raise argparse.ArgumentTypeError("window must be between 1 and 168 hours")
    return hours


def read_args(argv):
    parser = argparse.ArgumentParser(
        description="Collect news and public attention signals."
    )
    parser.add_argument(
        "--version", action="version", version="trend-engine %s" % __version__
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands_options = (
        ("collect", "Save a new snapshot"),
        ("report", "Report the latest stored snapshot"),
        ("run", "Collect, then report"),
        ("doctor", "Check settings and key availability offline"),
    )

    for name, help_text in commands_options:
        command = commands.add_parser(name, help=help_text)
        config_default = Path("config.toml")
        if name == "doctor":
            config_default = None

        command.add_argument(
            "--config",
            type=Path,
            default=config_default,
        )

        # Only report-producing commands support machine output and history windows.
        if name in {"report", "run"}:
            command.add_argument("--window", type=_window, help="History hours (1–168)")
            command.add_argument(
                "--json",
                action="store_true",
                help="Print report JSON to stdout instead of writing report files",
            )
        if name in {"collect", "run"}:
            command.add_argument("--sources", nargs="+", choices=SOURCES)

        if name == "doctor":
            command.add_argument(
                "--json", action="store_true", help="Print offline checks as JSON"
            )
            command.add_argument(
                "--min-version", type=version_parts, help="Inclusive MAJOR.MINOR.PATCH"
            )
            command.add_argument(
                "--max-version", type=version_parts, help="Exclusive MAJOR.MINOR.PATCH"
            )
            for contract in CONTRACT_VERSIONS:
                command.add_argument("--require-%s-schema" % contract, type=int)

    schema = commands.add_parser("schema", help="Print a bundled JSON Schema offline")
    schema.add_argument("name", nargs="?", default="report", choices=CONTRACT_VERSIONS)
    args = parser.parse_args(argv)

    if (
        args.command == "doctor"
        and args.min_version is not None
        and args.max_version is not None
        and args.min_version >= args.max_version
    ):
        parser.error("--min-version must be less than --max-version (exclusive)")
    return args


def doctor_once(args):
    document, status = check_installation(args)
    if args.json:
        text = json.dumps(document, indent=2)
        print(text)
        return status

    # The human-readable check uses the same results, never the API key values.
    config = document["configuration"]
    print("trend-engine %s" % document["version"])
    if config["status"] == "ok":
        print("Configuration OK; SQLite storage uses Peewee.")
        sources = ", ".join(config["enabled_sources"])
        print("Enabled sources: %s" % sources)
    elif config["status"] == "not_checked":
        print("Configuration not checked; pass --config /absolute/path/config.toml.")
    else:
        print(
            "Configuration invalid or unreadable; check the TOML and environment files."
        )

    youtube = "skipped until YOUTUBE_API_KEY is set (free)"
    if config["youtube_key_present"]:
        youtube = "key present"
    print("YouTube: %s" % youtube)
    print("TikTok: limited public samples. Instagram: not integrated.")
    print("No network checks performed. See README.md for source limits.")

    for error in document["errors"]:
        print("Check failed: %s" % error, file=sys.stderr)
    return status


def collect_once(settings, output=None):
    # Store successful sources even when another source fails.
    snapshot = collect(settings)
    run_id = save_snapshot(settings["database"], snapshot)

    failures = 0
    for row in snapshot["health"]:
        if row["status"] == "failed":
            failures += 1

    observation_count = len(snapshot["observations"])
    summary = "Snapshot %s: %s observations, %s failed operations" % (
        run_id,
        observation_count,
        failures,
    )
    print(summary, file=output)

    # Show actionable source limits without hiding the successful observations.
    for row in snapshot["health"]:
        if row["status"] in {"failed", "partial", "missing_credentials"}:
            country = row["country"] or "all"
            detail = "  %s/%s/%s: %s — %s" % (
                row["source"],
                country,
                row["kind"],
                row["status"],
                row["message"],
            )
            print(detail, file=output)

    if failures > 0 or not snapshot["observations"]:
        return 1

    return 0


def report_once(settings, json_output=False):
    # Reports read stored snapshots; this step does not contact any sources.
    report = build_report(settings)
    if json_output:
        text = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
        print(text)
    else:
        paths = write_report(report, settings["reports_dir"])
        for path in paths:
            print(path)

    # An empty report still has a complete JSON document, but exits unsuccessfully.
    if report["collected_at"] is None:
        print("No stored snapshot in this window.", file=sys.stderr)
        return 1
    if not report["platform_trends"]:
        print("No news or social observations in the latest snapshot.", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    args = read_args(argv)

    try:
        # Discovery commands never collect data or open the database.
        if args.command == "schema":
            schema = load_schema(args.name)
            text = json.dumps(schema, indent=2, ensure_ascii=False)
            print(text)
            return 0
        if args.command == "doctor":
            return doctor_once(args)

        # Command-line options override the matching config values.
        settings = load_settings(args.config)
        if getattr(args, "window", None) is not None:
            settings["window"] = args.window
        if getattr(args, "sources", None):
            settings["sources"] = args.sources

        status = 0
        json_output = getattr(args, "json", False)
        if args.command in {"collect", "run"}:
            # Credentials are unnecessary for stored reports and discovery commands.
            load_environment()

            # Keep status messages out of piped JSON; collection still saves to SQLite.
            output = sys.stderr if json_output else sys.stdout
            status = collect_once(settings, output=output)
        if args.command in {"report", "run"}:
            report_status = report_once(settings, json_output=json_output)
            status = max(status, report_status)

        return status
    except (OSError, ValueError, PeeweeException) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
