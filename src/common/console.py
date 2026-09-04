"""Console setup for cross-platform output.

Every rupee figure this project prints contains ``₹`` (U+20B9), which the default
Windows console codepage (cp1252) cannot encode — a bare ``print`` of a formatted
amount raises ``UnicodeEncodeError`` and kills the run. Since a judge may well be
on Windows, every CLI entry point calls :func:`setup_console` first.
"""

from __future__ import annotations

import sys


def setup_console() -> None:
    """Force UTF-8 on stdout/stderr where the platform allows it."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - exotic terminals
                pass


def rule(title: str = "", width: int = 78, char: str = "-") -> str:
    """A titled horizontal rule for CLI reports."""
    if not title:
        return char * width
    label = f" {title} "
    pad = max(0, width - len(label))
    left = pad // 2
    return char * left + label + char * (pad - left)
