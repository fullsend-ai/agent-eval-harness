"""GitHub Actions ``::add-mask::`` scrubbing for captured process output."""

import re
import threading


_ADD_MASK_RE = re.compile(r"::add-mask::([^\r\n]*)")
_ACTIONS_ESCAPE_RE = re.compile(r"%25|%0D|%0A", re.IGNORECASE)
_ACTIONS_ESCAPE_MAP = {
    "%25": "%",
    "%0D": "\r",
    "%0A": "\n",
}


def _unescape_actions_value(value: str) -> str:
    """Unescape the subset GitHub Actions uses in workflow-command data."""
    return _ACTIONS_ESCAPE_RE.sub(
        lambda match: _ACTIONS_ESCAPE_MAP[match.group(0).upper()], value)


def actions_mask_values(text: str) -> tuple:
    """Return escaped and decoded values declared by ``::add-mask::``.

    Values are returned longest-first so a value that contains another is
    replaced whole. Empty values are ignored because they cannot identify a
    secret and would otherwise rewrite every declaration to itself.
    """
    values = set()
    for match in _ADD_MASK_RE.finditer(text or ""):
        escaped = match.group(1)
        decoded = _unescape_actions_value(escaped)
        if escaped:
            values.add(escaped)
        if decoded:
            values.add(decoded)
    return tuple(sorted(values, key=len, reverse=True))


def redact_actions_masks(text: str, values=None) -> str:
    """Replace Actions mask values and rewrite their declarations.

    ``values`` may be supplied when mask declarations were collected from
    another stream; otherwise they are collected from ``text`` itself. Both
    forms are replaced so an escaped value cannot survive when its decoded
    form was the one emitted later, or vice versa.
    """
    if not text:
        return text
    mask_values = actions_mask_values(text) if values is None else tuple(values)
    for value in sorted(set(mask_values), key=len, reverse=True):
        text = text.replace(value, "***")
    return _ADD_MASK_RE.sub("::add-mask::***", text)


class ActionsMaskScrubber:
    """Thread-safe accumulator for mask values from concurrent pipes."""

    def __init__(self):
        self._values = set()
        self._lock = threading.Lock()
        self._cached_values = None

    def observe(self, text: str) -> None:
        values = actions_mask_values(text)
        if not values:
            return
        with self._lock:
            self._values.update(values)
            self._cached_values = None

    def values(self) -> tuple:
        with self._lock:
            if self._cached_values is None:
                self._cached_values = tuple(
                    sorted(self._values, key=len, reverse=True))
            return self._cached_values

    def redact(self, text: str) -> str:
        return redact_actions_masks(text, self.values())
