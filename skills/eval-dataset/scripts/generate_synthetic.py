#!/usr/bin/env python3
"""Generate synthetic test cases from generation prompts using an LLM.

This script reads the ``generation`` block from eval.yaml, resolves each seed's
generation prompt (builtin / prompt_file / inline), then generates test cases
through either the direct Anthropic client (for Claude models) or the eval
runner abstraction (for runner-managed model ids).
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

import yaml

from agent_eval.config import EvalConfig, GenerationSeed
from agent_eval.prompt_backends import (
    is_anthropic_model,
    run_prompt_via_runner,
    split_model_uri,
)
from agent_eval.prompts import resolve_seed_prompt


def _seed_source(seed: GenerationSeed) -> str:
    """Human-readable description of a seed's prompt source (for logs/metadata)."""
    return seed.builtin or seed.prompt_file or ("inline" if seed.prompt else "?")


def generate_synthetic(
    config: EvalConfig,
    output_dir: Path,
    model: str = "claude-opus-4-6",
    api_key: Optional[str] = None,
) -> list[dict]:
    """Generate test cases from the generation seeds in ``config``.

    Args:
        config: EvalConfig with a ``generation`` block (seeds + context)
        output_dir: Where to write generated test cases
        model: Model to use for generation
        api_key: Optional Anthropic API key override for Claude-family models

    Returns:
        List of generated case metadata

    Raises:
        ValueError: If no generation seeds defined
        ImportError: If Anthropic support is required but not installed
    """
    if not config.generation.seeds:
        raise ValueError(
            "No generation seeds defined in config. "
            "Synthetic generation requires generation.seeds.")

    # Route on the model's provider, independent of the runner. A provider prefix
    # (`anthropic:/…`, `runner:/…`) is stripped to the bare id the SDK/runner
    # expects; a bare Claude id uses the Anthropic SDK, anything else the runner.
    provider, gen_model = split_model_uri(model)
    if provider == "openai":
        raise ValueError(
            "OpenAI-native synthetic generation is not supported. Use a Claude "
            "model, or a runner-managed model ('runner:/<model>') so the "
            "configured runner generates the cases.")
    if provider == "openrouter":
        raise ValueError(
            "OpenRouter-backed synthetic generation is not supported "
            "('openrouter:/…' models serve the agent and judge roles). Use a "
            "Claude model, or a runner-managed model ('runner:/<model>') so the "
            "configured runner generates the cases.")

    client = None
    use_anthropic = is_anthropic_model(model)
    if use_anthropic:
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic package required for Claude synthetic generation. "
                "Install with: pip install anthropic")

        # Support direct API, bearer-token gateway, and Vertex AI
        # authentication.  ``ANTHROPIC_AUTH_TOKEN`` is a bearer token, not an
        # API key; passing it as ``api_key`` makes the SDK send the wrong auth
        # scheme to gateways that distinguish the two.
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        vertex_project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")

        if api_key:
            client = anthropic.Anthropic(api_key=api_key)
        elif auth_token:
            client = anthropic.Anthropic(auth_token=auth_token)
        elif vertex_project:
            client = anthropic.AnthropicVertex(
                project_id=vertex_project,
                region=os.environ.get("ANTHROPIC_VERTEX_REGION", "us-east5"),
            )
        else:
            raise ValueError(
                "Claude synthetic generation requires ANTHROPIC_API_KEY, "
                "ANTHROPIC_AUTH_TOKEN, or ANTHROPIC_VERTEX_PROJECT_ID. Use a "
                "runner-backed model to avoid Anthropic credentials.")
    all_cases = []
    failed_categories = []
    case_counter = 1

    for seed in config.generation.seeds:
        print(f"Generating {seed.count} test cases for category: {seed.category}")

        # Resolve the generation prompt via the seed's discriminator
        # (builtin / prompt_file / prompt — validated by EvalConfig.from_yaml)
        try:
            generation_prompt = resolve_seed_prompt(seed, config.config_dir)
        except (ValueError, FileNotFoundError) as e:
            print(
                f"ERROR: Failed to resolve generation prompt for category '{seed.category}': {e}",
                file=sys.stderr,
            )
            raise

        # Generate cases for this category
        try:
            cases = _generate_category_cases(
                config=config,
                client=client,
                generation_prompt=generation_prompt,
                seed=seed,
                context=config.generation.context,
                count=seed.count,
                model=gen_model,
            )
        except ValueError as e:
            print(
                f"ERROR: Failed to generate cases for category '{seed.category}': {e}\n"
                f"Continuing with other categories...",
                file=sys.stderr,
            )
            failed_categories.append(seed.category)
            # Continue with next category instead of failing entirely
            continue
        except Exception as e:
            print(f"ERROR: Unexpected error for category '{seed.category}': {e}", file=sys.stderr)
            failed_categories.append(seed.category)
            continue

        # Write cases to disk
        for case in cases:
            # Validate case structure
            if not isinstance(case, dict):
                print(
                    f"  WARNING: Skipping invalid case (not a dict): {case}",
                    file=sys.stderr,
                )
                continue
            if "input" not in case:
                print(
                    f"  WARNING: Skipping case without 'input' field: {case}",
                    file=sys.stderr,
                )
                continue

            # Validate input is a dict (not a string, list, etc.)
            if not isinstance(case["input"], dict):
                print(
                    f"  WARNING: Skipping case with invalid 'input' (not a dict): {case['input']!r}",
                    file=sys.stderr,
                )
                continue

            # Build annotations dict - always include category for judge filtering
            annotations = {}
            if "annotations" in case:
                if not isinstance(case["annotations"], dict):
                    print(
                        f"  WARNING: Skipping case with invalid 'annotations' (not a dict): {case['annotations']!r}",
                        file=sys.stderr,
                    )
                    continue
                annotations = case["annotations"].copy()

            # CRITICAL: Always set category to match the seed's category
            # Judges use `if: "annotations.get('category') == 'navigation'"` for filtering.
            # This is also how the category list is derived — never declared separately.
            annotations["category"] = seed.category

            case_id = f"case-{case_counter:03d}"
            case_dir = output_dir / case_id
            case_dir.mkdir(parents=True, exist_ok=True)

            # Write input.yaml
            (case_dir / "input.yaml").write_text(
                yaml.dump(case["input"], sort_keys=False, allow_unicode=True)
            )

            # Always write annotations.yaml with guaranteed category field
            (case_dir / "annotations.yaml").write_text(
                yaml.dump(annotations, sort_keys=False, allow_unicode=True)
            )

            # Track metadata
            all_cases.append({
                "case_id": case_id,
                "category": seed.category,
                "source": _seed_source(seed),
            })

            case_counter += 1

    if not all_cases:
        raise RuntimeError(
            "Failed to generate any synthetic cases"
            + (f": {', '.join(failed_categories)}" if failed_categories else "")
        )
    return all_cases


def _generate_category_cases(
    config: EvalConfig,
    client,
    generation_prompt: str,
    seed: GenerationSeed,
    context,
    count: int,
    model: str,
) -> list[dict]:
    """Use an LLM to generate cases from a resolved generation prompt.

    Args:
        config: EvalConfig for runner-backed fallbacks
        client: Anthropic client, or None for runner-backed generation
        generation_prompt: Resolved generation-prompt markdown content
        seed: GenerationSeed config
        context: Repository knowledge (dict or str) injected into the prompt
        count: Number of cases to generate
        model: Model to use

    Returns:
        List of test case dicts with 'input' and optional 'annotations' keys
    """
    # Build generation prompt
    context_yaml = (
        yaml.dump(context, sort_keys=False, allow_unicode=True) if context else "None"
    )

    prompt = f"""You are generating test cases for an agent evaluation harness.

# Generation Prompt

{generation_prompt}

# Generation Context

The repository-specific context that should inform test case generation:

```yaml
{context_yaml}
```

# Generation Task

Generate exactly {count} test case(s) following the generation prompt above.

**IMPORTANT**: Return ONLY a valid JSON array with no additional text, markdown formatting, or explanation.

Each test case MUST be a JSON object with exactly two top-level keys:
- "input": dict with fields the agent receives (typically just "prompt" — the question or task).
  Do NOT put evaluation metadata here.
- "annotations": dict with evaluation metadata used by judges to score the response.
  This includes: category, difficulty, expected_files, expected_mentions,
  expected_rejection, expected_guidance, severity, constraint_type, topic, and
  any other scoring criteria.

Example format:
```json
[
  {{
    "input": {{
      "prompt": "User question or task for the agent"
    }},
    "annotations": {{
      "category": "{seed.category}",
      "difficulty": "medium",
      "expected_files": ["path/to/relevant-doc.md"],
      "expected_mentions": ["keyword1", "keyword2"]
    }}
  }}
]
```

CRITICAL RULES:
- "input" contains ONLY what the agent sees (typically just "prompt")
- "annotations" contains ALL evaluation metadata (expected_files, expected_mentions,
  expected_rejection, expected_guidance, category, difficulty, etc.)
- NEVER put expected_files, expected_mentions, or any expected_* fields in "input"

Generate realistic, varied test cases that:
1. Use actual paths/topics from the generation context (if available)
2. Cover different difficulty levels
3. Test different aspects of the capability
4. Are specific and verifiable

Return the JSON array now:"""

    if client is not None:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            temperature=1.0,
        )

        # Parse response (validate content exists and has text attribute - CWE-20)
        if not response.content:
            raise ValueError("Anthropic API returned empty content")

        first_block = response.content[0]
        if not hasattr(first_block, 'text'):
            raise ValueError(
                f"Expected text content from Anthropic API, got type: {type(first_block).__name__}")
        response_text = first_block.text.strip()
    else:
        result, response_text, workspace = run_prompt_via_runner(
            config,
            prompt,
            model,
            timeout_s=int(config.execution.timeout
                          if config.execution.timeout is not None else 600),
            max_budget_usd=5.0,
            # Scoped, not bare: a bare `Read`/`Grep`/`Glob` allow removes the
            # CLI's working-directory confinement rather than merely
            # pre-approving the tool, and all three work inside the workspace
            # with no rule at all. See `_judge_tool_policy` in score.py.
            permissions={"allow": ["Read(**)"]},
        )
        try:
            if result.exit_code != 0:
                raise ValueError(
                    f"Runner-backed generation failed with exit code {result.exit_code}: "
                    f"{(result.stderr or result.stdout).strip()[:400]}"
                )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    # Extract JSON from response (may be wrapped in markdown)
    cases = _extract_json_from_response(response_text)

    if not isinstance(cases, list):
        raise ValueError(
            f"Expected JSON array from LLM, got: {type(cases).__name__}")

    if len(cases) != count:
        print(
            f"WARNING: Requested {count} cases, got {len(cases)} "
            f"for category {seed.category}",
            file=sys.stderr,
        )

    # Move annotation fields that the LLM misplaced into input
    _fix_misplaced_annotation_fields(cases)

    return cases


_ANNOTATION_FIELDS = {
    "expected_files", "expected_mentions", "expected_rejection",
    "expected_guidance", "expected_constraint", "expected_structure",
    "expected_patterns", "expected_api", "expected_example_type",
    "expected_fields", "expected_components", "expected_interactions",
    "correct_approach", "category", "difficulty", "severity",
    "constraint_type", "topic",
}


def _fix_misplaced_annotation_fields(cases: list[dict]) -> None:
    """Move known annotation fields from input to annotations if the LLM misplaced them."""
    for case in cases:
        if not isinstance(case.get("input"), dict):
            continue
        misplaced = _ANNOTATION_FIELDS & set(case["input"].keys())
        if misplaced:
            if "annotations" not in case:
                case["annotations"] = {}
            for field in misplaced:
                case["annotations"][field] = case["input"].pop(field)
            print(
                f"  WARNING: Moved {misplaced} from input to annotations",
                file=sys.stderr,
            )


def _extract_json_from_response(text: str) -> list:
    """Extract JSON array from LLM response.

    Handles responses that may be wrapped in markdown code blocks.

    Args:
        text: Response text from LLM

    Returns:
        Parsed JSON array

    Raises:
        ValueError: If JSON cannot be parsed
    """
    original_text = text

    # Try parsing directly
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    if "```" in text:
        # Find content between ``` markers
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            # Skip language identifiers
            if part.startswith("json") or part.startswith("JSON"):
                part = part[4:].strip()

            # Try parsing if it looks like JSON
            if part.startswith("[") or part.startswith("{"):
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue

    # Try finding JSON array with regex (handles text before/after)
    array_pattern = r'\[(?:[^\[\]]|\[[^\[\]]*\])*\]'
    matches = list(re.finditer(array_pattern, text, re.DOTALL))

    # Try matches from longest to shortest
    for match in sorted(matches, key=lambda m: len(m.group(0)), reverse=True):
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue

    # Try cleaning common issues
    # Remove trailing commas before ] or }
    cleaned = re.sub(r',(\s*[\]}])', r'\1', text)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Final attempt: find the largest bracket pair
    start = text.find('[')
    if start >= 0:
        end = text.rfind(']')
        if end > start:
            candidate = text[start:end+1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # Give up with detailed error
    raise ValueError(
        f"Could not extract valid JSON from response.\n"
        f"Response preview: {original_text[:500]}...\n"
        f"Response length: {len(original_text)} chars"
    )


def main():
    """CLI for testing synthetic generation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate synthetic test cases from generation prompts")
    parser.add_argument(
        "--config", required=True,
        help="Path to eval.yaml")
    parser.add_argument(
        "--output", required=True,
        help="Output directory for generated test cases")
    parser.add_argument(
        "--model", default="claude-opus-4-6",
        help="Claude model to use (default: claude-opus-4-6)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be generated without calling API")

    args = parser.parse_args()

    # Load config
    config = EvalConfig.from_yaml(args.config)

    if not config.generation.seeds:
        print(
            "ERROR: No generation seeds defined in config. "
            "Synthetic generation requires generation.seeds.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_dir = Path(args.output)

    if args.dry_run:
        print("Would generate test cases:")
        for seed in config.generation.seeds:
            print(f"  - {seed.category}: {seed.count} cases from {_seed_source(seed)}")
        total = sum(s.count for s in config.generation.seeds)
        print(f"\nTotal: {total} test cases")
        print(f"Output: {output_dir}")
        return

    # Generate cases
    try:
        cases = generate_synthetic(
            config=config,
            output_dir=output_dir,
            model=args.model,
        )

        print(f"\nGenerated {len(cases)} test cases:")
        for case in cases:
            print(f"  {case['case_id']}: {case['category']}")

        print(f"\nOutput written to: {output_dir}")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
