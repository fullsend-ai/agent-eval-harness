"""Pairwise comparison routes on the judge model's provider (cross-provider).

Regression coverage for the follow-up to the Cursor runner PR: the pairwise
path must strip a ``provider:/`` prefix and dispatch to the OpenAI backend, not
hand the raw prefixed id to the Anthropic Messages API.
"""

import json
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import score  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_verdict_memory():
    """Spec 015 keeps a per-run memory of the verdict transport that worked for
    a (backend, model); one test must never inherit another's."""
    import score as _score
    _score._reset_verdict_state()
    yield
    _score._reset_verdict_state()


def _openai_client(*, arguments=None, content=None, finish_reason="stop"):
    def create(**kwargs):
        create.captured = kwargs
        tool_calls = None
        if arguments is not None:
            call = SimpleNamespace(function=SimpleNamespace(
                name="submit_comparison", arguments=json.dumps(arguments)))
            tool_calls = [call]
        message = SimpleNamespace(tool_calls=tool_calls, content=content)
        choice = SimpleNamespace(message=message, finish_reason=finish_reason)
        return SimpleNamespace(choices=[choice])

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


def test_call_pairwise_openai_reads_tool_call():
    client = _openai_client(arguments={"reasoning": "A wins", "preferred": "A"})
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o",
                                               verdict="tool")
    assert err is None and verdict["preferred"] == "A"


def test_call_pairwise_openai_text_fallback():
    client = _openai_client(content='{"reasoning": "r", "preferred": "tie"}')
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o")
    assert err is None and verdict["preferred"] == "tie"


def test_call_judge_dispatches_to_openai_backend():
    with patch("score._call_pairwise_openai",
               return_value=({"preferred": "B"}, None)) as mocked:
        verdict, err = score._call_judge("openai", object(), "sys", "msg", "gpt-4o")
    assert verdict["preferred"] == "B" and err is None
    mocked.assert_called_once()


def test_compare_runs_rejects_runner_model():
    config = EvalConfig(name="t", skill="s")
    result = score.compare_runs(Path("a"), Path("b"), config, [],
                                model="runner:/gpt-5.4-medium")
    assert "error" in result and "runner" in result["error"].lower()


def _run_one_comparison(model, *, openai_raises=False, anthropic_raises=False):
    """Drive compare_runs over a single stubbed case and capture the backend +
    stripped model handed to _call_judge, plus which client getter was used."""
    config = EvalConfig(name="t", skill="s")
    captured = {}

    def fake_call_judge(backend, client, system_prompt, user_message, m,
                        max_tokens=16384, **kw):
        captured["backend"] = backend
        captured["model"] = m
        return {"reasoning": "r", "preferred": "A"}, None

    oai = (AssertionError("must not use OpenAI") if openai_raises
           else None)
    anth = (AssertionError("must not use Anthropic") if anthropic_raises
            else None)
    with patch("score.load_case_record", return_value={"files": {}}), \
            patch("score._format_outputs_for_pairwise", return_value="output"), \
            patch("score._call_judge", side_effect=fake_call_judge), \
            patch("score._get_openai_client",
                  **({"side_effect": oai} if oai else {"return_value": object()})), \
            patch("score._get_anthropic_client",
                  **({"side_effect": anth} if anth else {"return_value": object()})):
        result = score.compare_runs(Path("a"), Path("b"), config, ["case-1"],
                                    model=model)
    return result, captured


def test_compare_runs_openai_backend_strips_prefix():
    result, captured = _run_one_comparison("openai:/gpt-4o", anthropic_raises=True)
    assert "error" not in result
    assert captured["backend"] == "openai" and captured["model"] == "gpt-4o"


def test_compare_runs_anthropic_backend_strips_prefix():
    result, captured = _run_one_comparison("anthropic:/claude-sonnet-4-5",
                                           openai_raises=True)
    assert "error" not in result
    assert captured["backend"] == "anthropic"
    assert captured["model"] == "claude-sonnet-4-5"


# --- openrouter:/ pairwise judges (spec 014) ---------------------------------

from agent_eval.config import (  # noqa: E402
    JudgeClientOptions, ModelsConfig, OpenRouterConfig, ProvidersConfig, RoutingConfig)
from agent_eval.providers.openrouter import RoutingTable  # noqa: E402


def _openrouter_config(inherit_pins=True):
    table = RoutingTable.from_dict(
        {"models": {"z-ai/glm-5.2": {"order": ["z-ai"], "allow_fallbacks": False}}})
    config = EvalConfig(name="t", skill="s")
    config.models = ModelsConfig(providers=ProvidersConfig(openrouter=OpenRouterConfig(
        routing=RoutingConfig(defaults=table.defaults, models=table.models),
        judge=JudgeClientOptions(inherit_pins=inherit_pins))))
    return config


def _raw_client(*script):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        step = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    client.calls = calls
    return client


def _pw_response(tool_name="submit_comparison", arguments=None, content=None,
                 finish_reason="stop", error=None):
    tool_calls = None
    if tool_name is not None:
        tool_calls = [SimpleNamespace(function=SimpleNamespace(
            name=tool_name, arguments=arguments))]
    message = SimpleNamespace(tool_calls=tool_calls, content=content)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])
    if error is not None:
        response.error = error
    return response


class _NotFound(Exception):
    status_code = 404
    message = "No endpoints found for z-ai/glm-5.2 that support tool use."


def test_compare_runs_openrouter_uses_the_dedicated_client_and_extra_body():
    config = _openrouter_config()
    captured = {}
    sentinel = object()

    def fake_call_judge(backend, client, system_prompt, user_message, m,
                        max_tokens=16384, **kw):
        captured.update(backend=backend, client=client, model=m,
                        max_tokens=max_tokens, **kw)
        return {"reasoning": "r", "preferred": "A"}, None

    def must_not(*a, **k):
        raise AssertionError("wrong client getter used")

    with patch("score.load_case_record", return_value={"files": {}}), \
            patch("score._format_outputs_for_pairwise", return_value="output"), \
            patch("score._call_judge", side_effect=fake_call_judge), \
            patch("score._client_for", return_value=sentinel) as client_for, \
            patch("score._get_openai_client", side_effect=must_not), \
            patch("score._get_anthropic_client", side_effect=must_not):
        result = score.compare_runs(Path("a"), Path("b"), config, ["case-1"],
                                    model="openrouter:/z-ai/glm-5.2",
                                    provider_options={"max_tokens": 4096})

    assert result.get("error") is None
    assert captured["backend"] == "openai" and captured["client"] is sentinel
    assert captured["model"] == "z-ai/glm-5.2"
    assert captured["client_cfg"].name == "openrouter"
    assert captured["token_param"] == "max_tokens"
    assert captured["max_tokens"] == 4096
    assert captured["extra_body"]["provider"]["order"] == ["z-ai"]
    assert captured["extra_body"]["provider"]["require_parameters"] is True
    assert client_for.call_args.args[0].name == "openrouter"


def test_compare_runs_plain_openai_model_passes_no_binding():
    """Existing openai:/ pairwise judges keep the bare call signature."""
    captured = {}

    def fake_call_judge(backend, client, system_prompt, user_message, m,
                        max_tokens=16384, **kw):
        captured.update(kw)
        return {"reasoning": "r", "preferred": "A"}, None

    with patch("score.load_case_record", return_value={"files": {}}), \
            patch("score._format_outputs_for_pairwise", return_value="output"), \
            patch("score._call_judge", side_effect=fake_call_judge), \
            patch("score._get_openai_client", return_value=object()), \
            patch("score._client_for", side_effect=AssertionError("no cfg expected")):
        score.compare_runs(Path("a"), Path("b"), EvalConfig(name="t", skill="s"),
                           ["case-1"], model="openai:/gpt-4o")
    assert captured == {"verdict": "auto"}


def test_call_pairwise_openai_accepts_dict_arguments():
    client = _raw_client(_pw_response(arguments={"reasoning": "A wins", "preferred": "A"}))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2",
                                               verdict="tool")
    assert err is None and verdict["preferred"] == "A"


def test_call_pairwise_openai_reports_a_committed_200_error():
    client = _raw_client(_pw_response(
        tool_name=None, content="", finish_reason="error",
        error={"code": 502, "message": "Provider returned error",
               "metadata": {"provider_name": "Novita"}}))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2")
    assert verdict is None and "Novita" in err and "200" in err


def test_call_pairwise_openai_ladder_strict_parses_the_fallback():
    client = _raw_client(_NotFound(), _pw_response(
        tool_name="other", arguments="{}", content='{"preferred": "A"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2",
                                               verdict="tool")
    assert verdict is None and "instead of" in err
    assert client.calls[1]["tool_choice"] == "required"


def test_call_pairwise_openai_passes_extra_body_and_token_param():
    client = _raw_client(_pw_response(arguments={"reasoning": "r", "preferred": "tie"}))
    verdict, err = score._call_pairwise_openai(
        client, "compare", "A vs B", "openai/gpt-5.2", verdict="tool",
        extra_body={"provider": {"sort": "price"}}, token_param="max_tokens")
    assert err is None and verdict["preferred"] == "tie"
    sent = client.calls[0]
    assert sent["extra_body"] == {"provider": {"sort": "price"}}
    assert "max_tokens" in sent and "max_completion_tokens" not in sent


# --- pairwise judge usage (spec 014 PR-2) ------------------------------------

def _priced_pw_response(arguments, cost=None, model="z-ai/glm-5.2"):
    response = _pw_response(arguments=arguments)
    response.id = "gen-pw"
    response.model = model
    response.usage = SimpleNamespace(prompt_tokens=50, completion_tokens=5,
                                     **({"cost": cost} if cost is not None else {}))
    return response


def test_call_pairwise_openai_attaches_the_usage_record():
    client = _raw_client(_priced_pw_response({"reasoning": "r", "preferred": "A"}, cost=0.002))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2",
                                               verdict="tool")
    assert err is None and verdict["preferred"] == "A"
    assert verdict["_usage"]["cost_usd"] == 0.002
    assert verdict["_usage"]["cost_source"] == "provider-inline"
    assert verdict["_usage"]["prompt_tokens"] == 50


def test_call_judge_anthropic_attaches_the_usage_record():
    block = SimpleNamespace(type="tool_use", name="submit_comparison",
                            input={"reasoning": "r", "preferred": "B"})
    response = SimpleNamespace(id="msg_pw", model="claude-opus-4-8", content=[block],
                               stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=30, output_tokens=3))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))
    verdict, err = score._call_judge("anthropic", client, "sys", "msg", "claude-opus-4-8",
                                     verdict="tool")
    assert err is None and verdict["preferred"] == "B"
    assert verdict["_usage"]["provider"] == "anthropic"
    assert verdict["_usage"]["prompt_tokens"] == 30 and verdict["_usage"]["cost_usd"] is None


def test_compare_runs_aggregates_pairwise_usage_and_keeps_reasoning_clean():
    calls = []

    def fake_call_judge(backend, client, system_prompt, user_message, m,
                        max_tokens=16384, **kw):
        calls.append(1)
        return {"reasoning": "r", "preferred": "A",
                "_usage": {"model": m, "provider": "Z.AI", "id": f"gen-{len(calls)}",
                           "prompt_tokens": 10, "completion_tokens": 1,
                           "reasoning_tokens": None, "cost_usd": 0.01,
                           "cost_source": "provider-inline"}}, None

    with patch("score.load_case_record", return_value={"files": {}}), \
            patch("score._format_outputs_for_pairwise", return_value="output"), \
            patch("score._call_judge", side_effect=fake_call_judge), \
            patch("score._get_openai_client", return_value=object()):
        result = score.compare_runs(Path("a"), Path("b"), EvalConfig(name="t", skill="s"),
                                    ["case-1", "case-2"], model="openai:/gpt-4o")

    assert result["cases_compared"] == 2 and result["errors"] == 0
    usage = result["judge_usage"]
    assert usage["requests"] == 4                      # two positions × two cases
    assert usage["judge_cost_usd"] == pytest.approx(0.04)
    assert usage["requests_missing_cost"] == 0
    assert usage["prompt_tokens"] == 40
    # The stored reasoning is the judge's own field; the usage record was
    # moved off the verdict before it was kept.
    assert [case["reasoning"] for case in result["per_case"]] == ["r", "r"]


def test_take_pairwise_usage_moves_the_record_off_the_verdict():
    verdict = {"preferred": "A", "_usage": {"cost_usd": 0.5}}
    result = score.PairwiseResult(case_id="c")
    score._take_pairwise_usage(verdict, result)
    assert verdict == {"preferred": "A"} and result.usage == [{"cost_usd": 0.5}]
    score._take_pairwise_usage(None, result)          # a failed call adds nothing
    assert len(result.usage) == 1


def test_compare_runs_without_usage_has_no_judge_usage_block():
    with patch("score.load_case_record", return_value={"files": {}}), \
            patch("score._format_outputs_for_pairwise", return_value="output"), \
            patch("score._call_judge",
                  return_value=({"reasoning": "r", "preferred": "tie"}, None)), \
            patch("score._get_openai_client", return_value=object()):
        result = score.compare_runs(Path("a"), Path("b"), EvalConfig(name="t", skill="s"),
                                    ["case-1"], model="openai:/gpt-4o")
    assert "judge_usage" not in result


# --- verdict transport (spec 015) --------------------------------------------

def test_pairwise_openai_structured_output_is_the_default_first_rung():
    client = _raw_client(_pw_response(tool_name=None,
                                      content='{"reasoning": "A is tighter", "preferred": "A"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "openai/gpt-5.2")
    assert err is None and verdict["preferred"] == "A" and verdict["reasoning"] == "A is tighter"
    sent = client.calls[0]
    assert sent["response_format"]["json_schema"]["name"] == "submit_comparison"
    assert "tools" not in sent and "JSON object" in sent["messages"][0]["content"]


def test_pairwise_openai_falls_back_to_the_tool_ladder_on_a_response_format_refusal():
    class _Unsupported(Exception):
        status_code = 400
        message = "response_format is not supported by this endpoint"

    client = _raw_client(_Unsupported(), _pw_response(arguments={"reasoning": "r", "preferred": "B"}))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2")
    assert err is None and verdict["preferred"] == "B"
    assert client.calls[1]["tool_choice"]["type"] == "function"


def test_pairwise_openai_doubles_the_budget_when_structured_output_is_cut_off():
    client = _raw_client(
        _pw_response(tool_name=None, content='{"reasoning": "long', finish_reason="length"),
        _pw_response(tool_name=None, content='{"reasoning": "r", "preferred": "tie"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o",
                                               max_tokens=1024)
    assert err is None and verdict["preferred"] == "tie"
    assert client.calls[1]["max_tokens"] == 2048 and "response_format" in client.calls[1]


def test_pairwise_anthropic_structured_output_and_fallback():
    calls = []
    text_block = SimpleNamespace(type="text", text='{"reasoning": "r", "preferred": "B"}')
    reply = SimpleNamespace(id="m", model="claude-opus-5-5", content=[text_block],
                            stop_reason="end_turn",
                            usage=SimpleNamespace(input_tokens=30, output_tokens=3))

    def create(**kw):
        calls.append(kw)
        return reply

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    verdict, err = score._call_judge("anthropic", client, "sys", "msg", "claude-opus-5-5")
    assert err is None and verdict["preferred"] == "B"
    assert verdict["_usage"]["prompt_tokens"] == 30
    assert "output_config" in calls[0] and "tools" not in calls[0]
    assert "JSON object" in calls[0]["system"]
    # the explicit tool policy keeps the forced tool call
    block = SimpleNamespace(type="tool_use", name="submit_comparison",
                            input={"reasoning": "r", "preferred": "A"})
    reply.content = [block]
    verdict, err = score._call_judge("anthropic", client, "sys", "msg", "claude-opus-5-5",
                                     verdict="tool")
    assert err is None and verdict["preferred"] == "A"
    assert calls[1]["tool_choice"] == {"type": "tool", "name": "submit_comparison"}


def test_cmd_pairwise_passes_the_selected_judges_verdict_policy(tmp_path, monkeypatch):
    """`score.py pairwise --judge <name>` hands the judge's own `verdict:` to
    compare_runs; without one, compare_runs falls back to models.judge_verdict."""
    import yaml
    from types import SimpleNamespace

    (tmp_path / "eval.yaml").write_text(yaml.safe_dump({
        "name": "t", "execution": {"skill": "s"},
        "models": {"judge": "claude-opus-5-5"},
        "judges": [{"name": "pairwise", "prompt": "compare", "verdict": "tool"},
                   {"name": "other", "prompt": "compare too"}]}))
    runs_dir = tmp_path / "runs"
    for run in ("run-a", "run-b"):
        (runs_dir / run / "cases" / "case-1").mkdir(parents=True)
    captured = {}

    def fake_compare(*args, **kw):
        captured.update(kw)
        return {"wins_a": 1, "wins_b": 0, "ties": 0, "errors": 0, "per_case": []}

    monkeypatch.setattr(score, "_get_runs_dir", lambda name="": runs_dir)
    monkeypatch.setattr(score, "compare_runs", fake_compare)
    monkeypatch.setattr(score, "_merge_summary", lambda *a, **k: None)
    args = SimpleNamespace(config=str(tmp_path / "eval.yaml"), run_id="run-a", baseline="run-b",
                           judge="pairwise", model=None, prompt_file=None, samples=None)
    score.cmd_pairwise(args)
    assert captured["verdict"] == "tool"

    args.judge = "other"
    score.cmd_pairwise(args)
    assert captured["verdict"] is None


def _anthropic_scripted(*script):
    calls = []

    def create(**kw):
        calls.append(kw)
        step = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    client.calls = calls
    return client


class _Refusal(Exception):
    status_code = 400

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _anthropic_reply(content, stop_reason="end_turn"):
    return SimpleNamespace(id="m", model="claude-opus-5-5", content=content,
                           stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def test_pairwise_anthropic_auto_rung_rejects_prose_and_invalid_tool_calls():
    """Under `tool_choice: auto` (a model that rejects forcing) a reply without
    a valid submit_comparison call is an error — prose is never parsed."""
    refusals = (_Refusal("output_config: Extra inputs are not permitted"),
                _Refusal('tool_choice: type "tool" and "any" are not supported for this model.'))
    prose = _anthropic_reply([SimpleNamespace(
        type="text", text='{"reasoning": "r", "preferred": "A"}')])
    verdict, err = score._call_judge("anthropic", _anthropic_scripted(*refusals, prose),
                                     "sys", "msg", "claude-opus-5-5")
    assert verdict is None and "No submit_comparison tool_use" in err
    score._reset_verdict_state()
    invalid = _anthropic_reply([SimpleNamespace(
        type="tool_use", name="submit_comparison", input={"reasoning": "r", "preferred": "maybe"})])
    verdict, err = score._call_judge("anthropic", _anthropic_scripted(*refusals, invalid),
                                     "sys", "msg", "claude-opus-5-5")
    assert verdict is None and "No submit_comparison tool_use" in err
    score._reset_verdict_state()
    valid = _anthropic_reply([SimpleNamespace(
        type="tool_use", name="submit_comparison", input={"reasoning": "r", "preferred": "B"})])
    verdict, err = score._call_judge("anthropic", _anthropic_scripted(*refusals, valid),
                                     "sys", "msg", "claude-opus-5-5")
    assert err is None and verdict["preferred"] == "B"


def test_pairwise_anthropic_forced_rung_rejects_an_invalid_tool_call():
    """An invalid submit_comparison input on the forced rung is no longer
    returned as-is: it falls through to the text fallback, then to an error."""
    invalid = SimpleNamespace(type="tool_use", name="submit_comparison",
                              input={"reasoning": "r", "preferred": "maybe"})
    verdict, err = score._call_judge("anthropic",
                                     _anthropic_scripted(_anthropic_reply([invalid])),
                                     "sys", "msg", "claude-opus-5-5", verdict="tool")
    assert verdict is None and "preferred must be A, B or tie" in err
    score._reset_verdict_state()
    text = SimpleNamespace(type="text", text='{"reasoning": "r", "preferred": "B"}')
    verdict, err = score._call_judge("anthropic",
                                     _anthropic_scripted(_anthropic_reply([invalid, text])),
                                     "sys", "msg", "claude-opus-5-5", verdict="tool")
    assert err is None and verdict["preferred"] == "B"


_INVALID_PAIRWISE_ARGS = {"reasoning": "r", "preferred": "maybe"}


def test_pairwise_openai_forced_mode_rejects_an_invalid_tool_call():
    client = _raw_client(_pw_response(arguments=_INVALID_PAIRWISE_ARGS))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o",
                                               verdict="tool")
    assert client.calls[0]["tool_choice"]["type"] == "function"
    assert verdict is None and "preferred must be A, B or tie" in err
    # the forced mode falls through to the text fallback
    client = _raw_client(_pw_response(arguments=_INVALID_PAIRWISE_ARGS,
                                      content='{"reasoning": "r", "preferred": "A"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o",
                                               verdict="tool")
    assert err is None and verdict["preferred"] == "A"


@pytest.mark.parametrize("mode, steps_down", [("required", 1), ("auto", 2)])
def test_pairwise_openai_degraded_modes_reject_an_invalid_tool_call(mode, steps_down):
    client = _raw_client(*[_NotFound()] * steps_down,
                         _pw_response(arguments=_INVALID_PAIRWISE_ARGS,
                                      content='{"reasoning": "r", "preferred": "A"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2",
                                               verdict="tool")
    assert client.calls[steps_down]["tool_choice"] == mode
    # a degraded mode never falls back to the text, valid or not
    assert verdict is None and "preferred must be A, B or tie" in err


@pytest.mark.parametrize("payload", [
    '{"reasoning": "r", "preferred": "C"}',
    '{"reasoning": "r"}',
    '{"reasoning": 3, "preferred": "A"}'])
def test_pairwise_structured_output_validates_the_verdict_object(payload):
    reply = _anthropic_reply([SimpleNamespace(type="text", text=payload)])
    verdict, err = score._call_judge("anthropic", _anthropic_scripted(reply),
                                     "sys", "msg", "claude-opus-5-5")
    assert verdict is None and "does not match the submit_comparison" in err
    client = _raw_client(_pw_response(tool_name=None, content=payload))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o")
    assert verdict is None and "does not match the submit_comparison" in err


def test_structured_verdict_rejects_boolean_scores_and_missing_rationale():
    bounds = (1, 5, True)
    assert score._verdict_from_data({"rationale": "r", "score": True}, False, bounds) is None
    assert score._verdict_from_data({"rationale": "r", "score": 4}, False, bounds) == (4, "r")
    assert score._verdict_from_data({"score": 4}, False, bounds) == (4, "(no rationale provided)")
    assert score._verdict_from_data({"score": 4}, False, bounds, strict=True) is None
    assert score._verdict_from_data({"rationale": 1, "passed": True}, True, bounds,
                                    strict=True) is None


def test_pairwise_anthropic_invalid_tool_call_and_invalid_text_verdict_is_an_error():
    """The forced rung's text fallback is validated like the tool call: an
    invalid text verdict is never returned, and the error names the field."""
    invalid_call = SimpleNamespace(type="tool_use", name="submit_comparison",
                                   input=_INVALID_PAIRWISE_ARGS)
    invalid_text = SimpleNamespace(type="text", text='{"reasoning": "r", "preferred": "C"}')
    client = _anthropic_scripted(_anthropic_reply([invalid_call, invalid_text]))
    verdict, err = score._call_judge("anthropic", client, "sys", "msg", "claude-opus-5-5",
                                     verdict="tool")
    assert verdict is None and "preferred must be A, B or tie" in err
    score._reset_verdict_state()
    client = _anthropic_scripted(_anthropic_reply([invalid_text]))
    verdict, err = score._call_judge("anthropic", client, "sys", "msg", "claude-opus-5-5",
                                     verdict="tool")
    assert verdict is None and "text verdict: preferred must be A, B or tie" in err


def test_pairwise_anthropic_invalid_text_verdict_still_takes_the_truncation_retry():
    invalid_text = SimpleNamespace(type="text", text='{"reasoning": "r", "preferred": "C"}')
    valid = SimpleNamespace(type="tool_use", name="submit_comparison",
                            input={"reasoning": "r", "preferred": "B"})
    client = _anthropic_scripted(_anthropic_reply([invalid_text], stop_reason="max_tokens"),
                                 _anthropic_reply([valid]))
    verdict, err = score._call_judge("anthropic", client, "sys", "msg", "claude-opus-5-5",
                                     max_tokens=1024, verdict="tool")
    assert err is None and verdict["preferred"] == "B"
    assert client.calls[1]["max_tokens"] == 2048


def test_pairwise_openai_invalid_tool_call_and_invalid_text_verdict_is_an_error():
    client = _raw_client(_pw_response(arguments=_INVALID_PAIRWISE_ARGS,
                                      content='{"reasoning": "r", "preferred": "C"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o",
                                               verdict="tool")
    assert client.calls[0]["tool_choice"]["type"] == "function"
    assert verdict is None and "preferred must be A, B or tie" in err
    client = _raw_client(_pw_response(tool_name=None,
                                      content='{"reasoning": "r", "preferred": "C"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o",
                                               verdict="tool")
    assert verdict is None and "text verdict: preferred must be A, B or tie" in err


def test_pairwise_openai_invalid_text_verdict_still_takes_the_truncation_retry():
    client = _raw_client(
        _pw_response(tool_name=None, content='{"reasoning": "r", "preferred": "C"}',
                     finish_reason="length"),
        _pw_response(arguments='{"reasoning": "r", "preferred": "B"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o",
                                               max_tokens=1024, verdict="tool")
    assert err is None and verdict["preferred"] == "B"
    assert client.calls[1]["max_tokens"] == 2048


@pytest.mark.parametrize("text", [
    'Here is my verdict: {"reasoning": "r", "preferred": "A"}',
    '["reasoning", "preferred"]'])
def test_pairwise_structured_output_rejects_prose_around_the_object(text):
    reply = _anthropic_reply([SimpleNamespace(type="text", text=text)])
    verdict, err = score._call_judge("anthropic", _anthropic_scripted(reply),
                                     "sys", "msg", "claude-opus-5-5")
    assert verdict is None and "no JSON object under structured output" in err
    client = _raw_client(_pw_response(tool_name=None, content=text))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o")
    assert verdict is None and "no JSON object under structured output" in err


def test_pairwise_structured_output_outer_verdict_wins_over_an_inline_fence():
    reasoning = 'Quoted ```json\n{"preferred": "B"}\n``` in the output.'
    payload = json.dumps({"reasoning": reasoning, "preferred": "A"})
    reply = _anthropic_reply([SimpleNamespace(type="text", text=payload)])
    verdict, err = score._call_judge("anthropic", _anthropic_scripted(reply),
                                     "sys", "msg", "claude-opus-5-5")
    assert err is None and verdict["preferred"] == "A"
    client = _raw_client(_pw_response(tool_name=None, content=payload))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o")
    assert err is None and verdict["preferred"] == "A"
