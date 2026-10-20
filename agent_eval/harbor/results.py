"""Parse a Harbor job directory into structured per-case results.

The agent-eval-harness side of the Harbor boundary: after `harbor run` (local
Podman or OpenShift K8s) produces a job directory, this reads each trial's
verifier output — `reward.json` (the flat metric contract) and `judges.json`
(our richer sidecar with per-judge values + rationales) — into a shape the suite
layer can feed to MLflow and the HTML report.

It intentionally reads the per-trial verifier files our reward bridge writes
(stable contract) rather than Harbor's top-level `result.json` stats (which vary
by Harbor version). Pairwise/regression remain suite-level above this.
"""

import json
from datetime import datetime
from pathlib import Path

# Stands in for a step id on single-step trials, which have no steps/ dir.
_WHOLE_TASK = "(whole task)"


def _add_verdict_counters(record: dict, sidecar: dict) -> None:
    """Fold a judges.json sidecar's spec 015 judge-usage counters
    (``verdict_transports`` per transport, ``verdict_fallbacks``) into
    ``record``, summing when several steps report them."""
    transports = sidecar.get("verdict_transports")
    if isinstance(transports, dict):
        into = record.setdefault("verdict_transports", {})
        for name, n in transports.items():
            if isinstance(n, int) and not isinstance(n, bool):
                into[name] = into.get(name, 0) + n
    fallbacks = sidecar.get("verdict_fallbacks")
    if isinstance(fallbacks, int) and not isinstance(fallbacks, bool):
        record["verdict_fallbacks"] = record.get("verdict_fallbacks", 0) + fallbacks


def _case_id_from_dir(trial_dir: Path) -> str:
    """Recover the full task case ID, falling back to Harbor's trial slug.

    Harbor truncates long trial directory names. Its result metadata retains
    the original ``<suite>/<case>`` task name, so prefer that when available.
    """
    result_path = trial_dir / "result.json"
    if result_path.is_file():
        try:
            task_name = json.loads(result_path.read_text()).get("task_name")
            if isinstance(task_name, str) and task_name.strip():
                case_id = task_name.rstrip("/").rsplit("/", 1)[-1]
                if case_id not in {"", ".", ".."}:
                    return case_id
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    name = trial_dir.name
    return name.rsplit("__", 1)[0] if "__" in name else name


def _merge_per_model(acc: dict, pmu: dict | None) -> None:
    """Accumulate a per-model usage dict into ``acc`` (sum per model, per key).

    Used to aggregate per-model token/cost breakdowns across steps (multi-step
    trials) and across trials (whole job). None-safe on both the source dict and
    individual values.
    """
    if not pmu:
        return
    for model, stats in pmu.items():
        if not isinstance(stats, dict):
            continue
        dst = acc.setdefault(model, {"input": 0, "output": 0,
                                     "cache_read": 0, "cache_create": 0,
                                     "cost_usd": None})
        for k in ("input", "output", "cache_read", "cache_create"):
            v = stats.get(k)
            if isinstance(v, (int, float)):
                dst[k] = (dst[k] or 0) + v
        c = stats.get("cost_usd")
        if isinstance(c, (int, float)):
            dst["cost_usd"] = (dst["cost_usd"] or 0) + c


def _int_field(mapping: dict, key: str) -> int:
    """Read a token count defensively from agent-influenced transcript data."""
    value = mapping.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _empty_transcript_metrics() -> dict:
    """The 'nothing extracted' shape every transcript reader returns."""
    return {
        "cost_usd": None, "token_usage": None, "per_model_usage": None,
        "num_turns": None, "duration_s": None, "agent_version": None,
        "message_ids": [],
    }


def _extract_transcript_metrics(transcript_path: Path) -> dict:
    """Extract cost, tokens, turns, duration, version from a stream-json transcript."""
    result = _empty_transcript_metrics()
    if not transcript_path.is_file():
        return result
    codex_input = codex_output = codex_cache = codex_turns = 0
    try:
        for line in transcript_path.read_text().splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            # Cost-truth key set (spec 014): every assistant message id — on a
            # direct OpenRouter connection these are `gen-…` generation ids.
            if ev.get("type") == "assistant":
                msg = ev.get("message")
                msg_id = msg.get("id") if isinstance(msg, dict) else None
                if isinstance(msg_id, str) and msg_id and msg_id not in result["message_ids"]:
                    result["message_ids"].append(msg_id)
            if (ev.get("type") == "system" and ev.get("subtype") == "init"
                    and not result["agent_version"]):
                result["agent_version"] = ev.get("claude_code_version")
            if ev.get("type") in {"turn.completed", "turn_completed"}:
                usage = ev.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
                turn_input = _int_field(usage, "input_tokens")
                turn_cache = _int_field(usage, "cached_input_tokens")
                codex_input += max(turn_input - turn_cache, 0)
                codex_output += _int_field(usage, "output_tokens")
                codex_cache += turn_cache
                codex_turns += 1
            if ev.get("type") == "result":
                # Transcript content is agent-influenced; malformed fields
                # must degrade to missing metrics, not crash the mapping of
                # an already-completed Harbor run.
                cost = ev.get("total_cost_usd")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    result["cost_usd"] = float(cost)
                result["num_turns"] = _number(ev, "num_turns")
                duration_ms = _number(ev, "duration_ms")
                if duration_ms is not None:
                    result["duration_s"] = duration_ms / 1000
                usage = ev.get("usage", {})
                if not isinstance(usage, dict):
                    usage = {}
                if usage:
                    result["token_usage"] = {
                        "input": _number(usage, "input_tokens"),
                        "output": _number(usage, "output_tokens"),
                        "cache_read": _number(usage, "cache_read_input_tokens"),
                        "cache_create": _number(
                            usage, "cache_creation_input_tokens"),
                    }
                # Per-model breakdown from the result event's modelUsage (same
                # source the local runner uses in stream_capture.py) — enables
                # per-model columns in the report's Model Usage table.
                mu = ev.get("modelUsage")
                if isinstance(mu, dict) and mu:
                    pmu = {}
                    for mname, st in mu.items():
                        if not isinstance(st, dict):
                            continue
                        pmu[mname] = {
                            "input": _number(st, "inputTokens") or 0,
                            "output": _number(st, "outputTokens") or 0,
                            "cache_read": _number(st, "cacheReadInputTokens") or 0,
                            "cache_create": _number(
                                st, "cacheCreationInputTokens") or 0,
                            "cost_usd": _number(st, "costUSD"),
                        }
                    if pmu:
                        result["per_model_usage"] = pmu
        if codex_turns and result["token_usage"] is None:
            result["token_usage"] = {
                "input": codex_input,
                "output": codex_output,
                "cache_read": codex_cache,
            }
            result["num_turns"] = codex_turns
    except (OSError, UnicodeDecodeError):
        pass
    return result


def _agent_transcript_metrics(agent_dir: Path) -> dict:
    """Read the transcript emitted by the active stock Harbor agent."""
    for name in ("claude-code.txt", "codex.txt"):
        path = agent_dir / name
        if path.is_file():
            return _extract_transcript_metrics(path)
    return _empty_transcript_metrics()


def _number(mapping: dict, key: str):
    value = mapping.get(key)
    return (value if isinstance(value, (int, float))
            and not isinstance(value, bool) else None)


def _agent_result_metrics(agent_result) -> dict:
    """Normalize Harbor's AgentContext token/cost fields defensively."""
    if not isinstance(agent_result, dict):
        agent_result = {}
    total_input = _number(agent_result, "n_input_tokens")
    cache = _number(agent_result, "n_cache_tokens")
    output = _number(agent_result, "n_output_tokens")
    token_usage = None
    if any(value is not None for value in (total_input, cache, output)):
        token_usage = {
            "input": (max(total_input - (cache or 0), 0)
                      if total_input is not None else None),
            "output": output,
            "cache_read": cache,
        }
    return {
        "cost_usd": _number(agent_result, "cost_usd"),
        "token_usage": token_usage,
    }


def _timing_duration(timing) -> float | None:
    if not isinstance(timing, dict):
        return None
    started = timing.get("started_at")
    finished = timing.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        return max((datetime.fromisoformat(finished.replace("Z", "+00:00"))
                    - datetime.fromisoformat(started.replace("Z", "+00:00")))
                   .total_seconds(), 0)
    except ValueError:
        return None


def _errored_trial_record(trial_dir: Path) -> dict:
    """Minimal record for a trial that failed before producing any reward.

    Used when Harbor wrote an ``exception.txt`` but no ``steps/`` or
    ``reward.json`` (e.g. the pod never became Ready). Surfaced rather than
    dropped so the run counts all attempted cases and the failure is visible.
    """
    reason = "trial failed before producing a reward"
    try:
        # exception.txt is usually a Python traceback; the last non-empty line
        # carries the actual error (e.g. "RuntimeError: pod ... not Ready").
        lines = [ln.strip() for ln in
                 (trial_dir / "exception.txt").read_text().splitlines()
                 if ln.strip()]
        if lines:
            # exception.txt is untrusted (a failing container controls it).
            # Escape control chars / ANSI / newlines and bound the length before
            # it flows into run_result.json and CI logs — prevents log injection
            # (CWE-117) and avoids dumping unbounded/secret text (CWE-532).
            reason = lines[-1].encode("unicode_escape").decode("ascii")[:200]
    except OSError:
        pass
    return {
        "case_id": _case_id_from_dir(trial_dir),
        "trial_dir": trial_dir.name,
        "trial_path": str(trial_dir),
        "reward": None,
        "metrics": {},
        "per_judge": {},
        "errored": True,
        "infra_error_steps": [],
        "unjudged_steps": [],
        "trial_error": reason,
        "cost_usd": None,
        "token_usage": None,
        "per_model_usage": None,
        "message_ids": [],
        "num_turns": None,
        "duration_s": None,
        "agent_version": None,
    }


def parse_trial(trial_dir: Path) -> dict | None:
    """Parse one Harbor trial directory. Returns None if it has no reward.

    Supports both single-step trials (reward at ``verifier/reward.json``)
    and multi-step trials (per-step rewards under ``steps/<name>/verifier/``).
    A trial that failed before producing any reward but has Harbor's
    ``exception.txt`` is returned as a minimal errored record (not None).
    """
    steps_dir = trial_dir / "steps"
    if steps_dir.is_dir() and any(steps_dir.iterdir()):
        rec = _parse_multi_step_trial(trial_dir, steps_dir)
        if rec is not None:
            return rec

    reward_path = trial_dir / "verifier" / "reward.json"
    if not reward_path.is_file():
        # No reward at all. If Harbor recorded a trial-level failure
        # (exception.txt), surface it as an errored trial rather than dropping
        # it silently — a pod that never became Ready would otherwise vanish
        # from the run and under-count the case total.
        if (trial_dir / "exception.txt").is_file():
            return _errored_trial_record(trial_dir)
        return None

    try:
        reward_data = json.loads(reward_path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # reward.json present but truncated/unreadable. Same as the missing case:
        # surface it as an errored trial when Harbor recorded a failure, so it is
        # not silently dropped from the case total.
        if (trial_dir / "exception.txt").is_file():
            return _errored_trial_record(trial_dir)
        return None

    unjudged = bool(reward_data.get("agent_eval_unjudged"))
    metrics = {k: v for k, v in reward_data.items()
               if k not in {"reward", "agent_eval_unjudged"}}
    record = {
        "case_id": _case_id_from_dir(trial_dir),
        "trial_dir": trial_dir.name,
        "trial_path": str(trial_dir),
        "reward": None if unjudged else reward_data.get("reward"),
        "metrics": metrics,
        "per_judge": {},
        "errored": False,
        "infra_error_steps": [],
        # A single-step trial has no step id; name the task itself so the run
        # output reads correctly next to real step names from the multi-step path.
        "unjudged_steps": [_WHOLE_TASK] if unjudged else [],
        "cost_usd": None,
        "token_usage": None,
        "per_model_usage": None,
    }

    # Agent execution metrics from Harbor's trial result.json (cost/tokens),
    # plus agent_info/agent_execution fallbacks for agents whose transcripts
    # carry no result event (Codex emits neither version nor duration there).
    fallback_version = None
    fallback_duration = None
    trial_result = trial_dir / "result.json"
    if trial_result.is_file():
        try:
            parsed_result = json.loads(trial_result.read_text())
            ar = parsed_result.get("agent_result") or {}
            normalized = _agent_result_metrics(ar)
            record["cost_usd"] = normalized["cost_usd"]
            record["token_usage"] = normalized["token_usage"]
            info = parsed_result.get("agent_info") or {}
            version = info.get("version") if isinstance(info, dict) else None
            if isinstance(version, str) and version:
                fallback_version = version
            fallback_duration = _timing_duration(
                parsed_result.get("agent_execution"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass

    # Enrich from the agent transcript (turns, duration, version are only
    # available there; cost/tokens fall back to transcript when result.json
    # doesn't have them).
    extracted = _agent_transcript_metrics(trial_dir / "agent")
    record["message_ids"] = list(extracted.get("message_ids") or [])
    if record["cost_usd"] is None:
        record["cost_usd"] = extracted["cost_usd"]
    if record["token_usage"] is None:
        record["token_usage"] = extracted["token_usage"]
    elif extracted.get("token_usage"):
        for k, v in extracted["token_usage"].items():
            if v is not None and record["token_usage"].get(k) is None:
                record["token_usage"][k] = v
    record["num_turns"] = extracted.get("num_turns")
    record["duration_s"] = (extracted.get("duration_s")
                            if extracted.get("duration_s") is not None
                            else fallback_duration)
    record["agent_version"] = extracted.get("agent_version") or fallback_version
    record["per_model_usage"] = extracted.get("per_model_usage")

    # Richer sidecar (values + rationales) when present.
    judges_path = trial_dir / "verifier" / "judges.json"
    if judges_path.is_file():
        try:
            sidecar = json.loads(judges_path.read_text())
            record["per_judge"] = sidecar.get("per_judge", {})
            _add_verdict_counters(record, sidecar)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass

    # Trial-level error flag from Harbor (exception.txt present == errored).
    if (trial_dir / "exception.txt").is_file():
        record["errored"] = True

    return record


def _parse_multi_step_trial(trial_dir: Path, steps_dir: Path) -> dict | None:
    """Parse a multi-step Harbor trial into the same shape as a single-step one.

    Aggregates per-step rewards (mean), cost (sum), and tokens (sum) into
    a single record. Each step's reward becomes a judge keyed by step name.
    """
    step_dirs = sorted(d for d in steps_dir.iterdir() if d.is_dir())
    if not step_dirs:
        return None
    message_ids: list = []          # cost-truth key set across the steps (spec 014)

    rewards = []
    per_judge: dict = {}
    counters: dict = {}             # spec 015 verdict-transport counters, summed over steps
    infra_error_steps: list[str] = []
    unjudged_steps: list[str] = []
    total_cost = 0.0
    has_cost = False
    total_turns = 0
    total_duration = 0.0
    token_totals: dict = {}
    per_model_totals: dict = {}
    agent_version = None
    harbor_steps: dict[str, dict] = {}
    try:
        trial_result = json.loads((trial_dir / "result.json").read_text())
        raw_steps = trial_result.get("step_results") or []
        if isinstance(raw_steps, list):
            harbor_steps = {
                step["step_name"]: step for step in raw_steps
                if isinstance(step, dict)
                and isinstance(step.get("step_name"), str)
            }
        info = trial_result.get("agent_info") or {}
        version = info.get("version") if isinstance(info, dict) else None
        if isinstance(version, str) and version:
            agent_version = version
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        pass

    for step_dir in step_dirs:
        step_name = step_dir.name

        # The verifier (test.sh) always writes reward.json as its last action,
        # so a present-and-parseable reward means the verifier actually ran.
        # A missing/unreadable reward.json means the verifier never completed —
        # almost always a transient k8s exec / HAProxy connection drop, NOT a
        # genuine score of 0. We must not conflate the two.
        reward_path = step_dir / "verifier" / "reward.json"
        step_reward = None
        step_unjudged = False
        if reward_path.is_file():
            try:
                rd = json.loads(reward_path.read_text())
                step_reward = rd.get("reward")
                step_unjudged = bool(rd.get("agent_eval_unjudged"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass

        # bool is an int subclass — guard so a genuine reward of 0.0 still
        # counts toward the mean while a missing reward never does.
        verifier_ran = (not step_unjudged
                        and isinstance(step_reward, (int, float))
                        and not isinstance(step_reward, bool))
        if verifier_ran:
            rewards.append(step_reward)

        extracted = _agent_transcript_metrics(step_dir / "agent")
        for msg_id in extracted.get("message_ids") or []:
            if msg_id not in message_ids:
                message_ids.append(msg_id)
        harbor_step = harbor_steps.get(step_name, {})
        fallback = _agent_result_metrics(harbor_step.get("agent_result"))
        step_cost = extracted.get("cost_usd")
        if step_cost is None:
            step_cost = fallback["cost_usd"]
        step_turns = extracted.get("num_turns")
        step_duration = extracted.get("duration_s")
        if step_duration is None:
            step_duration = _timing_duration(harbor_step.get("agent_execution"))
        if not agent_version:
            agent_version = extracted.get("agent_version")

        rationale_parts = []
        if step_turns:
            rationale_parts.append(f"{step_turns} turns")
        if step_cost:
            rationale_parts.append(f"${step_cost:.2f}")
        if step_duration:
            rationale_parts.append(f"{step_duration:.0f}s")
        rationale = ", ".join(rationale_parts) if rationale_parts else ""

        if verifier_ran:
            per_judge[step_name] = {
                "value": step_reward,
                "rationale": rationale,
                "judge_type": "step",
            }
        elif step_unjudged:
            per_judge[step_name] = {
                "value": None,
                "rationale": (rationale + "; " if rationale else "")
                + "no deterministic judge targeted this step",
                "judge_type": "step",
                "error": "unjudged",
            }
            unjudged_steps.append(step_name)
        else:
            # value=None (not False) so it is excluded from the judge mean and
            # not counted as a real 0. Flagged so the run can surface it.
            per_judge[step_name] = {
                "value": None,
                "rationale": (rationale + "; " if rationale else "")
                + "verifier produced no reward (infra/exec failure, not a score)",
                "judge_type": "step",
                "error": "no_verifier_reward",
            }
            infra_error_steps.append(step_name)

        if isinstance(step_cost, (int, float)):
            total_cost += step_cost
            has_cost = True
        if isinstance(step_turns, (int, float)):
            total_turns += int(step_turns)
        if isinstance(step_duration, (int, float)):
            total_duration += step_duration
        step_tokens = extracted.get("token_usage") or fallback["token_usage"] or {}
        for k, v in step_tokens.items():
            if isinstance(v, (int, float)):
                token_totals[k] = token_totals.get(k, 0) + v
        _merge_per_model(per_model_totals, extracted.get("per_model_usage"))

    mean_reward = sum(rewards) / len(rewards) if rewards else None

    # If any step has a judges.json (from the full judge engine), merge those
    # judges into per_judge — they provide richer scoring than step rewards.
    # Merge every step's judges.json sidecar so earlier steps' step-scoped
    # judges keep their values and rationales in the report; iterating in
    # step order lets the final step's engine reward win, matching the
    # ``multi_step_reward_strategy = final`` semantics of generated tasks.
    for step_dir in step_dirs:
        judges_path = step_dir / "verifier" / "judges.json"
        if judges_path.is_file():
            try:
                jdata = json.loads(judges_path.read_text())
                engine_judges = jdata.get("per_judge", {})
                _add_verdict_counters(counters, jdata)
                if engine_judges:
                    per_judge.update(engine_judges)
                    engine_reward = jdata.get("reward")
                    if isinstance(engine_reward, (int, float)):
                        mean_reward = engine_reward
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass

    return {
        "case_id": _case_id_from_dir(trial_dir),
        "trial_dir": trial_dir.name,
        "trial_path": str(trial_dir),
        "reward": mean_reward,
        "metrics": {s.name: per_judge[s.name]["value"] for s in step_dirs},
        "per_judge": per_judge,
        **counters,
        "errored": (trial_dir / "exception.txt").is_file(),
        "infra_error_steps": infra_error_steps,
        "unjudged_steps": unjudged_steps,
        "cost_usd": total_cost if has_cost else None,
        "token_usage": token_totals or None,
        "per_model_usage": per_model_totals or None,
        "num_turns": total_turns if total_turns > 0 else None,
        "duration_s": total_duration if total_duration > 0 else None,
        "agent_version": agent_version,
        "message_ids": message_ids,
    }


def price_trials(trials: list, rows: list) -> None:
    """Per-trial cost truth (spec 014): join each trial's transcript ids
    against the run's ledger rows, in place.

    A trial is priced only when every one of its ``gen-…`` ids has an ``ok``
    generation row (a partial sum is not spend); the transcript's own
    ``total_cost_usd`` moves to ``cost_usd_estimate``. Attribution is per
    trial and exact — the ids are per request and the transcript is per
    trial, so replications of one case never share a number. Trials without
    ids stay unpriced with the estimate preserved.
    """
    from agent_eval.providers.openrouter.generation import is_generation_id

    priced_rows = {r["gen_id"]: r for r in rows
                   if r.get("source") == "generation" and r.get("status") == "ok"
                   and r.get("gen_id") and isinstance(r.get("cost_usd"), (int, float))}
    for trial in trials:
        ids = [i for i in (trial.get("message_ids") or []) if is_generation_id(i)]
        trial["cost_usd_estimate"] = trial.get("cost_usd")
        costs = [priced_rows[i]["cost_usd"] for i in ids if i in priced_rows]
        if ids and len(costs) == len(ids):
            trial["cost_usd"] = round(sum(costs), 6)
            trial["cost_source"] = "openrouter:generation"
        else:
            trial["cost_usd"] = None
            trial["cost_source"] = "unavailable"
        trial["cost_coverage"] = {"requests": len(ids), "requests_priced": len(costs)}


def trial_costs(trials: list) -> list:
    """The per-trial cost view carried in ``run_result.json`` under a plan."""
    return [{k: t.get(k) for k in ("case_id", "trial_dir", "cost_usd", "cost_source",
                                   "cost_usd_estimate", "cost_coverage")}
            for t in trials]


def parse_job(job_dir: Path) -> dict:
    """Parse a Harbor job directory into aggregated per-case results.

    Returns ``{trials, mean_reward, n_completed, n_errored, aggregated}`` where
    ``aggregated`` maps each metric name to ``{values, mean}`` across trials —
    the same shape the local scorer's ``aggregated`` uses, so the report/MLflow
    code can consume Harbor runs uniformly.
    """
    job_dir = Path(job_dir)
    trials = []
    for child in sorted(job_dir.iterdir()):
        if not child.is_dir():
            continue
        trial = parse_trial(child)
        if trial is not None:
            trials.append(trial)

    rewards = [t["reward"] for t in trials if isinstance(t.get("reward"), (int, float))]
    mean_reward = sum(rewards) / len(rewards) if rewards else None

    # Steps whose verifier never produced a reward (transient k8s exec / HAProxy
    # drop). Surfaced separately so they are visible rather than silently scored 0.
    infra_errors = [(t["case_id"], step)
                    for t in trials
                    for step in t.get("infra_error_steps", [])]

    # Trials that failed before producing any reward (e.g. pod never Ready).
    # Surfaced so they are visible rather than dropped from the case total.
    trial_errors = [(t["case_id"], t["trial_error"])
                    for t in trials if t.get("trial_error")]

    # Steps/tasks whose verifier deliberately recorded no judgement — a setup
    # step no judge targets, or --no-llm-judges leaving nothing to run. Unlike
    # an infra error nothing failed, but the reward is absent either way, so
    # the run has to say so rather than let the case read as scored.
    unjudged_steps = [(t["case_id"], step)
                      for t in trials
                      for step in t.get("unjudged_steps", [])]

    # Aggregate each metric across trials (mean), mirroring score.py's shape.
    aggregated: dict[str, dict] = {}
    for trial in trials:
        for name, value in trial["metrics"].items():
            if isinstance(value, (int, float)):
                aggregated.setdefault(name, {"values": []})["values"].append(value)
    for name, agg in aggregated.items():
        vals = agg["values"]
        agg["mean"] = sum(vals) / len(vals) if vals else None

    # Aggregate agent cost/tokens across trials for run-level metrics.
    cost_values = [t["cost_usd"] for t in trials
                   if isinstance(t.get("cost_usd"), (int, float))]
    total_cost = sum(cost_values) if cost_values else None
    token_usage: dict = {}
    for t in trials:
        for k, v in (t.get("token_usage") or {}).items():
            if isinstance(v, (int, float)):
                token_usage[k] = token_usage.get(k, 0) + v
    per_model_usage: dict = {}
    for t in trials:
        _merge_per_model(per_model_usage, t.get("per_model_usage"))

    # Aggregate turns, duration, and pick agent version from trials.
    turn_values = [t["num_turns"] for t in trials
                   if isinstance(t.get("num_turns"), (int, float))]
    total_turns = sum(turn_values) if turn_values else None
    dur_values = [t["duration_s"] for t in trials
                  if isinstance(t.get("duration_s"), (int, float))]
    total_agent_duration = sum(dur_values) if dur_values else None
    agent_version = next((t["agent_version"] for t in trials
                          if t.get("agent_version")), None)

    # Wall-clock duration from the Harbor job's result.json timestamps.
    wall_clock_s = None
    result_file = job_dir / "result.json"
    if result_file.exists():
        try:
            job_result = json.loads(result_file.read_text())
            # fromisoformat (via _timing_duration) accepts timestamps
            # with or without fractional seconds.
            wall_clock_s = _timing_duration(job_result)
        except Exception:
            pass

    return {
        "job_dir": str(job_dir),
        "trials": trials,
        "mean_reward": mean_reward,
        "n_completed": len(trials),
        "n_errored": sum(1 for t in trials if t["errored"]),
        "infra_errors": infra_errors,
        "n_infra_errors": len(infra_errors),
        "trial_errors": trial_errors,
        "n_trial_errors": len(trial_errors),
        "unjudged_steps": unjudged_steps,
        "n_unjudged_steps": len(unjudged_steps),
        "aggregated": aggregated,
        "cost_usd": total_cost,
        "token_usage": token_usage or None,
        "per_model_usage": per_model_usage or None,
        "num_turns": total_turns,
        "duration_s": wall_clock_s or total_agent_duration,
        "agent_duration_s": total_agent_duration,
        "agent_version": agent_version,
    }
