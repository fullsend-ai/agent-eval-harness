"""Search-engine-specific integrations for agent_eval/optimize's engine-agnostic layer.

Each module here is the one place coupled to a particular external search
engine's API/contract (e.g. ``capevolve.py`` for cap-evolve). Everything
engine-agnostic — candidate resolution, running one rollout, exporting results,
redacting gold — lives in ``agent_eval.optimize`` instead.
"""
