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
    values = actions_mask_values("::add-mask::abcdef%25ghij%0A")

    assert "abcdef%25ghij%0A" in values
    assert "abcdef%ghij\n" in values


def test_collects_case_insensitive_and_indented_declarations():
    values = actions_mask_values("  ::ADD-MASK::synthetic-secret\n")

    assert "synthetic-secret" in values


def test_ignores_declarations_embedded_in_other_text():
    text = '{"message":"not a command ::add-mask::synthetic-secret"}\n'
    scrubbed = redact_actions_masks(text)

    assert json.loads(scrubbed)["message"] == (
        "not a command ::add-mask::synthetic-secret")


def test_collects_each_decoded_line_of_a_multiline_value():
    values = actions_mask_values("::add-mask::LONGALPHA%0ALONGBETA\n")

    assert "LONGALPHA" in values
    assert "LONGBETA" in values


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
    text = "::add-mask::abcdef%25ghij%0Aabcdef%ghij\n"
    scrubbed = redact_actions_masks(text)

    assert "abcdef%25ghij%0A" not in scrubbed
    assert "abcdef%ghij" not in scrubbed
    assert scrubbed == "::add-mask::***\n"


def test_preserves_non_secret_diagnostics():
    text = "Error: unrelated diagnostic\n::add-mask::synthetic-secret\n"
    scrubbed = redact_actions_masks(text)

    assert "Error: unrelated diagnostic" in scrubbed
    assert "synthetic-secret" not in scrubbed


def test_ignores_short_and_whitespace_only_values_without_shredding_text():
    text = "::add-mask::e\n::add-mask::   \nError: hello there\n"
    scrubbed = redact_actions_masks(text)

    assert scrubbed == (
        "::add-mask::***\n::add-mask::***\nError: hello there\n")


def test_ignores_declarations_inside_stop_commands_block():
    text = (
        "::stop-commands::pause-token\n"
        "::add-mask::short-secret\n"
        "Error: short-secret and unrelated text\n"
        "::pause-token::\n"
        "::add-mask::valid-secret-value\n"
        "Error: valid-secret-value\n"
    )
    scrubbed = redact_actions_masks(text)

    assert "short-secret" in scrubbed
    assert "unrelated text" in scrubbed
    assert "valid-secret-value" not in scrubbed
    assert scrubbed.count("::add-mask::***") == 2
