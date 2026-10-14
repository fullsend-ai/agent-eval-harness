#!/usr/bin/env python3
"""Install Python dependencies into an isolated venv.

Creates .eval-venv/ at the plugin root and installs packages there.
Prefers uv for speed, falls back to stdlib venv + pip.

Checks eval.yaml (if it exists) to decide which optional deps to install:
- pyyaml: always required
- mlflow[genai]: if a mlflow block is present in eval.yaml
- anthropic[vertex]: if LLM judges or pairwise comparison are configured

Caches a stamp file in CLAUDE_PLUGIN_DATA so installs only run once
(or when eval.yaml changes).
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

VENV_DIR_NAME = ".eval-venv"


def main():
    plugin_data = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    plugin_root = Path(__file__).parent.parent
    venv_dir = plugin_root / VENV_DIR_NAME
    venv_python = venv_dir / "bin" / "python3"

    if _import_yaml(plugin_root) is None:
        # First run on a host without PyYAML: install it before the scan so the
        # scan reads real YAML. The stdlib minimal parser would flatten nested
        # keys (models.judge, judges[].model) and under-install — e.g. miss the
        # OpenAI SDK an `openrouter:/` judge needs.
        _ensure_venv(venv_dir)
        _install_deps(venv_dir, ["pyyaml>=6.0"])
        _import_yaml(plugin_root)
    deps = _resolve_deps(plugin_root)
    stamp = _compute_stamp(deps)

    stamp_file = None
    if plugin_data:
        plugin_data.mkdir(parents=True, exist_ok=True)
        stamp_file = plugin_data / "deps.stamp"
        if stamp_file.exists() and stamp_file.read_text().strip() == stamp:
            if venv_python.exists() and _all_importable(venv_python, deps):
                return

    _ensure_venv(venv_dir)
    _install_deps(venv_dir, [spec for spec, _ in deps])

    if stamp_file:
        stamp_file.write_text(stamp)


def _resolve_deps(plugin_root):
    """Determine which deps are needed, as the union over every eval.yaml.

    A project can hold several evals (``eval/<name>/eval.yaml`` is a supported
    layout), and they don't need the same packages — one may log to MLflow while
    another uses LLM judges. Resolving against a single config left the others
    importing packages that were never installed, with the outcome depending on
    discovery order.
    """
    deps = [("pyyaml>=6.0", "yaml")]
    seen = {spec for spec, _ in deps}

    for eval_yaml in _find_eval_yamls(plugin_root):
        for spec, module in _deps_for_config(eval_yaml):
            if spec not in seen:
                seen.add(spec)
                deps.append((spec, module))
    return deps


_EXTENDS_MAX_DEPTH = 8


def _venv_site_packages(plugin_root):
    return sorted((plugin_root / VENV_DIR_NAME).glob("lib/python*/site-packages"))


def _import_yaml(plugin_root):
    """PyYAML, reaching into .eval-venv's site-packages when the host
    interpreter lacks it (the venv is built from this interpreter, so its
    packages import here). None when it is nowhere to be found."""
    try:
        import yaml
        return yaml
    except ImportError:
        pass
    for site in _venv_site_packages(plugin_root):
        if str(site) not in sys.path:
            sys.path.append(str(site))
    try:
        import yaml
        return yaml
    except ImportError:
        return None


def _load_config_following_extends(eval_yaml):
    """The config mapping with its `extends:` chain resolved.

    Prefers the harness loader (`agent_eval.config.load_raw`, the single raw
    reader). Before the venv exists that import can fail, so a stdlib-only
    follow of the `extends` key takes over: it merges only the keys this
    script inspects (`judges` concatenated, `models`/`mlflow` overlaid) — an
    overlay that *adds* an `openrouter:/` judge still pulls `openai` even when
    its base does not. Never raises on a broken file: returns {}.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent_eval.config import load_raw
        config, _chain = load_raw(eval_yaml)
        return config if isinstance(config, dict) else {}
    except Exception:
        pass

    class _Replaced(list):
        """A `!replace`-tagged list: replaces the base value instead of merging."""

    def _read(path):
        text = path.read_text()
        try:
            import yaml

            # A plain SafeLoader rejects the harness's `!replace` tag and would
            # push the file onto the lossy minimal parser; accept the tag here
            # with the same meaning (list values only).
            class _Loader(yaml.SafeLoader):
                pass

            def _replace(loader, node):
                if not isinstance(node, yaml.SequenceNode):
                    raise ValueError("!replace applies to list values only")
                return _Replaced(loader.construct_sequence(node, deep=True))

            _Loader.add_constructor("!replace", _replace)
            loader = _Loader(text)       # SafeLoader subclass: safe_load semantics
            try:
                data = loader.get_single_data() or {}
            finally:
                loader.dispose()
        except Exception:
            data = _parse_yaml_minimal(text)
        return data if isinstance(data, dict) else {}

    try:
        merged = {}
        path = Path(eval_yaml)
        seen = set()
        layers = []
        for _ in range(_EXTENDS_MAX_DEPTH + 1):
            resolved = path.resolve()
            if resolved in seen:
                break
            seen.add(resolved)
            layer = _read(resolved)
            layers.append(layer)
            base = layer.get("extends")
            if not isinstance(base, str) or not base.strip():
                break
            path = resolved.parent / base.strip()
            if not path.exists():
                break
        for layer in reversed(layers):        # root first, overlay wins
            for key, value in layer.items():
                if key == "extends":
                    continue
                if isinstance(value, _Replaced):
                    merged[key] = list(value)
                elif key == "judges" and isinstance(value, list) and isinstance(merged.get(key), list):
                    merged[key] = merged[key] + value
                elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
        return merged
    except Exception:
        return {}


def _deps_for_config(eval_yaml):
    """Optional deps implied by one eval.yaml (its `extends:` chain resolved).
    Never raises — an unparseable or unreadable config contributes nothing
    rather than sinking the whole scan."""
    deps = []

    config = _load_config_following_extends(Path(eval_yaml))

    if not isinstance(config, dict):
        return deps

    if config.get("mlflow") is not None:
        deps.append(("mlflow[genai]>=3.5", "mlflow"))

    judges = config.get("judges", [])
    if isinstance(judges, list):
        for j in judges:
            if not isinstance(j, dict):
                continue
            if j.get("prompt") or j.get("prompt_file") or j.get("pairwise"):
                deps.append(("anthropic[vertex]>=0.77", "anthropic"))
                deps.append(("jinja2>=3.0", "jinja2"))
                break
    elif isinstance(judges, dict):
        # _parse_yaml_minimal can't parse YAML lists, so judges may be
        # a dict or empty. Install anthropic+jinja2 as a safe default
        # since we can't tell whether LLM judges are configured.
        deps.append(("anthropic[vertex]>=0.77", "anthropic"))
        deps.append(("jinja2>=3.0", "jinja2"))

    # Non-Anthropic judge models grade through the OpenAI SDK (which also serves
    # OpenAI-compatible gateways via OPENAI_BASE_URL). Pull it in when a judge
    # model looks non-Anthropic. Best-effort: a miss surfaces as an actionable
    # runtime error in score._get_openai_client.
    models = config.get("models")
    judge_models = []
    if isinstance(models, dict) and models.get("judge"):
        judge_models.append(models["judge"])
    if isinstance(judges, list):
        judge_models += [j["model"] for j in judges
                         if isinstance(j, dict) and j.get("model")]
    if any(_needs_openai_backend(m) for m in judge_models):
        deps.append(("openai>=1.70", "openai"))

    return deps


def _needs_openai_backend(model):
    """Whether a judge model id routes to the OpenAI SDK (see resolve_judge_backend).

    Stdlib-only mirror of the routing classifier — ensure_deps runs before the
    venv exists, so it cannot import agent_eval.prompt_backends. Mirrors its
    two rules: an explicit provider other than `anthropic:/` / `runner:/`
    (`openai:/`, `openrouter:/`) uses the OpenAI SDK, and so does any bare id
    that is not a Claude-family alias (GPT/o-series or a gateway model name).
    """
    value = (str(model) if model else "").strip().lower()
    if not value:
        return False
    if ":/" in value:
        provider = value.split(":/", 1)[0].strip()
        return provider not in ("anthropic", "runner")
    if "claude" in value or value.startswith(("opus", "sonnet", "haiku", "anthropic")):
        return False
    return True


def _parse_yaml_minimal(text):
    """Minimal YAML-like extraction when pyyaml isn't available yet."""
    result = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if ":" in stripped and not stripped.startswith("-"):
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                result[key] = val
            else:
                result[key] = {}
    return result


def _find_venv_python(venv_dir):
    """Find the python binary in a venv (handles uv naming variations)."""
    for name in ("python3", "python", f"python{sys.version_info.major}.{sys.version_info.minor}"):
        candidate = venv_dir / "bin" / name
        if candidate.exists():
            return candidate
    return None


def _ensure_venv(venv_dir):
    """Create the venv if it doesn't exist."""
    if _find_venv_python(venv_dir):
        return

    uv = shutil.which("uv")
    if uv:
        print(f"Creating venv with uv: {venv_dir}")
        subprocess.run([uv, "venv", str(venv_dir), "--seed",
                        "--python", sys.executable],
                       check=True, capture_output=True, text=True)
        venv_py = _find_venv_python(venv_dir)
        if venv_py and venv_py.name != "python3":
            (venv_dir / "bin" / "python3").symlink_to(venv_py.name)
    else:
        print(f"Creating venv: {venv_dir}")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)],
                       check=True, capture_output=True, text=True)


def _install_deps(venv_dir, specs):
    """Install packages into the venv."""
    if not specs:
        return

    uv = shutil.which("uv")
    venv_pip = venv_dir / "bin" / "pip"
    venv_python = venv_dir / "bin" / "python3"

    print(f"Installing: {', '.join(specs)}")

    if uv:
        result = subprocess.run(
            [uv, "pip", "install", "-q", "--python", str(venv_python), *specs],
            capture_output=True, text=True,
        )
    elif venv_pip.exists():
        result = subprocess.run(
            [str(venv_pip), "install", "-q", *specs],
            capture_output=True, text=True,
        )
    else:
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-q", *specs],
            capture_output=True, text=True,
        )

    if result.returncode != 0:
        print(f"Install failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def _all_importable(venv_python, deps):
    """Check all deps are importable in the venv python."""
    imports = ";".join(f"__import__('{mod}')" for _, mod in deps)
    result = subprocess.run(
        [str(venv_python), "-c", imports],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _find_eval_yamls(plugin_root):
    """Every eval.yaml in the project, in discovery order.

    Deliberately *all* of them, not ``configs[0]``: deps are the union across the
    project, so which config happens to sort first must not decide what gets
    installed. The fallback stays first-match — it only runs when discovery itself
    failed, and cwd/eval.yaml vs plugin_root/eval.yaml are alternatives rather
    than siblings.
    """
    cwd = Path.cwd()
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent_eval.config import discover_configs
        # Profiles (`extends:` overlays) count too: an overlay may add a judge
        # whose SDK the base never needed.
        configs = discover_configs(cwd, include_profiles=True)
        if configs:
            return [c.path for c in configs]
    except Exception as exc:  # noqa: BLE001 - SessionStart must never hard-fail
        # Broad on purpose: a discovery bug must not block the session. But the
        # fallback below sees one config at most, so staying silent here is the
        # very under-installation this function exists to prevent — say so.
        print(f"Warning: eval.yaml discovery failed ({exc}); falling back to a "
              f"single config. .eval-venv may be missing deps required by other "
              f"evals in this project.", file=sys.stderr)
    for candidate in [cwd / "eval.yaml", plugin_root / "eval.yaml"]:
        if candidate.exists():
            return [candidate]
    return []


def _compute_stamp(deps):
    return hashlib.sha256("|".join(sorted(s for s, _ in deps)).encode()).hexdigest()[:12]


if __name__ == "__main__":
    main()
