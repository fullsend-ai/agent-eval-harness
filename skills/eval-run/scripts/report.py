#!/usr/bin/env python3
"""Generate an HTML report from eval run results.

Reads summary.yaml, run_result.json, eval.yaml, and optionally
review.yaml + a baseline run to produce a self-contained HTML report.
Works with any skill — reads judges, thresholds, and outputs dynamically.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/report.py \\
        --run-id <id> \\
        --config eval.yaml \\
        [--baseline <baseline-id>] \\
        [--open]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import base64
import difflib
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Helpers (ported from rfe-creator eval/reporting/report.py)
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pct(val) -> str:
    if val is None:
        return "?"
    return f"{val * 100:.0f}%" if isinstance(val, float) else str(val)


def _pairwise_badge(winner: str) -> str:
    badges = {"A": ("pw-win", "WIN"), "B": ("pw-loss", "LOSS"),
              "tie": ("pw-tie", "TIE")}
    cls, label = badges.get(winner, ("pw-error", "ERR"))
    return f'<span class="pw-badge {cls}">{label}</span>'


def _word_diff_markup(old_line: str, new_line: str):
    sm = difflib.SequenceMatcher(None, old_line.split(), new_line.split())
    left_parts, right_parts = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        old_words = " ".join(old_line.split()[i1:i2])
        new_words = " ".join(new_line.split()[j1:j2])
        if op == "equal":
            left_parts.append(_esc(old_words))
            right_parts.append(_esc(new_words))
        elif op == "replace":
            left_parts.append(f'<span class="wdel">{_esc(old_words)}</span>')
            right_parts.append(f'<span class="wadd">{_esc(new_words)}</span>')
        elif op == "delete":
            left_parts.append(f'<span class="wdel">{_esc(old_words)}</span>')
        elif op == "insert":
            right_parts.append(f'<span class="wadd">{_esc(new_words)}</span>')
    return " ".join(left_parts), " ".join(right_parts)


def _side_by_side_diff(a: str, b: str, left_label: str = "",
                       right_label: str = "", context: int = 3) -> str:
    a_lines, b_lines = a.splitlines(), b.splitlines()
    sm = difflib.SequenceMatcher(None, a_lines, b_lines)
    rows = [f'<tr class="hdr"><td class="ln"></td>'
            f'<td class="left">{_esc(left_label)}</td>'
            f'<td class="sep"></td>'
            f'<td class="ln"></td>'
            f'<td class="right">{_esc(right_label)}</td></tr>']

    for group in sm.get_grouped_opcodes(context):
        rows.append('<tr class="hdr"><td class="ln" colspan="2">...</td>'
                    '<td class="sep"></td>'
                    '<td class="ln" colspan="2">...</td></tr>')
        for op, i1, i2, j1, j2 in group:
            if op == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    rows.append(
                        f'<tr><td class="ln">{i+1}</td>'
                        f'<td class="left">{_esc(a_lines[i])}</td>'
                        f'<td class="sep"></td>'
                        f'<td class="ln">{j+1}</td>'
                        f'<td class="right">{_esc(b_lines[j])}</td></tr>')
            elif op == "replace":
                for k in range(max(i2 - i1, j2 - j1)):
                    ai = i1 + k if i1 + k < i2 else None
                    bj = j1 + k if j1 + k < j2 else None
                    if ai is not None and bj is not None:
                        lh, rh = _word_diff_markup(a_lines[ai], b_lines[bj])
                        rows.append(
                            f'<tr class="mod"><td class="ln">{ai+1}</td>'
                            f'<td class="left">{lh}</td><td class="sep"></td>'
                            f'<td class="ln">{bj+1}</td>'
                            f'<td class="right">{rh}</td></tr>')
                    elif ai is not None:
                        rows.append(
                            f'<tr class="del"><td class="ln">{ai+1}</td>'
                            f'<td class="left">{_esc(a_lines[ai])}</td>'
                            f'<td class="sep"></td><td class="ln"></td>'
                            f'<td class="right"></td></tr>')
                    elif bj is not None:
                        rows.append(
                            f'<tr class="add"><td class="ln"></td>'
                            f'<td class="left"></td><td class="sep"></td>'
                            f'<td class="ln">{bj+1}</td>'
                            f'<td class="right">{_esc(b_lines[bj])}</td></tr>')
            elif op == "delete":
                for i in range(i1, i2):
                    rows.append(
                        f'<tr class="del"><td class="ln">{i+1}</td>'
                        f'<td class="left">{_esc(a_lines[i])}</td>'
                        f'<td class="sep"></td><td class="ln"></td>'
                        f'<td class="right"></td></tr>')
            elif op == "insert":
                for j in range(j1, j2):
                    rows.append(
                        f'<tr class="add"><td class="ln"></td>'
                        f'<td class="left"></td><td class="sep"></td>'
                        f'<td class="ln">{j+1}</td>'
                        f'<td class="right">{_esc(b_lines[j])}</td></tr>')

    return f'<table class="diff-table">{"".join(rows)}</table>'


# ---------------------------------------------------------------------------
# Data loading (standalone — no agent_eval imports)
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def load_reward_cfg(config_path):
    """Return the parsed RewardConfig from an eval.yaml, or None on any failure.

    The report keeps a raw config dict, but the reward table needs the parsed
    RewardConfig so `compose_reward` uses the same resolution
    (`judge` / `weighted` / `formula` / gate) as `build_reward` — otherwise the
    displayed reward diverges from what the harness actually trains on.
    Degrade silently if EvalConfig parsing fails for unrelated field errors;
    the reward column then falls back to compose_reward's default path.
    """
    try:
        from agent_eval.config import EvalConfig
        return EvalConfig.from_yaml(config_path).reward
    except Exception:
        return None


def _read_text(path: Path, max_lines: int = 200) -> str:
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n\n... truncated ({len(lines)} lines total)"
        return "\n".join(lines)
    except (UnicodeDecodeError, OSError):
        return ""


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}


def _is_image_file(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES


def _image_to_data_uri(path: Path) -> str:
    mime = _IMAGE_MIME.get(path.suffix.lower(), "application/octet-stream")
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except OSError:
        return ""


def _find_gold_standard_images(case_id: str, dataset_path: str):
    """Find gold standard images for a case from the dataset annotations.

    Returns a dict with keys 'image' (PNG/SVG from file) and 'diagram'
    (SVG rendered from the gold standard source file, e.g. D2 or drawio).
    Either or both may be None.
    """
    result = {"image": None, "diagram_uri": None}
    if not dataset_path:
        return result
    dataset_root = Path(dataset_path).resolve()
    case_ds = (dataset_root / case_id).resolve()
    if not case_ds.is_relative_to(dataset_root):
        return result
    ann_path = case_ds / "annotations.yaml"
    if not ann_path.is_file() or ann_path.is_symlink():
        return result
    ann = _load_yaml(ann_path)
    gold_name = ann.get("gold_diagram")
    if not gold_name:
        return result
    stem = Path(gold_name).stem
    full_stem = gold_name  # e.g. "gold-standard.drawio" or "gold-standard.d2"

    # Find a pre-rendered image (PNG, SVG file)
    for suffix in _IMAGE_SUFFIXES:
        for candidate_name in [f"{full_stem}{suffix}", f"{stem}{suffix}"]:
            candidate = (case_ds / candidate_name).resolve()
            if (candidate.is_relative_to(dataset_root)
                    and candidate.is_file()
                    and not candidate.is_symlink()):
                result["image"] = candidate
                break
        if result["image"]:
            break

    # Render the gold standard source file to SVG for diagram comparison
    gold_source = (case_ds / gold_name).resolve()
    if (gold_source.is_file() and not gold_source.is_symlink()
            and gold_source.is_relative_to(dataset_root)
            and gold_source.suffix in _DIAGRAM_SUFFIXES):
        sibs = {s.name for s in case_ds.iterdir()}
        svg_uri = _try_render_diagram(gold_source, sibs)
        if svg_uri:
            result["diagram_uri"] = svg_uri

    return result


_DIAGRAM_SUFFIXES = {".d2", ".drawio"}


def _render_d2_to_svg(d2_path: Path) -> str:
    """Render a D2 file to SVG. Returns SVG text or empty string."""
    salt = hashlib.md5(str(d2_path).encode()).hexdigest()[:8]
    try:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            ["d2", "--bundle", "--layout", "elk", "--salt", salt, str(d2_path), tmp_path],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return Path(tmp_path).read_text()
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, OSError):
        return ""
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except (NameError, OSError):
            pass


def _render_drawio_to_svg(drawio_path: Path) -> str:
    """Render a drawio file to SVG. Returns SVG text or empty string."""
    cli = ("/Applications/draw.io.app/Contents/MacOS/draw.io"
           if platform.system() == "Darwin" else "drawio")
    try:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            [cli, "-x", "-f", "svg", "--embed-svg-fonts", "false",
             "-o", tmp_path, str(drawio_path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return Path(tmp_path).read_text()
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, OSError):
        return ""
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except (NameError, OSError):
            pass


def _render_graph_spec_to_svg(json_path: Path) -> str:
    """Render a graph JSON to SVG via D2. Supports both graph-spec format
    (nodes/edges arrays) and layout-plan format (elements array with types)."""
    try:
        spec = json.loads(json_path.read_text())
        if not isinstance(spec, dict):
            return ""

        nodes = []
        edges = []

        if "nodes" in spec:
            # graph-spec format
            nodes = spec["nodes"]
            edges = spec.get("edges", [])
        elif "elements" in spec:
            # layout-plan format
            for elem in spec["elements"]:
                etype = elem.get("type", "")
                if etype in ("node", "container"):
                    label = elem.get("label_html", elem.get("id", ""))
                    label = label.split("<b>")[-1].split("</b>")[0] if "<b>" in label else label
                    label = label.split("<br>")[0].strip()
                    nodes.append({"id": elem["id"], "label": label,
                                  "role": "external" if "dashed" in elem.get("style", "") else "processing"})
                elif etype == "edge":
                    edges.append({"from": elem["from"], "to": elem["to"],
                                  "label": elem.get("label", ""),
                                  "style": "dashed" if "dashed=1" in elem.get("style", "") else "solid"})
        else:
            return ""

        if not nodes:
            return ""

        # Convert to D2
        def _d2_esc(s):
            return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

        lines = ["direction: right", ""]
        for n in nodes:
            label = _d2_esc(n.get("label", n["id"]))
            style = ""
            if n.get("role") == "external":
                style = " { style.stroke-dash: 3 }"
            lines.append(f'{n["id"]}: "{label}"{style}')
        lines.append("")
        for e in edges:
            label = _d2_esc(e.get("label", ""))
            label_part = f': "{label}"' if label else ""
            style = " { style.stroke-dash: 3 }" if e.get("is_back_edge") or e.get("style") == "dashed" else ""
            lines.append(f'{e["from"]} -> {e["to"]}{label_part}{style}')
        d2_text = "\n".join(lines)

        salt = hashlib.md5(str(json_path).encode()).hexdigest()[:8]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".d2", delete=False) as d2_tmp:
            d2_tmp.write(d2_text)
            d2_path = d2_tmp.name
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as svg_tmp:
            svg_path = svg_tmp.name
        subprocess.run(
            ["d2", "--bundle", "--layout", "elk", "--salt", salt, d2_path, svg_path],
            check=True, capture_output=True, text=True, timeout=30,
        )
        svg_text = Path(svg_path).read_text()
        return svg_text
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return ""
    finally:
        for p in ("d2_path", "svg_path"):
            try:
                Path(locals().get(p, "")).unlink(missing_ok=True)
            except (OSError, TypeError):
                pass


def _resolve_artifact_type(filename: str, config: dict) -> str:
    """Resolve the semantic type of an artifact from eval.yaml outputs.types.

    Checks explicit filenames first, then glob patterns.
    Returns the type string (e.g. 'graph') or empty string.
    """
    import fnmatch
    for output in config.get("outputs", []):
        if isinstance(output, dict):
            types = output.get("types") or {}
        else:
            types = getattr(output, "types", None) or {}
        if not types:
            continue
        # Exact match first
        if filename in types:
            return types[filename]
        # Glob patterns
        for pattern, typ in types.items():
            if fnmatch.fnmatch(filename, pattern):
                return typ
    return ""


def _svg_to_data_uri(svg_text: str) -> str:
    data = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{data}"


def _try_render_diagram(path: Path, sibling_names: set) -> str:
    """Try to render a diagram file to SVG. Returns data URI or empty string."""
    if path.suffix == ".d2":
        svg = _render_d2_to_svg(path)
        return _svg_to_data_uri(svg) if svg else ""
    if path.suffix == ".drawio":
        if f"{path.name}.png" in sibling_names:
            return ""
        svg = _render_drawio_to_svg(path)
        return _svg_to_data_uri(svg) if svg else ""
    return ""


def _resolve_dataset_case_dir(dataset_path: str, case_id: str):
    """Resolve a dataset case directory, tolerating a slugged suffix on the
    directory name (e.g. case_id 'RHAIRFE-1056' -> 'RHAIRFE-1056-some-slug').
    Rejects symlinks and paths escaping the dataset root (CWE-22/59).
    Returns the Path, or None if it can't be found."""
    ds = Path(dataset_path)
    if not ds.is_dir():
        return None
    ds_root = ds.resolve()

    def _ok(p):
        return (p.is_dir() and not p.is_symlink()
                and p.resolve().is_relative_to(ds_root))

    case_dir = ds / case_id
    if not _ok(case_dir):
        matches = [d for d in ds.iterdir()
                   if d.name.startswith(case_id) and _ok(d)]
        if len(matches) != 1:
            return None
        case_dir = matches[0]
    return case_dir


def _case_input_files(case_dir: Path, config) -> list:
    """Input files to show for a case: the input.{yaml,yml,json} record plus the
    files staged into the workspace (``dataset.workspace.files``). Ground-truth
    (reference/, annotations.yaml) is intentionally excluded. Rejects symlinks
    and paths escaping the case directory (CWE-22/59)."""
    files = []
    case_root = case_dir.resolve()

    def _ok(p):
        return (p.is_file() and not p.is_symlink()
                and p.resolve().is_relative_to(case_root))

    for suffix in (".yaml", ".yml", ".json"):
        cand = case_dir / f"input{suffix}"
        if _ok(cand):
            files.append(cand)
            break
    ws_files = ((config.get("dataset") or {}).get("workspace") or {}).get("files") or []
    for entry in ws_files:
        # A {dest, source} entry is materialized at its `dest`; a string entry
        # is a per-case path. Both land under the case dir in the workspace.
        name = entry.get("dest") if isinstance(entry, dict) else entry
        if not isinstance(name, str) or not name:
            continue
        cand = case_dir / name
        if _ok(cand) and cand not in files:
            files.append(cand)
    return files


# ---------------------------------------------------------------------------
# HTML section generators
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #cbd1da;
  --surface: #ffffff;
  --surface-2: #f1f5f9;
  --surface-3: #e2e8f0;
  --border: #e2e8f0;
  --border-strong: #cbd5e1;
  --text: #0f172a;
  --text-muted: #64748b;
  --text-soft: #475569;
  --accent: #2563eb;
  --accent-strong: #1e3a8a;
  --accent-soft: #dbeafe;
  --success: #16a34a;
  --success-soft: #dcfce7;
  --success-border: #86efac;
  --danger: #dc2626;
  --danger-soft: #fee2e2;
  --danger-border: #fca5a5;
  --warning: #d97706;
  --warning-soft: #fef3c7;
  --warning-border: #fcd34d;
  --neutral-soft: #f1f5f9;
  --neutral-border: #cbd5e1;
  --code-bg: #eef2f7;
  --shadow: 0 1px 3px rgba(15,23,42,.06), 0 1px 2px rgba(15,23,42,.04);
  --shadow-strong: 0 4px 12px rgba(15,23,42,.08), 0 1px 3px rgba(15,23,42,.05);
  --diff-add-bg: #e6ffec;
  --diff-add-strong: #acf2bd;
  --diff-del-bg: #ffeef0;
  --diff-del-strong: #fdb8c0;
  --diff-hdr-bg: #f0f0f0;
  --case-pass-accent: #16a34a;
  --case-fail-accent: #dc2626;
  --case-pw-a-accent: #16a34a;
  --case-pw-b-accent: #dc2626;
  --case-pw-tie-accent: #d97706;
  --case-pw-error-accent: #94a3b8;
  color-scheme: light;
}
:root[data-theme="dark"] {
  --bg: #0b1220;
  --surface: #0f1729;
  --surface-2: #162033;
  --surface-3: #1e293b;
  --border: #1e2c44;
  --border-strong: #334155;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --text-soft: #cbd5e1;
  --accent: #60a5fa;
  --accent-strong: #93c5fd;
  --accent-soft: #1e3a8a;
  --success: #4ade80;
  --success-soft: #14532d;
  --success-border: #166534;
  --danger: #f87171;
  --danger-soft: #7f1d1d;
  --danger-border: #991b1b;
  --warning: #fbbf24;
  --warning-soft: #78350f;
  --warning-border: #92400e;
  --neutral-soft: #1e293b;
  --neutral-border: #334155;
  --code-bg: #1e293b;
  --shadow: 0 1px 3px rgba(0,0,0,.4), 0 1px 2px rgba(0,0,0,.3);
  --shadow-strong: 0 4px 14px rgba(0,0,0,.5), 0 1px 3px rgba(0,0,0,.35);
  --diff-add-bg: rgba(74,222,128,.08);
  --diff-add-strong: rgba(74,222,128,.28);
  --diff-del-bg: rgba(248,113,113,.08);
  --diff-del-strong: rgba(248,113,113,.28);
  --diff-hdr-bg: #1e293b;
  --case-pass-accent: #4ade80;
  --case-fail-accent: #f87171;
  --case-pw-a-accent: #4ade80;
  --case-pw-b-accent: #f87171;
  --case-pw-tie-accent: #fbbf24;
  --case-pw-error-accent: #64748b;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html { background: var(--bg); }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 15px; line-height: 1.55; max-width: 1200px; margin: 0 auto; padding: 1.5em 1.5em 4em; color: var(--text); background: var(--bg); transition: background-color .15s ease, color .15s ease; }
h1 { padding-bottom: 0; margin: 0 0 0.55em; font-size: 1.9em; letter-spacing: -0.015em; font-weight: 700; color: var(--accent-strong); }
.report-header { margin: 0 0 1.6em; padding-bottom: 1.1em; border-bottom: 2px solid var(--border-strong); }
.header-meta { display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: center; }
.meta-chip { display: inline-flex; align-items: center; gap: 8px; background: var(--surface); border: 1px solid var(--border-strong); border-radius: 999px; padding: 5px 13px 5px 12px; font-size: 0.95em; color: var(--text); box-shadow: var(--shadow); line-height: 1.4; }
.meta-chip .meta-label { font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.07em; color: var(--text-muted); font-weight: 700; }
.meta-chip code { background: var(--code-bg); padding: 1px 7px; border-radius: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.92em; color: var(--text); }
h2 { margin-top: 1.5em; letter-spacing: -0.005em; }
table { border-collapse: separate; border-spacing: 0; width: 100%; margin: 1em 0; font-variant-numeric: tabular-nums; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
th, td { border-bottom: 1px solid var(--border); padding: 9px 12px; text-align: left; }
tr:last-child td { border-bottom: none; }
th { background: var(--surface-2); font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); font-weight: 600; }
tbody tr:nth-child(even) td { background: color-mix(in srgb, var(--surface-2) 50%, transparent); }
tbody tr:hover td { background: var(--accent-soft); }
.pass, .fail, .warn, .skip { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 0.85em; font-weight: 600; line-height: 1.4; border: 1px solid transparent; }
.pass { color: var(--success); background: var(--success-soft); border-color: var(--success-border); }
.fail { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-border); }
.warn { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-border); }
.skip { color: var(--text-muted); background: var(--neutral-soft); border-color: var(--neutral-border); font-weight: 500; }
.metric-row td:last-child { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.judge-type { font-size: 0.85em; color: var(--text-muted); }
.subsection-heading { margin-top: 1.4em; font-size: 0.95em; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
.model-heading { margin: 1.2em 0 0.4em; font-size: 0.92em; font-weight: 600; color: var(--text-soft); }
.model-heading code { background: var(--code-bg); padding: 2px 8px; border-radius: 4px; font-size: 0.95em; font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text); }
.config-table { table-layout: fixed; }
.config-table th:first-child, .config-table td:first-child { width: 22%; }
.config-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 0.9em 1.6em; margin: 0.4em 0 0.8em; padding: 0; }
.kv { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.kv dt { font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; margin: 0; }
.kv dd { margin: 0; font-weight: 500; font-variant-numeric: tabular-nums; word-break: break-word; line-height: 1.35; color: var(--text); }
.kv dd.bl { color: var(--text-muted); font-size: 0.85em; font-weight: 400; }
.kv dd.bl::before { content: "vs "; opacity: 0.7; }
.kv dd.bl .delta { font-weight: 600; font-size: 0.95em; padding-left: 4px; }
details.eval-params { margin: 1.2em 0 0; padding: 0; }
details.eval-params > summary { cursor: pointer; padding: 6px 0; list-style: none; display: flex; align-items: center; gap: 8px; user-select: none; min-width: 0; }
details.eval-params > summary::-webkit-details-marker { display: none; }
details.eval-params > summary::before { content: "▸"; font-size: 0.9em; transition: transform .15s ease; display: inline-block; color: var(--text-muted); flex-shrink: 0; }
details.eval-params[open] > summary::before { transform: rotate(90deg); }
details.eval-params > summary:hover .eval-params-label { color: var(--accent); }
.eval-params-label { font-size: 0.95em; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; flex-shrink: 0; }
.eval-params-preview { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.85em; color: var(--text-soft); padding-left: 8px; border-left: 1px solid var(--border-strong); margin-left: 2px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
details.eval-params[open] .eval-params-preview { display: none; }
details.eval-params > .config-grid { margin-top: 0.5em; }
details.eval-params > .run-command { margin: 0.6em 0 0; }
.run-command { margin: 0.4em 0 1em; }
.run-command-label { display: block; font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; margin-bottom: 4px; }
.run-command pre { margin: 0; background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; overflow-x: auto; font-size: 0.85em; line-height: 1.4; }
.run-command code { background: transparent; padding: 0; font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text); white-space: pre; }
.usage-table th code { background: var(--code-bg); padding: 2px 7px; border-radius: 4px; font-size: 0.95em; font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text); text-transform: none; letter-spacing: 0; font-weight: 500; }
.usage-table td { font-variant-numeric: tabular-nums; }
.usage-table .cur-val { display: block; font-weight: 500; }
.usage-table .bl-val { display: block; font-size: 0.82em; color: var(--text-muted); margin-top: 1px; }
.usage-table .bl-val::before { content: "vs "; opacity: 0.7; }
.usage-table .delta { font-weight: 600; font-size: 0.9em; padding-left: 4px; }
.delta-good { color: var(--success); }
.delta-bad { color: var(--danger); }
.delta-flat { color: var(--text-muted); font-weight: 500; }
.baseline-row td { background: var(--surface-2); font-style: italic; color: var(--text-muted); }
.rationale { font-size: 0.88em; color: var(--text-soft); }
.rationale p { margin: 0 0 0.5em; line-height: 1.5; }
.rationale p:last-child { margin-bottom: 0; }
.rationale ul, .rationale ol { margin: 0.3em 0 0.5em 1.2em; padding: 0; }
.rationale li { margin: 0.2em 0; }
.rationale code { background: var(--code-bg); padding: 1px 5px; border-radius: 3px; font-size: 0.9em; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.rationale strong { color: var(--text); }
details.case { margin: 1em 0; border: 1px solid var(--border); border-left-width: 4px; border-radius: 8px; padding: 1em 1.2em; background: var(--surface); box-shadow: var(--shadow); }
details.case.case-pass { border-left-color: var(--case-pass-accent); }
details.case.case-fail { border-left-color: var(--case-fail-accent); }
details.case.case-pw-a { border-left-color: var(--case-pw-a-accent); }
details.case.case-pw-b { border-left-color: var(--case-pw-b-accent); }
details.case.case-pw-tie { border-left-color: var(--case-pw-tie-accent); }
details.case.case-pw-error { border-left-color: var(--case-pw-error-accent); }
details.case > summary { cursor: pointer; font-weight: 600; padding: 0.2em 0; font-size: 1.02em; list-style: none; display: flex; align-items: center; gap: 0.6em; }
details.case > summary::-webkit-details-marker { display: none; }
details.case > summary::before { content: "▸"; color: var(--text-muted); transition: transform .15s ease; display: inline-block; }
details.case[open] > summary::before { transform: rotate(90deg); }
details.case > summary:hover { color: var(--accent); }
.info-box { background: var(--accent-soft); border: 1px solid var(--border); border-radius: 6px; padding: 0.8em 1em; margin: 0.6em 0; font-size: 0.9em; }
.feedback-box { background: var(--warning-soft); border: 1px solid var(--warning-border); border-radius: 6px; padding: 0.8em 1em; margin: 0.6em 0; font-size: 0.9em; color: var(--text); }
.file-badge { display: inline-block; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.82em; background: var(--surface-2); border: 1px solid var(--border-strong); border-radius: 5px; padding: 3px 10px; margin: 1em 0 0.5em 0; color: var(--text-soft); }
details.file-entry { margin: 1em 0 0.5em 0; }
details.file-entry > summary { cursor: pointer; list-style: none; user-select: none; }
details.file-entry > summary::-webkit-details-marker { display: none; }
details.file-entry > summary::before { content: "\\25B8"; margin-right: 0.4em; color: var(--text-muted); }
details.file-entry[open] > summary::before { content: "\\25BE"; }
details.file-entry > summary .file-name { display: inline-block; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.82em; background: var(--surface-2); border: 1px solid var(--border-strong); border-radius: 5px; padding: 3px 10px; color: var(--text-soft); }
details.file-entry[open] > summary { margin-bottom: 0.5em; }
details.file-entry > summary .file-meta { color: var(--text-muted); font-size: 0.9em; margin-left: 0.6em; }
details.file-entry > summary .fm-chip, .file-badge .fm-chip { display: inline-block; font-size: 0.82em; padding: 1px 7px; margin-left: 0.4em; border-radius: 999px; background: var(--accent-soft); color: var(--accent-strong); border: 1px solid var(--border); vertical-align: middle; }
.file-badge .file-meta { color: var(--text-muted); font-weight: 400; margin-left: 0.4em; }
/* Input vs Output group accents */
details.io-group { border-left: 3px solid var(--border-strong); padding-left: 0.8em; margin: 0.8em 0; }
details.io-group > summary { cursor: pointer; font-weight: 600; list-style: none; }
details.io-group > summary::-webkit-details-marker { display: none; }
details.io-group > summary::before { content: "▸"; color: var(--text-muted); display: inline-block; margin-right: 0.4em; }
details.io-group[open] > summary::before { content: "▾"; }
details.io-input { border-left-color: var(--text-muted); }
details.io-output { border-left-color: var(--accent); }
details.io-diff { border-left-color: var(--warning); }
/* the raw/rendered toggle flips [hidden]; keep it authoritative over display rules */
[hidden] { display: none !important; }
/* Line-number gutter (copy-safe: the number column is not selectable). The
   WRAPPER is the single scroll box, so the gutter and content scroll together
   and stay aligned - the gutter can't run past the content's scroll viewport. */
.numwrap { display: flex; align-items: stretch; max-height: 480px; overflow: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--surface-2); }
.numwrap .lncol { flex: 0 0 auto; margin: 0; padding: 0.9em 0.6em; border: none; border-right: 1px solid var(--border); border-radius: 0; background: var(--surface-3); color: var(--text-muted); text-align: right; user-select: none; font-size: 0.88em; line-height: 1.5; font-family: ui-monospace, "SF Mono", Menlo, monospace; white-space: pre; }
.numwrap pre.output { margin: 0; padding: 0.9em 1em; border: none; border-radius: 0; background: transparent; flex: 1 0 auto; line-height: 1.5; white-space: pre; overflow: visible; max-height: none; }
.md-toggle { font-size: 0.78em; padding: 2px 9px; margin-bottom: 0.5em; border-radius: 5px; border: 1px solid var(--border-strong); background: var(--surface-2); color: var(--text-soft); cursor: pointer; }
.md-rendered { border: 1px solid var(--border); border-radius: 6px; padding: 0.6em 1em; background: var(--surface); max-height: 520px; overflow-y: auto; }
.diff-stat { font-size: 0.82em; font-weight: 600; color: var(--text-muted); margin-left: 0.4em; }
.pw-badge { display: inline-block; font-size: 0.78em; font-weight: 700; padding: 2px 9px; border-radius: 999px; margin-left: 8px; border: 1px solid transparent; letter-spacing: 0.04em; }
.pw-win { background: var(--success-soft); color: var(--success); border-color: var(--success-border); }
.pw-loss { background: var(--danger-soft); color: var(--danger); border-color: var(--danger-border); }
.pw-tie { background: var(--warning-soft); color: var(--warning); border-color: var(--warning-border); }
.pw-error { background: var(--neutral-soft); color: var(--text-muted); border-color: var(--neutral-border); }
.stab-wrap { display: inline-flex; align-items: center; gap: 5px; }
.stab-bar { vertical-align: middle; }
.stab-label { font-size: 0.8em; color: var(--text-muted); }
.ascii-range { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 0.85em; letter-spacing: 1px; color: var(--text-muted); margin-left: 6px; white-space: pre; }
.ascii-range .med { color: var(--accent); font-weight: 700; }
.sample-tabs { display: flex; gap: 0; border-bottom: 2px solid var(--border); margin-bottom: 0.5em; }
.sample-tab { padding: 3px 10px; font-size: 0.82em; cursor: pointer; border: 1px solid transparent; border-bottom: none; border-radius: 4px 4px 0 0; color: var(--text-muted); background: none; }
.sample-tab:hover { background: var(--surface-2); }
.sample-tab[aria-selected="true"] { color: var(--text); background: var(--surface); border-color: var(--border); border-bottom: 2px solid var(--surface); margin-bottom: -2px; font-weight: 600; }
.sample-tab .tab-score { font-weight: 700; margin-right: 4px; }
.sample-panel { display: none; }
.sample-panel.active { display: block; }
.diff-table { width: 100%; border-collapse: collapse; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.82em; table-layout: fixed; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.diff-table td { padding: 1px 6px; vertical-align: top; white-space: pre-wrap; word-wrap: break-word; border: 1px solid var(--border); }
.diff-table .ln { width: 35px; min-width: 35px; color: var(--text-muted); text-align: right; background: var(--surface-2); user-select: none; white-space: nowrap; }
.diff-table .left, .diff-table .right { width: calc(50% - 35px); background: var(--surface); }
.diff-table .sep { width: 1px; padding: 0; background: var(--border-strong); }
.diff-table tr.mod .left { background: var(--diff-del-bg); }
.diff-table tr.mod .right { background: var(--diff-add-bg); }
.diff-table tr.add .right { background: var(--diff-add-bg); }
.diff-table tr.add .left { background: var(--surface-2); }
.diff-table tr.del .left { background: var(--diff-del-bg); }
.diff-table tr.del .right { background: var(--surface-2); }
.diff-table tr.hdr td { background: var(--diff-hdr-bg); color: var(--text-muted); font-weight: 600; }
.diff-table .wdel { background: var(--diff-del-strong); border-radius: 2px; padding: 0 2px; }
.diff-table .wadd { background: var(--diff-add-strong); border-radius: 2px; padding: 0 2px; }
pre.output { background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 0.9em 1em; font-size: 0.88em; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; color: var(--text); font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.html-preview { width: 100%; height: 80vh; max-height: 2000px; border: 1px solid var(--border-strong); border-radius: 6px; margin: 0.5em 0; background: #fff; color-scheme: light; }
.section, .analysis { background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent); border-radius: 8px; padding: 1.3em 1.6em 1.4em; margin: 1.5em 0; box-shadow: var(--shadow); }
.section > h2:first-child, h2.section-heading { margin: 0 0 0.7em; padding: 0; border: none; font-size: 1.3em; color: var(--accent-strong); letter-spacing: -0.005em; }
h2.section-heading { margin: 1.8em 0 0.8em; }
.section-intro { color: var(--text-muted); margin: 0 0 1em; font-size: 0.92em; }
.section > table:last-child, .section > details:last-child { margin-bottom: 0; }
.analysis { border-left-width: 5px; margin-bottom: 2em; box-shadow: var(--shadow-strong); }
.analysis-banner { display: flex; align-items: center; gap: 0.7em; margin-bottom: 0.3em; }
.analysis-banner h2 { margin: 0; padding: 0; border: none; font-size: 1.4em; color: var(--accent-strong); letter-spacing: -0.005em; }
.analysis-body h2 { margin-top: 1.6em; padding-bottom: 0.35em; font-size: 1.1em; color: var(--accent-strong); border-bottom: 1px solid var(--border); }
.analysis-body h2:first-of-type { margin-top: 0; font-size: 1.2em; border-bottom-width: 2px; padding-bottom: 0.4em; border-bottom-color: var(--accent); }
.analysis-body h3 { margin-top: 1em; color: var(--text-soft); font-size: 1em; }
.analysis-body li { margin: 0.3em 0; line-height: 1.55; }
.analysis-body p { line-height: 1.6; }
.analysis-body code { background: var(--code-bg); padding: 1px 6px; border-radius: 4px; font-size: 0.88em; font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text); }
.analysis-body table { font-size: 0.92em; }
.analysis-body hr { border: none; border-top: 1px dashed var(--border-strong); margin: 1.4em 0; }
.section-intro code { background: var(--code-bg); padding: 1px 5px; border-radius: 4px; font-size: 0.92em; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
#report-toolbar { position: fixed; top: 12px; right: 16px; z-index: 1000; display: flex; gap: 4px; align-items: center; }
#report-toolbar button { background: transparent; color: var(--text-muted); border: none; border-radius: 6px; width: 36px; height: 36px; padding: 0; cursor: pointer; font-size: 16px; display: inline-flex; align-items: center; justify-content: center; transition: background-color .15s ease, color .15s ease; position: relative; }
#report-toolbar button:hover { background: var(--surface-2); color: var(--text); }
#report-toolbar button:active { background: var(--surface-3); }
#report-toolbar button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; border-radius: 6px; }
#report-toolbar button svg { width: 18px; height: 18px; fill: currentColor; pointer-events: none; }
#report-toolbar .toolbar-sep { width: 1px; height: 20px; background: var(--border-strong); margin: 0 4px; }
#report-toolbar .toolbar-tip { position: absolute; bottom: -32px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg); font-size: 12px; font-weight: 500; padding: 4px 10px; border-radius: 4px; white-space: nowrap; opacity: 0; pointer-events: none; transition: opacity .15s ease; z-index: 1; }
#report-toolbar .toolbar-tip::before { content: ""; position: absolute; top: -4px; left: 50%; transform: translateX(-50%); border-left: 5px solid transparent; border-right: 5px solid transparent; border-bottom: 4px solid var(--text); }
#report-toolbar button:hover .toolbar-tip { opacity: 1; }
#report-toolbar .toolbar-toast { position: fixed; top: 16px; right: 16px; background: var(--surface); border: 1px solid var(--success-border); border-left: 4px solid var(--success); border-radius: 6px; padding: 10px 16px 10px 12px; font-size: 13px; font-weight: 500; color: var(--success); box-shadow: var(--shadow-strong); display: flex; align-items: center; gap: 8px; transform: translateY(-8px); opacity: 0; pointer-events: none; transition: opacity .2s ease, transform .2s ease; z-index: 1001; }
#report-toolbar .toolbar-toast.show { opacity: 1; transform: translateY(0); pointer-events: auto; }
#report-toolbar .toolbar-toast svg { width: 16px; height: 16px; fill: var(--success); flex-shrink: 0; }
@media print {
  :root { color-scheme: light; --bg: #fff; --surface: #fff; --text: #000; --text-muted: #444; }
  #report-toolbar { display: none; }
  .section, .analysis, details.case { box-shadow: none; break-inside: avoid; }
}

/* Image artifacts */
.img-artifact { margin: 0.5em 0; }
.img-artifact img { max-width: 100%; height: auto; border: 1px solid var(--border); border-radius: 6px; background: #fff; }
.img-label { display: inline-block; font-size: 0.78em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; padding: 2px 9px; border-radius: 999px; margin-bottom: 6px; }
.img-label-gen { background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent); }
.img-label-ref { background: var(--success-soft); color: var(--success); border: 1px solid var(--success-border); }
.img-label-bl { background: var(--warning-soft); color: var(--warning); border: 1px solid var(--warning-border); }

/* Comparison widget */
.img-compare { margin: 0.8em 0; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--surface); }
.img-compare-tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); background: var(--surface-2); }
.img-compare-tab { padding: 7px 16px; font-size: 0.85em; font-weight: 600; color: var(--text-muted); background: transparent; border: none; cursor: pointer; border-bottom: 2px solid transparent; transition: color .15s, border-color .15s; }
.img-compare-tab:hover { color: var(--text); }
.img-compare-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.img-compare-panel { display: none; padding: 1em; }
.img-compare-panel.active { display: block; }

/* Side-by-side */
.img-compare-sbs { display: flex; gap: 1em; }
.img-compare-sbs > div { flex: 1; min-width: 0; }
.img-compare-sbs img { width: 100%; height: auto; border: 1px solid var(--border); border-radius: 4px; background: #fff; }

/* Swipe */
.img-compare-swipe-imgs { display: grid; border: 1px solid var(--border); border-radius: 4px; background: #fff; overflow: hidden; }
.img-compare-swipe-imgs > img { grid-area: 1/1; display: block; width: 100%; height: auto; }
.img-compare-swipe-imgs .swipe-ref { grid-area: 1/1; z-index: 1; background: #fff; clip-path: inset(0 50% 0 0); }
.img-compare-swipe-imgs .swipe-ref img { display: block; width: 100%; height: auto; }
.img-compare-swipe-imgs .swipe-line { grid-area: 1/1; z-index: 2; justify-self: start; width: 3px; background: var(--accent); pointer-events: none; margin-left: 50%; }
.img-compare-swipe input[type="range"] { display: block; width: 100%; margin: 8px 0 0; accent-color: var(--accent); }
.img-compare-swipe-labels { display: flex; justify-content: space-between; padding: 4px 1em; font-size: 0.82em; color: var(--text-muted); }

/* Onion */
.img-compare-onion-wrap { display: grid; border: 1px solid var(--border); border-radius: 4px; background: #fff; overflow: hidden; }
.img-compare-onion-wrap > img { grid-area: 1/1; display: block; width: 100%; height: auto; }
.img-compare-onion-wrap .onion-top { grid-area: 1/1; background: #fff; }
.img-compare-onion-wrap .onion-top img { display: block; width: 100%; height: auto; }
.img-compare-onion input[type="range"] { display: block; width: 100%; margin: 8px 0 0; accent-color: var(--accent); }
.img-compare-onion .onion-label { text-align: center; font-size: 0.82em; color: var(--text-muted); margin-top: 2px; }

/* Diagram source collapsible */
/* Metrics table */
.metrics-table { font-size: 0.88em; margin: 0.5em 0; border-collapse: collapse; }
.metrics-table th { text-align: left; padding: 4px 12px; background: var(--surface-2); border-bottom: 1px solid var(--border); font-weight: 600; color: var(--text-soft); }
.metrics-table td { padding: 4px 12px; border-bottom: 1px solid var(--border); }
.metrics-table td:last-child { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-weight: 600; }

.diagram-source { margin-top: 0.4em; }
.diagram-source summary { font-size: 0.85em; color: var(--text-muted); cursor: pointer; padding: 4px 0; }
.diagram-source summary:hover { color: var(--text); }
.diagram-source pre { max-height: 400px; overflow-y: auto; }

.reward-overview { overflow-x: auto; margin: 0.8em 0 1.5em; }
.reward-overview table { font-size: 0.88em; }
.reward-overview th { font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.05em; }
.reward-overview th.rv-rotate {
  writing-mode: vertical-rl; transform: rotate(180deg);
  height: 120px; min-width: 28px; padding: 8px 4px;
  font-size: 0.7em; letter-spacing: 0.04em;
}
.reward-overview td { text-align: center; padding: 7px 8px; }
.reward-overview td:first-child { text-align: left; font-weight: 500; font-size: 0.9em; }
.rv-reward { font-weight: 700; font-size: 1.05em; font-variant-numeric: tabular-nums; }
.reward-overview .rv-avg-row td {
  background: var(--surface-3) !important;
  font-weight: 700; border-top: 2px solid var(--border-strong);
}
.rv-gate-hdr { background: var(--success-soft) !important; color: var(--success) !important; border-bottom: 2px solid var(--success-border) !important; }
.rv-llm-hdr { background: var(--accent-soft) !important; color: var(--accent) !important; border-bottom: 2px solid var(--accent) !important; }
.rv-other-hdr { background: var(--warning-soft) !important; color: var(--warning) !important; border-bottom: 2px solid var(--warning-border) !important; }
"""

THEME_SCRIPT = """
(function () {
  try {
    var stored = localStorage.getItem('eval-report-theme');
    var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    var theme = stored || (prefersDark ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
"""

TOGGLE_SCRIPT = """
(function () {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  var sunSvg = '<svg viewBox="0 0 16 16"><path d="M8 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6z"/><path d="M8 0a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 0zm0 13a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 13zm8-5a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 16 8zM3 8a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 3 8zm10.657-5.657a.5.5 0 0 1 0 .707l-1.414 1.415a.5.5 0 1 1-.707-.708l1.414-1.414a.5.5 0 0 1 .707 0zm-9.193 9.193a.5.5 0 0 1 0 .707L3.05 13.657a.5.5 0 0 1-.707-.707l1.414-1.414a.5.5 0 0 1 .707 0zm9.193 2.121a.5.5 0 0 1-.707 0l-1.414-1.414a.5.5 0 0 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .707zM4.464 4.465a.5.5 0 0 1-.707 0L2.343 3.05a.5.5 0 1 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .708z"/></svg>';
  var moonSvg = '<svg viewBox="0 0 16 16"><path d="M6 .278a.768.768 0 0 1 .08.858 7.208 7.208 0 0 0-.878 3.46c0 4.021 3.278 7.277 7.318 7.277.527 0 1.04-.055 1.533-.16a.787.787 0 0 1 .81.316.733.733 0 0 1-.031.893A8.349 8.349 0 0 1 8.344 16C3.734 16 0 12.286 0 7.71 0 4.266 2.114 1.312 5.124.06A.752.752 0 0 1 6 .278z"/></svg>';
  function paint() {
    var t = document.documentElement.getAttribute('data-theme') || 'light';
    var svg = t === 'dark' ? sunSvg : moonSvg;
    btn.innerHTML = svg + '<span class="toolbar-tip">' + (t === 'dark' ? 'Light theme' : 'Dark theme') + '</span>';
    btn.setAttribute('aria-label', t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  }
  btn.addEventListener('click', function () {
    var current = document.documentElement.getAttribute('data-theme') || 'light';
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('eval-report-theme', next); } catch (e) {}
    paint();
  });
  paint();
})();
"""

SHARE_SCRIPT = """
(function () {
  var toast = document.getElementById('toolbar-toast');
  var toastMsg = document.getElementById('toolbar-toast-msg');
  var toastTimer;
  function showToast(msg) {
    clearTimeout(toastTimer);
    toastMsg.textContent = msg;
    toast.classList.add('show');
    toastTimer = setTimeout(function () { toast.classList.remove('show'); }, 2400);
  }

  var btnDownload = document.getElementById('btn-download');
  if (btnDownload) {
    btnDownload.addEventListener('click', function () {
      var html = document.documentElement.outerHTML;
      var blob = new Blob(['<!DOCTYPE html>\\n' + html], { type: 'text/html' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = document.title.replace(/[^a-zA-Z0-9_\\-]/g, '_') + '.html';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast('Report downloaded');
    });
  }

  var btnCopy = document.getElementById('btn-copy');
  if (btnCopy) {
    btnCopy.addEventListener('click', function () {
      var path = window.location.pathname || window.location.href;
      if (path.startsWith('file://')) path = path.slice(7);
      navigator.clipboard.writeText(path).then(function () {
        showToast('Path copied to clipboard');
      }).catch(function () {
        var ta = document.createElement('textarea');
        ta.value = path;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast('Path copied to clipboard');
      });
    });
  }
})();
"""

IMAGE_COMPARE_SCRIPT = """
(function () {
  document.querySelectorAll('.img-compare').forEach(function (widget) {
    var tabs = widget.querySelectorAll('.img-compare-tab');
    var panels = widget.querySelectorAll('.img-compare-panel');
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) { t.classList.remove('active'); });
        panels.forEach(function (p) { p.classList.remove('active'); });
        tab.classList.add('active');
        var target = widget.querySelector('#' + tab.dataset.panel);
        if (target) target.classList.add('active');
      });
    });
    var swipe = widget.querySelector('.img-compare-swipe');
    if (swipe) {
      (function (sw) {
        var sl = sw.querySelector('input[type="range"]');
        var ref = sw.querySelector('.swipe-ref');
        var ln = sw.querySelector('.swipe-line');
        if (sl && ref) {
          function update() {
            var p = sl.value;
            ref.style.clipPath = 'inset(0 ' + (100 - p) + '% 0 0)';
            if (ln) ln.style.marginLeft = p + '%';
          }
          sl.addEventListener('input', update);
          update();
        }
      })(swipe);
    }
    var onion = widget.querySelector('.img-compare-onion');
    if (onion) {
      (function (on) {
        var sl = on.querySelector('input[type="range"]');
        var top = on.querySelector('.onion-top');
        var lbl = on.querySelector('.onion-label');
        if (sl && top) {
          function update() {
            top.style.opacity = sl.value / 100;
            if (lbl) lbl.textContent = 'Generated opacity: ' + sl.value + '%';
          }
          sl.addEventListener('input', update);
          update();
        }
      })(onion);
    }
  });
})();
"""

SAMPLE_TABS_SCRIPT = """
(function () {
  document.querySelectorAll('.sample-tabs').forEach(function (bar) {
    bar.querySelectorAll('.sample-tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        var group = tab.closest('td');
        group.querySelectorAll('.sample-tab').forEach(function (t) {
          t.setAttribute('aria-selected', 'false');
        });
        group.querySelectorAll('.sample-panel').forEach(function (p) {
          p.classList.remove('active');
        });
        tab.setAttribute('aria-selected', 'true');
        var target = group.querySelector('#' + tab.dataset.panel);
        if (target) target.classList.add('active');
      });
    });
  });
})();
"""


def _render_header(config, run_id, run_result, baseline_id=None, title=None):
    # Precedence: explicit --title > config `title` (sibling to name/description) > default.
    title = title or config.get("title") or "Agent Eval Report"
    skill = config.get("skill", "")
    date = run_result.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))

    chips = []
    if skill:
        chips.append(
            f'<span class="meta-chip"><span class="meta-label">Skill</span>'
            f'<code>{_esc(skill)}</code></span>'
        )
    chips.append(
        f'<span class="meta-chip"><span class="meta-label">Run</span>'
        f'<code>{_esc(run_id)}</code></span>'
    )
    if baseline_id:
        chips.append(
            f'<span class="meta-chip meta-baseline"><span class="meta-label">Baseline</span>'
            f'<code>{_esc(baseline_id)}</code></span>'
        )
    chips.append(
        f'<span class="meta-chip"><span class="meta-label">Date</span>'
        f'{_esc(str(date)[:19])}</span>'
    )

    return (
        '<header class="report-header">\n'
        f'  <h1>{_esc(title)}</h1>\n'
        f'  <div class="header-meta">{"".join(chips)}</div>\n'
        '</header>\n'
    )


def _fmt_judge_cost(summary):
    """`Judge cost` cell from summary.yaml `judge_usage` (spec 014), or None
    when the run recorded no judge usage."""
    usage = (summary or {}).get("judge_usage") or {}
    if not usage:
        return None
    cost = usage.get("judge_cost_usd")
    requests = usage.get("requests") or 0
    unpriced = usage.get("requests_missing_cost") or 0
    text = f"${cost:.2f}" if isinstance(cost, (int, float)) else "n/a"
    detail = f"{requests} calls" + (f", {unpriced} unpriced" if unpriced else "")
    return f"{text} ({detail})"


def _fmt_total_cost(summary):
    """`Total cost` cell: agent + judge spend under the null-cost arithmetic —
    a null total is shown as such with the source that was numeric."""
    summary = summary or {}
    source = summary.get("total_cost_source")
    if not source or source == "none":
        return None
    total = summary.get("total_cost_usd")
    if isinstance(total, (int, float)):
        return f"${total:.2f} ({source})"
    return f"n/a ({source})"


def _render_cost_rows(summary, baseline_summary, has_bl):
    """Judge-cost and total-cost rows of the run configuration grid."""
    html = ""
    for label, fmt in (("Judge Cost", _fmt_judge_cost), ("Total Cost", _fmt_total_cost)):
        cur = fmt(summary)
        bl = fmt(baseline_summary) if has_bl else None
        if cur is None and bl is None:
            continue
        html += '<div class="kv">'
        html += f'<dt>{label}</dt>'
        html += f'<dd>{_esc(cur or "—")}</dd>'
        if has_bl and (bl or "—") != (cur or "—"):
            html += f'<dd class="bl">{_esc(bl or "—")}</dd>'
        html += '</div>\n'
    return html


def _fmt_usd(value, digits=2):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"${value:.{digits}f}"


def _cost_provenance_rows(run_result):
    """(label, value) rows for the cost-provenance panel (spec 014): shown
    only when the run carries a `cost_source`, i.e. it was reconciled or the
    runner labelled its own number."""
    rr = run_result or {}
    source = rr.get("cost_source")
    if not source:
        return []
    rows = [("Cost source", source)]
    confidence = rr.get("cost_confidence")
    cov = rr.get("cost_coverage") or {}
    if confidence or cov:
        detail = []
        if cov.get("requests") is not None:
            detail.append(f"{cov.get('requests_priced', 0)} / {cov['requests']} requests priced")
        if cov.get("requests_missing_cost"):
            detail.append(f"{cov['requests_missing_cost']} unpriced")
        rows.append(("Cost confidence", " — ".join(x for x in [confidence, ", ".join(detail)] if x) or "—"))
    cost, estimate = rr.get("cost_usd"), rr.get("cost_usd_estimate")
    if isinstance(estimate, (int, float)) and estimate != cost:
        factor = (f" (×{estimate / cost:.0f})" if isinstance(cost, (int, float)) and cost > 0 else "")
        rows.append(("Cost (runner estimate)", f"{_fmt_usd(estimate)}{factor}"))
    delta = cov.get("key_usage_delta_usd")
    if isinstance(delta, (int, float)):
        text = _fmt_usd(delta, 4)
        if isinstance(cost, (int, float)) and delta > 0:
            text += f" ({(cost - delta) / delta:+.1%})"
        rows.append(("Key-usage cross-check", text))
    hook = rr.get("hook_cost_usd")
    if isinstance(hook, (int, float)) and hook > 0:
        rows.append(("Hook cost", _fmt_usd(hook, 4)))
    budget = rr.get("budget") or {}
    if budget:
        parts = []
        if budget.get("invocation_usd") is not None:
            parts.append(f"invocation {_fmt_usd(budget['invocation_usd'])}")
        if budget.get("cli_cap_usd") is not None:
            parts.append(f"CLI cap {_fmt_usd(budget['cli_cap_usd'])}")
        if budget.get("run_usd") is not None:
            parts.append(f"run {_fmt_usd(budget['run_usd'])}")
        if budget.get("enforcement"):
            parts.append(f"enforcement: {budget['enforcement']}")
        if budget.get("exceeded"):
            parts.append(f"EXCEEDED ({budget['exceeded']}, {budget.get('exceeded_reason')})")
        rows.append(("Budget", ", ".join(parts) or "—"))
    provider = rr.get("provider") or {}
    if provider:
        rows.append(("Key", f"{provider.get('key_scope', '—')} {provider.get('key_hash') or ''}"
                            + (" — exposed to the agent" if provider.get("key_exposed_to_agent") else "")))
    return rows


def _routing_rows(run_result):
    routing = (run_result or {}).get("routing") or {}
    if not routing:
        return []
    rows = [("Enforcement", routing.get("enforcement") or "none")]
    declared = routing.get("declared") or {}
    if declared:
        rows.append(("Declared pins", "; ".join(
            f"{key}: " + ", ".join(f"{k}={v}" for k, v in spec.items()) for key, spec in declared.items())))
    served = routing.get("served") or {}
    if served:
        rows.append(("Providers served", ", ".join(f"{k}: {v}" for k, v in served.items())))
    audit = f"{routing.get('compliant', 0)} compliant / {routing.get('audited', 0)} audited"
    if routing.get("violations"):
        audit += f", {len(routing['violations'])} violation(s)"
    if routing.get("unattributed"):
        audit += f", {routing['unattributed']} unattributed"
    if routing.get("audit_complete") is False:
        audit += " — incomplete"
    rows.append(("Audit", audit))
    if routing.get("snapshot"):
        rows.append(("Snapshot", f"{routing['snapshot']} ({routing.get('sha') or '—'})"))
    return rows


def _render_cost_provenance(run_result):
    """Cost-provenance and routing-audit panel plus the banner for the loud
    states (cost unavailable, degraded routing, incomplete audit, budget
    exceeded). Empty for runs without a cost_source."""
    rows = _cost_provenance_rows(run_result)
    if not rows:
        return ""
    rr = run_result or {}
    banners = []
    if rr.get("cost_source") == "unavailable":
        banners.append(("red", "Cost unavailable: no /generation row and no key-usage delta "
                               "landed — cost_usd is null (the runner estimate is not spend)"))
    routing = rr.get("routing") or {}
    if routing.get("degraded"):
        banners.append(("red", f"Routing degraded: {routing.get('degraded_reason')}"))
    elif routing.get("violations"):
        banners.append(("amber", f"Routing: {len(routing['violations'])} violation(s) under policy: warn"))
    if routing.get("audit_complete") is False:
        banners.append(("red", "Routing audit incomplete: unattributed generations"))
    if (rr.get("budget") or {}).get("exceeded"):
        banners.append(("red", f"Budget exceeded ({rr['budget'].get('exceeded')}: "
                               f"{rr['budget'].get('exceeded_reason')})"))
    html = '<h2>Cost Provenance</h2>\n'
    for tone, text in banners:
        color = "#d1242f" if tone == "red" else "#bf8700"
        html += (f'<div class="banner banner-{tone}" style="border-left:4px solid {color};'
                 f'padding:6px 10px;margin:6px 0">{_esc(text)}</div>\n')
    html += '<dl class="config-grid">\n'
    for label, value in rows + _routing_rows(run_result):
        html += f'<div class="kv"><dt>{_esc(label)}</dt><dd>{_esc(str(value))}</dd></div>\n'
    html += "</dl>\n"
    warnings = rr.get("cost_warnings") or []
    if warnings:
        html += "<ul class=\"cost-warnings\">" + "".join(f"<li>{_esc(w)}</li>" for w in warnings) + "</ul>\n"
    return html


def _render_run_config(run_result, baseline_result=None, summary=None,
                       baseline_summary=None):
    has_bl = baseline_result is not None
    fields = [
        ("Model", "model"),
        ("Subagent Model", "subagent_model"),
        ("Effort", "effort"),
        ("Agent", "agent"),
        ("Agent Version", "agent_version"),
        ("Duration", "duration_s"),
        ("Cost", "cost_usd"),
        ("Turns", "num_turns"),
        ("Exit Code", "exit_code"),
    ]
    # Numeric fields where a baseline delta is meaningful (lower is better)
    numeric_lower_better = {"duration_s", "cost_usd", "num_turns"}
    # Optional fields that are hidden when not set on either run, to avoid
    # showing rows of "—" for knobs the user isn't using (e.g. effort).
    optional_fields = {"effort"}

    def _lookup(rr, key):
        if not rr:
            return None
        if key in rr:
            return rr.get(key)
        return (rr.get("eval_params") or {}).get(key)

    def _fmt(key, val):
        if val is None or val == "":
            return "—"
        if key == "duration_s":
            try:
                v = float(val)
                return f"{v / 60:.0f} min" if v >= 60 else f"{v:.0f}s"
            except (TypeError, ValueError):
                return str(val)
        if key == "cost_usd":
            try:
                return f"${float(val):.2f}"
            except (TypeError, ValueError):
                return str(val)
        if key == "num_turns" and isinstance(val, int):
            return f"{val:,}"
        return str(val)

    def _scalar_delta(key, cur_raw, bl_raw):
        """Delta tuple (arrow, str, css_class) for numeric fields, else None."""
        if key not in numeric_lower_better:
            return None
        if not isinstance(cur_raw, (int, float)) or not isinstance(bl_raw, (int, float)):
            return None
        if bl_raw == 0:
            return None
        diff = cur_raw - bl_raw
        if diff == 0:
            return ("→", "0%", "delta-flat")
        pct = (diff / bl_raw) * 100
        arrow = "↑" if diff > 0 else "↓"
        sign = "+" if diff > 0 else ""
        return (arrow, f"{sign}{pct:.1f}%",
                "delta-good" if diff < 0 else "delta-bad")

    html = '<h2>Run Configuration</h2>\n<dl class="config-grid">\n'
    for label, key in fields:
        cur_raw = _lookup(run_result, key)
        cur = _fmt(key, cur_raw)
        # Hide optional knobs (e.g. effort) when neither side set them, to
        # avoid showing a row of "—" for a feature the user isn't using.
        if (key in optional_fields and cur == "—"
                and (not has_bl or _fmt(key, _lookup(baseline_result, key)) == "—")):
            continue
        html += '<div class="kv">'
        html += f'<dt>{label}</dt>'
        html += f'<dd>{_esc(cur)}</dd>'
        if has_bl:
            bl_raw = _lookup(baseline_result, key)
            bl = _fmt(key, bl_raw)
            if bl != cur:
                d = _scalar_delta(key, cur_raw, bl_raw)
                delta_html = (f' <span class="delta {d[2]}">{d[0]} {d[1]}</span>'
                              if d else "")
                html += f'<dd class="bl">{_esc(bl)}{delta_html}</dd>'
        html += '</div>\n'
    html += _render_cost_rows(summary, baseline_summary, has_bl)
    html += "</dl>\n"
    html += _render_cost_provenance(run_result)
    html += _render_model_usage(run_result, baseline_result)
    html += _render_eval_params(run_result)
    return html


def _render_eval_params(run_result):
    """Render the user-facing eval parameters that defined the run."""
    params = run_result.get("eval_params") or {}
    if not params:
        return ""

    # Short params shown as kv chips
    short_fields = [
        ("Execution Mode", "execution_mode"),
        ("Max Budget", "max_budget_usd"),
        ("Timeout", "timeout_s"),
        ("Effort", "effort"),
        ("MLflow Experiment", "mlflow_experiment"),
    ]

    def _fmt(key, val):
        if val is None or val == "":
            return None
        if key == "max_budget_usd":
            try:
                return f"${float(val):.0f}"
            except (TypeError, ValueError):
                return str(val)
        if key == "timeout_s":
            try:
                v = int(val)
                return f"{v // 60} min" if v >= 60 else f"{v}s"
            except (TypeError, ValueError):
                return str(val)
        return str(val)

    chips = []
    for label, key in short_fields:
        formatted = _fmt(key, params.get(key))
        if formatted is None:
            continue
        chips.append(
            '<div class="kv">'
            f'<dt>{label}</dt>'
            f'<dd>{_esc(formatted)}</dd>'
            '</div>'
        )

    body = ""
    if chips:
        body += '<dl class="config-grid">\n' + "\n".join(chips) + '\n</dl>\n'

    skill_args = params.get("skill_args")
    if skill_args:
        body += (
            '<div class="run-command">'
            '<span class="run-command-label">Skill Args</span>'
            f'<pre><code>/{_esc(params.get("skill", "skill"))} {_esc(skill_args)}</code></pre>'
            '</div>\n'
        )

    if not body:
        return ""

    # One-line preview shown alongside the summary when collapsed.
    # Prefer skill args (the dataset/invocation), fall back to a compact
    # comma-joined view of the short params.
    preview_text = ""
    if skill_args:
        preview_text = f'/{params.get("skill", "skill")} {skill_args}'
    else:
        bits = [_fmt(k, params.get(k))
                for _, k in short_fields if _fmt(k, params.get(k))]
        preview_text = " · ".join(bits)

    preview_html = (
        f'<span class="eval-params-preview">{_esc(preview_text)}</span>'
        if preview_text else ""
    )

    return (
        '<details class="eval-params">\n'
        f'<summary><span class="eval-params-label">Parameters</span>{preview_html}</summary>\n'
        f'{body}'
        '</details>\n'
    )


def _render_model_usage(run_result, baseline_result=None):
    """Render model usage: token counts, cost, cache hit rate, and derived
    per-turn / per-Mtok efficiency metrics. One column per model plus an
    optional Total column when more than one model is present.

    Per-turn rows (cost / turn, output tokens / turn) require num_turns,
    which the runner only reports at the whole-run level. They render in
    the Total column for multi-model runs, and in the single model's
    column for single-model runs (where they're unambiguous)."""
    tokens = run_result.get("token_usage") or {}
    bl_tokens = (baseline_result or {}).get("token_usage") or {}
    per_model = run_result.get("per_model_usage") or {}
    bl_per_model = (baseline_result or {}).get("per_model_usage") or {}

    if not tokens and not bl_tokens and not per_model and not bl_per_model:
        return ""

    has_bl = bool(bl_tokens) or bool(bl_per_model)

    def _compute(usage, key):
        """Return numeric value for a key, or None if not derivable."""
        if not usage:
            return None
        inp = usage.get("input", 0) or 0
        out = usage.get("output", 0) or 0
        cr = usage.get("cache_read", 0) or 0
        cw = usage.get("cache_create", 0) or 0
        cost = usage.get("cost_usd")
        turns = usage.get("num_turns")
        total_in = inp + cr + cw
        total_tokens = total_in + out
        if key == "input":         return total_in or None
        if key == "output":        return out or None
        if key == "cache_read":    return cr or None
        if key == "cache_create":  return cw or None
        if key == "cost_usd":      return cost
        if key == "hit":           return (cr / total_in) if total_in else None
        if key == "cpt":           return (cost / turns) if (cost is not None and turns) else None
        if key == "opt":           return (out / turns) if (out and turns) else None
        if key == "mtok":          return (cost / total_tokens * 1_000_000) if (cost is not None and total_tokens) else None
        return None

    def _fmt(usage, key):
        v = _compute(usage, key)
        if v is None:
            return "—"
        if key == "hit":
            return f"{v:.1%}"
        if key == "cost_usd":
            return f"${v:.2f}"
        if key == "cpt":
            return f"${v:.4f}"
        if key == "mtok":
            return f"${v:.2f}"
        if key == "opt":
            return f"{v:,.0f}"
        return f"{v:,}"

    # Higher-is-better metrics; everything else assumed lower-is-better.
    # cache_read raw count is higher-better as a proxy for cache exploitation.
    higher_better = {"cache_read", "hit"}
    # Percentage-point delta (vs %) for these rate-like metrics
    pp_delta = {"hit"}

    def _delta(cur, bl, key):
        """Return (arrow, delta_str, css_class) or (None, None, None)."""
        cur_v = _compute(cur, key)
        bl_v = _compute(bl, key)
        if cur_v is None or bl_v is None or bl_v == 0:
            return (None, None, None)
        diff = cur_v - bl_v
        if key in pp_delta:
            if abs(diff) < 0.0005:
                return ("→", "0pp", "delta-flat")
            arrow = "↑" if diff > 0 else "↓"
            sign = "+" if diff > 0 else ""
            cls = "delta-good" if (diff > 0) == (key in higher_better) else "delta-bad"
            return (arrow, f"{sign}{diff * 100:.1f}pp", cls)
        if diff == 0:
            return ("→", "0%", "delta-flat")
        pct = (diff / bl_v) * 100
        arrow = "↑" if diff > 0 else "↓"
        sign = "+" if diff > 0 else ""
        cls = "delta-good" if (diff > 0) == (key in higher_better) else "delta-bad"
        return (arrow, f"{sign}{pct:.1f}%", cls)

    # Inject cost_usd and num_turns into a usage dict so derived metrics
    # can be computed. Used for the Total column (whole-run aggregate).
    def _enrich(usage_dict, run_data):
        if not usage_dict and not run_data:
            return None
        merged = dict(usage_dict or {})
        if run_data:
            if "cost_usd" in run_data:
                merged["cost_usd"] = run_data.get("cost_usd")
            if "num_turns" in run_data:
                merged["num_turns"] = run_data.get("num_turns")
        return merged

    cur_total = _enrich(tokens, run_result)
    bl_total = _enrich(bl_tokens, baseline_result)
    models = sorted(set(per_model) | set(bl_per_model))
    show_total = bool(cur_total or bl_total) and len(models) != 1

    # Build per-model usage dicts. Inject per-model num_turns when the runner
    # captured it (cross-model runs); fall back to whole-run num_turns when a
    # side's run had only one model (per-turn rows are then unambiguous).
    cur_pmt = run_result.get("per_model_turns") or {}
    bl_pmt = (baseline_result or {}).get("per_model_turns") or {}
    cur_per_model = {m: dict(per_model.get(m) or {}) for m in models}
    bl_per_model_d = {m: dict(bl_per_model.get(m) or {}) for m in models}
    cur_only_model = next(iter(per_model)) if len(per_model) == 1 else None
    bl_only_model = next(iter(bl_per_model)) if len(bl_per_model) == 1 else None
    for m in models:
        if cur_pmt.get(m) is not None:
            cur_per_model[m].setdefault("num_turns", cur_pmt[m])
        elif m == cur_only_model and run_result.get("num_turns") is not None:
            cur_per_model[m].setdefault("num_turns", run_result["num_turns"])
        if bl_pmt.get(m) is not None:
            bl_per_model_d[m].setdefault("num_turns", bl_pmt[m])
        elif (m == bl_only_model and baseline_result
              and baseline_result.get("num_turns") is not None):
            bl_per_model_d[m].setdefault("num_turns", baseline_result["num_turns"])

    # Build column list: optional Total, then each model
    columns = []
    if show_total:
        columns.append(("Total", cur_total, bl_total))
    for m in models:
        columns.append((m, cur_per_model.get(m) or None, bl_per_model_d.get(m) or None))

    if not columns:
        return ""

    metrics = [
        ("Input tokens (total)", "input"),
        ("Output tokens", "output"),
        ("Cache read", "cache_read"),
        ("Cache write", "cache_create"),
        ("Cache hit", "hit"),
        ("Cost", "cost_usd"),
        ("Cost / turn", "cpt"),
        ("Output tokens / turn", "opt"),
        ("Cost / Mtok", "mtok"),
    ]

    html = '<h3 class="subsection-heading">Model Usage</h3>\n'
    html += '<table class="usage-table">\n'
    html += '<tr><th></th>'
    for label, _, _ in columns:
        if label == "Total":
            html += f'<th>{label}</th>'
        else:
            html += f'<th><code>{_esc(label)}</code></th>'
    html += '</tr>\n'

    for mlabel, mkey in metrics:
        html += f'<tr><th>{mlabel}</th>'
        for _, cur, bl in columns:
            cur_v = _fmt(cur, mkey)
            html += '<td>'
            html += f'<span class="cur-val">{cur_v}</span>'
            if has_bl:
                bl_v = _fmt(bl, mkey)
                if bl_v != cur_v:
                    arrow, delta_str, dcls = _delta(cur, bl, mkey)
                    delta_html = (f' <span class="delta {dcls}">{arrow} {delta_str}</span>'
                                  if delta_str else "")
                    html += f'<span class="bl-val">{bl_v}{delta_html}</span>'
            html += '</td>'
        html += '</tr>\n'
    html += '</table>\n'
    return html


def _stability_proportion_bar(stable, total, samples):
    """Small proportion bar (stable vs varied) summarizing a judge's cross-case
    sampling consistency. Used in the Scoring Summary for LLM-judge rows and the
    pairwise row so the whole table shares one visual language."""
    if not total:
        return ""
    W, H = 46, 7
    frac = stable / total
    title = f"{stable}/{total} cases stable across {samples} samples"
    return (
        f'<span class="stab-wrap">'
        f'<svg class="stab-bar" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="{_esc(title)}"><title>{_esc(title)}</title>'
        f'<rect x="0" y="0" width="{W}" height="{H}" rx="3.5" fill="var(--warning-soft)"/>'
        f'<rect x="0" y="0" width="{W * frac:.1f}" height="{H}" rx="3.5" fill="var(--success)"/>'
        f'</svg><span class="stab-label">{stable}/{total} · {samples}×</span></span>')


def _render_scoring_summary(summary, config, baseline_summary=None):
    judges = summary.get("judges", {})
    thresholds = config.get("thresholds", {})
    # Authoritative PASS/FAIL per judge, from the same detector the CLI exits
    # on — the column below only picks which bound to display.
    try:
        _breached = {r.judge_name
                     for r in _detect_regressions(summary.get("judges", {}),
                                                  thresholds)}
        _breach_known = True
    except Exception:
        # Unknown, not clean: fall back to the metrics this table can compute
        # rather than asserting PASS on a verdict we could not obtain.
        _breached, _breach_known = set(), False
    bl_judges = baseline_summary.get("judges", {}) if baseline_summary else {}
    has_bl = bool(bl_judges)

    # Build judge type/model lookup from config
    judge_info = {}
    default_model = (config.get("models", {}).get("judge")
                     or os.environ.get("EVAL_JUDGE_MODEL")
                     or "—")
    for jc in config.get("judges", []):
        jname = jc.get("name", "")
        if jc.get("builtin"):
            judge_info[jname] = ("builtin", jc.get("model") or "—")
        elif jc.get("check"):
            judge_info[jname] = ("check", "—")
        elif jc.get("prompt") or jc.get("prompt_file"):
            judge_info[jname] = ("llm", jc.get("model") or default_model)
        elif jc.get("module"):
            judge_info[jname] = ("code", "—")

    html = "<h2>Scoring Summary</h2>\n<table>\n"
    html += f"<tr><th>Judge</th><th>Type</th><th>Metric</th><th>Value</th>"
    if has_bl:
        html += "<th>Baseline</th>"
    html += "<th>Threshold</th><th>Status</th></tr>\n"

    for judge_name, agg in sorted(judges.items()):
        if not isinstance(agg, dict):
            continue
        # Determine metric type and value
        pass_rate = agg.get("pass_rate")
        mean = agg.get("mean")

        if pass_rate is not None:
            metric_name = "pass_rate"
            metric_val = f"{pass_rate:.0%}"
        elif mean is not None:
            metric_name = "mean"
            metric_val = f"{mean:.2f}"
        else:
            metric_name = "—"
            metric_val = "—"

        # Sampling stability (from `score.py judges --samples N`) — a proportion
        # bar (stable vs varied across cases) appended to the metric.
        jst = agg.get("stability")
        if isinstance(jst, dict) and jst.get("samples", 1) > 1 and metric_val != "—":
            metric_val += " " + _stability_proportion_bar(
                jst.get("stable_cases", 0), jst.get("total_cases", 0),
                jst.get("samples"))

        # Baseline
        bl_val = ""
        if has_bl and judge_name in bl_judges:
            bl_agg = bl_judges[judge_name]
            if isinstance(bl_agg, dict):
                bl_pr = bl_agg.get("pass_rate")
                bl_mn = bl_agg.get("mean")
                if bl_pr is not None:
                    bl_val = f"{bl_pr:.0%}"
                elif bl_mn is not None:
                    bl_val = f"{bl_mn:.2f}"

        # Threshold and status
        thresh = thresholds.get(judge_name, {})
        thresh_str = "—"
        status_cls = "skip"
        status_label = "—"

        # The column shows one representative bound; which one is cosmetic.
        if isinstance(thresh, dict):
            if "min_pass_rate" in thresh and pass_rate is not None:
                thresh_str = f"&ge; {_pct(thresh['min_pass_rate'])}"
            elif "min_mean" in thresh and mean is not None:
                thresh_str = f"&ge; {thresh['min_mean']}"
            elif "min_win_rate" in thresh:
                thresh_str = f"&ge; {_pct(thresh['min_win_rate'])}"
            elif "max_error_rate" in thresh:
                thresh_str = f"&le; {_pct(thresh['max_error_rate'])} errored"

        # A breach is authoritative and is checked FIRST, because the metric it
        # gates on need not be one this table can display: a judge gated only
        # on `min_win_rate`, or on `max_error_rate` with every case errored,
        # has no mean and no pass_rate and used to fall through to SKIP while
        # the same threshold failed CI.
        if judge_name in _breached:
            status_cls = "fail"
            status_label = "FAIL"
        elif thresh_str != "—" and _breach_known:
            # A satisfied threshold is a PASS even when the metric it gates on
            # isn't one this table displays (win_rate, error rate).
            status_cls = "pass"
            status_label = "PASS"
        elif pass_rate is None and mean is None:
            # No aggregatable values — distinguish skip from error by
            # checking per-case results for this judge
            per_case = summary.get("per_case", {})
            has_errors = any(
                isinstance((per_case.get(c) or {}).get(judge_name), dict)
                and (per_case[c][judge_name].get("error")
                     or per_case[c][judge_name].get("value") is False)
                for c in per_case
            )
            if has_errors:
                status_cls = "fail"
                status_label = "ERROR"
            else:
                status_cls = "skip"
                status_label = "SKIP"

        jtype, jmodel = judge_info.get(judge_name, (None, "—"))
        if jtype is None:
            first_case = next(iter(summary.get("per_case", {}).values()), {})
            jtype = (first_case.get(judge_name) or {}).get("judge_type", "—")
        if jtype in ("check", "code", "builtin", "step") and jmodel == "—":
            type_label = jtype
        else:
            type_label = f'{jtype} ({jmodel.split("@")[0]})'
        html += f'<tr class="metric-row"><td>{_esc(judge_name)}</td>'
        html += f'<td class="judge-type">{_esc(type_label)}</td>'
        html += f"<td>{metric_name}</td><td>{metric_val}</td>"
        if has_bl:
            html += f"<td>{bl_val}</td>"
        html += f'<td>{thresh_str}</td>'
        html += f'<td><span class="{status_cls}">{status_label}</span></td></tr>\n'

    # Pairwise summary row (if available)
    pw = summary.get("pairwise")
    if pw and not pw.get("error"):
        wins_a = pw.get("wins_a", 0)
        wins_b = pw.get("wins_b", 0)
        ties = pw.get("ties", 0)
        errors = pw.get("errors", 0)
        total = wins_a + wins_b + ties + errors
        pw_val = f"{wins_a}W / {wins_b}L / {ties}T"
        if errors:
            pw_val += f" / {errors}E"
        # Verdict-stability (from `score.py pairwise --samples N`) — same
        # proportion bar the judge rows use.
        st = pw.get("stability") or {}
        n = st.get("runs", 1)
        if n and n > 1:
            pw_val += " " + _stability_proportion_bar(
                st.get("stable_cases", 0), st.get("total_cases", 0), n)
        if wins_a > wins_b:
            pw_status_cls, pw_status = "pass", "WIN"
        elif wins_b > wins_a:
            pw_status_cls, pw_status = "fail", "LOSS"
        else:
            pw_status_cls, pw_status = "skip", "TIE"
        pw_jc = next((j for j in config.get("judges", []) if j.get("name") == "pairwise"), {})
        pw_model = pw_jc.get("model") or default_model
        html += (f'<tr class="metric-row"><td>pairwise</td>'
                 f'<td class="judge-type">llm ({pw_model.split("@")[0]})</td>'
                 f'<td>comparison</td><td>{pw_val}</td>')
        if has_bl:
            html += "<td>—</td>"
        html += f'<td>—</td><td><span class="{pw_status_cls}">{pw_status}</span></td></tr>\n'

    html += "</table>\n"
    return html


def _detect_regressions(judges, thresholds):
    """score.py's regression detector.

    score.py sits beside this file but is a script, not a package module, so
    it is imported by name off the same directory. Raises rather than
    returning [] — a detector that could not run is not a clean run, and
    callers render that distinction.
    """
    from score import detect_regressions
    return detect_regressions(judges, thresholds)


def _render_regressions(summary, config):
    judges = summary.get("judges", {})
    thresholds = config.get("thresholds", {})
    regressions = []

    # Reuse score.py's detector rather than restating it. This section had
    # drifted to two of the four keys, so a `min_win_rate` or `max_error_rate`
    # breach failed the run while the report showed no Regressions table at all.
    try:
        for reg in _detect_regressions(judges, thresholds):
            regressions.append((reg.judge_name, reg.metric,
                                reg.baseline_value, reg.current_value))
    except Exception as exc:
        # Say so. Rendering an empty section would read as "no regressions"
        # while CI evaluates the same thresholds and may fail the run.
        return ('<h2>Regressions</h2>\n<p class="fail">Could not evaluate '
                f'thresholds: {_esc(str(exc))}. Check the scoring CLI output '
                "— this report cannot confirm the run is clean.</p>\n")

    if not regressions:
        return ""

    html = "<h2>Regressions</h2>\n<table>\n"
    html += "<tr><th>Judge</th><th>Metric</th><th>Threshold</th><th>Actual</th></tr>\n"
    for judge, metric, expected, actual in regressions:
        html += (f'<tr><td>{_esc(judge)}</td><td>{metric}</td>'
                 f'<td>{expected}</td><td><span class="fail">{actual}</span></td></tr>\n')
    html += "</table>\n"
    return html


def _render_pairwise(summary):
    pw = summary.get("pairwise")
    if not pw:
        return ""

    html = "<h2>Pairwise Comparison</h2>\n"
    html += (f"<p><strong>{_esc(str(pw.get('run_a', 'A')))}</strong> vs "
             f"<strong>{_esc(str(pw.get('run_b', 'B')))}</strong> "
             f"({pw.get('cases_compared', '?')} cases)</p>\n")
    html += (f"<p>Wins: {pw.get('wins_a', 0)} | "
             f"Losses: {pw.get('wins_b', 0)} | "
             f"Ties: {pw.get('ties', 0)}")
    if pw.get("errors"):
        html += f" | Errors: {pw['errors']}"
    html += "</p>\n"

    per_case = pw.get("per_case", [])
    if per_case:
        html += "<p>"
        for pc in per_case:
            cid = pc.get("case_id", "?")
            winner = pc.get("winner", "error")
            html += f"{_esc(cid)} {_pairwise_badge(winner)} "
        html += "</p>\n"

    return html


def _parse_analysis_frontmatter(content: str):
    """Split optional YAML frontmatter from the analysis body.

    Returns (meta_dict, body_markdown). When the file has no frontmatter,
    meta_dict is empty and body_markdown is the unchanged content.
    """
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines()
    # Find the closing '---' line (must be on its own line, after line 0)
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, content
    raw = "\n".join(lines[1:end_idx])
    try:
        meta = yaml.safe_load(raw) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    body = "\n".join(lines[end_idx + 1:]).lstrip("\n")
    return meta, body


def _build_analysis_subtitle(meta, summary, run_result, baseline_summary):
    """Construct a dynamic subtitle paragraph for the analysis section.

    Pulls the analysis author from the frontmatter, run shape from summary,
    and execution metadata from run_result. Falls back gracefully when fields
    are missing.
    """
    author_agent = (meta or {}).get("agent")
    author_model = (meta or {}).get("model")
    author_date = (meta or {}).get("date")
    if author_agent and author_model:
        author_html = (f"<strong>{_esc(str(author_agent))}</strong> "
                       f"(<code>{_esc(str(author_model))}</code>)")
    elif author_model:
        author_html = f"<strong><code>{_esc(str(author_model))}</code></strong>"
    elif author_agent:
        author_html = f"<strong>{_esc(str(author_agent))}</strong>"
    else:
        author_html = "<strong>Claude Code</strong>"

    date_html = (f" on <code>{_esc(str(author_date))}</code>"
                 if author_date else "")

    judge_count = len((summary or {}).get("judges") or {})
    case_count = len((summary or {}).get("per_case") or {})

    scope_bits = []
    if judge_count:
        scope_bits.append(f"{judge_count} judge score{'s' if judge_count != 1 else ''}")
    if case_count:
        scope_bits.append(f"{case_count} case{'s' if case_count != 1 else ''}")
    scope = " across ".join(scope_bits) if scope_bits else "this run's results"

    rr = run_result or {}
    metric_parts = []
    dur = rr.get("duration_s")
    if isinstance(dur, (int, float)) and dur > 0:
        metric_parts.append(f"{dur / 60:.0f} min" if dur >= 60 else f"{dur:.0f}s")
    cost = rr.get("cost_usd")
    if isinstance(cost, (int, float)) and cost > 0:
        metric_parts.append(f"${cost:.2f}")
    turns = rr.get("num_turns")
    if isinstance(turns, int) and turns > 0:
        metric_parts.append(f"{turns:,} turns")
    exit_code = rr.get("exit_code")
    if exit_code is not None:
        metric_parts.append(f"exit {exit_code}")
    metrics_html = f" ({', '.join(metric_parts)})" if metric_parts else ""

    baseline_html = ""
    bl_run = (baseline_summary or {}).get("run_id")
    if bl_run:
        baseline_html = f" Baseline: <code>{_esc(str(bl_run))}</code>."

    return (
        f"Generated by {author_html}{date_html} from {scope}, "
        f"plus execution metadata{metrics_html}.{baseline_html} "
        "Recommendation leads; failure patterns, root causes, and regressions "
        "follow as supporting evidence."
    )


def _render_analysis(run_dir, summary=None, run_result=None, baseline_summary=None):
    """Render the agent's analysis (recommendation + supporting findings) if saved.

    The analysis is wrapped in a visually distinct callout and placed near the
    top of the report so the agent's recommendation is highly visible. The
    subtitle is built dynamically from optional YAML frontmatter (author model,
    date) plus the run's summary and run_result.
    """
    analysis_path = run_dir / "analysis.md"
    if not analysis_path.exists():
        return ""

    content = analysis_path.read_text().strip()
    if not content:
        return ""

    meta, body_md = _parse_analysis_frontmatter(content)
    subtitle = _build_analysis_subtitle(meta, summary, run_result, baseline_summary)
    body = _md_to_html(body_md)
    return (
        '<section class="analysis">\n'
        '<div class="analysis-banner"><h2>Analysis</h2></div>\n'
        f'<p class="section-intro">{subtitle}</p>\n'
        f'<div class="analysis-body">\n{body}\n</div>\n'
        '</section>\n'
    )


def _normalize_escapes(text: str) -> str:
    """Convert JSON-style escape sequences (literal \\n, \\t) sometimes
    emitted by judge LLMs inside their rationale into the real characters.
    Only handles the safe subset; ignores \\x / \\u to avoid surprises."""
    if not text or "\\" not in text:
        return text
    return (text.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\t", "\t"))


import re as _re
from urllib.parse import urlsplit as _urlsplit

_SAFE_LINK_SCHEMES = {"http", "https", "mailto"}


def _safe_href(raw: str) -> str | None:
    """Return a safe href value or None if the URL must be rejected.

    Defends against XSS via judge rationales / analysis.md content like
    `[click](javascript:alert(1))` or attribute-injection via stray quotes.
    Allows http/https/mailto URLs and relative paths/anchors only.

    `raw` is expected to already be HTML-escaped (since `_md_inline` escapes
    the full text before running the link regex), so we do not re-escape.
    """
    href = raw.strip()
    if not href:
        return None
    parsed = _urlsplit(href)
    if parsed.scheme:
        if parsed.scheme.lower() not in _SAFE_LINK_SCHEMES:
            return None
    elif not href.startswith(("/", "#", "./", "../")):
        return None
    return href


def _md_inline(text: str) -> str:
    """Apply inline markdown formatting (bold, italic, code, links) to text.
    Always HTML-escapes first."""
    text = _esc(text)
    text = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    text = _re.sub(r'`(.+?)`', r'<code>\1</code>', text)

    def _link_sub(match):
        label = match.group(1)
        href = _safe_href(match.group(2))
        if href is None:
            return label
        return f'<a href="{href}" rel="noopener noreferrer">{label}</a>'

    text = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _link_sub, text)
    return text


def _md_to_html(md_text):
    """Convert markdown to HTML. Handles headers, lists, tables, code blocks,
    bold, italic, inline code, and links."""
    import re

    lines = md_text.splitlines()
    out = []
    i = 0
    list_stack = []  # stack of "ul" or "ol"

    def _close_lists():
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    _inline = _md_inline

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            _close_lists()
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            out.append(f'<pre class="output">{_esc(chr(10).join(code_lines))}</pre>')
            i += 1
            continue

        # Table (line starts with |)
        if stripped.startswith("|") and "|" in stripped[1:]:
            _close_lists()
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            out.append(_md_table_to_html(table_lines))
            continue

        # Headers — # maps to h1, ## to h2, ### to h3
        if stripped.startswith("### "):
            _close_lists()
            out.append(f"<h3>{_inline(stripped[4:])}</h3>")
            i += 1
            continue
        if stripped.startswith("## "):
            _close_lists()
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
            i += 1
            continue
        if stripped.startswith("# "):
            _close_lists()
            out.append(f"<h1>{_inline(stripped[2:])}</h1>")
            i += 1
            continue

        # Unordered list
        if stripped.startswith("- "):
            if not list_stack or list_stack[-1] != "ul":
                if list_stack and list_stack[-1] == "ol":
                    out.append(f"</{list_stack.pop()}>")
                list_stack.append("ul")
                out.append("<ul>")
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            i += 1
            continue

        # Ordered list
        m = re.match(r'^(\d+)\.\s(.+)', stripped)
        if m:
            if not list_stack or list_stack[-1] != "ol":
                if list_stack and list_stack[-1] == "ul":
                    out.append(f"</{list_stack.pop()}>")
                list_stack.append("ol")
                out.append("<ol>")
            out.append(f"<li>{_inline(m.group(2))}</li>")
            i += 1
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            _close_lists()
            out.append("<hr>")
            i += 1
            continue

        # Blank line — only close lists if the next content line isn't a
        # continuation of the same list type (LLM rationales commonly use
        # paragraph-spaced list items like "1. ...\n\n2. ...").
        if not stripped:
            if list_stack:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                next_stripped = lines[j].strip() if j < len(lines) else ""
                continues_ol = list_stack[-1] == "ol" and re.match(r'^\d+\.\s', next_stripped)
                continues_ul = list_stack[-1] == "ul" and next_stripped.startswith("- ")
                if not (continues_ol or continues_ul):
                    _close_lists()
            i += 1
            continue

        # Paragraph — accumulate consecutive non-blank lines into a single
        # <p> so that soft-wrapped source lines reflow to the container width
        # instead of each becoming its own (artificially narrow) paragraph.
        _close_lists()
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt:
                break
            # Stop at anything that begins a different block-level construct.
            if (nxt.startswith("```")
                    or (nxt.startswith("|") and "|" in nxt[1:])
                    or nxt.startswith(("### ", "## ", "# ", "- "))
                    or re.match(r'^(\d+)\.\s(.+)', nxt)
                    or nxt in ("---", "***", "___")):
                break
            para_lines.append(nxt)
            i += 1
        out.append(f"<p>{_inline(' '.join(para_lines))}</p>")

    _close_lists()
    return "\n".join(out)


def _md_table_to_html(table_lines):
    """Convert markdown table lines to an HTML table."""
    if len(table_lines) < 2:
        return ""

    def _parse_row(line):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        return cells

    headers = _parse_row(table_lines[0])

    # Skip separator row (line with dashes/colons)
    data_start = 1
    if len(table_lines) > 1 and all(
            c.strip().replace("-", "").replace(":", "").replace(" ", "") == ""
            for c in table_lines[1].strip().strip("|").split("|")):
        data_start = 2

    html = "<table>\n<tr>"
    for h in headers:
        html += f"<th>{_md_inline(h)}</th>"
    html += "</tr>\n"

    for row_line in table_lines[data_start:]:
        cells = _parse_row(row_line)
        html += "<tr>"
        for c in cells:
            # Color-code PASS/FAIL keywords as pills, leaving the rest
            # of the cell as plain text.
            import re as _re
            stripped = c.strip()
            bare = stripped.strip("*_")
            _STATUS_CLS = {"PASS": "pass", "FIXED": "pass", "FAIL": "fail",
                           "REGRESSION": "fail", "SKIP": "skip", "SKIPPED": "skip"}
            match = _re.match(r'^(\*{0,2}_?)(PASS|FIXED|FAIL|REGRESSION|SKIP|SKIPPED)(_?\*{0,2})(.*)', bare)
            if match:
                kw = match.group(2)
                rest = match.group(4).strip()
                pill = f'<span class="{_STATUS_CLS[kw]}">{kw}</span>'
                rest_html = f" {_md_inline(rest)}" if rest else ""
                html += f"<td>{pill}{rest_html}</td>"
            else:
                html += f"<td>{_md_inline(c)}</td>"
        html += "</tr>\n"

    html += "</table>"
    return html


_img_compare_counter = 0


def _render_image_compare(gen_uri, ref_uri, gen_label="Generated",
                          ref_label="Reference", ref_class="img-label-ref"):
    global _img_compare_counter
    _img_compare_counter += 1
    uid = f"ic{_img_compare_counter}"
    return (
        f'<div class="img-compare">\n'
        f'  <div class="img-compare-tabs">\n'
        f'    <button class="img-compare-tab active" data-panel="{uid}-sbs">Side by side</button>\n'
        f'    <button class="img-compare-tab" data-panel="{uid}-swipe">Swipe</button>\n'
        f'    <button class="img-compare-tab" data-panel="{uid}-onion">Onion</button>\n'
        f'  </div>\n'
        f'  <div id="{uid}-sbs" class="img-compare-panel active">\n'
        f'    <div class="img-compare-sbs">\n'
        f'      <div><span class="img-label {ref_class}">{_esc(ref_label)}</span><br>'
        f'<img src="{ref_uri}" loading="lazy" alt="{_esc(ref_label)}"></div>\n'
        f'      <div><span class="img-label img-label-gen">{_esc(gen_label)}</span><br>'
        f'<img src="{gen_uri}" loading="lazy" alt="{_esc(gen_label)}"></div>\n'
        f'    </div>\n'
        f'  </div>\n'
        f'  <div id="{uid}-swipe" class="img-compare-panel">\n'
        f'    <div class="img-compare-swipe">\n'
        f'      <div class="img-compare-swipe-imgs">\n'
        f'        <img src="{gen_uri}" alt="{_esc(gen_label)}">\n'
        f'        <div class="swipe-ref"><img src="{ref_uri}" alt="{_esc(ref_label)}"></div>\n'
        f'        <div class="swipe-line"></div>\n'
        f'      </div>\n'
        f'      <input type="range" min="0" max="100" value="50">\n'
        f'    </div>\n'
        f'    <div class="img-compare-swipe-labels">'
        f'<span>◀ {_esc(gen_label)}</span><span>{_esc(ref_label)} ▶</span></div>\n'
        f'  </div>\n'
        f'  <div id="{uid}-onion" class="img-compare-panel">\n'
        f'    <div class="img-compare-onion">\n'
        f'      <div class="img-compare-onion-wrap">\n'
        f'        <img src="{ref_uri}" alt="{_esc(ref_label)}">\n'
        f'        <div class="onion-top" style="opacity:0.5"><img src="{gen_uri}" alt="{_esc(gen_label)}"></div>\n'
        f'      </div>\n'
        f'      <input type="range" min="0" max="100" value="50">\n'
        f'      <div class="img-compare-swipe-labels">'
        f'<span>◀ {_esc(ref_label)}</span>'
        f'<span class="onion-label">{_esc(gen_label)} opacity: 50%</span>'
        f'<span>{_esc(gen_label)} ▶</span></div>\n'
        f'    </div>\n'
        f'  </div>\n'
        f'</div>\n'
    )


def _render_standalone_image(data_uri, label):
    return (
        f'<div class="img-artifact">\n'
        f'  <img src="{data_uri}" loading="lazy" alt="{_esc(label)}">\n'
        f'</div>\n'
    )


def _shared_output_paths(config):
    """Return set of output paths with batch_pattern '*' (shared across cases)."""
    return {o.get("path") for o in config.get("outputs", [])
            if o.get("batch_pattern") == "*" and o.get("path")}


def _render_shared_outputs(run_dir, config):
    """Render shared output files (batch_pattern='*') once before per-case."""
    shared_paths = _shared_output_paths(config)
    if not shared_paths:
        return ""

    cases_dir = run_dir / "cases"
    if not cases_dir.exists():
        return ""

    # Read shared files from the first case directory
    first_case = next((d for d in sorted(cases_dir.iterdir()) if d.is_dir()), None)
    if not first_case:
        return ""

    html = "<h2>Shared Outputs</h2>\n"
    html += '<p class="section-intro">These files are shared across all cases (not repeated per-case below).</p>\n'
    for out_path in sorted(shared_paths):
        shared_entry = first_case / out_path
        if not shared_entry.exists():
            continue
        if shared_entry.is_file():
            files = [shared_entry]
        else:
            files = sorted(f for f in shared_entry.rglob("*") if f.is_file())
        for f in files:
            rel = f.relative_to(first_case)
            if f.suffix == ".html":
                try:
                    html_content = f.read_text()
                    srcdoc = html_content.replace("&", "&amp;").replace('"', "&quot;")
                    html += (f'<div class="file-badge">{_esc(str(rel))}</div>\n'
                             f'<iframe class="html-preview" srcdoc="{srcdoc}" '
                             f'sandbox="allow-same-origin"'
                             f'></iframe>\n')
                except (UnicodeDecodeError, OSError):
                    html += (f'<div class="file-badge">{_esc(str(rel))} '
                             f'<span class="skip">(could not read)</span></div>\n')
            else:
                content = _read_text(f, max_lines=200)
                if content:
                    html += (f'<div class="file-badge">{_esc(str(rel))}</div>\n'
                             f'<pre class="output">{_esc(content)}</pre>\n')
    return html


def _ascii_hist(items, lo_label, hi_label, mark_key):
    """Monospace ASCII histogram of sampled judge values on a labelled axis.

    `items` is an ordered list of (key, count); each renders as a block bar
    (height = count, tallest = █), empty categories stay ░, and the bar for
    `mark_key` is wrapped in markers (\\x01..\\x02) for colourising in HTML.
    A stable judge — all samples in one bucket — is a single bar (e.g.
    ``0 ░░█ 2``), so the column reads "agree" vs "wobble" at a glance.
    Used for numeric judges (over each judge's own score range), bool judges
    (F … P) and pairwise (A … B).
    """
    blocks = "▁▂▃▄▅▆▇█"
    maxc = max((c for _, c in items), default=1) or 1
    cells = []
    for key, c in items:
        ch = "░" if c == 0 else blocks[round(c / maxc * 7)]
        if key == mark_key:
            ch = "\x01" + ch + "\x02"   # marker (colourised in HTML)
        cells.append(ch)
    return f"{lo_label} " + "".join(cells) + f" {hi_label}"


def _ascii_score_hist(med, values, smin=None, smax=None):
    """`_ascii_hist` over the numeric score scale, marking the median level."""
    from collections import Counter
    # Keep the fractional part until after the axis is sized. Truncating first
    # turned an off-scale 2.9 on a [0, 2] judge into an in-range 2, so the
    # reading this widening exists to expose was the one it hid.
    raw = [float(v) for v in values if isinstance(v, (int, float))]
    if not raw:
        return ""
    lo = smin if smin is not None else min(raw)
    hi = smax if smax is not None else max(raw)
    # Bins are whole numbers, so a fractional declared bound has to widen to
    # the enclosing integers: ceil keeps a [0, 2.5] judge's top bin, where
    # truncating to 2 would drop every reading above it off the chart.
    lo, hi = math.floor(lo), math.ceil(hi)
    # Widen to cover values off the declared scale: `range(lo, hi + 1)` below
    # would otherwise drop them, hiding the very readings worth seeing.
    lo, hi = min(lo, math.floor(min(raw))), max(hi, math.ceil(max(raw)))
    if hi - lo > _MAX_HIST_BINS:   # pathological declared range -> use observed span
        lo, hi = math.floor(min(raw)), math.ceil(max(raw))
        # Still unbounded if the observations themselves are: cap the axis so a
        # single wild reading cannot build one cell per integer up to it.
        hi = min(hi, lo + _MAX_HIST_BINS)
    # A fractional reading bins to the integer at or above it, so an off-scale
    # 2.9 lands in the 3 bin rather than masquerading as a 2.
    counts = Counter(math.ceil(v) for v in raw)
    # Bin the marker the same way the counts are binned, or it lands on an
    # empty cell for any fractional median.
    med = max(lo, min(hi, math.ceil(med)))
    items = [(s, counts.get(s, 0)) for s in range(lo, hi + 1)]
    return _ascii_hist(items, str(lo), str(hi), med)


def _colorise_hist(glyph):
    """HTML-escape an ASCII histogram and colourise its marked bar."""
    return (_esc(glyph)
            .replace("\x01", '<span class="med">')
            .replace("\x02", "</span>"))


_MAX_HIST_BINS = 40  # safety cap: fall back to observed span for pathological ranges


def _judge_score_ranges(config):
    """Map judge name -> validated (lo, hi) from its `score_range`.

    report.py reads eval.yaml as raw dicts (bypassing EvalConfig validation),
    so guard here: keep only well-formed ranges (exactly two float-coercible,
    strictly increasing endpoints). Malformed ranges (non-numeric, reversed,
    wrong length) are dropped so downstream histograms/coloring fall back to a
    safe default instead of raising on range()/arithmetic.

    Bounds keep their fractional part — `int()` here turned a `[0, 2.5]` scale
    into `[0, 2]` and banded a legitimate 2.4 as off-scale. Only the histogram
    needs whole numbers, and it derives its own bins.
    """
    ranges = {}
    for j in config.get("judges", []):
        name = j.get("name", "")
        sr = j.get("score_range")
        if not isinstance(sr, list) or len(sr) != 2:
            continue
        try:
            lo, hi = float(sr[0]), float(sr[1])
        except (TypeError, ValueError):
            continue
        if lo < hi:
            ranges[name] = (lo, hi)
    return ranges


def _render_reward_overview(summary, config, reward_cfg=None):
    """Render a compact per-case reward overview table with all judge scores.

    `reward_cfg`, when provided, is threaded to `compose_reward` so the
    displayed reward matches what `build_reward` trains on. Without it,
    `compose_reward` falls back to its default resolution (bool gates +
    averaged normalized numerics), which diverges from any configured
    `reward:` section (single-judge / weighted / formula).
    """
    per_case = summary.get("per_case", {})
    if not per_case:
        return ""

    _compose_reward = None
    try:
        from agent_eval.harbor.reward import compose_reward as _cr
        _compose_reward = _cr
    except ImportError:
        pass

    # Builtin judges cover both LLM-prompt scorers (quality/, safety/) and
    # deterministic Python callables (efficiency/, process/), all stamped
    # judge_type="builtin" by score.py. Consult the registry to disambiguate
    # so LLM builtins group with LLM judges (1-5 default) and Python builtins
    # land in Other (0-1 default) — otherwise e.g. efficiency=0.85 mis-colors
    # against the LLM 1-5 fallback.
    builtin_kinds = {}
    try:
        from agent_eval.judges import BuiltinJudgeRegistry
        _registry = BuiltinJudgeRegistry()
        _registry.discover()
        for j in config.get("judges", []):
            builtin = j.get("builtin")
            name = j.get("name", "")
            if builtin and name:
                try:
                    builtin_kinds[name] = _registry.get(builtin).kind
                except Exception:
                    pass
    except ImportError:
        pass

    # Classify judges using judge_type stamped in per-case results by score.py,
    # not the config YAML (which has no "type" field).
    gate_judges = []
    llm_judges = []
    other_judges = []
    seen = set()
    for case_results in per_case.values():
        if not isinstance(case_results, dict):
            continue
        for jn, jv in case_results.items():
            if jn in seen or not isinstance(jv, dict):
                continue
            seen.add(jn)
            jtype = jv.get("judge_type", "")
            if jtype == "check":
                gate_judges.append(jn)
            elif jtype in ("llm", "agent"):
                # Agent judges are model-based scored judges (score + rationale),
                # so they render alongside LLM judges rather than under "Other".
                llm_judges.append(jn)
            elif jtype == "builtin":
                # LLM-prompt builtins → LLM group; Python builtins → Other.
                if builtin_kinds.get(jn) == "llm":
                    llm_judges.append(jn)
                else:
                    other_judges.append(jn)
            else:
                other_judges.append(jn)
    gate_judges.sort()
    llm_judges.sort()
    other_judges.sort()

    # Resolve score_range per judge from config for color bands
    judge_score_range = _judge_score_ranges(config)

    def _label(name):
        return _esc(name.replace("_", " ").title())

    def _cell(val_entry, judge_name=""):
        if not isinstance(val_entry, dict):
            return '<span class="skip">\u2014</span>'
        v = val_entry.get("value")
        if v is True:
            return '<span class="pass">PASS</span>'
        if v is False:
            return '<span class="fail">FAIL</span>'
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            s = f"{v:.2f}" if isinstance(v, float) else str(v)
            # Band by the judge's declared score_range (same helper as the
            # per-case detail table). Without a declared range we can't know the
            # scale, so render neutral rather than guessing (a (0,1) guess made
            # any value >= 1, e.g. a pipeline total of 3.0, render green).
            rng = judge_score_range.get(judge_name)
            if not rng:
                return s
            return f'<span class="{_score_band_class(v, rng[0], rng[1])}">{s}</span>'
        return f'<span class="skip">{_esc(str(v)[:20])}</span>'

    def _reward_cell(val):
        if val is None:
            return '<span class="skip">\u2014</span>'
        try:
            f = float(val)
            s = f"{f:.4f}"
            if f >= 0.40:
                return f'<span class="rv-reward pass">{s}</span>'
            elif f >= 0.30:
                return f'<span class="rv-reward warn">{s}</span>'
            return f'<span class="rv-reward fail">{s}</span>'
        except (ValueError, TypeError):
            return f'<span class="skip">{_esc(str(val)[:20])}</span>'

    all_judges = gate_judges + llm_judges + other_judges

    html = '<h2 class="section-heading">Per-Case Reward Overview</h2>\n'
    html += ('<p style="font-size:0.9em;color:var(--text-muted);margin:0 0 0.5em;">'
             'Reward and all judge scores per case. '
             '<span class="pass" style="font-size:0.8em">Gates</span> are binary. '
             '<span class="warn" style="font-size:0.8em">LLM judges</span> are scored. '
             '<span class="skip" style="font-size:0.8em">Other</span> scores vary.</p>\n')
    html += '<div class="reward-overview"><table>\n'

    html += '<tr><th rowspan="2" style="vertical-align:bottom">Case</th>'
    html += '<th rowspan="2" style="vertical-align:bottom">Reward</th>'
    if gate_judges:
        html += (f'<th colspan="{len(gate_judges)}" class="rv-gate-hdr" '
                 f'style="text-align:center">Gate Judges</th>')
    if llm_judges:
        html += (f'<th colspan="{len(llm_judges)}" class="rv-llm-hdr" '
                 f'style="text-align:center">LLM Judges</th>')
    if other_judges:
        html += (f'<th colspan="{len(other_judges)}" class="rv-other-hdr" '
                 f'style="text-align:center">Other</th>')
    html += '</tr>\n<tr>'
    for jn in all_judges:
        html += f'<th class="rv-rotate">{_label(jn)}</th>'
    html += '</tr>\n'

    total_cases = 0
    rewards = []
    for case_id in sorted(per_case.keys()):
        case_results = per_case[case_id]
        if not isinstance(case_results, dict):
            continue
        total_cases += 1
        reward_val = None
        if _compose_reward is not None:
            try:
                reward_val, metrics = _compose_reward(
                    case_results, reward_cfg=reward_cfg,
                    judge_ranges=judge_score_range)
                # compose_reward returns 1.0 when all judges are None/skipped;
                # treat as unscored instead of inflating the average.
                if not metrics:
                    reward_val = None
            except Exception:
                pass
        html += f'<tr><td>{_esc(case_id)}</td>'
        html += f'<td>{_reward_cell(reward_val)}</td>'
        for jn in all_judges:
            html += f'<td>{_cell(case_results.get(jn, {}), jn)}</td>'
        html += '</tr>\n'
        if reward_val is not None:
            try:
                rewards.append(float(reward_val))
            except (ValueError, TypeError):
                pass

    if rewards:
        avg = sum(rewards) / len(rewards)
        n_cols = len(all_judges)
        scored_label = f"{len(rewards)} of {total_cases} scored" if len(rewards) < total_cases else f"{len(rewards)} cases"
        html += (f'<tr class="rv-avg-row"><td>Average ({scored_label})</td>'
                 f'<td>{_reward_cell(avg)}</td>'
                 f'<td colspan="{n_cols}"></td></tr>\n')

    html += '</table></div>\n'
    return html


def _score_band_class(val, lo, hi):
    """CSS class for a numeric score by its position in its own [lo, hi] range:
    green (top quartile) / amber (mid) / red (bottom third).

    Per-case cells are coloured by where the score sits on the judge's own
    scale — NOT against the aggregate `min_mean` threshold, which is a floor on
    the mean ACROSS cases and mis-flags valid per-case scores (e.g. a 1 on a
    0-2 judge with min_mean 1.5 would render red even though the judge passes).

    A value OFF the scale is invalid, not excellent: without the guard below a
    stray 4 from a 0-2 judge scores frac 2.0 and renders as a green pass.
    """
    if val < lo or val > hi:
        return "fail"
    span = hi - lo
    frac = (val - lo) / span if span else 0.5
    if frac >= 0.75:
        return "pass"
    if frac >= 0.375:
        return "warn"
    return "fail"


def _render_artifact_body(f, rel, config, gold_image_uri, gold_diagram_uri):
    """Render one output file's body HTML (no filename header; the caller adds it).

    Returns ``(kind, body)``:
    - ``kind="rendered"``: a visual artifact (image, diagram, HTML preview,
      metrics table) shown inline; the caller keeps it expanded.
    - ``kind="raw"``: a plain-text dump (``<pre>``) the caller collapses by
      default so large files don't force scrolling.
    - ``body=""``: nothing to show (e.g. a diagram whose rendered sibling is
      displayed as its own file).
    """
    if f.suffix == ".html":
        try:
            html_content = f.read_text()
            srcdoc = html_content.replace("&", "&amp;").replace('"', "&quot;")
            return ("rendered",
                    f'<iframe class="html-preview" srcdoc="{srcdoc}" '
                    f'sandbox="allow-same-origin" '
                    f'onload="this.style.height=this.contentDocument.documentElement.scrollHeight+20+\'px\'"'
                    f'></iframe>\n')
        except (UnicodeDecodeError, OSError):
            return ("rendered", '<span class="skip">(could not read)</span>\n')
    if _is_image_file(f):
        data_uri = _image_to_data_uri(f)
        if data_uri:
            if gold_image_uri:
                return ("rendered", _render_image_compare(
                    data_uri, gold_image_uri, gen_label="Generated",
                    ref_label="Gold Standard", ref_class="img-label-ref"))
            return ("rendered", _render_standalone_image(data_uri, str(rel)))
        size = f.stat().st_size
        return ("rendered", f'<span class="skip">({size} bytes, unreadable)</span>\n')
    if f.suffix in _DIAGRAM_SUFFIXES:
        sibling_names = {s.name for s in f.parent.iterdir()}
        has_rendered_sibling = (
            f"{f.name}.png" in sibling_names or f"{f.name}.svg" in sibling_names)
        svg_uri = _try_render_diagram(f, sibling_names)
        if svg_uri:
            if gold_diagram_uri:
                body = _render_image_compare(
                    svg_uri, gold_diagram_uri, gen_label="Generated",
                    ref_label="Gold Standard", ref_class="img-label-ref")
            else:
                body = _render_standalone_image(svg_uri, str(rel))
            source = _read_text(f, max_lines=200)
            if source:
                body += (f'<details class="diagram-source"><summary>Source</summary>\n'
                         f'<pre class="output">{_esc(source)}</pre></details>\n')
            return ("rendered", body)
        if not has_rendered_sibling:
            content = _read_text(f, max_lines=200)
            if content:
                return ("raw", f'<pre class="output">{_esc(content)}</pre>\n')
        return ("", "")
    if _resolve_artifact_type(f.name, config) == "graph":
        svg = _render_graph_spec_to_svg(f)
        if svg:
            body = _render_standalone_image(_svg_to_data_uri(svg), str(rel))
            content = _read_text(f, max_lines=200)
            if content:
                body += (f'<details class="diagram-source"><summary>Source</summary>\n'
                         f'<pre class="output">{_esc(content)}</pre></details>\n')
            return ("rendered", body)
        content = _read_text(f, max_lines=200)
        return ("raw", f'<pre class="output">{_esc(content)}</pre>\n') if content else ("", "")
    if _resolve_artifact_type(f.name, config) == "metrics":
        try:
            metrics = json.loads(f.read_text())
            if isinstance(metrics, dict) and metrics:
                body = '<table class="metrics-table"><tr><th>Metric</th><th>Value</th></tr>\n'
                for k, v in metrics.items():
                    body += f'<tr><td>{_esc(str(k))}</td><td>{_esc(str(v))}</td></tr>\n'
                return ("rendered", body + '</table>\n')
            return ("raw", f'<pre class="output">{_esc(f.read_text())}</pre>\n')
        except (json.JSONDecodeError, OSError):
            content = _read_text(f, max_lines=200)
            return ("raw", f'<pre class="output">{_esc(content)}</pre>\n') if content else ("", "")
    content = _read_text(f, max_lines=200)
    if content:
        return ("raw", f'<pre class="output">{_esc(content)}</pre>\n')
    size = f.stat().st_size
    return ("rendered", f'<span class="skip">({size} bytes, binary)</span>\n')


_FM_CHIP_KEYS = ("status", "recommendation", "priority", "size", "needs_attention")


def _file_meta(f):
    """Compact 'N lines, 12 KB' meta for a file summary (size-only for binaries)."""
    try:
        size = f.stat().st_size
    except OSError:
        return ""
    if size >= 1024 * 1024:
        human = f"{size / 1048576:.1f} MB"
    elif size >= 1024:
        human = f"{size / 1024:.1f} KB"
    else:
        human = f"{size} B"
    if f.suffix.lower() not in _IMAGE_SUFFIXES:
        try:
            return f"{len(f.read_text().splitlines())} lines · {human}"
        except (UnicodeDecodeError, OSError):
            pass
    return human


def _frontmatter_chips(f):
    """Small badges from a markdown file's YAML frontmatter (status, recommendation,
    scores.total, ...) so the output list is scannable without expanding."""
    if f.suffix.lower() not in (".md", ".markdown"):
        return ""
    try:
        meta, _ = _parse_analysis_frontmatter(f.read_text())
    except (UnicodeDecodeError, OSError):
        return ""
    if not isinstance(meta, dict) or not meta:
        return ""
    chips = []
    for k in _FM_CHIP_KEYS:
        v = meta.get(k)
        label = k.replace("_", " ")
        if isinstance(v, bool):
            # show the key with the flag only when set (a bare value is cryptic)
            if v:
                chips.append(f'<span class="fm-chip">{_esc(label)}: true</span>')
            continue
        if isinstance(v, (str, int, float)) and str(v).strip():
            chips.append(f'<span class="fm-chip">{_esc(label)}: {_esc(str(v))}</span>')
    scores = meta.get("scores")
    if isinstance(scores, dict) and isinstance(scores.get("total"), (int, float)):
        chips.append(f'<span class="fm-chip">total: {_esc(str(scores["total"]))}</span>')
    return "".join(chips)


def _numbered_pre(text, cls="output"):
    """A <pre> with a copy-safe line-number gutter (the number column is not
    selectable, so copying the content excludes line numbers)."""
    lines = text.splitlines() or [""]
    gutter = "\n".join(str(i + 1) for i in range(len(lines)))
    return (f'<div class="numwrap"><pre class="lncol" aria-hidden="true">{gutter}</pre>'
            f'<pre class="{cls}">{_esc(chr(10).join(lines))}</pre></div>')


def _diff_stats(old_text, new_text):
    """(added, deleted) line counts between two texts."""
    sm = difflib.SequenceMatcher(None, old_text.splitlines(), new_text.splitlines())
    adds = dels = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            dels += i2 - i1
        if op in ("replace", "insert"):
            adds += j2 - j1
    return adds, dels


def _render_file_entries(files, base_dir, config, gold_image_uri="", gold_diagram_uri=""):
    """Render files as report entries. Returns ``(html, count)``.

    Raw text is collapsed by default with a line-numbered, no-wrap (diff-safe)
    body, a size/line-count meta, frontmatter chips, and a raw/rendered toggle
    for markdown. Rendered artifacts (images/diagrams/tables) stay inline.
    Shared by the Input and Output groups so both look identical."""
    out = ""
    count = 0
    for f in files:
        rel = f.relative_to(base_dir)
        kind, body = _render_artifact_body(f, rel, config, gold_image_uri, gold_diagram_uri)
        if not body:
            continue
        count += 1
        meta = _file_meta(f)
        if kind == "raw":
            text = _read_text(f, max_lines=200)
            content = _numbered_pre(text)
            toggle = rendered = ""
            if f.suffix.lower() in (".md", ".markdown"):
                rendered = f'<div class="md-rendered" hidden>{_md_to_html(text)}</div>'
                toggle = (
                    '<button type="button" class="md-toggle" onclick="'
                    "var d=this.closest('details'),n=d.querySelector('.numwrap'),"
                    "r=d.querySelector('.md-rendered');n.hidden=!n.hidden;r.hidden=!r.hidden;"
                    "this.textContent=n.hidden?'raw':'rendered';\">rendered</button>")
            out += (f'<details class="file-entry"><summary>'
                    f'<span class="file-name">{_esc(str(rel))}</span>{_frontmatter_chips(f)}'
                    f'<span class="file-meta">{_esc(meta)}</span></summary>\n'
                    f'{toggle}{content}{rendered}</details>\n')
        else:
            meta_html = f' <span class="file-meta">{_esc(meta)}</span>' if meta else ""
            out += f'<div class="file-badge">{_esc(str(rel))}{meta_html}</div>\n{body}'
    return out, count


def _render_per_case(summary, run_dir, config, baseline_dir, review):
    per_case = summary.get("per_case", {})
    if not per_case:
        return ""

    dataset_path = config.get("dataset", {}).get("path", "")
    shared_paths = _shared_output_paths(config)
    output_paths = [o.get("path", ".") for o in config.get("outputs", [])
                    if o.get("path") and o.get("path") not in shared_paths]
    feedback = review.get("feedback", {}) if review else {}
    cases_dir = run_dir / "cases"
    bl_cases_dir = baseline_dir / "cases" if baseline_dir else None

    # Resolve score_range per judge from config so each sampling histogram uses
    # its own axis (e.g. rubric judges are 0-2, not a hardcoded 1-5). Judges
    # without a configured score_range fall back to the observed min/max.
    judge_score_range = _judge_score_ranges(config)

    # Build pairwise lookup per case (full entry: winner + reasoning)
    pw_by_case = {}
    pw = summary.get("pairwise", {})
    for pc in pw.get("per_case", []):
        pw_by_case[pc.get("case_id", "")] = pc
    # Per-case verdict spread across repeated runs (stability), if available.
    pw_flipped = {fc.get("case_id"): fc.get("verdicts", [])
                  for fc in (pw.get("stability") or {}).get("flipped_cases", [])}
    pw_runs = (pw.get("stability") or {}).get("runs", 1)

    html = '<h2 class="section-heading">Per-Case Details</h2>\n'
    if baseline_dir:
        html += (f'<p class="section-intro">Comparing <strong>{run_dir.name}</strong> vs '
                 f'<strong>{baseline_dir.name}</strong></p>\n')

    for case_id in sorted(per_case.keys()):
        case_results = per_case[case_id]
        if not isinstance(case_results, dict):
            continue

        case_dir = cases_dir / case_id
        label = case_id

        # Count pass/fail/skip — bool judges: True=pass, False=fail
        # Numeric judges (LLM): pass if score >= threshold min_mean
        # None values: skipped (condition was false)
        thresholds = config.get("thresholds", {})
        passed = 0
        failed = 0
        skipped = 0
        total = len(case_results)
        for jname, r in case_results.items():
            if not isinstance(r, dict):
                continue
            val = r.get("value")
            if val is None:
                skipped += 1
            elif val is True:
                passed += 1
            elif val is False:
                failed += 1
            elif isinstance(val, (int, float)):
                # A numeric cell drags its case red only on a genuine low (a
                # bottom-band score on the judge's own scale), not for being
                # below the aggregate min_mean floor. Judges without a declared
                # score_range (e.g. pipeline_total) are neutral -> count as pass.
                lo, hi = judge_score_range.get(jname, (None, None))
                if lo is not None and hi is not None and _score_band_class(val, lo, hi) == "fail":
                    failed += 1
                else:
                    passed += 1
        status = "pass" if failed == 0 else "fail"

        # Pairwise badge or pass/fail accent — applied as a left-border class
        pw_badge = ""
        pw_entry = pw_by_case.get(case_id)
        if pw_entry:
            pw_winner = pw_entry.get("winner", "error")
            pw_badge = f" {_pairwise_badge(pw_winner)}"
            pw_class_map = {"A": "case-pw-a", "B": "case-pw-b",
                            "tie": "case-pw-tie", "error": "case-pw-error"}
            accent_class = pw_class_map.get(pw_winner, "case-pw-error")
        else:
            pw_winner = None
            accent_class = f"case-{status}"

        scored = total - skipped
        skip_note = f", {skipped} skip" if skipped else ""
        html += (f'<details open class="case {accent_class}"><summary>'
                 f'<span class="{status}">{_esc(str(label))}</span> '
                 f'<span class="skip">({passed}/{scored} pass{skip_note})</span>'
                 f'{pw_badge}</summary>\n')

        # Judge results table
        html += '<table><tr><th>Judge</th><th>Value</th><th>Rationale</th></tr>\n'
        for jname, jresult in sorted(case_results.items()):
            if not isinstance(jresult, dict):
                continue
            val = jresult.get("value")
            rat = str(jresult.get("rationale", ""))
            err = jresult.get("error", "")

            if val is None:
                val_html = '<span class="skip">SKIP</span>'
            elif val is True:
                val_html = '<span class="pass">PASS</span>'
            elif val is False:
                val_html = '<span class="fail">FAIL</span>'
            elif isinstance(val, (int, float)):
                # Colour by the score's position on the judge's own scale
                # (green/amber/red), not against the aggregate min_mean floor.
                lo, hi = judge_score_range.get(jname, (None, None))
                if lo is not None and hi is not None:
                    val_html = f'<span class="{_score_band_class(val, lo, hi)}">{val}</span>'
                else:
                    val_html = str(val)  # unknown scale: neutral, no verdict colour
            else:
                val_html = _esc(str(val)[:100])

            if err:
                rat = f"ERROR: {err}"
            # Per-case sampling distribution (from --samples): an ASCII glyph
            # (monospace, aligns cleanly) showing the spread on the judge's own
            # score scale. Shown for every sampled judge — a stable judge is a
            # single bar, which reads at a glance as "all samples agree". Raw
            # samples on hover.
            jst = jresult.get("stability")
            if (isinstance(jst, dict) and jst.get("samples", 1) > 1
                    and "min" in jst):
                vals = jst.get("values", [])
                samples = ", ".join(str(v) for v in vals)
                smin, smax = judge_score_range.get(jname, (None, None))
                glyph = _colorise_hist(_ascii_score_hist(val, vals, smin, smax))
                val_html += (f' <span class="ascii-range" '
                             f'title="samples: {_esc(samples)}">{glyph}</span>')
            elif (isinstance(jst, dict) and jst.get("samples", 1) > 1
                  and "pass_count" in jst):
                npass = jst["pass_count"]
                nsamp = jst["samples"]
                winner = "pass" if npass * 2 >= nsamp else "fail"
                glyph = _colorise_hist(_ascii_hist(
                    [("fail", nsamp - npass), ("pass", npass)], "F", "P", winner))
                val_html += (f' <span class="ascii-range" '
                             f'title="{npass}/{nsamp} pass">{glyph}</span>')

            sample_rats = jresult.get("sample_rationales")
            if sample_rats and len(sample_rats) > 1:
                tab_id = f"sr-{_esc(case_id)}-{_esc(jname)}"
                rat_html = '<div class="sample-tabs" role="tablist">'
                for si, sr in enumerate(sample_rats):
                    sv = sr.get("value")
                    sel = "true" if si == 0 else "false"
                    label = f'<span class="tab-score">{sv}</span>#{si+1}'
                    if sr.get("error"):
                        label = f'<span class="tab-score">ERR</span>#{si+1}'
                    rat_html += (f'<button class="sample-tab" role="tab" '
                                 f'aria-selected="{sel}" '
                                 f'data-panel="{tab_id}-{si}">{label}</button>')
                rat_html += '</div>'
                for si, sr in enumerate(sample_rats):
                    active = " active" if si == 0 else ""
                    sr_rat = sr.get("rationale") or sr.get("error") or ""
                    sr_html = _md_to_html(_normalize_escapes(str(sr_rat))) if sr_rat else ""
                    rat_html += (f'<div class="sample-panel{active}" '
                                 f'id="{tab_id}-{si}" role="tabpanel">{sr_html}</div>')
            else:
                rat_html = _md_to_html(_normalize_escapes(rat)) if rat else ""
            html += (f'<tr><td>{_esc(jname)}</td><td>{val_html}</td>'
                     f'<td class="rationale">{rat_html}</td></tr>\n')

        # Pairwise comparison as a judge-table row (verdict + reasoning)
        if pw_entry:
            pw_reasoning = pw_entry.get("reasoning") or ""
            if isinstance(pw_reasoning, dict):
                pw_reasoning = pw_reasoning.get("reasoning", "")
            pw_reasoning = str(pw_reasoning)
            pw_rat_html = _md_to_html(_normalize_escapes(pw_reasoning)) if pw_reasoning else ""
            # If this case flipped across repeated runs, show the verdict spread.
            verdict_html = _pairwise_badge(pw_winner)
            if pw_runs and pw_runs > 1:
                # stable cases didn't flip → all runs == winner (single bar)
                verds = pw_flipped.get(case_id) or [pw_winner] * pw_runs
                from collections import Counter as _Counter
                vc = _Counter(verds)
                cats = [("A", vc.get("A", 0)), ("tie", vc.get("tie", 0)),
                        ("B", vc.get("B", 0))]
                errs = vc.get("error", 0)
                if errs:
                    cats.append(("error", errs))
                glyph = _colorise_hist(_ascii_hist(
                    cats, "A", "E" if errs else "B", pw_winner))
                verdict_html += (f'<br><span class="ascii-range" '
                                 f'title="verdicts: {_esc("/".join(verds))}">{glyph}</span>')
            html += (f'<tr><td>pairwise</td>'
                     f'<td>{verdict_html}</td>'
                     f'<td class="rationale">{pw_rat_html}</td></tr>\n')

        html += "</table>\n"

        # Human feedback
        case_feedback = feedback.get(case_id, "")
        if case_feedback:
            html += (f'<div class="feedback-box"><strong>Human feedback:</strong> '
                     f'{_esc(str(case_feedback))}</div>\n')

        # Input files: the case input record plus the files staged into the
        # workspace, rendered like Output files (a group, each file collapsible).
        if dataset_path:
            in_dir = _resolve_dataset_case_dir(dataset_path, case_id)
            input_files = _case_input_files(in_dir, config) if in_dir else []
            if input_files:
                in_body, in_n = _render_file_entries(input_files, in_dir, config)
                if in_n:
                    html += (f'<details open class="io-group io-input">'
                             f'<summary>Input ({in_n})</summary>\n{in_body}</details>\n')

        # Output files — when baseline is provided, skip files under output_paths
        # since those will appear in the baseline diff section below.
        has_baseline = bl_cases_dir and (bl_cases_dir / case_id).exists()

        # Resolve gold standard image for comparison
        gold = _find_gold_standard_images(case_id, dataset_path)
        gold_image_uri = _image_to_data_uri(gold["image"]) if gold["image"] else ""
        gold_diagram_uri = gold.get("diagram_uri") or ""

        # Execution logs at the case root — not skill output, exclude from
        # the report.  Only exclude root-level files, not nested ones with the
        # same name (a skill could legitimately produce artifacts/stdout.log).
        _EXEC_LOG_PATHS = {"stdout.log", "stderr.log", "run_result.json", "input.yaml"}
        if case_dir.exists():
            def _file_sort_key(f):
                """Sort visual artifacts first: images, then diagrams, then the rest."""
                if _is_image_file(f):
                    return (0, f.name)
                if f.suffix in _DIAGRAM_SUFFIXES:
                    return (1, f.name)
                return (2, f.name)

            files = sorted((f for f in case_dir.rglob("*") if f.is_file()
                           and str(f.relative_to(case_dir)) not in _EXEC_LOG_PATHS
                           and not str(f.relative_to(case_dir)).startswith("subagents/")
                           and not any(str(f.relative_to(case_dir)).startswith(sp)
                                       for sp in shared_paths)),
                           key=_file_sort_key)
            if has_baseline:
                # Exclude files under output_paths — they'll be in the diff
                files = [f for f in files
                         if not any(str(f.relative_to(case_dir)).startswith(op)
                                    for op in output_paths)]
            if files:
                out_body, out_n = _render_file_entries(
                    files, case_dir, config, gold_image_uri, gold_diagram_uri)
                if out_n:
                    html += (f'<details open class="io-group io-output">'
                             f'<summary>Output ({out_n})</summary>\n{out_body}</details>\n')

        # Baseline diff
        if has_baseline:
            bl_case_dir = bl_cases_dir / case_id
            diffs = []
            diff_stats = {}
            for out_path in output_paths:
                curr_dir = case_dir / out_path if out_path != "." else case_dir
                base_dir = bl_case_dir / out_path if out_path != "." else bl_case_dir
                if not curr_dir.exists() and not base_dir.exists():
                    continue
                curr_files = {f.name: f for f in curr_dir.iterdir() if f.is_file()} if curr_dir.exists() else {}
                base_files = {f.name: f for f in base_dir.iterdir() if f.is_file()} if base_dir.exists() else {}
                def _diff_sort_key(name):
                    p = Path(name)
                    if p.suffix.lower() in _IMAGE_SUFFIXES:
                        return (0, name)
                    if p.suffix.lower() in _DIAGRAM_SUFFIXES:
                        return (1, name)
                    return (2, name)

                for name in sorted(set(curr_files) | set(base_files), key=_diff_sort_key):
                    curr_f = curr_files.get(name)
                    base_f = base_files.get(name)
                    # Image comparison
                    if ((curr_f and _is_image_file(curr_f))
                            or (base_f and _is_image_file(base_f))):
                        curr_uri = _image_to_data_uri(curr_f) if curr_f else ""
                        base_uri = _image_to_data_uri(base_f) if base_f else ""
                        if curr_uri and base_uri:
                            diffs.append((f"{out_path}/{name}",
                                _render_image_compare(
                                    curr_uri, base_uri,
                                    gen_label="Current",
                                    ref_label="Baseline",
                                    ref_class="img-label-bl")))
                        elif curr_uri:
                            diffs.append((f"{out_path}/{name} (new)",
                                _render_standalone_image(curr_uri, name)))
                        elif base_uri:
                            diffs.append((f"{out_path}/{name} (deleted)",
                                _render_standalone_image(base_uri, f"{name} (baseline)")))
                        continue
                    # Diagram comparison (render to SVG)
                    if name.endswith(tuple(_DIAGRAM_SUFFIXES)):
                        sib_c = {s.name for s in curr_dir.iterdir()} if curr_dir.exists() else set()
                        sib_b = {s.name for s in base_dir.iterdir()} if base_dir.exists() else set()
                        # Skip if a rendered sibling (PNG) is already shown
                        has_rendered = (f"{name}.png" in sib_c or f"{name}.svg" in sib_c
                                        or f"{name}.png" in sib_b or f"{name}.svg" in sib_b)
                        if has_rendered:
                            continue
                        curr_uri = _try_render_diagram(curr_f, sib_c) if curr_f else ""
                        base_uri = _try_render_diagram(base_f, sib_b) if base_f else ""
                        if curr_uri and base_uri:
                            diffs.append((f"{out_path}/{name}",
                                _render_image_compare(
                                    curr_uri, base_uri,
                                    gen_label="Current",
                                    ref_label="Baseline",
                                    ref_class="img-label-bl")))
                        elif curr_uri:
                            diffs.append((f"{out_path}/{name}",
                                _render_standalone_image(curr_uri, name)))
                        # Fall through to text diff if rendering failed
                        if curr_uri or base_uri:
                            continue
                    # Graph JSON comparison (render to SVG)
                    if _resolve_artifact_type(name, config) == "graph":
                        curr_svg = _render_graph_spec_to_svg(curr_f) if curr_f else ""
                        base_svg = _render_graph_spec_to_svg(base_f) if base_f else ""
                        if curr_svg or base_svg:
                            curr_uri = _svg_to_data_uri(curr_svg) if curr_svg else ""
                            base_uri = _svg_to_data_uri(base_svg) if base_svg else ""
                            if curr_uri and base_uri:
                                diffs.append((f"{out_path}/{name}",
                                    _render_image_compare(
                                        curr_uri, base_uri,
                                        gen_label="Current",
                                        ref_label="Baseline",
                                        ref_class="img-label-bl")))
                            elif curr_uri:
                                diffs.append((f"{out_path}/{name}",
                                    _render_standalone_image(curr_uri, name)))
                            continue
                    # Metrics comparison (render as side-by-side table)
                    if _resolve_artifact_type(name, config) == "metrics":
                        try:
                            curr_m = json.loads(curr_f.read_text()) if curr_f else {}
                            base_m = json.loads(base_f.read_text()) if base_f else {}
                            if curr_m or base_m:
                                all_keys = list(dict.fromkeys(list(base_m.keys()) + list(curr_m.keys())))
                                rows = f'<div class="file-badge">{_esc(f"{out_path}/{name}")}</div>\n'
                                rows += '<table class="metrics-table"><tr><th>Metric</th><th>Baseline</th><th>Current</th></tr>\n'
                                for k in all_keys:
                                    bv = base_m.get(k, "—")
                                    cv = curr_m.get(k, "—")
                                    rows += f'<tr><td>{_esc(str(k))}</td><td>{_esc(str(bv))}</td><td>{_esc(str(cv))}</td></tr>\n'
                                rows += '</table>\n'
                                diffs.append((f"{out_path}/{name}", rows))
                                continue
                        except (json.JSONDecodeError, OSError):
                            pass
                    # Text comparison
                    try:
                        ct = curr_f.read_text() if curr_f else ""
                        bt = base_f.read_text() if base_f else ""
                    except (UnicodeDecodeError, OSError):
                        continue
                    if ct != bt:
                        diff_html = _side_by_side_diff(
                            bt, ct,
                            left_label=f"baseline/{out_path}/{name}",
                            right_label=f"current/{out_path}/{name}")
                        adds, dels = _diff_stats(bt, ct)
                        diff_stats[f"{out_path}/{name}"] = (
                            f' <span class="diff-stat">+{adds} -{dels}</span>')
                        diffs.append((f"{out_path}/{name}", diff_html))

            if diffs:
                html += (f'<details open class="io-group io-diff">'
                         f'<summary>Baseline diff ({len(diffs)})</summary>\n')
                for fname, diff_html in diffs:
                    html += (f'<div class="file-badge">{_esc(fname)}'
                             f'{diff_stats.get(fname, "")}</div>\n{diff_html}\n')
                html += "</details>\n"

        html += "</details>\n"

    return html


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _wrap_section(content: str) -> str:
    """Wrap a section's HTML in a styled card. Returns empty if content is empty."""
    if not content.strip():
        return ""
    return f'<section class="section">\n{content}</section>\n'


def generate_report(config, summary, run_result, run_dir,
                    review=None, baseline_dir=None,
                    baseline_summary=None, baseline_result=None,
                    reward_cfg=None, report_title=None):
    global _img_compare_counter
    _img_compare_counter = 0
    name = config.get("name", "Eval")
    run_id = summary.get("run_id", run_dir.name)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(name)} — {_esc(run_id)}</title>
<script>{THEME_SCRIPT}</script>
<style>{CSS}</style>
</head>
<body>
<div id="report-toolbar">
<button id="btn-download" type="button" aria-label="Download report"><svg viewBox="0 0 16 16"><path d="M8 1a.5.5 0 0 1 .5.5v8.793l2.146-2.147a.5.5 0 0 1 .708.708l-3 3a.5.5 0 0 1-.708 0l-3-3a.5.5 0 1 1 .708-.708L7.5 10.293V1.5A.5.5 0 0 1 8 1z"/><path d="M2.5 13a.5.5 0 0 0 0 1h11a.5.5 0 0 0 0-1h-11z"/></svg><span class="toolbar-tip">Download</span></button>
<button id="btn-copy" type="button" aria-label="Copy file path"><svg viewBox="0 0 16 16"><path d="M10.5 1h-7A1.5 1.5 0 0 0 2 2.5v9A1.5 1.5 0 0 0 3.5 13h7a1.5 1.5 0 0 0 1.5-1.5v-9A1.5 1.5 0 0 0 10.5 1zm.5 10.5a.5.5 0 0 1-.5.5h-7a.5.5 0 0 1-.5-.5v-9a.5.5 0 0 1 .5-.5h7a.5.5 0 0 1 .5.5v9z"/><path d="M12.5 4a.5.5 0 0 1 .5.5v9a1.5 1.5 0 0 1-1.5 1.5h-7a.5.5 0 0 1 0-1h7a.5.5 0 0 0 .5-.5v-9a.5.5 0 0 1 .5-.5z"/></svg><span class="toolbar-tip">Copy path</span></button>
<div class="toolbar-sep"></div>
<button id="theme-toggle" type="button" aria-label="Toggle theme"><svg viewBox="0 0 16 16"><path d="M8 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6z"/><path d="M8 0a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 0zm0 13a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 13zm8-5a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 16 8zM3 8a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 3 8zm10.657-5.657a.5.5 0 0 1 0 .707l-1.414 1.415a.5.5 0 1 1-.707-.708l1.414-1.414a.5.5 0 0 1 .707 0zm-9.193 9.193a.5.5 0 0 1 0 .707L3.05 13.657a.5.5 0 0 1-.707-.707l1.414-1.414a.5.5 0 0 1 .707 0zm9.193 2.121a.5.5 0 0 1-.707 0l-1.414-1.414a.5.5 0 0 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .707zM4.464 4.465a.5.5 0 0 1-.707 0L2.343 3.05a.5.5 0 1 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .708z"/></svg><span class="toolbar-tip">Light theme</span></button>
<div class="toolbar-toast" id="toolbar-toast"><svg viewBox="0 0 16 16"><path d="M8 0a8 8 0 1 0 0 16A8 8 0 0 0 8 0zm3.354 6.354-4 4a.5.5 0 0 1-.708 0l-2-2a.5.5 0 1 1 .708-.708L7 9.293l3.646-3.647a.5.5 0 0 1 .708.708z"/></svg><span id="toolbar-toast-msg"></span></div>
</div>
"""
    baseline_id = None
    if baseline_summary:
        baseline_id = baseline_summary.get("run_id")
    if not baseline_id and baseline_dir:
        baseline_id = baseline_dir.name
    html += _render_header(config, run_id, run_result, baseline_id, title=report_title)
    html += _wrap_section(_render_run_config(run_result, baseline_result,
                                             summary=summary,
                                             baseline_summary=baseline_summary))
    html += _render_analysis(run_dir, summary, run_result, baseline_summary)
    html += _wrap_section(_render_scoring_summary(summary, config, baseline_summary))
    html += _wrap_section(_render_regressions(summary, config))
    html += _wrap_section(_render_shared_outputs(run_dir, config))
    # Per-Case Reward Overview is an RL-training reward summary — only render it
    # when a reward is configured. For judge-only evals the Scoring Summary
    # (aggregate per judge) and Per-Case Details (per case) already cover the scores.
    if (config or {}).get("reward"):
        html += _wrap_section(_render_reward_overview(summary, config, reward_cfg))
    html += _render_per_case(summary, run_dir, config, baseline_dir, review)
    html += f"\n<script>{TOGGLE_SCRIPT}</script>\n"
    html += f"<script>{SHARE_SCRIPT}</script>\n"
    html += f"<script>{IMAGE_COMPARE_SCRIPT}</script>\n"
    html += f"<script>{SAMPLE_TABS_SCRIPT}</script>\n"
    html += "</body>\n</html>\n"
    return html


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--baseline", default=None,
                        help="Baseline run ID for comparison")
    parser.add_argument("--open", action="store_true",
                        help="Open report in browser")
    parser.add_argument("--title", default=None,
                        help="Report title (default: config 'title' or "
                             "'Agent Eval Report')")
    args = parser.parse_args()

    # Load config as EvalConfig object to access eval_name() method
    from agent_eval.config import EvalConfig
    config_obj = EvalConfig.from_yaml(Path(args.config))
    eval_name = config_obj.eval_name()

    # Also load as dict for backward compat with report template — through
    # the single raw loader, so an `extends:` overlay renders merged and paths
    # resolve against the root of the chain (the base config's directory).
    from agent_eval.config import load_raw
    config, config_chain = load_raw(Path(args.config))
    config_dir = Path(config_chain[0]).parent

    # Resolve dataset.path relative to config file location so downstream
    # functions get an absolute path (not CWD-relative).
    ds = config.get("dataset") if config else None
    if isinstance(ds, dict):
        ds_path = ds.get("path", "")
        if ds_path and not Path(ds_path).is_absolute():
            ds["path"] = str((config_dir / ds_path).resolve())
    if eval_name and (not isinstance(eval_name, str)
                      or "/" in eval_name or "\\" in eval_name
                      or eval_name in (".", "..")
                      or any(ord(c) < 32 for c in eval_name)):
        print(f"ERROR: invalid skill name: {eval_name!r}", file=sys.stderr)
        sys.exit(1)
    # Validate run_id / baseline to prevent path traversal (CWE-22)
    for _arg_name, _arg_val in [("--run-id", args.run_id),
                                ("--baseline", args.baseline)]:
        if _arg_val is None:
            continue
        if (not isinstance(_arg_val, str) or not _arg_val
                or "/" in _arg_val or "\\" in _arg_val
                or _arg_val in (".", "..")
                or any(ord(c) < 32 for c in _arg_val)):
            print(f"ERROR: {_arg_name} must be a single path segment: {_arg_val!r}",
                  file=sys.stderr)
            sys.exit(1)

    runs_base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    runs_dir = runs_base / eval_name if eval_name else runs_base
    run_dir = runs_dir / args.run_id
    baseline_dir = runs_dir / args.baseline if args.baseline else None

    if not run_dir.exists():
        print(f"ERROR: run directory not found: {run_dir}", file=sys.stderr)
        sys.exit(1)

    # Run before_report hooks: fired after analysis.md is authored (by the eval-run
    # agent in its analysis step) and before the HTML is assembled, so a hook may
    # annotate analysis.md or other run artifacts and have those changes appear in
    # the rendered report. Uses run_hooks_safe so report generation never fails
    # because of a reporting-side hook.
    if config_obj.hooks.before_report:
        from agent_eval.hooks import build_hook_env, run_hooks_safe
        # Give the hook the real run context: AGENT_EVAL_WORKSPACE = the run dir
        # (so $AGENT_EVAL_WORKSPACE/analysis.md and other artifacts resolve), and
        # the model from run_result.json.
        _rr = _load_json(run_dir / "run_result.json") or {}
        hook_env = build_hook_env(
            workspace=str(run_dir),
            run_id=args.run_id,
            config_path=str(Path(args.config).resolve()),
            project_root=str(Path.cwd()),
            model=_rr.get("model", ""),
        )
        print("Running before_report hooks...", file=sys.stderr)
        run_hooks_safe(config_obj.hooks.before_report, env=hook_env,
                       cwd=Path.cwd(), log_dir=run_dir / "hooks",
                       phase_name="before_report")

    summary = _load_yaml(run_dir / "summary.yaml")
    run_result = _load_json(run_dir / "run_result.json")
    review = _load_yaml(run_dir / "review.yaml") or None

    baseline_summary = _load_yaml(baseline_dir / "summary.yaml") if baseline_dir else None
    baseline_result = _load_json(baseline_dir / "run_result.json") if baseline_dir else None

    html = generate_report(
        config=config,
        summary=summary,
        run_result=run_result,
        run_dir=run_dir,
        review=review,
        baseline_dir=baseline_dir,
        baseline_summary=baseline_summary,
        reward_cfg=load_reward_cfg(args.config),
        baseline_result=baseline_result,
        report_title=args.title,
    )

    output_path = run_dir / "report.html"
    output_path.write_text(html)
    print(f"REPORT: {output_path}")

    if args.open:
        import webbrowser
        webbrowser.open(f"file://{output_path.resolve()}")


if __name__ == "__main__":
    main()
