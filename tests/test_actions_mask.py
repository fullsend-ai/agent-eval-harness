"""Actions mask scrubbing shared by runner capture and persistence."""

from agent_eval.actions_mask import (
    ActionsMaskScrubber,
    actions_mask_values,
    redact_actions_masks,
)


def test_redacts_declaration_repeated_and_earlier_values():
    text = "before synthetic-secret\n::add-mask::synthetic-secret\nafter synthetic-secret\n"
    scrubbed = redact_actions_masks(text)

    assert "synthetic-secret" not in scrubbed
    assert "::add-mask::***" in scrubbed
    assert scrubbed == "before ***\n::add-mask::***\nafter ***\n"


def test_collects_escaped_and_decoded_forms():
    values = actions_mask_values("::add-mask::abc%25def%0A")

    assert "abc%25def%0A" in values
    assert "abc%def\n" in values


def test_redacts_both_escaped_and_decoded_forms():
    text = "::add-mask::abc%25def%0Aabc%def\n"
    scrubbed = redact_actions_masks(text)

    assert "abc%25def%0A" not in scrubbed
    assert "abc%def" not in scrubbed
    assert scrubbed == "::add-mask::***\n"


def test_scrubber_redacts_values_across_streams():
    scrubber = ActionsMaskScrubber()
    scrubber.observe("::add-mask::shared-secret\n")

    assert scrubber.redact("before shared-secret\n") == "before ***\n"
    assert scrubber.redact("after shared-secret\n") == "after ***\n"


def test_preserves_non_secret_diagnostics():
    text = "Error: unrelated diagnostic\n::add-mask::synthetic-secret\n"
    scrubbed = redact_actions_masks(text)

    assert "Error: unrelated diagnostic" in scrubbed
    assert "synthetic-secret" not in scrubbed
