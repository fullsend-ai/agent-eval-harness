"""Actions mask scrubbing shared by runner capture and persistence."""

import json

from agent_eval.actions_mask import (
    actions_mask_values,
    redact_actions_masks,
    redact_actions_masks_across_streams,
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


def test_collects_case_insensitive_and_indented_declarations():
    values = actions_mask_values("  ::ADD-MASK::synthetic-secret\n")

    assert "synthetic-secret" in values


def test_ignores_declarations_embedded_in_other_text():
    text = '{"message":"not a command ::add-mask::synthetic-secret"}\n'
    scrubbed = redact_actions_masks(text)

    assert json.loads(scrubbed)["message"] == (
        "not a command ::add-mask::synthetic-secret")


def test_collects_each_decoded_line_of_a_multiline_value():
    values = actions_mask_values("::add-mask::ALPHA%0ABETA\n")

    assert "ALPHA" in values
    assert "BETA" in values


def test_collects_stream_values_without_concatenating_streams():
    stdout = "::add-mask::stream-secret"
    stderr = "Error: stream-secret\n"
    scrubbed_stdout, scrubbed_stderr = redact_actions_masks_across_streams(
        stdout, stderr)

    assert "stream-secret" not in scrubbed_stdout
    assert "stream-secret" not in scrubbed_stderr
    assert scrubbed_stdout == "::add-mask::***"
    assert scrubbed_stderr == "Error: ***\n"


def test_redacts_both_escaped_and_decoded_forms():
    text = "::add-mask::abc%25def%0Aabc%def\n"
    scrubbed = redact_actions_masks(text)

    assert "abc%25def%0A" not in scrubbed
    assert "abc%def" not in scrubbed
    assert scrubbed == "::add-mask::***\n"


def test_preserves_non_secret_diagnostics():
    text = "Error: unrelated diagnostic\n::add-mask::synthetic-secret\n"
    scrubbed = redact_actions_masks(text)

    assert "Error: unrelated diagnostic" in scrubbed
    assert "synthetic-secret" not in scrubbed
