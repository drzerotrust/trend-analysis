"""Small TOML configuration; credentials come only from the environment."""

import math
import tomllib

__all__ = ["COUNTRIES", "SOURCES", "load_settings"]

COUNTRIES = {
    "US": "north_america",
    "CA": "north_america",
    "MX": "north_america",
    "JP": "asia",
    "KR": "asia",
    "IN": "asia",
    "ID": "asia",
    "SG": "asia",
}
SOURCES = ("hacker_news", "google_trends", "tiktok", "youtube")
DEFAULTS = {
    "countries": list(COUNTRIES),
    "sources": list(SOURCES),
    "database": "trend_engine.db",
    "reports_dir": "reports",
    "timeout": 20,
    "workers": 4,
    "window": 24,
    "limit": 25,
}


def load_settings(path):
    # Load the user's settings over the defaults, then reject unknown options.
    if path.suffix in {".yaml", ".yml"}:
        raise ValueError("Configuration now uses TOML. See config.toml and README.md.")

    with path.open("rb") as file:
        values = tomllib.load(file)

    unknown = values.keys() - DEFAULTS.keys()
    if unknown:
        names = sorted(unknown)
        names = ", ".join(names)
        raise ValueError("Unknown settings: %s" % names)

    settings = DEFAULTS.copy()
    settings.update(values)
    check_settings(settings)

    # Relative paths belong to the config's directory, not the shell's directory.
    config_path = path.resolve()
    config_directory = config_path.parent
    for key in ("database", "reports_dir"):
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise ValueError("%s must be a nonempty path" % key)

        settings[key] = config_directory / settings[key]

    return settings


def check_settings(settings):
    # Validate source/country lists before starting any network work.
    for key, choices in (("countries", COUNTRIES), ("sources", SOURCES)):
        selected = settings[key]
        if not isinstance(selected, list) or not selected:
            raise ValueError("%s must be a nonempty list" % key)

        for item in selected:
            if not isinstance(item, str) or item not in choices:
                allowed = ", ".join(choices)
                raise ValueError("%s must contain values from: %s" % (key, allowed))

        unique_values = set(selected)
        if len(unique_values) != len(selected):
            raise ValueError("%s must not contain duplicates" % key)

    # Keep timeouts, worker counts, and report sizes within useful bounds.
    for key, low, high in (
        ("timeout", 1, 120),
        ("workers", 1, 8),
        ("window", 1, 168),
        ("limit", 1, 100),
    ):
        value = settings[key]
        # Exact types deliberately reject booleans, which Python treats as ints.
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("%s must be a finite number" % key)
        if not low <= value <= high:
            raise ValueError("%s must be between %s and %s" % (key, low, high))
        if key in {"workers", "window", "limit"} and type(value) is not int:
            raise ValueError("%s must be an integer" % key)
