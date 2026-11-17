"""Write-site guard (spec 014): every ``run_result.json`` writer goes through
the reconciling ``write_run_result`` helper, so the cost-provenance fields are
written on the crash and step paths too. Grep/AST based; runs without a key.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTE = REPO_ROOT / "skills" / "eval-run" / "scripts" / "execute.py"
HARBOR_RUN = REPO_ROOT / "agent_eval" / "harbor" / "run.py"
EVALHUB_RUNNER = REPO_ROOT / "agent_eval" / "evalhub" / "runner.py"
RECONCILE = REPO_ROOT / "agent_eval" / "providers" / "reconcile.py"

# execute.py's sites; 9 (Harbor) and 10 (EvalHub) live in their own modules.
EXECUTE_WRITE_SITES = [*range(1, 9), 11]


def _lines_writing_run_result(path):
    """Source lines that open/dump/write_text a ``run_result.json`` target
    outside the helper (comments and docstrings excluded)."""
    text = path.read_text()
    tree = ast.parse(text, filename=str(path))
    helper_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "write_run_result":
            helper_ranges.append((node.lineno, node.end_lineno))
    offenders = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if "run_result.json" not in stripped or stripped.startswith("#"):
            continue
        if any(a <= lineno <= b for a, b in helper_ranges):
            continue
        if re.search(r"open\(|write_text\(|json\.dump\(", stripped):
            offenders.append((lineno, stripped))
    return offenders


def test_execute_has_no_direct_run_result_writer():
    assert _lines_writing_run_result(EXECUTE) == []


def test_every_enumerated_write_site_calls_the_helper():
    text = EXECUTE.read_text()
    markers = sorted(int(m) for m in re.findall(r"# run_result write-site (\d+)", text))
    assert markers == EXECUTE_WRITE_SITES, markers
    for n in markers:
        block = text.split(f"# run_result write-site {n}", 1)[1].splitlines()[1:4]
        assert any("write_run_result(" in line for line in block), (
            f"write-site {n} is not followed by a write_run_result( call")
    assert text.count("write_run_result(") >= len(EXECUTE_WRITE_SITES) + 1  # calls + the def


def test_evalhub_runner_only_writes_through_the_helper():
    assert _lines_writing_run_result(EVALHUB_RUNNER) == []
    text = EVALHUB_RUNNER.read_text()
    assert "# run_result write-site 10" in text
    assert re.search(r'write_run_result\(\s*output_dir / "run_result.json", run_meta', text)


def test_the_helper_reconciles_exactly_once():
    text = RECONCILE.read_text()
    tree = ast.parse(text)
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "write_run_result")
    calls = [n for n in ast.walk(helper)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "reconcile"]
    assert len(calls) == 1
    # execute.py's wrapper delegates to the shared helper (one implementation).
    ex_tree = ast.parse(EXECUTE.read_text())
    wrapper = next(n for n in ast.walk(ex_tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "write_run_result")
    delegations = [n for n in ast.walk(wrapper) if isinstance(n, ast.Call)
                   and getattr(n.func, "id", None) == "_reconciled_write"]
    assert len(delegations) == 1


def test_harbor_run_only_writes_through_the_helper():
    assert _lines_writing_run_result(HARBOR_RUN) == []
    text = HARBOR_RUN.read_text()
    assert "# run_result write-site 9" in text
    assert re.search(r'write_run_result\(\s*output_dir / "run_result.json", run_meta', text)
