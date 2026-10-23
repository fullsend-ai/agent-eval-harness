"""Tests for parsing a Harbor job directory (agent_eval/harbor/results.py)."""

import json
from pathlib import Path

from agent_eval.harbor import results as R


def _make_trial(job: Path, name: str, reward: float, metrics: dict,
                per_judge: dict | None = None, errored: bool = False):
    tdir = job / name
    (tdir / "verifier").mkdir(parents=True)
    (tdir / "verifier" / "reward.json").write_text(
        json.dumps({"reward": reward, **metrics}))
    if per_judge is not None:
        (tdir / "verifier" / "judges.json").write_text(
            json.dumps({"reward": reward, "per_judge": per_judge}))
    if errored:
        (tdir / "exception.txt").write_text("boom")
    return tdir


def test_parse_trial_strips_id_and_reads_metrics(tmp_path):
    _make_trial(tmp_path, "case-001-foo__abc123", 0.75,
                {"files_exist": 1.0, "rfe_quality": 4.0},
                per_judge={"files_exist": {"value": True, "rationale": "ok"}})
    trial = R.parse_trial(tmp_path / "case-001-foo__abc123")
    assert trial["case_id"] == "case-001-foo"
    assert trial["reward"] == 0.75
    assert trial["metrics"] == {"files_exist": 1.0, "rfe_quality": 4.0}
    assert trial["per_judge"]["files_exist"]["value"] is True
    assert trial["errored"] is False


def test_parse_trial_recovers_untruncated_case_id_from_result(tmp_path):
    trial_dir = _make_trial(
        tmp_path, "case-011-a-very-long-name__abc123", 1.0, {})
    (trial_dir / "result.json").write_text(json.dumps({
        "task_name": "suite/case-011-a-very-long-name-that-harbor-truncated",
    }))

    trial = R.parse_trial(trial_dir)

    assert trial["case_id"] == (
        "case-011-a-very-long-name-that-harbor-truncated")


def test_case_id_falls_back_for_parent_segment_and_invalid_utf8(tmp_path):
    parent = _make_trial(tmp_path, "safe-case__a", 1.0, {})
    (parent / "result.json").write_text(json.dumps({"task_name": "suite/.."}))
    assert R.parse_trial(parent)["case_id"] == "safe-case"

    corrupt = _make_trial(tmp_path, "utf8-case__b", 1.0, {})
    (corrupt / "result.json").write_bytes(b"\xff\xfe")
    assert R.parse_trial(corrupt)["case_id"] == "utf8-case"


def test_parse_trial_none_without_reward(tmp_path):
    (tmp_path / "empty").mkdir()
    assert R.parse_trial(tmp_path / "empty") is None


def test_multi_step_merges_every_steps_judges_sidecar(tmp_path):
    # Earlier steps' step-scoped judges must keep their values/rationales
    # in per_judge; only the final step's engine reward wins (strategy
    # "final").
    trial = tmp_path / "trial"
    for step, judge, reward in (("s1", "judge_s1", 1.0), ("s2", "judge_s2", 0.8)):
        v = trial / "steps" / step / "verifier"
        v.mkdir(parents=True)
        (v / "reward.json").write_text(json.dumps({"reward": reward}))
        (v / "judges.json").write_text(json.dumps({
            "reward": reward,
            "per_judge": {judge: {"value": reward, "rationale": step}}}))

    parsed = R.parse_trial(trial)

    assert parsed["per_judge"]["judge_s1"]["value"] == 1.0
    assert parsed["per_judge"]["judge_s2"]["value"] == 0.8
    assert parsed["reward"] == 0.8


def test_verdict_counters_ride_the_sidecar_single_and_multi_step(tmp_path):
    # Spec 015: the in-container engine's transport tally is carried onto the
    # trial record, summed across steps of a multi-step trial.
    single = tmp_path / "single"
    (single / "verifier").mkdir(parents=True)
    (single / "verifier" / "reward.json").write_text(json.dumps({"reward": 1.0}))
    (single / "verifier" / "judges.json").write_text(json.dumps({
        "reward": 1.0, "per_judge": {},
        "verdict_transports": {"json_schema": 2, "tool": 1}, "verdict_fallbacks": 1}))
    parsed = R.parse_trial(single)
    assert parsed["verdict_transports"] == {"json_schema": 2, "tool": 1}
    assert parsed["verdict_fallbacks"] == 1

    multi = tmp_path / "multi"
    for step, n in (("s1", 1), ("s2", 2)):
        v = multi / "steps" / step / "verifier"
        v.mkdir(parents=True)
        (v / "reward.json").write_text(json.dumps({"reward": 1.0}))
        (v / "judges.json").write_text(json.dumps({
            "reward": 1.0, "per_judge": {f"j_{step}": {"value": 1.0}},
            "verdict_transports": {"json_schema": n}}))
    parsed = R.parse_trial(multi)
    assert parsed["verdict_transports"] == {"json_schema": 3}
    assert "verdict_fallbacks" not in parsed


def test_single_step_falls_back_to_result_json_for_version_and_duration(
        tmp_path):
    # Codex transcripts carry no result/system-init event, so version and
    # duration must come from Harbor's own result.json bookkeeping.
    trial = _make_trial(tmp_path, "case-001__a", 1.0, {})
    (trial / "result.json").write_text(json.dumps({
        "agent_info": {"name": "codex", "version": "0.147.0"},
        "agent_execution": {"started_at": "2026-08-13T10:00:00Z",
                            "finished_at": "2026-08-13T10:02:30Z"},
    }))

    parsed = R.parse_trial(trial)

    assert parsed["agent_version"] == "0.147.0"
    assert parsed["duration_s"] == 150.0


def test_parse_job_wall_clock_accepts_whole_second_timestamps(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    _make_trial(job, "case-001__a", 1.0, {})
    # Harbor may emit ISO timestamps with or without fractional
    # seconds; both forms must parse into the duration.
    (job / "result.json").write_text(json.dumps({
        "started_at": "2026-08-13T10:00:00Z",
        "finished_at": "2026-08-13T10:05:00Z",
    }))

    assert R.parse_job(job)["duration_s"] == 300.0


def test_parse_job_aggregates(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    _make_trial(job, "case-001__a", 1.0, {"files_exist": 1.0, "rfe_quality": 5.0})
    _make_trial(job, "case-002__b", 0.0, {"files_exist": 1.0, "rfe_quality": 3.0},
                errored=True)
    # Non-trial dirs/files are ignored.
    (job / "logs").mkdir()
    (job / "result.json").write_text("{}")

    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 2
    assert parsed["n_errored"] == 1
    assert parsed["mean_reward"] == 0.5
    assert parsed["aggregated"]["files_exist"]["mean"] == 1.0
    assert parsed["aggregated"]["rfe_quality"]["mean"] == 4.0
    case_ids = sorted(t["case_id"] for t in parsed["trials"])
    assert case_ids == ["case-001", "case-002"]


# ---------------------------------------------------------------------------
# Multi-step: distinguish a missing verifier reward (infra/exec failure) from a
# genuine score of 0. A missing reward.json must NOT be counted as 0.
# ---------------------------------------------------------------------------

def _make_step(trial_dir: Path, step: str, reward: float | None = None,
               *, unjudged: bool = False):
    sdir = trial_dir / "steps" / step
    (sdir / "verifier").mkdir(parents=True)
    if reward is not None:
        payload = {"reward": reward}
        if unjudged:
            payload["agent_eval_unjudged"] = 1
        (sdir / "verifier" / "reward.json").write_text(json.dumps(payload))
    return sdir


def test_multistep_missing_reward_is_infra_not_zero(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    trial = job / "case-015__x"
    trial.mkdir()
    _make_step(trial, "create")  # no reward.json -> verifier never ran

    parsed = R.parse_job(job)
    t = parsed["trials"][0]
    assert t["per_judge"]["create"]["value"] is None        # not False/0
    assert t["per_judge"]["create"]["error"] == "no_verifier_reward"
    assert t["infra_error_steps"] == ["create"]
    assert t["reward"] is None                               # no step scored
    # Excluded from judge aggregation entirely (not a 0).
    assert "create" not in parsed["aggregated"]
    assert parsed["n_infra_errors"] == 1
    assert parsed["infra_errors"] == [("case-015", "create")]


def test_multistep_genuine_zero_is_counted(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    trial = job / "case-001__a"
    trial.mkdir()
    _make_step(trial, "create", reward=0.0)   # ran, scored 0
    _make_step(trial, "submit", reward=1.0)

    parsed = R.parse_job(job)
    t = parsed["trials"][0]
    assert t["per_judge"]["create"]["value"] == 0.0
    assert "error" not in t["per_judge"]["create"]
    assert t["infra_error_steps"] == []
    assert parsed["aggregated"]["create"]["mean"] == 0.0     # genuine 0 counts
    assert t["reward"] == 0.5
    assert parsed["n_infra_errors"] == 0


def test_multistep_unjudged_placeholder_maps_to_none_not_pass_or_infra(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    trial = job / "case-001__a"
    trial.mkdir()
    _make_step(trial, "setup", reward=0.0, unjudged=True)
    _make_step(trial, "finish", reward=1.0)

    parsed = R.parse_job(job)
    record = parsed["trials"][0]
    assert record["per_judge"]["setup"]["value"] is None
    assert record["per_judge"]["setup"]["error"] == "unjudged"
    assert record["unjudged_steps"] == ["setup"]
    assert record["infra_error_steps"] == []
    assert "setup" not in parsed["aggregated"]
    assert record["reward"] == 1.0
    # Aggregated for the run, like infra errors — a per-trial field nothing
    # reads cannot tell an operator that a step went unscored.
    assert parsed["unjudged_steps"] == [("case-001", "setup")]
    assert parsed["n_unjudged_steps"] == 1


def test_single_step_unjudged_task_is_aggregated(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    tdir = job / "case-001__a"
    (tdir / "verifier").mkdir(parents=True)
    (tdir / "verifier" / "reward.json").write_text(
        json.dumps({"reward": 0.0, "agent_eval_unjudged": 1}))

    parsed = R.parse_job(job)
    assert parsed["trials"][0]["reward"] is None
    assert parsed["mean_reward"] is None
    assert parsed["n_unjudged_steps"] == 1
    # No steps/ dir means there is no step id to report; the placeholder must
    # read as the task rather than invent a step that never existed.
    assert parsed["unjudged_steps"] == [("case-001", "(whole task)")]


def test_multistep_infra_excluded_from_step_mean(tmp_path):
    # The real scenario: one case's create verifier ran (1.0), another's didn't.
    # The create mean must be 1.0 (over the one that ran), not 0.5.
    job = tmp_path / "job"
    job.mkdir()
    a = job / "case-001__a"
    a.mkdir()
    _make_step(a, "create", reward=1.0)
    b = job / "case-015__b"
    b.mkdir()
    _make_step(b, "create")  # infra failure

    parsed = R.parse_job(job)
    assert parsed["aggregated"]["create"]["values"] == [1.0]
    assert parsed["aggregated"]["create"]["mean"] == 1.0
    assert parsed["n_infra_errors"] == 1


def test_multistep_falls_back_to_harbor_agent_results(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    trial = job / "case-001__a"
    trial.mkdir()
    _make_step(trial, "analyze", reward=1.0)
    (trial / "result.json").write_text(json.dumps({
        "task_name": "suite/case-001",
        "agent_info": {"version": "codex-cli 0.147.0"},
        "step_results": [{
            "step_name": "analyze",
            "agent_result": {
                "n_input_tokens": 100, "n_cache_tokens": 80,
                "n_output_tokens": 5, "cost_usd": 0.25,
            },
            "agent_execution": {
                "started_at": "2026-08-13T10:00:00Z",
                "finished_at": "2026-08-13T10:00:03Z",
            },
        }],
    }))

    parsed = R.parse_trial(trial)

    assert parsed["cost_usd"] == 0.25
    assert parsed["token_usage"] == {
        "input": 20, "output": 5, "cache_read": 80}
    assert parsed["duration_s"] == 3.0
    assert parsed["agent_version"] == "codex-cli 0.147.0"


# ---------------------------------------------------------------------------
# Trial that failed before producing any reward (e.g. pod never Ready) must be
# surfaced as an errored trial, not silently dropped from the case total.
# ---------------------------------------------------------------------------

def test_trial_failed_before_reward_is_surfaced(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    # A healthy single-step trial.
    _make_trial(job, "case-001__a", 1.0, {"files_exist": 1.0})
    # A trial that never produced steps/ or reward.json but has exception.txt.
    bad = job / "case-013__b"
    bad.mkdir()
    (bad / "exception.txt").write_text("pod aeh-case-013-... not Ready after 300s\n")

    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 2                      # not dropped
    assert parsed["n_trial_errors"] == 1
    assert parsed["trial_errors"][0][0] == "case-013"
    assert "not Ready" in parsed["trial_errors"][0][1]
    bad_trial = next(t for t in parsed["trials"] if t["case_id"] == "case-013")
    assert bad_trial["errored"] is True
    assert bad_trial["reward"] is None
    assert parsed["mean_reward"] == 1.0                    # errored trial excluded


def test_trial_with_no_reward_and_no_exception_is_dropped(tmp_path):
    # Without an exception.txt there's nothing to surface — keep returning None.
    job = tmp_path / "job"
    job.mkdir()
    _make_trial(job, "case-001__a", 1.0, {})
    (job / "case-002__b").mkdir()  # empty, no reward, no exception
    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 1
    assert parsed["n_trial_errors"] == 0


def test_single_step_unreadable_reward_with_exception_is_surfaced(tmp_path):
    # reward.json present but corrupt + exception.txt -> errored trial, not dropped.
    job = tmp_path / "job"
    job.mkdir()
    bad = job / "case-099__z"
    (bad / "verifier").mkdir(parents=True)
    (bad / "verifier" / "reward.json").write_text("{ truncated")  # invalid JSON
    (bad / "exception.txt").write_text("RuntimeError: boom\n")
    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 1
    assert parsed["n_trial_errors"] == 1
    assert parsed["trial_errors"][0] == ("case-099", "RuntimeError: boom")
    t = parsed["trials"][0]
    assert t["errored"] is True and t["reward"] is None


def test_single_step_unreadable_reward_without_exception_is_dropped(tmp_path):
    # Corrupt reward.json but no exception.txt -> still nothing to surface.
    job = tmp_path / "job"
    job.mkdir()
    bad = job / "case-099__z"
    (bad / "verifier").mkdir(parents=True)
    (bad / "verifier" / "reward.json").write_text("{ truncated")
    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 0
    assert parsed["n_trial_errors"] == 0


def test_trial_error_reason_is_sanitized(tmp_path):
    # exception.txt is untrusted: control chars / ANSI / newlines must be escaped
    # and the reason bounded before it reaches run_result.json or CI logs.
    job = tmp_path / "job"
    job.mkdir()
    bad = job / "case-007__z"
    bad.mkdir()
    (bad / "exception.txt").write_text("RuntimeError: \x1b[31mboom\x1b[0m\twith\ttabs\n")
    parsed = R.parse_job(job)
    reason = parsed["trial_errors"][0][1]
    assert "\x1b" not in reason and "\t" not in reason   # raw control chars gone
    assert "\\x1b" in reason                              # escaped form retained
    assert len(reason) <= 200


def test_merge_per_model_accumulates():
    acc: dict = {}
    R._merge_per_model(acc, {"m1": {"input": 10, "output": 5, "cost_usd": 0.1}})
    R._merge_per_model(acc, {"m1": {"input": 3, "output": 2, "cost_usd": 0.05},
                             "m2": {"input": 7, "output": 1, "cost_usd": None}})
    R._merge_per_model(acc, None)  # None-safe no-op
    assert acc["m1"]["input"] == 13
    assert acc["m1"]["output"] == 7
    assert acc["m1"]["cache_read"] == 0 and acc["m1"]["cache_create"] == 0
    assert abs(acc["m1"]["cost_usd"] - 0.15) < 1e-9
    assert acc["m2"] == {"input": 7, "output": 1, "cache_read": 0,
                         "cache_create": 0, "cost_usd": None}


def test_extract_transcript_metrics_per_model(tmp_path):
    tp = tmp_path / "transcript.jsonl"
    result_ev = {
        "type": "result", "total_cost_usd": 0.42, "num_turns": 3,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "modelUsage": {
            "z-ai/glm-5.2": {
                "inputTokens": 100, "outputTokens": 50,
                "cacheReadInputTokens": 10, "cacheCreationInputTokens": 0,
                "costUSD": 0.42,
            },
        },
    }
    tp.write_text(json.dumps(result_ev) + "\n")
    m = R._extract_transcript_metrics(tp)
    assert m["per_model_usage"] == {
        "z-ai/glm-5.2": {"input": 100, "output": 50, "cache_read": 10,
                         "cache_create": 0, "cost_usd": 0.42},
    }


def test_parse_trial_extracts_codex_transcript_metrics(tmp_path):
    trial = _make_trial(tmp_path, "case-codex__a", 1.0, {})
    agent_dir = trial / "agent"
    agent_dir.mkdir()
    (agent_dir / "codex.txt").write_text(json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 4,
            "cached_input_tokens": 3,
        },
    }) + "\n")

    parsed = R.parse_trial(trial)
    assert parsed["token_usage"] == {
        "input": 9, "output": 4, "cache_read": 3}
    assert parsed["num_turns"] == 1


def test_parse_trial_rejects_malformed_harbor_agent_metrics(tmp_path):
    trial = _make_trial(tmp_path, "case-malformed__a", 1.0, {})
    (trial / "result.json").write_text(json.dumps({
        "agent_result": {
            "cost_usd": "abc", "n_input_tokens": "many",
            "n_cache_tokens": False, "n_output_tokens": None,
        },
    }))

    parsed = R.parse_trial(trial)

    assert parsed["cost_usd"] is None
    assert parsed["token_usage"] is None


def test_malformed_transcript_values_degrade_to_missing_metrics(tmp_path):
    # Transcript content is agent-influenced; one malformed line must not
    # crash the mapping of a completed Harbor run.
    tp = tmp_path / "codex.txt"
    tp.write_text("\n".join([
        json.dumps({"type": "turn.completed", "usage": "oops"}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": "abc", "output_tokens": 4}}),
        json.dumps({"type": "result", "usage": "oops",
                    "total_cost_usd": "abc", "duration_ms": "xyz"}),
        json.dumps(["not", "an", "object"]),
    ]) + "\n")

    metrics = R._extract_transcript_metrics(tp)
    assert metrics["cost_usd"] is None
    assert metrics["duration_s"] is None
    assert metrics["token_usage"] == {"input": 0, "output": 4, "cache_read": 0}
    assert metrics["num_turns"] == 2


def test_malformed_result_event_fields_degrade_to_missing_metrics(tmp_path):
    # A dict-valued result event stores its fields directly; malformed
    # values must come out as None (or drop), never as strings, and a
    # bool must not pass an isinstance-int guard.
    tp = tmp_path / "claude-code.txt"
    tp.write_text(json.dumps({
        "type": "result", "num_turns": "two", "total_cost_usd": True,
        "duration_ms": "xyz",
        "usage": {"input_tokens": "abc", "output_tokens": 7,
                  "cache_read_input_tokens": None},
        "modelUsage": {
            "claude": {"inputTokens": "abc", "outputTokens": 3,
                       "costUSD": "oops"},
            "bad": "not-a-dict",
        },
    }) + "\n")

    metrics = R._extract_transcript_metrics(tp)

    assert metrics["num_turns"] is None
    assert metrics["cost_usd"] is None
    assert metrics["duration_s"] is None
    assert metrics["token_usage"] == {
        "input": None, "output": 7, "cache_read": None, "cache_create": None}
    assert metrics["per_model_usage"] == {
        "claude": {"input": 0, "output": 3, "cache_read": 0,
                   "cache_create": 0, "cost_usd": None}}


def test_parse_trial_collects_assistant_message_ids(tmp_path):
    """The trial transcript's assistant message ids are the cost-truth key set
    for Harbor runs (spec 014); duplicates (one per content block) collapse."""
    job = tmp_path / "job"
    tdir = job / "trial__case-1"
    (tdir / "verifier").mkdir(parents=True)
    (tdir / "verifier" / "reward.json").write_text(json.dumps({"reward": 1.0, "metrics": {}}))
    (tdir / "agent").mkdir()
    (tdir / "agent" / "claude-code.txt").write_text("\n".join([
        json.dumps({"type": "system", "subtype": "init", "claude_code_version": "2.1.280"}),
        json.dumps({"type": "assistant", "message": {"id": "gen-1", "model": "z-ai/glm-5.2"}}),
        json.dumps({"type": "assistant", "message": {"id": "gen-1", "model": "z-ai/glm-5.2"}}),
        json.dumps({"type": "assistant", "message": {"id": "gen-2", "model": "z-ai/glm-5.2"}}),
        json.dumps({"type": "result", "total_cost_usd": 1.2, "num_turns": 2, "duration_ms": 1000,
                    "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ]))
    record = R.parse_trial(tdir)
    assert record is not None
    assert record["message_ids"] == ["gen-1", "gen-2"]
    assert record["cost_usd"] == 1.2


def test_multi_step_trial_unions_message_ids_and_errored_trials_carry_the_field(tmp_path):
    job = tmp_path / "job"
    tdir = job / "trial__case-2"
    for step, ids in (("create", ["gen-a", "gen-b"]), ("assess", ["gen-b", "gen-c"])):
        sdir = tdir / "steps" / step
        (sdir / "verifier").mkdir(parents=True)
        (sdir / "verifier" / "reward.json").write_text(json.dumps({"reward": 1.0}))
        (sdir / "agent").mkdir()
        (sdir / "agent" / "claude-code.txt").write_text("\n".join(
            json.dumps({"type": "assistant", "message": {"id": i}}) for i in ids) + "\n")
    record = R.parse_trial(tdir)
    assert record is not None and sorted(record["message_ids"]) == ["gen-a", "gen-b", "gen-c"]
    assert len(record["message_ids"]) == 3                     # deduplicated across steps
    errored = job / "trial__case-3"
    errored.mkdir()
    (errored / "exception.txt").write_text("RuntimeError: pod not ready\n")
    assert R.parse_trial(errored)["message_ids"] == []


def test_price_trials_joins_each_trial_against_its_own_ids():
    rows = [
        {"source": "generation", "status": "ok", "gen_id": "gen-a1", "cost_usd": 0.01},
        {"source": "generation", "status": "ok", "gen_id": "gen-a2", "cost_usd": 0.02},
        {"source": "generation", "status": "backfill_failed", "gen_id": "gen-b1", "cost_usd": None},
    ]
    trials = [
        {"case_id": "c", "trial_dir": "c__1", "cost_usd": 0.9, "message_ids": ["gen-a1", "gen-a2"]},
        {"case_id": "c", "trial_dir": "c__2", "cost_usd": 0.8, "message_ids": ["gen-b1"]},
        {"case_id": "d", "trial_dir": "d__1", "cost_usd": 0.7, "message_ids": []},
    ]
    R.price_trials(trials, rows)
    assert trials[0]["cost_usd"] == 0.03 and trials[0]["cost_source"] == "openrouter:generation"
    assert trials[0]["cost_usd_estimate"] == 0.9
    assert trials[1]["cost_usd"] is None and trials[1]["cost_source"] == "unavailable"
    assert trials[1]["cost_usd_estimate"] == 0.8 and trials[1]["cost_coverage"] == {"requests": 1, "requests_priced": 0}
    assert trials[2]["cost_usd"] is None and trials[2]["cost_usd_estimate"] == 0.7
    view = R.trial_costs(trials)
    assert [v["trial_dir"] for v in view] == ["c__1", "c__2", "d__1"] and "message_ids" not in view[0]

