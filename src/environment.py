"""Load one trusted dotenv file without replacing exported environment variables."""

import os
from io import StringIO
from pathlib import Path

from dotenv import load_dotenv
from dotenv.parser import parse_stream

__all__ = ["load_environment"]

# A normal wheel has no checkout. Never discover credentials in site-packages.
MODULE_PATH = Path(__file__)
MODULE_PATH = MODULE_PATH.resolve()
CHECKOUT_ROOT = None
if MODULE_PATH.parent.name == "src":
    CHECKOUT_ROOT = MODULE_PATH.parents[1]


def _environment_file():
    explicit = os.getenv("TREND_ENGINE_ENV_FILE", "")
    explicit = explicit.strip()
    if explicit:
        path = Path(explicit)
        path = path.expanduser()
        return path.absolute()

    # Source/editable installs use their own checkout, not the working directory.
    if CHECKOUT_ROOT is not None:
        metadata = CHECKOUT_ROOT / "pyproject.toml"
        path = CHECKOUT_ROOT / ".env"
        if metadata.is_file() and path.is_file():
            return path

    config_root = os.getenv("XDG_CONFIG_HOME")
    if config_root:
        directory = Path(config_root)
        directory = directory.expanduser()
    else:
        home_directory = Path.home()
        directory = home_directory / ".config"

    path = directory / "trend-engine" / ".env"
    if path.is_file():
        return path

    return None


def _validate_environment(text):
    # Reject the whole file before applying any value or invoking dotenv logging.
    if "\x00" in text:
        raise ValueError("Invalid environment file")

    stream = StringIO(text)
    bindings = parse_stream(stream)
    for binding in bindings:
        if binding.error:
            raise ValueError("Invalid environment file")

        key = binding.key
        if key is not None and (not key or "=" in key):
            raise ValueError("Invalid environment file")


def load_environment():
    """Missing optional files are fine; invalid selected files fail without secrets."""
    try:
        path = _environment_file()
        if path is None:
            return

        text = path.read_text(encoding="utf-8")
        _validate_environment(text)
        stream = StringIO(text)
        load_dotenv(stream=stream, override=False)
    except (OSError, UnicodeError, ValueError, RuntimeError):
        # Paths and parse errors can contain credentials; do not include them.
        raise ValueError(
            "Environment file is missing, unreadable, or invalid"
        ) from None
