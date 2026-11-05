"""`load_case_record` and the dataset case's annotations.yaml.

The file is evaluation material the harness reads for every case. A file that
cannot be read or parsed must leave the case unannotated, as a missing file
does: invalid bytes raised UnicodeDecodeError (a ValueError, outside the
`(yaml.YAMLError, OSError)` clause) through `load_case_record`, so every judge
of that case errored before any ran and the case left the pass-rate
denominator with nothing but a traceback in the log.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig

import score


def _config(dataset_root):
    config = EvalConfig(name="test", skill="test")
    config.dataset.path = str(dataset_root)
    return config


def _case(tmp_path, annotations):
    """A dataset case and its run directory; ``annotations`` is str or bytes."""
    dataset = tmp_path / "dataset"
    (dataset / "case-001").mkdir(parents=True)
    path = dataset / "case-001" / "annotations.yaml"
    if isinstance(annotations, bytes):
        path.write_bytes(annotations)
    else:
        path.write_text(annotations, encoding="utf-8")
    case_dir = tmp_path / "run" / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    return case_dir, _config(dataset)


def _annotations(tmp_path, annotations):
    case_dir, config = _case(tmp_path, annotations)
    record = score.load_case_record(case_dir, config, load_files=False)
    return record["annotations"]


def test_readable_annotations_are_loaded(tmp_path):
    assert _annotations(tmp_path, "tags: [revision-expected]\ndifficulty: hard\n") == {
        "tags": ["revision-expected"],
        "difficulty": "hard",
    }


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"\xff\xfe", id="invalid-utf8"),
        pytest.param("tags: [\n", id="yaml-syntax-error"),
        pytest.param(b"\xff\xfetags: []\n", id="utf16-bom-garbage"),
        pytest.param("- just\n- a list\n", id="not-a-mapping"),
        pytest.param("", id="empty"),
    ],
)
def test_unreadable_or_unusable_annotations_leave_the_case_unannotated(tmp_path, content):
    assert _annotations(tmp_path, content) == {}


def test_missing_annotations_file_is_the_same_as_unreadable(tmp_path):
    case_dir, config = _case(tmp_path, "x: 1\n")
    (Path(config.dataset.path) / "case-001" / "annotations.yaml").unlink()
    record = score.load_case_record(case_dir, config, load_files=False)
    assert record["annotations"] == {}
