"""Engine-agnostic optimization primitives: candidate handling, one-case
rollouts, run-dir export, and gold redaction.

Nothing in this package imports or knows about any particular search engine
(cap-evolve or otherwise) — that coupling lives in ``agent_eval.engines``.
"""
