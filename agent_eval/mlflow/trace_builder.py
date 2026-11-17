"""Build hierarchical MLflow traces from Claude Code stream-json output.

Extracted from log_results.py for reuse by both the eval pipeline
and the standalone claude-trace wrapper.
"""

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ── Trace builder ────────────────────────────────────────────────────

def iso_to_ns(ts_str):
    """Convert ISO 8601 timestamp string to nanoseconds since epoch."""
    from dateutil.parser import parse as _dt_parse
    return int(_dt_parse(ts_str).timestamp() * 1e9)


def _clamp_ns(ns, lo, hi):
    """Clamp a timestamp into [lo, hi].

    Trajectory.json timestamps come from a different clock/source than the
    stream-json events that define the trace window; without clamping, a
    clock offset can push a child span (CHAIN/thinking) before the trace
    start or after the trace end, rendering outside its own parent span.
    """
    return max(lo, min(ns, hi))


def make_span(trace_id, parent_id, name, span_type, start_ns, end_ns,
               inputs=None, outputs=None, extra_attrs=None):
    """Create a span dict for the trace."""
    span_id = uuid.uuid4().bytes[:8].hex()
    attrs = {
        "mlflow.traceRequestId": json.dumps(trace_id),
        "mlflow.spanType": json.dumps(span_type),
    }
    if inputs is not None:
        attrs["mlflow.spanInputs"] = json.dumps(inputs)
    if outputs is not None:
        attrs["mlflow.spanOutputs"] = json.dumps(outputs)
    if extra_attrs:
        attrs.update(extra_attrs)
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_id,
        "name": name,
        "start_time_unix_nano": start_ns,
        "end_time_unix_nano": end_ns,
        "events": [],
        "status": {"code": "STATUS_CODE_OK", "message": ""},
        "attributes": attrs,
    }


def _user_text_from_content(content):
    """Extract plain user text from a stream-json message content field."""
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                text = (b.get("text") or "").strip()
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)
    return ""


_MAX_TOOL_OUTPUT = 4000  # chars per tool result (stream or ATIF-enriched)
_MAX_WRITE_CONTENT = 4000
_MAX_EDIT_FRAGMENT = 2000
_MAX_THINKING = 4000


def _load_trajectory_json(trajectory_path):
    """Load ATIF trajectory.json as a dict, or None."""
    if trajectory_path is None:
        return None
    path = Path(trajectory_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_trajectory_user_steps(trajectory_path):
    """Load user steps from a Harbor ATIF trajectory.json (if present).

    Returns a list of ``{"message": str, "timestamp": str|None}``.
    """
    data = _load_trajectory_json(trajectory_path)
    if not data:
        return []
    steps = data.get("steps") or []
    out = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("source") != "user":
            continue
        msg = step.get("message")
        if not isinstance(msg, str) or not msg.strip():
            continue
        out.append({
            "message": msg.strip(),
            "timestamp": step.get("timestamp"),
        })
    return out


def _load_trajectory_tool_data(trajectory_path):
    """Index ATIF agent tool_calls and observations by tool id.

    Returns ``(tool_args, tool_results)`` where:
      - tool_args: tool_call_id -> arguments dict
      - tool_results: source_call_id -> richer result string
    """
    data = _load_trajectory_json(trajectory_path)
    if not data:
        return {}, {}
    tool_args = {}
    tool_results = {}
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("source") != "agent":
            continue
        for call in step.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            tuid = call.get("tool_call_id") or ""
            args = call.get("arguments")
            if tuid and isinstance(args, dict):
                tool_args[tuid] = args
        obs = step.get("observation") or {}
        if not isinstance(obs, dict):
            continue
        for result in obs.get("results") or []:
            if not isinstance(result, dict):
                continue
            tuid = result.get("source_call_id") or ""
            if not tuid:
                continue
            content = result.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            # Prefer observation content (often includes [metadata] JSON
            # with Write file body) over the short stream-json success line.
            tool_results[tuid] = content.strip()[:_MAX_TOOL_OUTPUT]
    return tool_args, tool_results


def _load_trajectory_reasoning(trajectory_path):
    """Ordered list of non-empty ``reasoning_content`` from ATIF agent steps.

    Returns ``[(text, timestamp|None), ...]`` in trajectory step order.
    """
    data = _load_trajectory_json(trajectory_path)
    if not data:
        return []
    out = []
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("source") != "agent":
            continue
        reasoning = step.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            out.append((reasoning.strip(), step.get("timestamp")))
    return out


def build_trace(stdout_path, run_result, run_id, experiment_id,
                trace_name="", subagent_dir=None,
                subagent_model=None, trajectory_path=None):
    """Build a hierarchical MLflow Trace from the stream-json stdout log.

    Structure:
      root AGENT
        ├── CHAIN user (skill invoke / instructions; from trajectory or stream)
        ├── AGENT step
        │   ├── LLM thinking
        │   ├── TOOL ...
        │   └── LLM response
        └── ...

    Harbor runs often omit user text from stream-json; pass ``trajectory_path``
    (ATIF ``trajectory.json``) to restore user turns and the root prompt.

    Returns a dict suitable for Trace.from_dict(), or None.
    """
    if not stdout_path.exists():
        return None

    events = []
    with open(stdout_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not events:
        return None

    # ── Extract metadata from events ────────────────────────────
    session_id = None
    prompt = ""
    final_response = ""
    stream_user_turns = []

    for e in events:
        if not session_id:
            session_id = e.get("session_id")

    # Prompt: prefer first user text message (the skill invocation).
    # Harbor stream-json often has only tool_result user events — then fall
    # back to trajectory.json user steps, then first assistant text.
    for e in events:
        if e.get("type") != "user":
            continue
        text = _user_text_from_content(e.get("message", {}).get("content", ""))
        if text:
            stream_user_turns.append({
                "message": text,
                "timestamp": e.get("timestamp"),
            })
            if not prompt:
                prompt = text

    traj_user_turns = _load_trajectory_user_steps(trajectory_path)
    if traj_user_turns:
        # Trajectory is the richer Harbor source of truth for user turns.
        # Always derive prompt from it here too, so the root span's prompt
        # attribute and the "user:" CHAIN spans agree on the same source.
        prompt = "\n\n".join(t["message"] for t in traj_user_turns)
        user_turns_for_spans = traj_user_turns
    else:
        user_turns_for_spans = stream_user_turns

    if not prompt:
        for e in events:
            if e.get("type") == "assistant":
                for b in e.get("message", {}).get("content", []):
                    if isinstance(b, dict) and b.get("type") == "text":
                        text = b.get("text", "").strip()
                        if text:
                            prompt = text
                            break
                if prompt:
                    break

    # Final response: last assistant text
    for e in reversed(events):
        if e.get("type") == "assistant":
            for b in e.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "text":
                    text = b.get("text", "").strip()
                    if text:
                        final_response = text
                        break
            if final_response:
                break

    # ATIF trajectory tool args / richer observations (Harbor).
    traj_tool_args, traj_tool_results = _load_trajectory_tool_data(
        trajectory_path)

    # ── Build tool_result timestamp and content lookups ─────────
    tool_result_ns = {}  # tool_use_id -> timestamp_ns
    tool_result_content = {}  # tool_use_id -> truncated output string
    for e in events:
        if e.get("type") != "user":
            continue
        ts = e.get("timestamp")
        if not ts:
            continue
        ts_ns = iso_to_ns(ts)
        content = e.get("message", {}).get("content", [])
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tuid = b.get("tool_use_id", "")
                    if tuid:
                        tool_result_ns[tuid] = ts_ns
                        # Extract tool result text
                        c = b.get("content", "")
                        if isinstance(c, list):
                            text = "\n".join(
                                x.get("text", "") for x in c
                                if isinstance(x, dict)
                                and x.get("type") == "text")
                        elif isinstance(c, str):
                            text = c
                        else:
                            text = ""
                        if text:
                            tool_result_content[tuid] = (
                                text[:_MAX_TOOL_OUTPUT])

    # Prefer richer ATIF observation text when available.
    for tuid, text in traj_tool_results.items():
        existing = tool_result_content.get(tuid, "")
        if len(text) > len(existing):
            tool_result_content[tuid] = text

    # ── Override timestamps for background agents ───────────────
    # Background agents return an immediate "async launched" tool_result,
    # but their real completion time is in task_notification events.
    # Use the task_notification timestamp as the true end time.
    for e in events:
        if (e.get("type") == "system"
                and e.get("subtype") == "task_notification"
                and e.get("status") == "completed"):
            tuid = e.get("tool_use_id", "")
            ts = e.get("timestamp")
            if tuid and ts:
                tool_result_ns[tuid] = iso_to_ns(ts)

    # ── Build result_indices for parallel detection ─────────────
    result_indices = set()
    for i, e in enumerate(events):
        if e.get("type") == "user":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        result_indices.add(i)

    # ── Build subagent child lookup ──────────────────────────────
    # Maps parent Agent tool_use_id → list of child spans (tool calls
    # AND LLM reasoning segments).
    # Sources: (1) inline via parent_tool_use_id in the main stream,
    #          (2) background agent output files referenced in tool_results.
    # Each child is a tuple:
    #   ("tool", tuid, name, input)  — tool call
    #   ("llm", None, text, {})     — LLM reasoning text
    subagent_children = {}  # parent_tuid -> [("tool"|"llm", ...), ...]
    subagent_tuids = set()  # tool_use_ids that belong to subagents

    # Source 1: inline children (foreground subagents)
    for e in events:
        ptui = e.get("parent_tool_use_id")
        if not ptui or e.get("type") != "assistant":
            continue
        for b in e.get("message", {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tuid = b.get("id", "")
                subagent_children.setdefault(ptui, []).append(
                    ("tool", tuid, b.get("name", "unknown"), b.get("input", {})))
                subagent_tuids.add(tuid)
            elif isinstance(b, dict) and b.get("type") == "text":
                text = b.get("text", "").strip()
                if text:
                    subagent_children.setdefault(ptui, []).append(
                        ("llm", None, text, {}))

    # Source 2: background agent output files
    # Parse tool_results to map agentId → parent tool_use_id and find
    # output file paths, then read each file for its tool calls.
    _agent_to_parent = {}  # agentId -> parent_tool_use_id
    _agent_output_files = {}  # agentId -> output_file_path

    for e in events:
        if e.get("type") != "user":
            continue
        content = e.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            tuid = b.get("tool_use_id", "")
            text = ""
            c = b.get("content", "")
            if isinstance(c, list):
                text = " ".join(x.get("text", "") for x in c
                                if isinstance(x, dict))
            elif isinstance(c, str):
                text = c
            m_id = re.search(r"agentId:\s*(\w+)", text)
            m_file = re.search(r"output_file:\s*(\S+)", text)
            if m_id:
                _agent_to_parent[m_id.group(1)] = tuid
            if m_id and m_file:
                _agent_output_files[m_id.group(1)] = m_file.group(1)

    # Resolve subagent output directory: saved copies from execute.py
    # live alongside stdout.log in <run_dir>/subagents/.
    _subagent_dir = subagent_dir or (stdout_path.parent / "subagents")

    for agent_id, output_path in _agent_output_files.items():
        parent_tuid = _agent_to_parent.get(agent_id)
        if not parent_tuid or parent_tuid in subagent_children:
            continue  # already have inline children
        # Only read from the SubagentStop hook's saved copies in
        # _subagent_dir.  Never open output_path from stream content
        # directly — it's untrusted input (CWE-22/CWE-73).
        safe_copy = _subagent_dir / f"agent-{agent_id}.jsonl"
        if not safe_copy.exists():
            safe_copy = _subagent_dir / f"{agent_id}.jsonl"
        if not (safe_copy.exists() and safe_copy.is_file()
                and not safe_copy.is_symlink()):
            continue
        # Verify resolved path stays under _subagent_dir
        try:
            resolved = safe_copy.resolve(strict=True)
            if not resolved.is_relative_to(_subagent_dir.resolve()):
                continue
        except (OSError, ValueError):
            continue
        output_path = str(resolved)
        try:
            with open(output_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        se = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if se.get("type") != "assistant":
                        continue
                    for b in se.get("message", {}).get("content", []):
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            child_tuid = b.get("id", "")
                            subagent_children.setdefault(
                                parent_tuid, []).append(
                                ("tool", child_tuid,
                                 b.get("name", "unknown"),
                                 b.get("input", {})))
                            subagent_tuids.add(child_tuid)
                        elif (isinstance(b, dict)
                              and b.get("type") == "text"):
                            text = b.get("text", "").strip()
                            if text:
                                subagent_children.setdefault(
                                    parent_tuid, []).append(
                                    ("llm", None, text, {}))
            # Also extract timestamps and tool results from the output file
            with open(output_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        se = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = se.get("timestamp")
                    if not ts:
                        continue
                    ts_ns = iso_to_ns(ts)
                    if se.get("type") == "user":
                        sc = se.get("message", {}).get("content", [])
                        if isinstance(sc, list):
                            for sb in sc:
                                if (isinstance(sb, dict)
                                        and sb.get("type") == "tool_result"):
                                    stuid = sb.get("tool_use_id", "")
                                    if stuid:
                                        tool_result_ns[stuid] = ts_ns
                                        # Extract tool result content
                                        c = sb.get("content", "")
                                        if isinstance(c, list):
                                            txt = "\n".join(
                                                x.get("text", "")
                                                for x in c
                                                if isinstance(x, dict)
                                                and x.get("type") == "text")
                                        elif isinstance(c, str):
                                            txt = c
                                        else:
                                            txt = ""
                                        if txt:
                                            tool_result_content[stuid] = (
                                                txt[:_MAX_TOOL_OUTPUT])
        except (OSError, UnicodeDecodeError):
            continue

    # ── Parse events into ordered segments ──────────────────────
    # Only top-level tool calls (no parent_tool_use_id) go into segments.
    # Subagent children are nested under their Agent span later.
    # A segment is either:
    #   ("llm", text, timestamp, context)  — context = preceding tool names
    #   ("batch", [(event_idx, tool_use_id, name, input), ...])
    segments = []
    current_batch = []
    # Track recent tool calls for LLM context (what ran before this LLM call)
    _recent_tools = []  # list of (name, summary_str)

    def _tool_one_liner(name, inp):
        """Short summary of a tool call for LLM context."""
        if name == "Bash":
            cmd = inp.get("command", "")
            # Extract script name from command
            for part in cmd.split():
                if part.endswith(".py") or part.endswith(".sh"):
                    return f"Bash({part.split('/')[-1]})"
            return f"Bash({cmd[:60]})"
        elif name == "Read":
            path = inp.get("file_path", "")
            return f"Read({path.split('/')[-1]})"
        elif name == "Write":
            path = inp.get("file_path", "")
            return f"Write({path.split('/')[-1]})"
        elif name == "Edit":
            path = inp.get("file_path", "")
            return f"Edit({path.split('/')[-1]})"
        elif name == "Skill":
            return f"Skill(/{inp.get('skill', '?')})"
        elif name == "Agent":
            return f"Agent({inp.get('description', '?')[:40]})"
        elif name in ("Glob", "Grep"):
            return f"{name}({inp.get('pattern', '')[:30]})"
        else:
            return name

    def _flush_batch():
        if current_batch:
            segments.append(("batch", list(current_batch)))
            for _, _, name, inp in current_batch:
                _recent_tools.append(_tool_one_liner(name, inp))
            current_batch.clear()

    for i, e in enumerate(events):
        if e.get("type") == "assistant":
            ptui = e.get("parent_tool_use_id")
            if ptui:
                continue  # skip subagent events
            content = e.get("message", {}).get("content", [])
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "thinking" and (b.get("thinking") or "").strip():
                    _flush_batch()
                    segments.append(("thinking", b["thinking"].strip(),
                                     e.get("timestamp"), None))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    _flush_batch()
                    context = "; ".join(_recent_tools) if _recent_tools else ""
                    segments.append(("llm", b["text"].strip(),
                                     e.get("timestamp"), context))
                    _recent_tools.clear()
                elif b.get("type") == "tool_use":
                    # Check if a tool_result appeared between previous
                    # tool_use and this one → batch boundary
                    if current_batch:
                        prev_idx = current_batch[-1][0]
                        if any(ri > prev_idx and ri < i
                               for ri in result_indices):
                            _flush_batch()
                    current_batch.append((
                        i, b.get("id", ""), b.get("name", "unknown"),
                        b.get("input", {}),
                    ))
    _flush_batch()

    # Backfill thinking from ATIF reasoning_content when the stream has none
    # at all (e.g. Harbor runs where extended thinking wasn't captured in
    # stream-json). Only applies when the stream is fully silent on thinking,
    # so it never second-guesses/conflicts with real per-turn stream data.
    if not any(seg_type == "thinking" for seg_type, *_ in segments):
        traj_reasoning = _load_trajectory_reasoning(trajectory_path)
        if traj_reasoning:
            reasoning_iter = iter(traj_reasoning)
            backfilled = []
            for seg in segments:
                if seg[0] == "llm":
                    reasoning = next(reasoning_iter, None)
                    if reasoning:
                        text, _ts = reasoning
                        # Use the llm segment's own timestamp so the
                        # synthetic thinking lands right before its turn,
                        # matching natural stream ordering.
                        backfilled.append(("thinking", text, seg[2], None))
                backfilled.append(seg)
            segments = backfilled

    # ── Derive timing from event timestamps ─────────────────────
    all_event_ts = [iso_to_ns(e["timestamp"])
                    for e in events if e.get("timestamp")]

    duration_s = run_result.get("duration_s", 0)
    duration_ns = int(duration_s * 1e9)
    duration_ms = int(duration_s * 1000)

    if all_event_ts:
        trace_start = min(all_event_ts)
        trace_end = max(all_event_ts)
        if trace_end - trace_start < duration_ns:
            trace_end = trace_start + duration_ns
    else:
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
        trace_start = now_ns - duration_ns
        trace_end = now_ns

    # ── Build spans ─────────────────────────────────────────────
    trace_id = f"tr-{uuid.uuid4().hex}"
    root_span_id = uuid.uuid4().bytes[:8].hex()
    token_usage = run_result.get("token_usage", {})
    cost_usd = run_result.get("cost_usd")
    model = run_result.get("model", "")
    _subagent_model = subagent_model or run_result.get("subagent_model") or model
    per_model_usage = run_result.get("per_model_usage", {})

    # Count tools
    tool_counts = {}
    for seg_type, seg_data, *_ in segments:
        if seg_type == "batch":
            for _, _, name, _ in seg_data:
                tool_counts[name] = tool_counts.get(name, 0) + 1

    # Root span
    root_attrs = {
        "mlflow.traceRequestId": json.dumps(trace_id),
        "mlflow.spanType": json.dumps("AGENT"),
        "mlflow.spanInputs": json.dumps({"prompt": prompt}),
        "mlflow.spanOutputs": json.dumps({
            "response": final_response,
            "exit_code": run_result.get("exit_code"),
        }),
        "run_id": json.dumps(run_id),
        "model": json.dumps(model),
    }
    # NOTE: Do NOT set mlflow.llm.cost on the root AGENT span.
    # Per-model costs are distributed across individual LLM spans;
    # setting it here would double-count in the "Cost Over Time" chart.

    spans = [{
        "trace_id": trace_id,
        "span_id": root_span_id,
        "parent_span_id": None,
        "name": trace_name or f"skill-run ({run_id})",
        "start_time_unix_nano": trace_start,
        "end_time_unix_nano": trace_end,
        "events": [],
        "status": {"code": "STATUS_CODE_OK", "message": ""},
        "attributes": root_attrs,
    }]

    # User turns as CHAIN spans under root (visible in the MLflow graph).
    user_cursor = trace_start
    for idx, turn in enumerate(user_turns_for_spans):
        ts = turn.get("timestamp")
        start_ns = iso_to_ns(ts) if ts else user_cursor
        start_ns = _clamp_ns(start_ns, trace_start, trace_end)
        end_ns = _clamp_ns(start_ns + int(0.1e9), trace_start, trace_end)
        user_cursor = end_ns
        msg = turn["message"]
        first_line = msg.split("\n")[0].strip()[:80] or f"user-{idx + 1}"
        spans.append(make_span(
            trace_id, root_span_id,
            name=f"user: {first_line}",
            span_type="CHAIN",
            start_ns=start_ns,
            end_ns=end_ns,
            inputs={"role": "user", "turn": idx + 1},
            outputs={"message": msg},
        ))

    # ── Group segments into agent steps ───────────────────────────
    # Each step = optional thinking + one LLM text output + tool actions.
    # Steps are direct children of root; tools are nested inside steps.
    #
    # Segments before the first LLM text (e.g. initial tool calls from
    # the skill setup) are grouped into a "Setup" step.
    #
    # Status-update texts (e.g. "RFE-014 created. 1/20 complete...")
    # are NOT new steps — they are progress notifications from background
    # agents and get merged into the preceding dispatch step.
    _STATUS_RE = re.compile(
        r"^(RFE-\d+|RHAIRFE-\d+)\s+(created|submitted|reviewed|processed)"
        r".*\d+/\d+\s+(complete|done)",
        re.IGNORECASE,
    )
    # Also catch "waiting" texts that are just polling updates
    _WAITING_RE = re.compile(
        r"(waiting for|agents? (are |is )?(still )?(running|creating|processing))",
        re.IGNORECASE,
    )

    # list of (llm_text, llm_ts, llm_context, [batch_segments], [thinkings])
    # thinkings: list of (text, timestamp)
    steps = []
    current_llm = None
    current_ts = None
    current_context = []
    current_batches = []
    current_thinkings = []
    # Thinking precedes its own turn's "llm" text segment in stream order, so
    # it can't be appended to current_thinkings directly: _flush_step() fires
    # on the *next* llm segment, which would close out the *previous* step
    # bundled with thinking that actually belongs to the incoming turn. Stash
    # it here until the matching llm segment arrives and claims it.
    pending_thinkings = []
    # Track whether the current step launched background agents
    _has_bg_agents = False

    def _is_status_update(text):
        """Detect LLM texts that are just background agent status updates."""
        first_line = text.split("\n")[0].strip()
        return bool(_STATUS_RE.match(first_line) or _WAITING_RE.search(first_line))

    def _flush_step():
        nonlocal current_llm, current_ts, current_context, current_batches
        nonlocal current_thinkings, _has_bg_agents
        if current_llm is not None or current_batches or current_thinkings:
            steps.append((current_llm, current_ts, current_context,
                          current_batches, current_thinkings))
            # Reset all step fields so a subsequent thinking→tool (no text)
            # turn cannot reuse the previous step's llm text.
            current_llm = None
            current_ts = None
            current_context = []
            current_batches = []
            current_thinkings = []
            _has_bg_agents = False

    for seg_type, seg_data, *rest in segments:
        if seg_type == "thinking":
            pending_thinkings.append((seg_data, rest[0] if rest else None))
        elif seg_type == "llm":
            if _has_bg_agents and _is_status_update(seg_data):
                # Merge status update (and any thinking ahead of it) into the
                # current dispatch step rather than starting a new one.
                current_thinkings.extend(pending_thinkings)
                pending_thinkings = []
                continue
            # Save previous step
            _flush_step()
            current_llm = seg_data
            current_ts = rest[0] if rest else None
            current_context = rest[1] if len(rest) > 1 else []
            # Pending thinking belongs to *this* turn's step, not the one
            # just flushed.
            current_thinkings = pending_thinkings
            pending_thinkings = []
        elif seg_type == "batch":
            if pending_thinkings:
                # If we have pending thinking but no llm segment arrived before
                # the batch (e.g. a tool-only turn), flush the previous step
                # and attach the thinking to the new step that will own this batch.
                _flush_step()
                current_thinkings = pending_thinkings
                pending_thinkings = []
            current_batches.append(seg_data)
            # Detect if this batch contains Agent calls (potential bg agents)
            if any(name == "Agent" for _, _, name, _ in seg_data):
                _has_bg_agents = True
    # Any trailing thinking with no following turn belongs to the last step.
    current_thinkings.extend(pending_thinkings)
    _flush_step()

    # ── Build spans from steps ──────────────────────────────────
    cursor_ns = trace_start

    for step_idx, (llm_text, llm_ts, llm_context, batches, thinkings) in enumerate(steps):
        # Compute step timing from its children
        if llm_ts:
            step_start = iso_to_ns(llm_ts)
        elif thinkings and thinkings[0][1]:
            step_start = iso_to_ns(thinkings[0][1])
        else:
            step_start = cursor_ns
        step_end = step_start

        # Pre-compute batch timing to find step_end
        batch_timings = []
        for batch in batches:
            batch_ends = [tool_result_ns.get(tuid)
                          for _, tuid, _, _ in batch]
            valid_ends = [t for t in batch_ends if t]
            # Batch start: use the event timestamp of the tool_use call
            # (when the tools were launched), not derived from completion.
            launch_times = [iso_to_ns(events[eidx].get("timestamp"))
                            for eidx, _, _, _ in batch
                            if events[eidx].get("timestamp")]
            if launch_times:
                b_start = min(launch_times)
            elif valid_ends:
                b_start = max(min(valid_ends) - int(1e9), trace_start)
            else:
                b_start = cursor_ns
            b_end = max(valid_ends) if valid_ends else b_start + int(1e9)
            batch_timings.append((b_start, b_end, batch, batch_ends))
            step_end = max(step_end, b_end)

        if step_end <= step_start:
            step_end = step_start + int(1e9)

        # Include thinking timestamps in step window
        for think_text, think_ts in thinkings:
            if think_ts:
                t_ns = iso_to_ns(think_ts)
                step_start = min(step_start, t_ns) if step_start else t_ns
                step_end = max(step_end, t_ns + int(0.5e9))

        # Step label from first line of LLM text (or thinking / Setup)
        if llm_text:
            first_line = llm_text.split("\n")[0].strip()
            # Strip markdown headers
            step_name = first_line.lstrip("#").strip()[:80]
        elif thinkings:
            step_name = thinkings[0][0].split("\n")[0].strip()[:80] or "thinking"
        else:
            step_name = "Setup"

        step_tool_names = []
        for _, _, batch, _ in batch_timings:
            for _, _, name, _ in batch:
                step_tool_names.append(name)
        step_inputs = {"step": step_idx + 1}
        if step_tool_names:
            step_inputs["tools"] = step_tool_names
        step_outputs = {}
        if llm_text:
            step_outputs["text"] = llm_text
        if thinkings:
            step_outputs["thinking"] = thinkings[0][0][:_MAX_THINKING]
        step_span = make_span(
            trace_id, root_span_id,
            name=step_name,
            span_type="AGENT",
            start_ns=step_start,
            end_ns=step_end,
            inputs=step_inputs,
            outputs=step_outputs or None,
        )
        step_span_id = step_span["span_id"]
        spans.append(step_span)

        # Thinking spans (extended thinking / reasoning_content)
        think_cursor = step_start
        for think_text, think_ts in thinkings:
            t_start = iso_to_ns(think_ts) if think_ts else think_cursor
            t_start = _clamp_ns(t_start, trace_start, trace_end)
            t_end = _clamp_ns(t_start + int(0.5e9), trace_start, trace_end)
            think_cursor = t_end
            spans.append(make_span(
                trace_id, step_span_id,
                name="thinking",
                span_type="LLM",
                start_ns=t_start,
                end_ns=t_end,
                inputs={"model": model, "kind": "thinking"},
                outputs={"thinking": think_text[:_MAX_THINKING]},
                extra_attrs=({"mlflow.llm.model": json.dumps(model)}
                             if model else None),
            ))

        # LLM span inside the step
        if llm_text:
            llm_start = iso_to_ns(llm_ts) if llm_ts else step_start
            llm_end = llm_start + int(0.5e9)
            # Input: preceding tool results that informed this LLM call
            llm_inputs = {"model": model}
            if llm_context:
                llm_inputs["context"] = llm_context
            spans.append(make_span(
                trace_id, step_span_id,
                name="LLM",
                span_type="LLM",
                start_ns=llm_start,
                end_ns=llm_end,
                inputs=llm_inputs,
                outputs={"text": llm_text},
                extra_attrs=({"mlflow.llm.model": json.dumps(model)}
                             if model else None),
            ))

        # Tool batches inside the step
        for b_start, b_end, batch, batch_ends in batch_timings:
            is_parallel = len(batch) > 1

            if is_parallel:
                names = set(n for _, _, n, _ in batch)
                if names == {"Agent"}:
                    group_name = f"{len(batch)} parallel agents"
                else:
                    group_name = f"{len(batch)} parallel calls"

                group_span = make_span(
                    trace_id, step_span_id,
                    name=group_name,
                    span_type="TASK",
                    start_ns=b_start,
                    end_ns=b_end,
                    inputs={"count": len(batch)},
                )
                spans.append(group_span)
                parent_for_children = group_span["span_id"]
            else:
                parent_for_children = step_span_id

            for (_, tuid, name, inp), end_ns in zip(batch, batch_ends):
                child_end = end_ns if end_ns else b_end
                span_type = "AGENT" if name == "Agent" else "TOOL"
                # Merge richer ATIF tool arguments when stream input is thin.
                merged_inp = inp if isinstance(inp, dict) else {}
                traj_args = traj_tool_args.get(tuid)
                if isinstance(traj_args, dict):
                    merged_inp = {**traj_args, **merged_inp}
                    # Prefer traj values that stream omitted or left empty.
                    for k, v in traj_args.items():
                        if k not in merged_inp or merged_inp.get(k) in (
                                None, "", {}, []):
                            merged_inp[k] = v
                        elif (isinstance(v, str) and isinstance(
                                merged_inp.get(k), str)
                              and len(v) > len(merged_inp[k])):
                            merged_inp[k] = v
                # Include tool result content as span output
                tool_output = None
                if tuid in tool_result_content:
                    tool_output = {"result": tool_result_content[tuid]}
                tool_span = make_span(
                    trace_id, parent_for_children,
                    name=name,
                    span_type=span_type,
                    start_ns=b_start,
                    end_ns=child_end,
                    inputs=summarize_tool_input(name, merged_inp),
                    outputs=tool_output,
                )
                spans.append(tool_span)

                # Nest subagent children under Agent spans
                if name == "Agent" and tuid in subagent_children:
                    agent_span_id = tool_span["span_id"]
                    children_data = subagent_children[tuid]
                    # Derive the subagent's time window from its tool
                    # children's timestamps (LLM spans don't have tuids).
                    child_timestamps = [
                        tool_result_ns[ct]
                        for ctype, ct, _, _ in children_data
                        if ctype == "tool" and ct in tool_result_ns]
                    if child_timestamps:
                        sa_start = min(child_timestamps) - int(1e9)
                        sa_start = max(sa_start, b_start)
                    else:
                        sa_start = b_start
                    # Also update the Agent span itself to cover its children
                    if child_timestamps:
                        tool_span["start_time_unix_nano"] = sa_start
                        tool_span["end_time_unix_nano"] = max(
                            max(child_timestamps), child_end or 0)

                    _llm_idx = 0
                    _sa_recent_tools = []
                    for c_type_tag, c_tuid, c_name, c_inp in children_data:
                        if c_type_tag == "llm":
                            # LLM reasoning span
                            _llm_idx += 1
                            llm_text = c_name  # text stored in name slot
                            sa_llm_inputs = {"model": _subagent_model}
                            if _sa_recent_tools:
                                sa_llm_inputs["context"] = (
                                    "; ".join(_sa_recent_tools))
                                _sa_recent_tools.clear()
                            spans.append(make_span(
                                trace_id, agent_span_id,
                                name="LLM",
                                span_type="LLM",
                                start_ns=sa_start,
                                end_ns=sa_start + int(0.5e9),
                                inputs=sa_llm_inputs,
                                outputs={"text": llm_text[:500]},
                                extra_attrs=(
                                    {"mlflow.llm.model":
                                     json.dumps(_subagent_model)}
                                    if _subagent_model else None),
                            ))
                            sa_start += int(0.5e9)
                        else:
                            # Tool call span
                            c_end = tool_result_ns.get(
                                c_tuid, child_end)
                            c_start = max(
                                sa_start,
                                c_end - int(1e9)) if c_end else sa_start
                            c_type = ("AGENT" if c_name == "Agent"
                                      else "TOOL")
                            c_output = None
                            if c_tuid in tool_result_content:
                                c_output = {
                                    "result": tool_result_content[c_tuid]}
                            spans.append(make_span(
                                trace_id, agent_span_id,
                                name=c_name,
                                span_type=c_type,
                                start_ns=c_start,
                                end_ns=max(c_end,
                                           c_start + int(0.1e9)),
                                inputs=summarize_tool_input(
                                    c_name, c_inp),
                                outputs=c_output,
                            ))
                            _sa_recent_tools.append(
                                _tool_one_liner(c_name, c_inp))
                            sa_start = max(
                                sa_start,
                                c_end + int(0.1e9)) if c_end else (
                                sa_start + int(1e9))

        cursor_ns = step_end

    # ── Distribute per-model cost across LLM spans ─────────────
    # The "Cost Over Time" chart aggregates mlflow.llm.cost from
    # individual LLM spans grouped by mlflow.llm.model.  Distribute
    # each model's total cost evenly across its LLM spans so the
    # chart totals match run_result.per_model_usage.
    #
    # Fallback: when per_model_usage is absent (older Claude Code versions
    # don't include modelUsage in the result event), distribute the total
    # cost_usd evenly across all LLM spans so the Cost Over Time chart
    # shows a non-zero value instead of $0.00.
    llm_spans = [s for s in spans
                 if s["attributes"].get("mlflow.spanType") == json.dumps("LLM")]
    if per_model_usage:
        # Count LLM spans per model
        model_span_counts = {}
        for span in llm_spans:
            m = span["attributes"].get("mlflow.llm.model")
            if m:
                try:
                    m = json.loads(m)
                except (json.JSONDecodeError, TypeError):
                    pass
                model_span_counts[m] = model_span_counts.get(m, 0) + 1

        # Build per-model cost-per-span
        # Normalize model names: per_model_usage keys may use "@"
        # (e.g. "claude-sonnet-4-5@20250929") while span attributes
        # use "-" (e.g. "claude-sonnet-4-5-20250929").
        model_cost_per_span = {}
        for m_name, m_stats in per_model_usage.items():
            m_cost = m_stats.get("cost_usd")
            normalized = m_name.replace("@", "-")
            m_count = (model_span_counts.get(m_name, 0)
                       or model_span_counts.get(normalized, 0))
            if m_cost and m_count > 0:
                model_cost_per_span[m_name] = m_cost / m_count
                model_cost_per_span[normalized] = m_cost / m_count

        # Set mlflow.llm.cost on each LLM span
        spans_with_cost = 0
        for span in llm_spans:
            m = span["attributes"].get("mlflow.llm.model")
            if m:
                try:
                    m = json.loads(m)
                except (json.JSONDecodeError, TypeError):
                    pass
                per_span_cost = model_cost_per_span.get(m)
                if per_span_cost:
                    span["attributes"]["mlflow.llm.cost"] = json.dumps({
                        "total_cost": per_span_cost,
                    })
                    spans_with_cost += 1

        # If per_model_usage was present but no spans matched (e.g. model name
        # not captured in stream events), fall back to even distribution.
        # Also inject mlflow.llm.model so the store populates dimension_attributes,
        # which the Cost Breakdown chart groups by.
        if spans_with_cost == 0 and cost_usd and llm_spans:
            cost_per_span = cost_usd / len(llm_spans)
            # Use first model from per_model_usage if there's exactly one
            fallback_model = (
                next(iter(per_model_usage)) if len(per_model_usage) == 1 else None
            )
            for span in llm_spans:
                span["attributes"]["mlflow.llm.cost"] = json.dumps({
                    "total_cost": cost_per_span,
                })
                if fallback_model and not span["attributes"].get("mlflow.llm.model"):
                    span["attributes"]["mlflow.llm.model"] = json.dumps(fallback_model)

    elif cost_usd and llm_spans:
        # Fallback: no per-model breakdown — distribute total cost evenly
        # across all LLM spans so Cost Over Time / Cost Breakdown populate.
        cost_per_span = cost_usd / len(llm_spans)
        fallback_model = model  # from run_result
        for span in llm_spans:
            span["attributes"]["mlflow.llm.cost"] = json.dumps({
                "total_cost": cost_per_span,
            })
            if fallback_model and not span["attributes"].get("mlflow.llm.model"):
                span["attributes"]["mlflow.llm.model"] = json.dumps(fallback_model)

    # ── Trace metadata ──────────────────────────────────────────
    trace_metadata = {}
    if cost_usd:
        trace_cost = {"total_cost": cost_usd}
        if per_model_usage:
            for m_name, m_stats in per_model_usage.items():
                if m_stats.get("cost_usd") is not None:
                    trace_cost[m_name] = m_stats["cost_usd"]
        trace_metadata["mlflow.trace.cost"] = json.dumps(trace_cost)
    if token_usage:
        # Follow MLflow's anthropic / claude_code token-usage convention so the
        # Usage dashboard renders Input / Output / Cache Read / Cache Write as
        # distinct lines: input_tokens is the NON-cached (fresh) input, cache
        # tokens are separate optional keys, and total_tokens = input + output
        # (cache excluded). The cache lines carry the bulk of the volume; cost
        # stays separate via mlflow.llm.cost / mlflow.trace.cost.
        input_tokens = token_usage.get("input", 0)
        output_tokens = token_usage.get("output", 0)
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        cache_read = token_usage.get("cache_read", 0)
        cache_create = token_usage.get("cache_create", 0)
        if cache_read:
            usage["cache_read_input_tokens"] = cache_read
        if cache_create:
            usage["cache_creation_input_tokens"] = cache_create
        trace_metadata["mlflow.trace.tokenUsage"] = json.dumps(usage)
    if session_id:
        trace_metadata["mlflow.trace.session"] = session_id
    # Inject a synthetic gateway endpoint ID so that MLflow's "Cost Over Time"
    # and "Cost Breakdown" charts (which join on mlflow.gateway.endpointId) can
    # aggregate cost from our eval traces.  The value is namespaced to avoid
    # collision with real AI Gateway endpoints.
    if cost_usd:
        trace_metadata["mlflow.gateway.endpointId"] = f"eval-harness/{run_id}"

    return {
        "info": {
            "trace_id": trace_id,
            "trace_location": {
                "type": "MLFLOW_EXPERIMENT",
                "mlflow_experiment": {"experiment_id": experiment_id},
            },
            "request_time": (datetime.fromtimestamp(
                trace_start / 1e9, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
            "trace_metadata": trace_metadata,
            "state": "OK",
            "execution_duration_ms": duration_ms,
            "request_preview": prompt[:200],
            "response_preview": final_response[:200],
            "tags": {
                "eval_run_id": run_id,
                "source": "stream-json",
                "mlflow.traceName": trace_name or f"skill-run ({run_id})",
            },
        },
        "data": {"spans": spans},
    }


def summarize_tool_input(tool_name, tool_input):
    """Compact tool-call payload for span display (keeps file bodies)."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        return {"command": tool_input.get("command", "")[:200]}
    elif tool_name == "Write":
        out = {"file_path": tool_input.get("file_path", "")}
        content = tool_input.get("content")
        if isinstance(content, str) and content:
            out["content"] = content[:_MAX_WRITE_CONTENT]
        return out
    elif tool_name == "Edit":
        out = {"file_path": tool_input.get("file_path", "")}
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if isinstance(old, str) and old:
            out["old_string"] = old[:_MAX_EDIT_FRAGMENT]
        if isinstance(new, str) and new:
            out["new_string"] = new[:_MAX_EDIT_FRAGMENT]
        return out
    elif tool_name == "Read":
        out = {"file_path": tool_input.get("file_path", "")}
        if tool_input.get("offset") is not None:
            out["offset"] = tool_input.get("offset")
        if tool_input.get("limit") is not None:
            out["limit"] = tool_input.get("limit")
        return out
    elif tool_name == "Agent":
        return {"description": tool_input.get("description", "")}
    elif tool_name == "Skill":
        return {"skill": tool_input.get("skill", "")}
    elif tool_name in ("Glob", "Grep"):
        return {"pattern": tool_input.get("pattern", "")}
    else:
        s = json.dumps(tool_input)
        return {"input": s[:200]}



def log_trace(trace_dict):
    """Submit a trace dict to MLflow. Returns trace_id or None.

    Uses MlflowClient._log_trace (internal API) as MLflow 3.5 has no
    public API for logging pre-built Trace objects. The public fluent
    API (mlflow.start_span, @mlflow.trace) requires live instrumentation
    and can't accept a pre-constructed trace dict. If a public method
    becomes available, this should be updated.
    """
    try:
        from mlflow import MlflowClient
        from mlflow.entities.trace import Trace

        trace = Trace.from_dict(trace_dict)
        client = MlflowClient()
        # No public API for logging pre-built traces as of MLflow 3.5.
        # _log_trace is the only way to submit a Trace object.
        # IMPORTANT: _log_trace returns a backend-generated trace ID that
        # differs from the client-provided trace_dict["info"]["trace_id"].
        # Always return the backend ID so downstream FK operations succeed.
        server_trace_id = client._log_trace(trace)
        if not server_trace_id:
            print(
                "WARNING: _log_trace returned no backend ID; falling back to "
                "client trace_id — downstream log_feedback may hit FK errors.",
                file=sys.stderr,
            )
            return trace_dict["info"]["trace_id"]
        return server_trace_id
    except Exception as e:
        print(f"WARNING: failed to log trace: {e}", file=sys.stderr)
        return None
