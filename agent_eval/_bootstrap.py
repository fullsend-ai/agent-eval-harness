"""Make the eval-harness venv's third-party deps importable.

Imported (as the first line) by every skill script and by score.py/report.py.

The venv at ``<plugin_root>/.eval-venv`` contains ONLY third-party deps — the
``agent_eval`` package itself is always resolved from the source tree via
PYTHONPATH. So the DEFAULT and correct action is to put the venv's
site-packages on ``sys.path`` of the running interpreter: no process
replacement, fully re-entrant, safe under pytest and under
``importlib.util.exec_module`` (how run.py loads report.py / score.py by path).

We re-exec into the venv interpreter via ``os.execv`` ONLY in the single case
where ``sys.path`` patching is insufficient AND re-exec is safe:

  * INSUFFICIENT: the running interpreter's ABI (major.minor) differs from the
    venv's, so the venv's compiled wheels (cpXY ``.so`` files) cannot be
    imported under the running interpreter.
  * SAFE: this is a genuine top-level *script* entry (``python script.py``),
    detected via ``__main__.__spec__ is None`` — so the re-exec happens at
    import time, before the script does any work. ``python -m`` yields a
    non-None ``__spec__`` and ``-c``/exec_module are likewise excluded, because
    re-exec there would either re-run ``__main__`` after side effects (the
    duplicate-run bug) or have no script to re-run.

The env-var sentinel makes activation idempotent and survives ``os.execv``
(env is inherited by the new process image), so the re-exec'd process
short-circuits instead of looping.
"""
import glob
import os
import sys

_SENTINEL = "_AGENT_EVAL_BOOTSTRAP_DONE"
#: Names the harness checkout whose ``.eval-venv`` a skill script should use
#: when the ``agent_eval`` it imported is a staged COPY with no venv beside it
#: (a rollout's restored plumbing, the runner's plugin staging materializing
#: ``scripts/agent_eval``). Set by the runners and the rollout runner for their
#: children; a venv beside the real package always takes precedence.
HARNESS_ROOT_ENV = "AGENT_EVAL_HARNESS_ROOT"


def harness_root():
    """The checkout this package really lives in — where its ``.eval-venv`` is."""
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def ensure_harness_root(env):
    """Name this checkout in *env* unless the caller already named one.

    For the environment a runner or the rollout runner hands its children. A
    blank value counts as unset — ``_activate`` treats it that way, and an
    ``export AGENT_EVAL_HARNESS_ROOT=`` from a ``${VAR:-}`` template must not
    silently switch the fallback off. Returns *env* for chaining.
    """
    if not env.get(HARNESS_ROOT_ENV):
        env[HARNESS_ROOT_ENV] = harness_root()
    return env


def _venv_python_and_site(plugin_root):
    """Return (venv_python_path_or_None, [site_packages_dirs])."""
    venv_dir = os.path.join(plugin_root, ".eval-venv")
    v = sys.version_info
    candidates = [
        os.path.join(venv_dir, "bin", "python3"),
        os.path.join(venv_dir, "bin", f"python{v.major}.{v.minor}"),
    ]
    venv_python = next((p for p in candidates if os.path.isfile(p)), None)
    site_dirs = glob.glob(os.path.join(venv_dir, "lib", "python*", "site-packages"))
    return venv_python, site_dirs


def _venv_pyver(site_dirs):
    """Extract 'X.Y' from a '.../lib/pythonX.Y/site-packages' path."""
    for d in site_dirs:
        name = os.path.basename(os.path.dirname(d))  # 'pythonX.Y'
        if name.startswith("python"):
            return name[len("python"):]
    return None


def _patch_syspath(site_dirs):
    real = {os.path.realpath(p) for p in sys.path}
    for d in site_dirs:
        if os.path.realpath(d) not in real:
            sys.path.insert(0, d)


def _is_true_script_entry():
    """True only for `python script.py` (not `-m`, `-c`, `-`, REPL, exec_module).

    `python script.py` -> __main__.__spec__ is None, argv[0] is the script.
    `python -m pkg.mod` -> __main__.__spec__ is a ModuleSpec (non-None).
    `python -c "..."`   -> __main__.__spec__ is None, argv[0] == '-c'.
    `python -` / stdin  -> argv[0] == '-'.
    REPL / embedded     -> argv[0] == '' (or argv empty).
    exec_module'd file  -> runs under its loader name, __main__ is unchanged.

    Only a real script file can be re-exec'd; the other forms have nothing to
    rerun, so they must not take the execv path.
    """
    main = sys.modules.get("__main__")
    if main is None or getattr(main, "__spec__", None) is not None:
        return False
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 in {"", "-", "-c"}:
        return False
    return True


def _activate():
    # Idempotent across re-import and across os.execv (env inherited).
    if os.environ.get(_SENTINEL):
        return

    pkg_dir = os.path.dirname(os.path.realpath(__file__))
    plugin_root = os.path.dirname(pkg_dir)
    venv_python, site_dirs = _venv_python_and_site(plugin_root)
    if not venv_python or not site_dirs:
        # No venv beside the package: this is a staged copy of it. The
        # process that launched us may name the real checkout — fall back to
        # that venv. Fallback only, never precedence: whatever resolved
        # before this variable existed still resolves exactly as it did.
        named = os.environ.get(HARNESS_ROOT_ENV)
        if named:
            venv_python, site_dirs = _venv_python_and_site(named)

    # No venv (e.g. in-container harbor verifier / evalhub pod) — nothing to do.
    if not venv_python or not site_dirs:
        os.environ[_SENTINEL] = "1"
        return

    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    venv_ver = _venv_pyver(site_dirs)
    abi_mismatch = venv_ver is not None and venv_ver != running

    # Set before any potential execv so the re-exec'd image short-circuits.
    os.environ[_SENTINEL] = "1"

    # Default, always-safe path: make deps importable in THIS process.
    if not abi_mismatch:
        _patch_syspath(site_dirs)
        return

    # ABI mismatch: sys.path patching would load incompatible compiled wheels.
    # Re-exec into the venv interpreter ONLY when it is provably safe — a true
    # top-level script entry, before any side effects. For -m / -c / exec_module
    # we must NOT execv (it would re-run __main__ after side effects, or there's
    # no script to re-run); fall back to patching and let an incompatible .so
    # import fail loudly rather than silently double-running.
    if _is_true_script_entry():
        os.execv(venv_python, [venv_python] + sys.argv)
    else:
        _patch_syspath(site_dirs)


def _inject_os_trust():
    """Verify TLS against the OS trust store instead of certifi's bundle.

    This lets the harness reach endpoints fronted by an internal CA — notably a
    Red Hat-issued MLflow tracking server — without anyone setting
    ``REQUESTS_CA_BUNDLE``, because the OS store (macOS keychain, Linux system
    certs) already trusts both public and internal roots.

    Gated on the CA-bundle env vars: when one is set (CI, containers) we leave
    verification untouched, since truststore makes the OS store authoritative and
    would otherwise override that bundle. Best-effort and called after deps are
    importable (and after any os.execv in ``_activate``) — a no-op if truststore
    is absent, e.g. the in-pod harbor verifier that has no venv.
    """
    # Respect any explicit CA configuration — requests also falls back to
    # CURL_CA_BUNDLE and OpenSSL honors SSL_CERT_FILE / SSL_CERT_DIR — so an
    # operator-supplied trust store is never silently replaced by the OS store.
    if any(os.environ.get(v) for v in
           ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
            "SSL_CERT_FILE", "SSL_CERT_DIR")):
        return
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass  # truststore not installed — fall back to certifi
    except Exception as e:
        # Injection itself failed (unsupported platform / API change). Warn
        # rather than silently fall back, so a real CA-chain problem is visible.
        print(f"WARNING: truststore.inject_into_ssl() failed: {e}",
              file=sys.stderr)


_activate()
_inject_os_trust()
