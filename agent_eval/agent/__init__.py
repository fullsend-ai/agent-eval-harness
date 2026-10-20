"""Agent runner abstraction for eval harness."""

from .base import EvalRunner, RunResult
from .claude_code import ClaudeCodeRunner
from .cli_runner import CliRunner
from .codex import CodexRunner
from .cursor_agent import CursorAgentRunner
from .fullsend import FullsendRunner
from .responses_api import ResponsesAPIRunner

RUNNERS = {
    "claude-code": ClaudeCodeRunner,
    "cli": CliRunner,
    "codex": CodexRunner,
    "cursor": CursorAgentRunner,
    "fullsend": FullsendRunner,
    "responses-api": ResponsesAPIRunner,
}

__all__ = [
    "EvalRunner", "RunResult",
    "ClaudeCodeRunner", "CliRunner", "CodexRunner", "CursorAgentRunner",
    "FullsendRunner", "ResponsesAPIRunner",
    "RUNNERS",
]
