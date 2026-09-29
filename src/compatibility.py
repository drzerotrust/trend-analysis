"""Offline installation checks and bundled JSON contracts for agents."""

import json
import os
import re
from pathlib import Path

from config import load_settings

__all__ = [
    "CONTRACT_VERSIONS",
    "__version__",
    "check_installation",
    "load_schema",
    "version_parts",
]

# Hatchling reads the same version used by the CLI.
__version__ = "0.4.0"
CONTRACT_VERSIONS = {"report": 3, "doctor": 2}


def version_parts(value):
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError("version must use MAJOR.MINOR.PATCH, for example 0.4.0")

    parts = value.split(".")
    numbers = []
    for part in parts:
        number = int(part)
        numbers.append(number)

    return tuple(numbers)


def load_schema(name):
    if name not in CONTRACT_VERSIONS:
        raise ValueError("Unknown contract: %s" % name)

    # Wheels carry data beside the flat modules; source runs use the root contracts.
    module_path = Path(__file__)
    module_path = module_path.resolve()
    directory = module_path.with_name("trend_engine_contracts")
    if not directory.is_dir():
        directory = module_path.parents[1] / "contracts"

    filename = "%s.schema.json" % name
    schema_path = directory / filename
    text = schema_path.read_text(encoding="utf-8")
    return json.loads(text)


def check_installation(args):
    # Version bounds and output contracts are independent compatibility checks.
    errors = []
    current = version_parts(__version__)
    if args.min_version is not None and current < args.min_version:
        errors.append("version_too_old")
    if args.max_version is not None and current >= args.max_version:
        errors.append("version_too_new")

    for name, version in CONTRACT_VERSIONS.items():
        argument = "require_%s_schema" % name
        required = getattr(args, argument)
        if required is not None and required != version:
            errors.append("%s_schema_mismatch" % name)

    # A bare installation can be checked from anywhere, without a user config.
    youtube_key = os.getenv("YOUTUBE_API_KEY")
    configuration = {
        "status": "not_checked",
        "enabled_sources": [],
        "youtube_key_present": bool(youtube_key),
    }
    config = args.config
    default_config = Path("config.toml")
    if config is None and default_config.exists():
        config = default_config

    if config is not None:
        try:
            settings = load_settings(config)
        except (OSError, ValueError):
            # Do not echo arbitrary config contents or paths into a machine response.
            configuration["status"] = "error"
            errors.append("configuration_invalid")
        else:
            configuration.update(status="ok", enabled_sources=settings["sources"])

    # Config errors have their own exit code; other mismatches use exit 1.
    status = 0
    document_status = "success"
    if errors:
        status = 1
        document_status = "error"
    if "configuration_invalid" in errors:
        status = 2

    document = {
        "doctor_version": CONTRACT_VERSIONS["doctor"],
        "tool": "trend-engine",
        "version": __version__,
        "status": document_status,
        "contracts": dict(CONTRACT_VERSIONS),
        "configuration": configuration,
        "network_checked": False,
        "errors": errors,
    }
    return document, status
