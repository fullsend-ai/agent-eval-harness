#!/usr/bin/env python3
"""Compare evaluation results across multiple models or runs.

Usage:
    compare.py discover <input-dir>
    compare.py generate <input-dir> --output <dir> [--title TEXT] [--overview TEXT]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from html import escape
from pathlib import Path
from urllib.parse import quote

import yaml

from agent_eval.optimize.export import is_rollout_run


def load_yaml(path):
    """Load a YAML mapping, tolerating unreadable/malformed files.

    Returns {} on any read/parse error or when the document is not a mapping,
    so a single corrupt summary.yaml never aborts the whole comparison.
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"WARNING: could not parse {path}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def load_json(path):
    """Load a JSON object, tolerating unreadable/malformed files.

    Returns None on any read/parse error so the run is treated as missing its
    run_result.json rather than crashing the whole comparison.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARNING: could not parse {path}: {e}", file=sys.stderr)
        return None


def _slugify(text):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return slug or "run"


def _unique_slug(run_dir, input_dir, used):
    """Filesystem-safe, unique-per-run identifier.

    Derived from the run directory's path relative to input_dir (not just its
    basename), so two runs whose parent dirs differ but whose basenames match
    (e.g. evalA/2026-07-30-opus and evalB/2026-07-30-opus) do not collide when
    their report.html is copied into the output tree or referenced by iframes.
    """
    try:
        rel = run_dir.relative_to(input_dir)
    except ValueError:
        rel = Path(run_dir.name)
    base = _slugify(rel.as_posix())
    slug = base
    i = 2
    while slug in used:
        slug = f"{base}-{i}"
        i += 1
    used.add(slug)
    return slug


def discover_runs(input_dir):
    input_dir = Path(input_dir)
    if not input_dir.exists() or not input_dir.is_dir():
        raise NotADirectoryError(f"Input directory not found: {input_dir}")
    runs = []
    used_slugs = set()
    for summary_path in sorted(input_dir.rglob("summary.yaml")):
        d = summary_path.parent
        if is_rollout_run(d, input_dir):
            # An optimization campaign writes one run dir per rollout. They are
            # real runs, but there are hundreds of them and they are not what
            # someone comparing models asked about. Point the tool at the
            # .optimize dir to get them.
            continue
        summary = load_yaml(summary_path)
        run = {
            "dir": str(d),
            "name": d.name,
            "slug": _unique_slug(d, input_dir, used_slugs),
            "summary": summary,
            "run_result": None,
            "html_report": None,
        }
        result_path = d / "run_result.json"
        if result_path.exists():
            run["run_result"] = load_json(result_path)
        html_path = d / "report.html"
        if html_path.exists():
            run["html_report"] = str(html_path)
        runs.append(run)
    return runs


def get_model(run):
    if run["run_result"]:
        return run["run_result"].get("model") or "unknown"
    # No run_result.json: recover the model from the run_id if it embeds a
    # "claude-..." id (default run_ids can look like "<date>-claude-opus-4-8").
    # Reconstruct the FULL id so it matches the run_result branch and the
    # MODEL_COLORS/MODEL_SHORT keys — never a bare "opus-4-8".
    run_id = run["summary"].get("run_id", "") or ""
    tokens = run_id.split("-")
    for i, token in enumerate(tokens):
        if token == "claude":
            return "-".join(tokens[i:])
    # Truly unknown: fall back to the run's own name so distinct runs are not
    # merged (and averaged) together under a single meaningless "unknown" model.
    return run.get("name") or "unknown"


REAL_COST_PREFIX = "openrouter:"
LEGACY_COST_SOURCES = {"openrouter-reconciled": "openrouter:generation",
                       "runner-reported": "runner:reported", "harness-estimate": "harness:estimate"}


def get_cost_source(run):
    """Canonical cost_source of a run (legacy literals normalised); a run
    without the field is a runner-reported estimate (the historical default)."""
    rr = run.get("run_result") or {}
    value = rr.get("cost_source") or "runner:reported"
    return LEGACY_COST_SOURCES.get(value, value)


def cost_source_class(source):
    """`real` for provider-priced costs, `unavailable`, else `estimate`."""
    if source == "unavailable":
        return "unavailable"
    return "real" if str(source).startswith(REAL_COST_PREFIX) else "estimate"


def cost_source_notes(groups):
    """Footnotes for the cost table (spec 014): a mix of provider-priced and
    estimated/unavailable costs must not be read as one comparable column, and
    runs whose routing audit differs are different factor levels."""
    notes = []
    classes = {}
    for m, model_runs in groups.items():
        for r in model_runs:
            classes.setdefault(cost_source_class(get_cost_source(r)), set()).add(m)
    if len(classes) > 1:
        detail = "; ".join(f"{cls}: {', '.join(sorted(short_name(x) for x in ms))}"
                           for cls, ms in sorted(classes.items()))
        notes.append("Cost sources are mixed and not directly comparable — " + detail
                     + ". Provider-priced (openrouter:*) costs are billed spend; runner/harness "
                       "figures are estimates; unavailable means no truth source landed.")
    for m, model_runs in groups.items():
        shas, dirty, levels, served_by_run = set(), 0, set(), []
        for r in model_runs:
            routing = (r.get("run_result") or {}).get("routing") or {}
            levels.add(routing.get("enforcement") or "none")
            if routing:
                shas.add(routing.get("sha"))
                if routing.get("violations") or routing.get("audit_complete") is False:
                    dirty += 1
                served_by_run.append((r.get("name") or "?", routing.get("declared") or {},
                                      routing.get("served") or {}))
        if len(shas) > 1:
            notes.append(f"{short_name(m)}: runs were pooled across different routing declarations "
                         f"({len(shas)} routing shas) — served providers may differ.")
        if len(levels) > 1:
            notes.append(f"{short_name(m)}: runs were pooled across enforcement levels "
                         f"({', '.join(sorted(levels))}) — these are different factor levels.")
        if dirty:
            notes.append(f"{short_name(m)}: {dirty} run(s) have routing violations or an incomplete audit.")
        histograms = {tuple(sorted(s.items())) for _, _, s in served_by_run if s}
        if len(histograms) > 1:
            # The snapshot diff: declared pins next to what was actually served.
            detail = " | ".join(
                f"{name}: declared {', '.join(f'{k}={v}' for k, v in d.items()) or 'none'}; "
                f"served {', '.join(f'{k}: {n}' for k, n in sorted(s.items())) or 'none'}"
                for name, d, s in served_by_run)
            notes.append(f"{short_name(m)}: served providers differ across runs — {detail}")
    return notes


def get_metric(run, key, default=None):
    rr = run["run_result"] or {}
    if key == "cost_usd":
        return rr.get("cost_usd", default)
    if key == "num_turns":
        return rr.get("num_turns", default)
    if key == "wall_clock_s":
        # `wall_clock_s` is what execute.py writes today; the harbor runner and
        # older runs record elapsed time as `duration_s`. Fall back so those
        # runs still report a wall clock instead of "--" — but only when
        # `duration_s` really is elapsed time. Harbor sets
        # `duration_s = wall_clock_s or total_agent_duration`, so when its job
        # timestamps are missing the field degrades to the SUM of trial
        # durations, which over-reports wall clock for parallel trials. That
        # case is identifiable: harbor also writes the sum as
        # `agent_duration_s`, and the two are equal only on the degraded path.
        wall = rr.get("wall_clock_s")
        if wall is None:
            duration = rr.get("duration_s")
            if duration is not None and duration != rr.get("agent_duration_s"):
                wall = duration
        return default if wall is None else wall
    if key == "output_tokens":
        return (rr.get("token_usage") or {}).get("output", default)
    if key == "cache_hit_rate":
        return (run["summary"].get("run_metrics") or {}).get("cache_hit_rate", default)
    if key == "cost_per_turn":
        return (run["summary"].get("run_metrics") or {}).get("cost_per_turn_usd", default)
    if key == "output_tokens_per_turn":
        return (run["summary"].get("run_metrics") or {}).get("output_tokens_per_turn", default)
    return default


def get_judge_mean(run, judge_name):
    judges = run["summary"].get("judges", {})
    j = judges.get(judge_name, {})
    if isinstance(j, dict):
        v = j.get("mean")
        if v is not None:
            return v
        return j.get("pass_rate")
    return None


def get_case_score(run, case_name, judge_name):
    per_case = run["summary"].get("per_case", {})
    case = per_case.get(case_name, {})
    judge = case.get(judge_name, {})
    return judge.get("value")


def get_all_judge_names(runs):
    names = set()
    for r in runs:
        for k in (r["summary"].get("judges") or {}):
            names.add(k)
    return sorted(names)


def _is_pass_rate(judge_name, runs):
    """Whether a judge is pass-rate (boolean) style.

    Authoritative, not a value-shape guess: score.py records a ``pass_rate`` for
    boolean judges and leaves it ``None`` for numeric ones, so a numeric judge
    whose scores happen to all be 0/1 is no longer mis-rendered as a percentage.
    """
    for r in runs:
        j = (r["summary"].get("judges") or {}).get(judge_name)
        if isinstance(j, dict) and j.get("pass_rate") is not None:
            return True
    return False


def pick_card_judges(all_judges, model_aggs, models, max_judges=4):
    """Pick the most interesting judges for model cards.

    Ranks by variance across models — judges where all models score identically
    (e.g. pass_rate=1.0 everywhere) are least interesting.
    """
    scored = []
    for judge in all_judges:
        values = [model_aggs[m].get(f"judge_{judge}", {}).get("avg") for m in models]
        clean = [v for v in values if v is not None]
        if not clean:
            continue
        spread = max(clean) - min(clean) if len(clean) > 1 else 0
        scored.append((spread, judge))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [j for _, j in scored[:max_judges]]


def get_all_case_names(runs):
    names = set()
    for r in runs:
        for k in (r["summary"].get("per_case") or {}):
            names.add(k)
    return sorted(names)


def group_by_model(runs):
    groups = defaultdict(list)
    for r in runs:
        groups[get_model(r)].append(r)
    return dict(groups)


def aggregate(values):
    clean = [v for v in values if v is not None]
    if not clean:
        return {"avg": None, "min": None, "max": None, "count": 0}
    return {
        "avg": sum(clean) / len(clean),
        "min": min(clean),
        "max": max(clean),
        "count": len(clean),
    }


MODEL_COLORS = {
    "claude-opus-4-6": "#58a6ff",
    "claude-opus-4-7": "#bc8cff",
    "claude-opus-4-8": "#db6d28",
    "claude-opus-4-8[1m]": "#db6d28",
    "claude-sonnet-4-6": "#f0883e",
    "claude-sonnet-4-6[1m]": "#f0883e",
    "claude-haiku-4-5": "#f85149",
}

MODEL_SHORT = {
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-8[1m]": "Opus 4.8 [1M]",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-6[1m]": "Sonnet 4.6 [1M]",
    "claude-haiku-4-5": "Haiku 4.5",
}


def short_name(model):
    """Human-readable model label.

    Falls back to deriving one from a "claude-<family>-<version>[suffix]" id so
    models not in MODEL_SHORT (e.g. a newly-shipped version) still render cleanly
    instead of showing the raw id.
    """
    if model in MODEL_SHORT:
        return MODEL_SHORT[model]
    m = re.match(r"^claude-([a-z]+)-([0-9]+(?:-[0-9]+)*)(\[.*\])?$", model or "")
    if m:
        family = m.group(1).capitalize()
        version = m.group(2).replace("-", ".")
        suffix = f" {m.group(3).upper()}" if m.group(3) else ""
        return f"{family} {version}{suffix}"
    return model


def color_for(model):
    return MODEL_COLORS.get(model, "#8b949e")


def _fmt_usd_nonzero(v, decimals, max_decimals=10):
    """Format a dollar amount at `decimals` places, widening until the first
    significant digit survives rounding. A real cost never renders as zero."""
    while decimals < max_decimals and v != 0 and round(abs(v), decimals) == 0:
        decimals += 1
    return f"${v:,.{decimals}f}"


def fmt(v, fmt_type="num"):
    if v is None:
        return "--"
    if fmt_type == "usd":
        # Keep cents for run totals, but never round a real cost down to
        # "$0.00": sub-cent figures (per-turn costs on cheap models) get
        # enough decimals to stay distinguishable.
        if v != 0 and abs(v) < 0.01:
            return _fmt_usd_nonzero(v, 4)
        return f"${v:,.2f}"
    if fmt_type == "usd_precise":
        # Per-turn costs span three orders of magnitude across models; a fixed
        # 4 decimals keeps the whole column comparable at a glance.
        return _fmt_usd_nonzero(v, 4)
    if fmt_type == "pct":
        return f"{v * 100:.1f}%"
    if fmt_type == "int":
        return f"{int(v):,}"
    if fmt_type == "time":
        if v < 60:
            return f"{int(v)}s"
        if v < 3600:
            return f"{int(v / 60)} min"
        hours, minutes = divmod(int(v / 60), 60)
        return f"{hours}h {minutes:02d}m"
    if fmt_type == "tokens":
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1000:
            return f"{v / 1000:.1f}K"
        return f"{int(v)}"
    return f"{v:.2f}"


def fmt_range(agg, fmt_type="num"):
    if agg["count"] == 0:
        return "--"
    if agg["count"] == 1:
        return fmt(agg["avg"], fmt_type)
    return f"{fmt(agg['avg'], fmt_type)} ({fmt(agg['min'], fmt_type)}-{fmt(agg['max'], fmt_type)})"


CSS = """
:root {
  --bg: #cbd1da; --surface: #ffffff; --surface2: #f1f5f9;
  --border: #e2e8f0; --text: #0f172a; --text-muted: #64748b;
  --accent: #2563eb; --green: #16a34a; --yellow: #d97706; --red: #dc2626;
  --hover: rgba(15,23,42,0.04);
  --best-bg: #dcfce7; --worst-bg: #fee2e2;
  --header-1: #ffffff; --header-2: #e2e8f0;
  --verdict-1: rgba(22,163,74,0.08); --verdict-2: rgba(37,99,235,0.06);
  --verdict-border: rgba(22,163,74,0.35);
  color-scheme: light;
}
:root[data-theme="dark"] {
  --bg: #0b1220; --surface: #0f1729; --surface2: #162033;
  --border: #1e2c44; --text: #e2e8f0; --text-muted: #94a3b8;
  --accent: #60a5fa; --green: #4ade80; --yellow: #fbbf24; --red: #f87171;
  --hover: rgba(255,255,255,0.03);
  --best-bg: rgba(74,222,128,0.10); --worst-bg: rgba(248,113,113,0.10);
  --header-1: #162033; --header-2: #0b1220;
  --verdict-1: rgba(74,222,128,0.08); --verdict-2: rgba(96,165,250,0.06);
  --verdict-border: rgba(74,222,128,0.25);
  color-scheme: dark;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { background: var(--bg); }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
       background: var(--bg); color: var(--text); line-height: 1.6;
       transition: background-color .15s ease, color .15s ease; }
.header { position: relative; background: linear-gradient(135deg, var(--header-1) 0%, var(--header-2) 100%);
           border-bottom: 1px solid var(--border); padding: 24px 32px; }
.header h1 { font-size: 24px; font-weight: 600; margin-bottom: 4px; }
.header .subtitle { color: var(--text-muted); font-size: 14px; }
#theme-toggle { position: absolute; top: 20px; right: 24px; width: 34px; height: 34px;
                background: var(--surface); border: 1px solid var(--border); color: var(--text);
                border-radius: 8px; cursor: pointer; font-size: 16px; line-height: 1;
                display: flex; align-items: center; justify-content: center; transition: border-color .15s; }
#theme-toggle:hover { border-color: var(--accent); }
.tab-bar { display: flex; background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 0 16px; overflow-x: auto; }
.tab-bar button { background: none; border: none; color: var(--text-muted); padding: 12px 20px;
                  cursor: pointer; font-size: 14px; font-weight: 500;
                  border-bottom: 2px solid transparent; white-space: nowrap; transition: all 0.15s; }
.tab-bar button:hover { color: var(--text); background: var(--hover); }
.tab-bar button.active { border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }
.page { max-width: 1400px; margin: 0 auto; padding: 24px 32px; }
.overview { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px;
            padding: 20px 24px; margin-bottom: 20px; font-size: 14px; line-height: 1.7; color: var(--text-muted); }
.verdict { background: linear-gradient(135deg, var(--verdict-1) 0%, var(--verdict-2) 100%);
           border: 1px solid var(--verdict-border); border-radius: 12px;
           padding: 24px 28px; margin-bottom: 28px; }
.verdict h2 { font-size: 18px; color: var(--green); margin-bottom: 10px; }
.verdict p { font-size: 15px; line-height: 1.7; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
         gap: 16px; margin-bottom: 28px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
        padding: 20px 24px; position: relative; }
.card .badge { position: absolute; top: -10px; right: 16px; font-size: 11px; font-weight: 700;
               padding: 3px 10px; border-radius: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
.card h3 { font-size: 18px; font-weight: 600; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 16px; }
.stat { display: flex; flex-direction: column; }
.stat .label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; display: block; }
.stat .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; display: block; }
.green { color: var(--green); } .yellow { color: var(--yellow); }
.red { color: var(--red); } .muted { color: var(--text-muted); }
section { margin-bottom: 32px; }
section h2 { font-size: 17px; font-weight: 600; margin-bottom: 14px; padding-bottom: 8px;
             border-bottom: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th { text-align: left; padding: 10px 12px; background: var(--surface);
           border-bottom: 1px solid var(--border); font-weight: 600; color: var(--text-muted);
           text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
tbody td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
tbody tr:hover { background: var(--hover); }
.best { background: var(--best-bg); font-weight: 600; color: var(--green); }
.worst { background: var(--worst-bg); color: var(--red); }
.insight { background: var(--surface2); border-left: 3px solid var(--accent); padding: 14px 18px;
           margin: 14px 0; border-radius: 0 6px 6px 0; font-size: 14px; line-height: 1.6; }
.analysis-section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
            padding: 24px 28px; margin-bottom: 28px; }
.analysis-section h2 { font-size: 17px; font-weight: 600; margin-bottom: 14px; padding-bottom: 8px;
             border-bottom: 1px solid var(--border); }
.analysis-section h3 { font-size: 15px; font-weight: 600; margin: 16px 0 8px; }
.analysis-section p { font-size: 14px; line-height: 1.7; margin-bottom: 8px; }
.analysis-section table { margin: 12px 0; }
.analysis-section .placeholder { color: var(--text-muted); font-style: italic; }
.iframe-wrap { width: 100%; height: calc(100vh - 90px); border: none; }
.run-link { padding: 8px 16px; background: var(--surface); border-bottom: 1px solid var(--border); font-size: 12px; }
.sub-bar { display: flex; align-items: center; gap: 4px; padding: 8px 16px;
           background: var(--surface); border-bottom: 1px solid var(--border); }
.sub-bar button { background: none; border: none; color: var(--text-muted); padding: 6px 14px;
                  cursor: pointer; font-size: 13px; border-bottom: 2px solid transparent; font-weight: 500; }
.sub-bar button.active { color: var(--accent); border-bottom-color: var(--accent); }
@media (max-width: 900px) { .cards { grid-template-columns: 1fr; } .page { padding: 16px; } }
@media print { :root { color-scheme: light; } }
"""


# Set the theme before first paint (avoids a flash). Shares the localStorage key
# with the per-run eval reports (report.py) so a chosen theme carries across both.
THEME_SCRIPT = """
(function () {
  try {
    var stored = localStorage.getItem('eval-report-theme');
    var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.setAttribute('data-theme', stored || (prefersDark ? 'dark' : 'light'));
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
"""

TOGGLE_SCRIPT = """
(function () {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  function paint() {
    var t = document.documentElement.getAttribute('data-theme') || 'light';
    btn.textContent = t === 'dark' ? '\\u2600' : '\\u263E';
    btn.title = t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    btn.setAttribute('aria-label', btn.title);
  }
  btn.addEventListener('click', function () {
    var next = (document.documentElement.getAttribute('data-theme') || 'light') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('eval-report-theme', next); } catch (e) {}
    paint();
  });
  paint();
})();
"""


def _rank_color(value, all_values, higher_is_better):
    """Color a value green/yellow/red based on its rank among all_values."""
    if value is None:
        return "muted"
    clean = sorted([v for v in all_values if v is not None],
                   reverse=higher_is_better)
    # Need at least two distinct values to rank; when everything ties, nothing
    # is uniquely best or worst, so leave it uncolored.
    if len(clean) < 2 or clean[0] == clean[-1]:
        return ""
    if value == clean[0]:
        return "green"
    if value == clean[-1]:
        return "red"
    return "yellow"


def best_worst_indices(values, higher_is_better=True):
    clean = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(clean) < 2:
        return None, None
    if higher_is_better:
        best_i = max(clean, key=lambda x: x[1])[0]
        worst_i = min(clean, key=lambda x: x[1])[0]
    else:
        best_i = min(clean, key=lambda x: x[1])[0]
        worst_i = max(clean, key=lambda x: x[1])[0]
    if values[best_i] == values[worst_i]:
        return None, None
    return best_i, worst_i


def render_comparison_table(models, rows):
    """Render a metric-by-model table.

    Each row is ``(label, aggs, fmt_type, higher_is_better)`` where ``aggs`` is a
    per-model list of aggregate() dicts. Best/worst highlighting is computed on
    each aggregate's ``avg``; cells show ``avg (min-max)`` for models with more
    than one run (via fmt_range) and just the value for single-run models.
    """
    html = "<table><thead><tr><th>Metric</th>"
    for m in models:
        html += f"<th>{escape(short_name(m))}</th>"
    html += "</tr></thead><tbody>"
    for label, aggs, fmt_type, higher in rows:
        values = [a.get("avg") for a in aggs]
        best_i, worst_i = best_worst_indices(values, higher)
        html += f"<tr><td>{escape(label)}</td>"
        for i, a in enumerate(aggs):
            cls = ""
            if i == best_i:
                cls = ' class="best"'
            elif i == worst_i:
                cls = ' class="worst"'
            html += f"<td{cls}>{fmt_range(a, fmt_type)}</td>"
        html += "</tr>"
    html += "</tbody></table>"
    return html


def load_stats_artifact(input_dir):
    """Load an eval-anova ``anova.json`` stats artifact if present.

    eval-compare stays standalone and dependency-free: this reads pre-computed
    statistics (never imports scipy/statsmodels/pingouin, never computes). Looks
    at the input dir root first, then falls back to a single unambiguous match
    anywhere below it. Returns the parsed dict, or None when absent.
    """
    input_dir = Path(input_dir)
    root = input_dir / "anova.json"
    if root.is_file():
        return load_json(root)
    matches = list(input_dir.rglob("anova.json"))
    if len(matches) == 1:
        return load_json(matches[0])
    return None


def _statnum(x, n=4):
    return f"{x:.{n}f}" if isinstance(x, (int, float)) else "--"


def _stats_pmap(an):
    """Map factor -> p-value for single- or multi-factor ANOVA results."""
    vals = an.get("p_values")
    if isinstance(vals, dict) and vals:
        return vals
    return {an.get("factor") or "effect": an.get("p_value")}


def _stats_sig_for(an, factor):
    sig = an.get("significant")
    return bool(sig.get(factor)) if isinstance(sig, dict) else bool(sig)


def _stats_padjmap(an):
    """Map term -> adjusted p-value, mirroring _stats_pmap's shapes."""
    vals = an.get("p_adjusted")
    if isinstance(vals, dict):
        return vals
    return {an.get("factor") or "effect": vals}


_STATS_CORRECTION_LABELS = {
    "holm": "Holm",
    "bh": "Benjamini-Hochberg (FDR)",
    "none": "none",
}


def _stats_correction_note(an):
    """Names the multiplicity correction behind the adjusted p column — an
    adjusted value is never rendered without saying which method produced it."""
    c = an.get("correction")
    if c is None:
        return None
    if c == "none":
        return "No multiple-comparison correction — significance on raw p-values."
    label = _STATS_CORRECTION_LABELS.get(c, str(c))
    return (f"{label}-corrected across a family of "
            f"{an.get('family_size', '?')} term test(s); "
            "significance on adjusted p.")


# --- per-judge fan-out rendering (self-contained; delimited on purpose) -----
def _render_per_judge_section(stats):
    """Compact judge-by-term table for the opt-in per-judge ANOVA fan-out.

    Rendered only when anova.json carries a ``per_judge`` block. Adjusted
    p-values there are Benjamini-Hochberg across the ONE family spanning all
    judges × terms (the block's own correction/family_size) — separate from
    the composite ANOVA's family above. Pure rendering — no stats libs.
    """
    pj = stats.get("per_judge") or {}
    judges = pj.get("judges") or {}
    excluded = pj.get("excluded") or []
    if not judges and not excluded:
        return ""
    muted = "color:var(--text-muted);font-size:12px;"
    html = '<h3>Per-judge effects (screening)</h3>\n'
    if judges:
        html += ('<table><thead><tr><th>Judge</th><th>Term</th><th>p (raw)</th>'
                 '<th>p (adjusted)</th><th>Result</th><th>n cases</th></tr>'
                 '</thead><tbody>\n')
        for name in sorted(judges):
            entry = judges[name] or {}
            for term, cell in (entry.get("terms") or {}).items():
                res = ("significant" if cell.get("significant")
                       else "not significant")
                html += (f'<tr><td>{escape(str(name))}</td>'
                         f'<td>{escape(str(term))}</td>'
                         f'<td>{_statnum(cell.get("p_raw"))}</td>'
                         f'<td>{_statnum(cell.get("p_adjusted"))}</td>'
                         f'<td>{res}</td>'
                         f'<td>{escape(str(entry.get("n_cases", "?")))}</td></tr>\n')
        html += '</tbody></table>\n'
        label = _STATS_CORRECTION_LABELS.get(pj.get("correction"),
                                             str(pj.get("correction")))
        note = (f"{label}-corrected across one family of "
                f"{pj.get('family_size', '?')} (judge, term) test(s); "
                "screening only — the composite ANOVA above keeps its own "
                "correction family.")
        methods = sorted({str(e.get("method")) for e in judges.values()
                          if isinstance(e, dict) and e.get("method")})
        if methods:
            note += f" Method: {'; '.join(methods)}."
        html += f'<p style="{muted}">{escape(note)}</p>\n'
    for ex in excluded:
        html += (f'<p style="{muted}">Excluded from the family: '
                 f'{escape(str(ex.get("judge", "?")))} — '
                 f'{escape(str(ex.get("reason", "")))}</p>\n')
    return html
# --- end per-judge fan-out rendering -----------------------------------------


def render_stats_section(stats):
    """Render the ANOVA / Pareto statistics section from a pre-computed
    ``anova.json`` (produced by /eval-anova). Pure rendering — no stats libs."""
    an = stats.get("anova") or {}
    design = stats.get("design") or {}
    pareto = stats.get("pareto_frontier") or []
    muted = "color:var(--text-muted);font-size:12px;"

    sig = an.get("significant")
    is_sig = any(bool(v) for v in sig.values()) if isinstance(sig, dict) else bool(sig)
    verdict = "SIGNIFICANT" if is_sig else "not significant"

    html = '<section id="statistics">\n<h2>Statistical Significance (ANOVA)</h2>\n'
    html += (f'<p>{escape(str(an.get("method", "ANOVA")))} — '
             f'<strong>{verdict}</strong> at &alpha;={escape(str(an.get("alpha", 0.05)))}.</p>\n')
    if an.get("note"):
        html += f'<p style="{muted}">{escape(str(an["note"]))}</p>\n'

    pmap = _stats_pmap(an)
    # Older anova.json artifacts predate the correction fields; render their
    # single p column unchanged rather than an unlabelled adjusted one.
    corrected = an.get("correction") is not None
    padj = _stats_padjmap(an)
    if any(v is not None for v in pmap.values()):
        if corrected:
            html += ('<table><thead><tr><th>Term</th><th>p (raw)</th>'
                     '<th>p (adjusted)</th><th>Result</th></tr></thead><tbody>\n')
        else:
            html += ('<table><thead><tr><th>Factor</th><th>p-value</th>'
                     '<th>Result</th></tr></thead><tbody>\n')
        for factor, p in pmap.items():
            res = "significant" if _stats_sig_for(an, factor) else "not significant"
            html += f'<tr><td>{escape(str(factor))}</td><td>{_statnum(p)}</td>'
            if corrected:
                html += f'<td>{_statnum(padj.get(factor))}</td>'
            html += f'<td>{res}</td></tr>\n'
        html += '</tbody></table>\n'
        correction_note = _stats_correction_note(an)
        if correction_note:
            html += f'<p style="{muted}">{escape(correction_note)}</p>\n'
        f_stat = an.get("f_statistic")
        bits = []
        if isinstance(f_stat, (int, float)):
            bits.append(f"F = {_statnum(f_stat, 3)}")
        bits.append(f"n_cases = {escape(str(design.get('n_cases', '?')))}")
        bits.append(f"replications = {escape(str(design.get('replications', '?')))}")
        html += f'<p style="{muted}">{" · ".join(bits)}</p>\n'

    excluded = design.get("excluded_cases") or stats.get("excluded_cases")
    if excluded:
        html += (f'<p style="{muted}">Excluded (missing from some condition): '
                 f'{escape(", ".join(map(str, excluded)))}</p>\n')

    # --- Post-hoc pairwise level contrasts (anova.json "contrasts") ---
    # Rendered only when the artifact carries them; the family (contrasts
    # within one factor) and the correction method are always named, and raw
    # p stays visible next to the adjusted one.
    contrasts = stats.get("contrasts") or {}
    if contrasts:
        html += '<h3>Pairwise level contrasts (post-hoc)</h3>\n'
        for factor, block in contrasts.items():
            corr = block.get("correction")
            if corr == "none":
                fam = ("no multiple-comparison correction across contrasts — "
                       "significance on raw p")
            else:
                label = _STATS_CORRECTION_LABELS.get(corr, str(corr))
                fam = (f"{label}-corrected across the "
                       f"{block.get('family_size', '?')} contrast(s) within "
                       "this factor")
            html += (f'<p><strong>{escape(str(factor))}</strong> — {escape(fam)} '
                     f'&middot; omnibus adjusted p: '
                     f'{_statnum(block.get("omnibus_p_adjusted"))}</p>\n')
            if block.get("note"):
                html += f'<p style="{muted}">{escape(str(block["note"]))}</p>\n'
            if block.get("reason"):
                html += (f'<p style="{muted}">No pairwise tests: '
                         f'{escape(str(block["reason"]))}</p>\n')
                continue
            html += ('<table><thead><tr><th>A</th><th>B</th><th>Estimate</th>'
                     '<th>SE</th><th>p (raw)</th><th>p (adjusted)</th>'
                     '<th>Result</th></tr></thead><tbody>\n')
            for pair in block.get("pairs", []):
                if pair.get("p_raw") is None:
                    res = "no test"
                elif pair.get("significant"):
                    res = "significant"
                else:
                    res = "not significant"
                html += (f'<tr><td>{escape(str(pair.get("a")))}</td>'
                         f'<td>{escape(str(pair.get("b")))}</td>'
                         f'<td>{_statnum(pair.get("estimate"))}</td>'
                         f'<td>{_statnum(pair.get("se"))}</td>'
                         f'<td>{_statnum(pair.get("p_raw"))}</td>'
                         f'<td>{_statnum(pair.get("p_adjusted"))}</td>'
                         f'<td>{res}</td></tr>\n')
            html += '</tbody></table>\n'
    # --- per-judge fan-out (opt-in; empty string unless per_judge present) ---
    html += _render_per_judge_section(stats)
    # -------------------------------------------------------------------------

    if pareto and any(isinstance(c, dict) and "cost" in c for c in pareto):
        html += ('<h3>Cost / quality Pareto frontier</h3>\n'
                 '<table><thead><tr><th>Condition</th><th>Mean score</th>'
                 '<th>Cost (USD)</th></tr></thead><tbody>\n')
        for c in sorted(pareto, key=lambda x: -(x.get("mean") or 0)):
            levels = c.get("levels") or {}
            label = (c.get("model")
                     or ", ".join(f"{k}={v}" for k, v in levels.items())
                     or c.get("condition_id", "?"))
            html += (f'<tr><td>{escape(str(label))}</td>'
                     f'<td>{_statnum(c.get("mean"), 3)}</td>'
                     f'<td>{_statnum(c.get("cost"), 4)}</td></tr>\n')
        html += '</tbody></table>\n'

    html += (f'<p style="{muted}">Statistics computed by <code>/eval-anova</code> '
             '(anova.json).</p>\n</section>\n\n')
    return html


def generate_report(runs, title, overview, output_dir, stats=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    groups = group_by_model(runs)
    all_judges = get_all_judge_names(runs)

    # Aggregate per model first (order-independent) so we can rank judges by
    # cross-model variance before deciding the tab/card order.
    model_aggs = {}
    for m, model_runs in groups.items():
        agg = {}
        for key in ["cost_usd", "num_turns", "wall_clock_s", "output_tokens",
                     "cache_hit_rate", "cost_per_turn", "output_tokens_per_turn"]:
            agg[key] = aggregate([get_metric(r, key) for r in model_runs])
        for judge in all_judges:
            agg[f"judge_{judge}"] = aggregate([get_judge_mean(r, judge) for r in model_runs])
        model_aggs[m] = agg

    # The most discriminating judge (highest variance across models) drives both
    # the card stats and the model ordering — not the alphabetically-first judge.
    card_judges = pick_card_judges(all_judges, model_aggs, list(groups.keys()))
    primary_judge = card_judges[0] if card_judges else (all_judges[0] if all_judges else None)
    if primary_judge:
        models = sorted(groups.keys(),
                        key=lambda m: model_aggs[m].get(f"judge_{primary_judge}", {}).get("avg") or 0,
                        reverse=True)
    else:
        models = sorted(groups.keys())

    multi_run = any(len(model_runs) > 1 for model_runs in groups.values())

    # Badges are added by the LLM agent in Step 3, not auto-computed

    # Copy HTML reports into a unique per-run subdir (slug), so runs that share a
    # directory basename don't overwrite each other or alias the same iframe.
    for r in runs:
        if r["html_report"]:
            dest = output_dir / r["slug"]
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(r["html_report"], dest / "report.html")

    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
<style>{CSS}</style>
<script>{THEME_SCRIPT}</script>
</head>
<body>

<div class="header">
  <button id="theme-toggle" type="button" aria-label="Toggle theme">&#9728;</button>
  <h1>{escape(title)}</h1>
  <div class="subtitle">{len(runs)} eval runs across {len(models)} models</div>
</div>

<div class="tab-bar" id="tabBar">
  <button class="active" data-tab="comparison" data-color="var(--accent)">Comparison</button>
"""
    for m in models:
        model_runs = groups[m]
        label = short_name(m)
        if len(model_runs) > 1:
            label += f" ({len(model_runs)} runs)"
        c = color_for(m)
        html += f'  <button data-tab="{escape(m)}" data-color="{c}">{escape(label)}</button>\n'

    html += "</div>\n\n"

    # Comparison tab
    html += '<div class="tab-content active" id="tab-comparison">\n<div class="page">\n'

    if overview:
        html += f'<div class="overview">{escape(overview)}</div>\n'

    # Bottom Line: placeholder for LLM analysis in Step 3
    html += '<div class="verdict">\n<h2>Bottom Line</h2>\n'
    html += '<p class="placeholder">Analysis pending — will be replaced with a per-model verdict.</p>\n'
    html += "</div>\n\n"

    # Pre-compute all values for relative coloring
    judge_all_values = {}
    for judge in card_judges:
        judge_all_values[judge] = [model_aggs[m].get(f"judge_{judge}", {}).get("avg") for m in models]
    all_cost = [model_aggs[m]["cost_usd"].get("avg") for m in models]
    all_wall = [model_aggs[m]["wall_clock_s"].get("avg") for m in models]

    # Model cards
    html += '<div class="cards">\n'
    for m in models:
        agg = model_aggs[m]
        c = color_for(m)

        cost = agg["cost_usd"].get("avg")
        turns = agg["num_turns"].get("avg")
        out_tok = agg["output_tokens"].get("avg")
        wall = agg["wall_clock_s"].get("avg")

        # The badge <div> and card border-color are injected by the LLM agent
        # in Step 3 (see SKILL.md); the generator emits a plain card.
        html += '<div class="card">\n'
        html += f'  <h3><span class="dot" style="background:{c}"></span> {escape(short_name(m))}'
        n = len(groups[m])
        if n > 1:
            html += f' <span style="font-size:11px; color:var(--text-muted); font-weight:400;">({n} runs avg)</span>'
        html += '</h3>\n  <div class="stats">\n'

        for judge in card_judges:
            jv = agg.get(f"judge_{judge}", {}).get("avg")
            is_pct = _is_pass_rate(judge, runs)
            judge_label = judge.replace("_", " ").title()
            rank_cls = _rank_color(jv, judge_all_values.get(judge, []), True)
            html += f'    <div class="stat"><span class="label">{escape(judge_label)}</span><span class="value {rank_cls}">{fmt(jv, "pct" if is_pct else "num") if jv is not None else "--"}</span></div>\n'

        html += f'    <div class="stat"><span class="label">{"Avg Run Cost" if n > 1 else "Total Cost"}</span><span class="value {_rank_color(cost, all_cost, False)}">{fmt(cost, "usd")}</span></div>\n'
        html += f'    <div class="stat"><span class="label">Wall Clock</span><span class="value {_rank_color(wall, all_wall, False)}">{fmt(wall, "time")}</span></div>\n'
        html += f'    <div class="stat"><span class="label">Output Tokens</span><span class="value">{fmt(out_tok, "tokens")}</span></div>\n'
        html += f'    <div class="stat"><span class="label">Turns</span><span class="value">{fmt(turns, "int")}</span></div>\n'
        html += '  </div>\n</div>\n'
    html += '</div>\n\n'

    # Cost table. Labels reflect per-run averages when any model has >1 run
    # (cells then show "avg (min-max)"); otherwise a single run's own totals.
    html += '<section>\n<h2>Cost &amp; Efficiency</h2>\n'
    cost_spec = [
        ("Avg Run Cost" if multi_run else "Total Cost", "cost_usd", "usd", False),
        ("Avg Output Tokens" if multi_run else "Output Tokens", "output_tokens", "tokens", False),
        ("Tokens / Turn", "output_tokens_per_turn", "int", True),
        ("Avg Turns" if multi_run else "Total Turns", "num_turns", "int", False),
        ("Wall Clock", "wall_clock_s", "time", False),
        ("Cost / Turn", "cost_per_turn", "usd_precise", False),
        ("Cache Hit Rate", "cache_hit_rate", "pct", True),
    ]
    cost_rows = [(label, [model_aggs[m][key] for m in models], ft, hib)
                 for label, key, ft, hib in cost_spec]
    html += render_comparison_table(models, cost_rows)
    for note in cost_source_notes(groups):
        html += f'<p class="cost-note" style="color:var(--text-muted);font-size:12px">{escape(note)}</p>\n'
    html += '</section>\n\n'

    # Quality table
    html += '<section>\n<h2>Quality Scores</h2>\n'
    quality_rows = []
    for judge in all_judges:
        aggs = [model_aggs[m].get(f"judge_{judge}", aggregate([])) for m in models]
        ft = "pct" if _is_pass_rate(judge, runs) else "num"
        label = judge.replace("_", " ").title()
        quality_rows.append((label, aggs, ft, True))
    html += render_comparison_table(models, quality_rows)
    html += '</section>\n\n'

    # Per-case tables for each judge
    all_cases = get_all_case_names(runs)
    if all_cases:
        for judge in all_judges:
            # Check if this judge has any variation across models — skip if uniform
            has_variation = False
            for case in all_cases:
                case_values = []
                for m in models:
                    scores = [get_case_score(r, case, judge) for r in groups[m]]
                    agg = aggregate(scores)
                    case_values.append(agg["avg"])
                clean = [v for v in case_values if v is not None]
                if len(clean) >= 2 and max(clean) != min(clean):
                    has_variation = True
                    break
            if not has_variation and len(models) > 1:
                continue

            judge_label = judge.replace("_", " ").title()
            is_pct = _is_pass_rate(judge, runs)
            html += f'<section>\n<h2>Per-Case: {escape(judge_label)}</h2>\n'
            html += "<table><thead><tr><th>Case</th>"
            for m in models:
                html += f"<th>{escape(short_name(m))}</th>"
            html += "</tr></thead><tbody>"
            for case in all_cases:
                case_short = case.replace("case-", "").replace("-", " ", 1).split(" ", 1)
                label = case_short[1] if len(case_short) > 1 else case_short[0]
                html += f"<tr><td>{escape(label)}</td>"
                values = []
                for m in models:
                    scores = [get_case_score(r, case, judge) for r in groups[m]]
                    agg = aggregate(scores)
                    values.append(agg["avg"])
                best_i, worst_i = best_worst_indices(values, True)
                for i, v in enumerate(values):
                    cls = ""
                    if i == best_i:
                        cls = ' class="best"'
                    elif i == worst_i:
                        cls = ' class="worst"'
                    if v is not None:
                        n = len(groups[models[i]])
                        if is_pct:
                            cell = fmt(v, "pct")
                        elif n > 1:
                            cell = f"{v:.1f}"
                        else:
                            cell = f"{v:.2f}" if v != int(v) else f"{int(v)}"
                    else:
                        cell = "--"
                    html += f"<td{cls}>{cell}</td>"
                html += "</tr>"
            html += "</tbody></table>\n</section>\n\n"

    # Statistics section (ANOVA / Pareto) — only when an eval-anova anova.json
    # artifact is present. eval-compare stays standalone and scipy-free: it
    # renders pre-computed numbers, it never computes them.
    if stats:
        html += render_stats_section(stats)

    # LLM analysis placeholder sections — populated by the agent in Step 3
    html += '<div class="analysis-section" id="model-strengths">\n'
    html += '<h2>Where Each Model Shined</h2>\n'
    html += '<p class="placeholder">Analysis pending — will be replaced with per-model strengths.</p>\n'
    html += '</div>\n\n'

    html += '<div class="analysis-section" id="shared-weaknesses">\n'
    html += '<h2>Shared Weaknesses Across All Models</h2>\n'
    html += '<p class="placeholder">Analysis pending — will be replaced with cross-cutting weaknesses.</p>\n'
    html += '</div>\n\n'

    html += '<div class="analysis-section" id="recommendations">\n'
    html += '<h2>Recommendations</h2>\n'
    html += '<p class="placeholder">Analysis pending — will be replaced with actionable recommendations.</p>\n'
    html += '</div>\n\n'

    html += '</div>\n</div>\n\n'

    # Report tabs for each model
    for m in models:
        model_runs = groups[m]
        c = color_for(m)

        if len(model_runs) == 1:
            r = model_runs[0]
            html += f'<div class="tab-content" id="tab-{escape(m)}">\n'
            if r["html_report"]:
                html += f'  <iframe class="iframe-wrap" src="{quote(r["slug"], safe="")}/report.html"></iframe>\n'
            else:
                html += '  <div class="page"><p>No HTML report available for this run.</p></div>\n'
            html += '</div>\n\n'
        else:
            html += f'<div class="tab-content" id="tab-{escape(m)}">\n'
            html += f'  <div class="sub-bar" id="subbar-{escape(m)}">\n'
            for j, r in enumerate(model_runs):
                active = " active" if j == 0 else ""
                act_style = f"color:var(--accent); border-bottom-color:var(--accent);" if j == 0 else ""
                html += f'    <button class="{active}" data-sub="{j}" style="{act_style}">Run {j + 1} ({escape(r["name"])})</button>\n'
            html += '  </div>\n'
            for j, r in enumerate(model_runs):
                display = "" if j == 0 else ' style="display:none;"'
                html += f'  <div class="sub-panel" data-model="{escape(m)}" data-idx="{j}"{display}>\n'
                if r["html_report"]:
                    html += f'    <iframe class="iframe-wrap" src="{quote(r["slug"], safe="")}/report.html"></iframe>\n'
                else:
                    html += '    <div class="page"><p>No HTML report available.</p></div>\n'
                html += '  </div>\n'
            html += '</div>\n\n'

    # JavaScript
    html += """<script>
document.getElementById('tabBar').addEventListener('click', e => {
  if (e.target.tagName !== 'BUTTON') return;
  document.querySelectorAll('.tab-bar button').forEach(b => {
    b.classList.remove('active'); b.style.color = ''; b.style.borderBottomColor = '';
  });
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  const c = e.target.dataset.color;
  e.target.classList.add('active');
  e.target.style.color = c; e.target.style.borderBottomColor = c;
  document.getElementById('tab-' + e.target.dataset.tab).classList.add('active');
});
const initTab = document.querySelector('.tab-bar button.active');
if (initTab) { initTab.style.color = initTab.dataset.color; initTab.style.borderBottomColor = initTab.dataset.color; }

document.querySelectorAll('.sub-bar').forEach(bar => {
  bar.addEventListener('click', e => {
    if (e.target.tagName !== 'BUTTON') return;
    bar.querySelectorAll('button').forEach(b => {
      b.classList.remove('active'); b.style.color = 'var(--text-muted)'; b.style.borderBottomColor = 'transparent';
    });
    e.target.classList.add('active');
    e.target.style.color = 'var(--accent)'; e.target.style.borderBottomColor = 'var(--accent)';
    const idx = e.target.dataset.sub;
    const parent = bar.parentElement;
    parent.querySelectorAll('.sub-panel').forEach(p => p.style.display = 'none');
    parent.querySelector('.sub-panel[data-idx="' + idx + '"]').style.display = '';
  });
});
</script>
""" + f"<script>{TOGGLE_SCRIPT}</script>\n</body>\n</html>"

    out_path = output_dir / "index.html"
    with open(out_path, "w") as f:
        f.write(html)
    return str(out_path)


def cmd_discover(args):
    try:
        runs = discover_runs(args.input_dir)
    except NotADirectoryError as e:
        print(json.dumps({"error": str(e), "runs": []}))
        sys.exit(1)
    if not runs:
        print(json.dumps({"error": "No valid runs found", "runs": []}))
        sys.exit(1)
    all_judge_names = get_all_judge_names(runs)
    out = []
    for r in runs:
        entry = {
            "name": r["name"],
            "dir": r["dir"],
            "model": get_model(r),
            "cost_usd": get_metric(r, "cost_usd"),
            "cost_source": get_cost_source(r),
            "judges": {j: get_judge_mean(r, j) for j in all_judge_names},
            "has_html": r["html_report"] is not None,
        }
        out.append(entry)
    result = {"runs": out, "has_stats": load_stats_artifact(args.input_dir) is not None}
    print(json.dumps(result, indent=2))


def cmd_generate(args):
    try:
        runs = discover_runs(args.input_dir)
    except NotADirectoryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if not runs:
        print("ERROR: No valid runs found", file=sys.stderr)
        sys.exit(1)
    output_dir = args.output or str(Path(args.input_dir) / "comparison-report")
    stats = load_stats_artifact(args.input_dir)
    path = generate_report(runs, args.title, args.overview, output_dir, stats=stats)
    groups = group_by_model(runs)
    print(f"Report generated: {path}")
    for note in cost_source_notes(groups):
        print(f"NOTE: {note}", file=sys.stderr)
    print(f"Runs: {len(runs)} across {len(groups)} models")
    if stats:
        print("Included eval-anova statistics (anova.json)")
    all_judges = get_all_judge_names(runs)
    first_judge = all_judges[0] if all_judges else None
    for m, model_runs in groups.items():
        cost = aggregate([get_metric(r, "cost_usd") for r in model_runs])
        parts = [f"{short_name(m)}: {len(model_runs)} run(s)"]
        if first_judge:
            score = aggregate([get_judge_mean(r, first_judge) for r in model_runs])
            label = first_judge.replace("_", " ").title()
            parts.append(f"{label}={fmt(score['avg'], 'num')}")
        parts.append(f"cost={fmt(cost['avg'], 'usd')}")
        print(f"  {', '.join(parts)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_discover = sub.add_parser("discover")
    p_discover.add_argument("input_dir")

    p_generate = sub.add_parser("generate")
    p_generate.add_argument("input_dir")
    p_generate.add_argument("--output", default=None)
    p_generate.add_argument("--title", default="Model Comparison")
    p_generate.add_argument("--overview", default=None)

    args = parser.parse_args()
    if args.command == "discover":
        cmd_discover(args)
    elif args.command == "generate":
        cmd_generate(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
