"""
Claude Code Agent SDK wrapper.

Provides a clean interface for executing the Claude Code agent on a codebase
with a given prompt and returning a typed AgentResult.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)
from loguru import logger

from sentry2cc.models import AgentResult

if TYPE_CHECKING:
    from sentry2cc.config import ClaudeCodeConfig


def build_agent_options(config: ClaudeCodeConfig) -> ClaudeAgentOptions:
    """
    Build a ``ClaudeAgentOptions`` instance from the sentry2cc config.

    Parameters
    ----------
    config:
        The ``claude_code`` section of the sentry2cc YAML configuration.

    Returns
    -------
    ClaudeAgentOptions
        Ready-to-use options for ``claude_agent_sdk.query()``.
    """
    kwargs: dict = {
        "cwd": config.cwd,
        "allowed_tools": config.allowed_tools,
        "permission_mode": config.permission_mode,
    }

    if config.system_prompt:
        kwargs["system_prompt"] = config.system_prompt

    if config.max_turns is not None:
        kwargs["max_turns"] = config.max_turns

    if config.max_budget_usd is not None:
        kwargs["max_budget_usd"] = config.max_budget_usd

    if config.model:
        kwargs["model"] = config.model

    if config.add_dirs:
        kwargs["add_dirs"] = list(config.add_dirs)

    return ClaudeAgentOptions(**kwargs)


async def run_agent(
    prompt: str,
    config: ClaudeCodeConfig,
    *,
    log_progress: bool = True,
) -> AgentResult:
    """
    Execute the Claude Code agent with the given prompt.

    Streams messages as they arrive, logging Claude's reasoning and tool
    calls as debug output. Returns the final ``AgentResult`` once the
    agent completes.

    Parameters
    ----------
    prompt:
        The prompt to send to the Claude Code agent.
    config:
        The ``claude_code`` config section.
    log_progress:
        If True (default), log Claude's text output and tool calls at INFO
        level so the user can follow progress in real-time.

    Returns
    -------
    AgentResult
        The final result including success status, cost, and turn count.

    Raises
    ------
    RuntimeError
        If the agent stream ends without a ResultMessage.
    """
    options = build_agent_options(config)

    logger.info(
        "Starting Claude Code agent (cwd={}, tools={}, mode={})",
        config.cwd,
        config.allowed_tools,
        config.permission_mode,
    )

    result_message: ResultMessage | None = None

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            if log_progress:
                _log_assistant_message(message)

        elif isinstance(message, ResultMessage):
            result_message = message
            logger.info(
                "Agent finished: subtype={}, turns={}, cost=${:.4f}",
                message.subtype,
                message.num_turns,
                message.total_cost_usd or 0.0,
            )

    if result_message is None:
        raise RuntimeError("Claude Code agent stream ended without a ResultMessage")

    return AgentResult(
        session_id=result_message.session_id,
        is_error=result_message.is_error,
        num_turns=result_message.num_turns,
        duration_ms=result_message.duration_ms,
        duration_api_ms=result_message.duration_api_ms,
        total_cost_usd=result_message.total_cost_usd,
        result=result_message.result,
        stop_reason=result_message.stop_reason,
        usage=result_message.usage,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _log_assistant_message(message: AssistantMessage) -> None:
    """Log Claude's text output and tool calls for real-time visibility."""
    for block in message.content:
        if isinstance(block, TextBlock) and block.text.strip():
            # Log each line separately for cleaner output
            for line in block.text.splitlines():
                if line.strip():
                    logger.info("[Claude] {}", line)
        elif isinstance(block, ToolUseBlock):
            # Show tool calls with key input fields
            input_summary = _summarise_tool_input(block.name, block.input)
            logger.info("[Tool] {}({})", block.name, input_summary)


def _summarise_tool_input(tool_name: str, input_data: dict) -> str:
    """Return a compact single-line summary of a tool's input."""
    # Show the most informative field for each common tool
    key_fields = {
        "Read": "file_path",
        "Write": "file_path",
        "Edit": "file_path",
        "Bash": "command",
        "Glob": "pattern",
        "Grep": "pattern",
        "WebFetch": "url",
    }
    field = key_fields.get(tool_name)
    if field and field in input_data:
        value = str(input_data[field])
        if len(value) > 80:
            value = value[:77] + "..."
        return f"{field}={value!r}"

    # Fallback: show first key/value pair
    if input_data:
        k, v = next(iter(input_data.items()))
        v_str = str(v)
        if len(v_str) > 80:
            v_str = v_str[:77] + "..."
        return f"{k}={v_str!r}"

    return ""
