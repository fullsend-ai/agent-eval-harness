"""GitHub Actions ``::add-mask::`` scrubbing for captured process output."""
import re


_ADD_MASK_RE = re.compile(
    r"(?im)^(?P<indent>[ \t]*)::add-mask::(?P<value>[^\r\n]*)")
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
        escaped = match.group("value")
        decoded = _unescape_actions_value(escaped)
        if escaped:
            values.add(escaped)
        if decoded:
            values.add(decoded)
        for decoded_line in decoded.splitlines():
            if decoded_line.strip():
                values.add(decoded_line.strip())
    return tuple(sorted(values, key=len, reverse=True))


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


__all__ = [
    "actions_mask_values",
    "actions_mask_values_across_streams",
    "redact_actions_masks",
    "redact_actions_masks_across_streams",
]
