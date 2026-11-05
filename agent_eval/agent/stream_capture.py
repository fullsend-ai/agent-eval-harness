"""Stream-JSON capture utilities for Claude Code output.

Reusable functions and classes for processing the stream-json output
from ``claude --print --output-format stream-json``.  Used by both
:class:`ClaudeCodeRunner` (eval pipeline) and ``claude-trace`` (standalone).
"""

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


# ── Synthetic event injection ────────────────────────────────────────

def make_prompt_event(prompt: str) -> str:
    """Create a synthetic user event JSON line for the prompt.

    ``claude --print`` doesn't emit the stdin prompt as an event, so
    this injects it for downstream consumers (trace builder, MLflow).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "timestamp": ts,
    })


def inject_timestamp(line: str) -> str:
    """Add a receive timestamp to a stream-json line that lacks one.

    Assistant events from Claude Code don't have timestamps.  This
    injects wall-clock time so traces have real timing for every event.
    Returns the original line unchanged if it already has a timestamp
    or can't be parsed as JSON.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    if "timestamp" not in obj:
        obj["timestamp"] = (datetime.now(timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
        return json.dumps(obj)
    return line


# ── Usage extraction ─────────────────────────────────────────────────

def _is_real_model(name):
    """Whether ``name`` is a genuine model, not a Claude Code placeholder.

    Claude Code stamps some assistant messages with a bracketed pseudo-model
    such as ``"<synthetic>"`` — zero-token placeholder turns it materializes
    itself (e.g. a turn terminated by a stop sequence, which has no
    server-attributed model). These must be excluded from model/turn
    accounting, otherwise the pseudo-model surfaces as a spurious "subagent
    model" and pollutes per-model turn counts.
    """
    return bool(name) and not (name.startswith("<") and name.endswith(">"))


def extract_usage(stdout_lines):
    """Extract token usage, cost, turns, and models from stream-json events.

    - Token totals: from ``result.modelUsage`` (includes all subagents).
      Falls back to summing ``assistant`` event usage if modelUsage is absent.
    - Cost: from the last ``result`` event (cumulative in Claude Code,
      includes all subagents).
    - Per-model usage: from ``result.modelUsage``, with per-model ``costUSD``,
      token counts, and context window info.
    - Turns: count of unique ``assistant`` messages (by message ID) visible
      in the stream.  Since Claude Code >= 2.1.108 streams subagent messages
      too (with ``parent_tool_use_id``), this includes both root and subagent
      turns.  Call :func:`count_subagent_turns` and merge with
      :attr:`seen_msg_ids` to get the correct total without double-counting.
    - Models: all distinct models observed in ``assistant`` events.

    Returns:
        Tuple of (token_usage, cost_usd, num_turns, seen_msg_ids,
        models_seen, per_model_usage, seen_msg_ids_by_model).
        ``num_turns`` counts all assistant turns visible in the stream.
        ``seen_msg_ids`` is the set of message IDs for deduplication with
        subagent transcripts.
        ``per_model_usage`` is a dict mapping model name to token/cost breakdown,
        or None if modelUsage was absent.
        ``seen_msg_ids_by_model`` is a dict mapping model name to its set of
        observed message IDs (for per-model turn counting and deduplication
        with :func:`count_subagent_turns_by_model`), or None if no assistant
        events were seen. Per-model turn counts are ``{m: len(ids)}``.
    """
    cost_usd = None
    models_seen = set()
    model_usage = None
    seen_msg_ids = set()
    seen_msg_ids_by_model = {}
    fb_input = fb_output = fb_cache_read = fb_cache_create = 0
    for line in stdout_lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") == "assistant":
            msg_id = obj.get("message", {}).get("id")
            model = obj.get("message", {}).get("model")
            # Keep placeholder turns in seen_msg_ids (they are real stream
            # turns for dedup/total counting), but exclude their pseudo-model
            # from per-model accounting and models_seen.
            if msg_id:
                seen_msg_ids.add(msg_id)
                if _is_real_model(model):
                    seen_msg_ids_by_model.setdefault(model, set()).add(msg_id)
            u = obj.get("message", {}).get("usage", {})
            fb_input += u.get("input_tokens") or 0
            fb_output += u.get("output_tokens") or 0
            fb_cache_read += u.get("cache_read_input_tokens") or 0
            fb_cache_create += u.get("cache_creation_input_tokens") or 0
            if _is_real_model(model):
                models_seen.add(model)
        elif obj.get("type") == "result":
            cost_usd = obj.get("total_cost_usd", cost_usd)
            mu = obj.get("modelUsage")
            if isinstance(mu, dict):
                model_usage = mu

    token_usage = None
    per_model_usage = None
    if model_usage:
        total_input = total_output = total_cache_read = total_cache_create = 0
        per_model_usage = {}
        for model_name, stats in model_usage.items():
            m_input = stats.get("inputTokens") or 0
            m_output = stats.get("outputTokens") or 0
            m_cache_read = stats.get("cacheReadInputTokens") or 0
            m_cache_create = stats.get("cacheCreationInputTokens") or 0
            total_input += m_input
            total_output += m_output
            total_cache_read += m_cache_read
            total_cache_create += m_cache_create
            per_model_usage[model_name] = {
                "input": m_input,
                "output": m_output,
                "cache_read": m_cache_read,
                "cache_create": m_cache_create,
                "cost_usd": stats.get("costUSD"),
            }
        token_usage = {
            "input": total_input, "output": total_output,
            "cache_read": total_cache_read, "cache_create": total_cache_create,
        }
    elif fb_input or fb_output:
        token_usage = {
            "input": fb_input, "output": fb_output,
            "cache_read": fb_cache_read, "cache_create": fb_cache_create,
        }
    num_turns = len(seen_msg_ids) or None
    return (token_usage, cost_usd, num_turns, seen_msg_ids, models_seen,
            per_model_usage, seen_msg_ids_by_model or None)


# ── Subagent turn counting ──────────────────────────────────────────

def count_subagent_turns(subagent_dir, already_seen=None):
    """Count unique assistant turns across all subagent transcripts.

    Subagent transcripts are JSONL files captured by the SubagentStop hook.
    Each line is a message object with ``message.role`` and ``message.id``.
    Multiple JSONL lines can share the same message ID (one per content
    block), so we deduplicate by ID.

    Since Claude Code >= 2.1.108 streams subagent messages in the main
    stdout stream, some IDs may already appear in :func:`extract_usage`
    results.  Pass ``already_seen`` to avoid double-counting.

    Args:
        subagent_dir: Path to directory containing ``*.jsonl`` transcripts.
        already_seen: Optional set of message IDs already counted from the
            stream.  If provided, only IDs not in this set are counted.

    Returns:
        Number of unique NEW assistant turns (not in ``already_seen``),
        or 0 if the directory doesn't exist or has no transcripts.
    """
    subagent_path = Path(subagent_dir)
    if not subagent_path.is_dir():
        return 0
    seen = set(already_seen) if already_seen else set()
    initial_count = len(seen)
    for transcript in subagent_path.iterdir():
        if not transcript.is_file() or transcript.suffix != ".jsonl":
            continue
        try:
            with open(transcript) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = obj.get("message", {})
                    if msg.get("role") == "assistant":
                        msg_id = msg.get("id")
                        if msg_id:
                            seen.add(msg_id)
        except OSError:
            continue
    return len(seen) - initial_count


def subagent_message_ids(subagent_dir):
    """Every assistant ``message.id`` across the captured subagent transcripts
    (deduplicated). Empty when the directory does not exist."""
    subagent_path = Path(subagent_dir)
    ids = set()
    if not subagent_path.is_dir():
        return ids
    for transcript in subagent_path.iterdir():
        if not transcript.is_file() or transcript.suffix != ".jsonl":
            continue
        try:
            with open(transcript) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = obj.get("message", {}) if isinstance(obj, dict) else {}
                    if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("id"):
                        ids.add(msg["id"])
        except OSError:
            continue
    return ids


def subagent_generation_ids(subagent_dir):
    """Every OpenRouter generation id across captured subagent transcripts
    (deduplicated): a top-level ``requestId`` or the legacy ``message.id``."""
    subagent_path = Path(subagent_dir)
    ids = set()
    if not subagent_path.is_dir():
        return ids
    for transcript in subagent_path.iterdir():
        if not transcript.is_file() or transcript.suffix != ".jsonl":
            continue
        try:
            with open(transcript) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    generation_id = _event_generation_id(obj)
                    if generation_id is not None:
                        ids.add(generation_id)
        except OSError:
            continue
    return ids


def message_ids_for_run(stream_ids, subagent_dir):
    """The run's Claude message-id set: root-stream ids merged with the
    subagent transcripts' ids, in a stable order."""
    ids = set(stream_ids or ()) | subagent_message_ids(subagent_dir)
    return sorted(ids)


def generation_ids_for_run(stream_generation_ids, subagent_dir):
    """The run's OpenRouter generation-id set: root-stream ids merged with
    subagent transcript ids, in a stable order."""
    ids = set(stream_generation_ids or ()) | subagent_generation_ids(subagent_dir)
    return sorted(ids)


def extract_session_id(stdout_lines):
    """The Claude Code session id from the result or system/init event."""
    for line in reversed(list(stdout_lines or ())):
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result":
            session_id = obj.get("session_id")
            if isinstance(session_id, str) and session_id:
                return session_id
    for line in stdout_lines or ():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if (isinstance(obj, dict) and obj.get("type") == "system"
                and obj.get("subtype") == "init"):
            session_id = obj.get("session_id")
            if isinstance(session_id, str) and session_id:
                return session_id
    return None


def claude_session_dir_name(workspace):
    """Claude Code's encoded project-directory name for a workspace."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(Path(workspace).resolve()))


def claude_session_generation_ids(workspace, session_id, projects_dir=None):
    """OpenRouter generation ids from Claude Code's persisted session transcript.

    Claude Code's stream-json output does not always include the top-level
    ``requestId``, but its session transcript does. Older versions echo the
    generation id as ``message.id``. The transcript is removed after a
    successful run, so callers must read it before cleanup.
    """
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return set()
    try:
        projects = (Path(projects_dir) if projects_dir is not None
                    else Path.home() / ".claude" / "projects").resolve(strict=True)
    except OSError:
        return set()
    encoded = claude_session_dir_name(workspace)
    session_dir = projects / encoded
    if session_dir.is_symlink() or not session_dir.is_dir():
        return set()
    try:
        session_root = session_dir.resolve(strict=True)
    except OSError:
        return set()
    if not session_root.is_relative_to(projects):
        return set()
    root_transcript = session_root / f"{session_id}.jsonl"
    transcripts = [root_transcript]
    nested_dir = session_root / session_id
    if nested_dir.is_symlink() or not nested_dir.is_dir():
        nested_dir = None
    else:
        transcripts.extend(nested_dir.rglob("*.jsonl"))
    ids = set()
    for transcript in transcripts:
        if transcript.is_symlink():
            continue
        try:
            transcript = transcript.resolve(strict=True)
        except OSError:
            continue
        if not transcript.is_file() or not transcript.is_relative_to(session_root):
            continue
        try:
            with open(transcript) as stream:
                for line in stream:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    generation_id = _event_generation_id(obj)
                    if generation_id is not None:
                        ids.add(generation_id)
        except OSError:
            continue
    return ids


def extract_generation_ids(stdout_lines):
    """OpenRouter generation ids from stream-json output.

    Newer Claude Code emits a top-level ``requestId``. Older versions echo
    the generation id as ``assistant.message.id``.
    """
    ids = set()
    for line in stdout_lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        generation_id = _event_generation_id(obj)
        if generation_id is not None:
            ids.add(generation_id)
    return ids


def _event_generation_id(obj):
    """The OpenRouter generation id carried by a Claude event.

    ``requestId`` is preferred. For the legacy event shape, where Claude Code
    echoes the OpenRouter generation id as ``assistant.message.id``, that id
    is returned instead.
    """
    if not isinstance(obj, dict):
        return None
    request_id = obj.get("requestId")
    if isinstance(request_id, str) and request_id.startswith("gen-"):
        return request_id
    if obj.get("type") is not None and obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    if obj.get("type") is None and msg.get("role") != "assistant":
        return None
    message_id = msg.get("id")
    if isinstance(message_id, str) and message_id.startswith("gen-"):
        return message_id
    return None


def assistant_event_ids(obj):
    """``(message id, generation id, model)`` from an assistant event.

    Claude's ``message.id`` is retained for turn counting. On an OpenRouter
    connection the generation id is the top-level ``requestId`` or, on older
    versions, the same legacy ``message.id``.
    """
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None, None, None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None, None, None
    message_id = msg.get("id")
    request_id = obj.get("requestId")
    generation_id = _event_generation_id(obj)
    return message_id, generation_id, msg.get("model")


def assistant_message_id(obj):
    """``(message id, model)`` of an ``assistant`` stream event, used for
    turn counting; ``(None, None)`` otherwise."""
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None, None
    msg = obj.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("id"), str) or not msg["id"]:
        return None, None
    return msg["id"], msg.get("model")


_API_ERROR_RE = re.compile(r"API Error:?\s*(?:(\d{3})\b)?\s*(.*)", re.S)
_ERROR_TYPE_RE = re.compile(r'"(?:error_)?type"\s*:\s*"([a-z_]+)"')
_KEY_LIMIT_RE = re.compile(r"key limit|limit_usd|credit limit|insufficient credits", re.I)


def classify_stream_errors(stdout_lines):
    """Best-effort ``(error_class, budget)`` for the run's failed provider
    requests — the only view the harness has of an agent-path failure, since it
    is not on the HTTP path: the ``result`` event's ``api_error_status`` /
    ``is_error`` and Claude Code's ``API Error: <status> …`` assistant text.
    ``budget`` is ``{"exceeded": "run", "exceeded_reason": "limit_usd"}`` for
    OpenRouter's key-limit 402. ``(None, None)`` when no provider error shows."""
    status, message = None, ""
    for line in stdout_lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        texts = []
        if obj.get("type") == "result":
            if isinstance(obj.get("api_error_status"), int) and not isinstance(obj.get("api_error_status"), bool):
                status, message = obj["api_error_status"], str(obj.get("result") or message)
            elif obj.get("is_error") and isinstance(obj.get("result"), str):
                texts.append(obj["result"])
        elif obj.get("type") == "assistant" and isinstance(obj.get("message"), dict):
            texts += [b.get("text") for b in (obj["message"].get("content") or [])
                      if isinstance(b, dict) and isinstance(b.get("text"), str)]
        for text in texts:
            m = _API_ERROR_RE.search(text) if text.lstrip().startswith("API Error") else None
            if m:
                status = int(m.group(1)) if m.group(1) else status
                message = m.group(2) or message
    if status is None and not message:
        return None, None
    from agent_eval.providers.openrouter.errors import classify

    tm = _ERROR_TYPE_RE.search(message or "")
    error_class = classify(tm.group(1) if tm else None, status, message).value
    budget = ({"exceeded": "run", "exceeded_reason": "limit_usd"}
              if status == 402 and _KEY_LIMIT_RE.search(message or "") else None)
    return error_class, budget


def count_subagent_turns_by_model(subagent_dir, already_seen_by_model=None):
    """Count NEW unique assistant turns per model across subagent transcripts.

    Like :func:`count_subagent_turns` but groups by ``message.model``. Only
    counts message IDs that aren't already in ``already_seen_by_model[model]``,
    so callers can pass in IDs already counted from the main stream.

    Args:
        subagent_dir: Path to directory containing ``*.jsonl`` transcripts.
        already_seen_by_model: Optional dict mapping model name to a set of
            already-counted message IDs (from the main stream).

    Returns:
        Dict mapping model name to count of new assistant turns. Returns an
        empty dict if the directory is missing or has no assistant turns.
    """
    subagent_path = Path(subagent_dir)
    if not subagent_path.is_dir():
        return {}
    seen = {m: set(ids) for m, ids in (already_seen_by_model or {}).items()}
    initial_counts = {m: len(s) for m, s in seen.items()}
    for transcript in subagent_path.iterdir():
        if not transcript.is_file() or transcript.suffix != ".jsonl":
            continue
        try:
            with open(transcript) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = obj.get("message", {})
                    if msg.get("role") == "assistant":
                        msg_id = msg.get("id")
                        model = msg.get("model")
                        if msg_id and _is_real_model(model):
                            seen.setdefault(model, set()).add(msg_id)
        except OSError:
            continue
    return {m: len(s) - initial_counts.get(m, 0) for m, s in seen.items()
            if len(s) - initial_counts.get(m, 0) > 0}


def combine_per_model_turns(subagent_dir, stream_ids_by_model):
    """Combine stream-level per-model turn IDs with subagent transcripts.

    Returns ``{model: turn_count}`` summing stream IDs and any new IDs found
    in subagent transcripts (deduplicated by message ID). Returns None if no
    per-model data is available, so the field stays absent rather than {}."""
    by_model = {m: set(ids) for m, ids in (stream_ids_by_model or {}).items()}
    new_per_model = count_subagent_turns_by_model(subagent_dir, by_model) or {}
    counts = {m: len(ids) for m, ids in by_model.items()}
    for m, n in new_per_model.items():
        counts[m] = counts.get(m, 0) + n
    return counts or None


# ── Subagent capture via hooks ───────────────────────────────────────

def setup_subagent_hook(settings: dict, subagent_dir: str) -> None:
    """Add a SubagentStop hook to settings that copies transcripts.

    The SubagentStop hook fires synchronously when a subagent finishes.
    Session persistence must be ON (do NOT pass --no-session-persistence)
    so the transcript file still exists when the hook runs.

    Args:
        settings: The workspace .claude/settings.json dict (modified in place).
        subagent_dir: Absolute path to the directory where transcripts
            should be copied (e.g., /tmp/agent-eval/run-001/subagents).
    """
    # Write a small helper script instead of inlining shell — avoids
    # command injection via subagent_dir (CWE-78) and handles the
    # stdin JSON parsing cleanly.
    hook_script = Path(subagent_dir).parent / "hooks" / "subagent_stop.py"
    hook_script.parent.mkdir(parents=True, exist_ok=True)
    hook_script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, shutil, os\n"
        "d = json.load(sys.stdin)\n"
        "p = d.get('agent_transcript_path', '')\n"
        f"dest = {subagent_dir!r}\n"
        "if p and os.path.isfile(p) and not os.path.islink(p):\n"
        "    os.makedirs(dest, exist_ok=True)\n"
        "    shutil.copy2(p, os.path.join(dest, os.path.basename(p)))\n"
    )
    hook_script.chmod(0o755)

    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("SubagentStop", []).append({
        "hooks": [{
            "type": "command",
            "command": f"python3 '{hook_script}'",
        }],
    })
