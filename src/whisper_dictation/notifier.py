"""Desktop notifications intentionally disabled for this installation."""

from __future__ import annotations


def notify(title: str, message: str = "") -> None:
    """Suppress routine desktop notifications; logs and tones remain active."""
    return
