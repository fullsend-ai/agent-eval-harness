"""GitHub Actions ``::add-mask::`` scrubbing for captured process output."""
import re


_ADD_MASK_RE = re.compile(
    r"(?im)^(?P<indent>[ \t]*)::add-mask::(?P<value>[^\r\n]*)")
_STOP_COMMANDS_RE = re.compile(
    r"(?im)^[ \t]*::stop-commands::(?P<token>[^\r\n]+)")
_RESUME_COMMANDS_RE = re.compile(
    r"(?im)^[ \t]*::(?P<token>[^\r\n]+)::")
_ACTIONS_ESCAPE_RE = re.compile(r"%25|%0D|%0A", re.IGNORECASE)
_ACTIONS_ESCAPE_MAP = {
    "%25": "%",
    "%0D": "\r",
    "%0A": "\n",
}
_MIN_MASKED_VALUE_CHARS = 8


def _unescape_actions_value(value: str) -> str:
    """Unescape the subset GitHub Actions uses in workflow-command data."""
    return _ACTIONS_ESCAPE_RE.sub(
        lambda match: _ACTIONS_ESCAPE_MAP[match.group(0).upper()], value)


def actions_mask_values(text: str) -> tuple:
    """Return escaped and decoded values declared by ``::add-mask::``.

    Values are returned longest-first so a value that contains another is
    replaced whole. Empty, whitespace-only, and very short values are ignored
    because they cannot identify a secret and would otherwise shred ordinary
    diagnostics. Declarations between ``::stop-commands::`` and its resume
    command are ignored, matching GitHub Actions.
    """
    collector = ActionsMaskCollector()
    for line in (text or "").splitlines(keepends=True):
        collector.observe_line(line)
    return collector.values()


def actions_mask_values_across_streams(*texts: str) -> tuple:
    """Return mask values collected independently from several streams.

    Streams must not be concatenated before collection: a declaration at the
    end of one stream without a newline would otherwise absorb the beginning
    of the next stream.
    """
    values = set()
    for text in texts:
        values.update(actions_mask_values(text or ""))
    return tuple(sorted(values, key=len, reverse=True))


def redact_actions_masks_across_streams(*texts: str) -> tuple:
    """Redact every stream with the union of their Actions mask values."""
    values = actions_mask_values_across_streams(*texts)
    return tuple(
        redact_actions_masks(text or "", values) for text in texts)


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
    return _ADD_MASK_RE.sub(
        lambda match: f"{match.group('indent')}::add-mask::***", text)


def redact_actions_masks_in_data(value, values=None):
    """Recursively redact Actions mask values in JSON-like data."""
    if values is None:
        values = actions_mask_values_in_data(value)
    if isinstance(value, str):
        return redact_actions_masks(value, values)
    if isinstance(value, list):
        return [redact_actions_masks_in_data(item, values) for item in value]
    if isinstance(value, dict):
        return {key: redact_actions_masks_in_data(item, values)
                for key, item in value.items()}
    return value


def actions_mask_values_in_data(value) -> tuple:
    """Collect mask values from every string in JSON-like data."""
    if isinstance(value, str):
        return actions_mask_values(value)
    if isinstance(value, list):
        return actions_mask_values_across_streams(
            *(actions_mask_values_in_data(item) for item in value))
    if isinstance(value, dict):
        return actions_mask_values_across_streams(
            *(actions_mask_values_in_data(item) for item in value.values()))
    return ()


class ActionsMaskCollector:
    """Collect valid mask declarations from one ordered stream."""

    def __init__(self):
        self._values = set()
        self._stop_token = None

    def observe_line(self, line: str) -> tuple:
        if self._stop_token is not None:
            resume = _RESUME_COMMANDS_RE.match(line)
            if resume and resume.group("token") == self._stop_token:
                self._stop_token = None
            return ()

        stop = _STOP_COMMANDS_RE.match(line)
        if stop:
            self._stop_token = stop.group("token")
            return ()

        match = _ADD_MASK_RE.match(line)
        if match is None:
            return ()
        escaped = match.group("value")
        decoded = _unescape_actions_value(escaped)
        added = []
        for value in (escaped, decoded, *decoded.splitlines()):
            if self._add_value(value):
                added.append(value)
        return tuple(added)

    def _add_value(self, value: str):
        if len(value.strip()) >= _MIN_MASKED_VALUE_CHARS:
            self._values.add(value)
            return value
        return None

    def values(self) -> tuple:
        return tuple(sorted(self._values, key=lambda value: (-len(value), value)))


__all__ = [
    "actions_mask_values",
    "actions_mask_values_across_streams",
    "actions_mask_values_in_data",
    "redact_actions_masks",
    "redact_actions_masks_across_streams",
    "redact_actions_masks_in_data",
]
