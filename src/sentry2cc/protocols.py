"""
Protocol definitions for user-supplied extension functions.

Users implement these protocols as plain Python functions (sync or async)
and point to them via their YAML config file.

TriggerRule
-----------
Called for each Sentry issue polled. Return True to trigger the Claude Code
agent for that issue, False to skip it.

    async def should_fix(
        issue: SentryIssue,
        sentry_client: SentryClient,
        **kwargs: Any,
    ) -> bool:
        # e.g. only trigger for high-severity unresolved issues
        return issue.level in ("error", "fatal") and issue.status == "unresolved"


PostExecution
-------------
Called after the Claude Code agent finishes for a triggered issue.

    async def after_fix(
        issue: SentryIssue,
        event: SentryEvent,
        result: AgentResult,
        sentry_client: SentryClient,
        **kwargs: Any,
    ) -> None:
        if result.success:
            # e.g. mark the issue as resolved in Sentry
            await sentry_client.update_issue(issue.id, {"status": "resolved"})
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sentry2cc.agent import AgentResult
    from sentry2cc.models import SentryEvent, SentryIssue
    from sentry2cc.sentry_client import SentryClient


@runtime_checkable
class TriggerRule(Protocol):
    """
    Protocol for a function that decides whether a Sentry issue should
    trigger the Claude Code agent.

    Parameters
    ----------
    issue:
        The Sentry issue being evaluated.
    sentry_client:
        An authenticated Sentry REST API client, in case the trigger
        needs to make additional API calls (e.g. fetch tags, check history).
    **kwargs:
        Reserved for future extensibility; ignore or accept as needed.

    Returns
    -------
    bool
        True  → pass the issue to Claude Code for remediation.
        False → skip this issue.
    """

    async def __call__(
        self,
        issue: SentryIssue,
        sentry_client: SentryClient,
        **kwargs: Any,
    ) -> bool: ...


@runtime_checkable
class PostExecution(Protocol):
    """
    Protocol for a function called after the Claude Code agent finishes
    processing a Sentry issue.

    Parameters
    ----------
    issue:
        The Sentry issue that was processed.
    event:
        The latest Sentry event (error occurrence) for the issue.
    result:
        The result from the Claude Code agent execution.
    sentry_client:
        An authenticated Sentry REST API client (e.g. to mark the issue
        resolved, add a comment, etc.).
    **kwargs:
        Reserved for future extensibility; ignore or accept as needed.
    """

    async def __call__(
        self,
        issue: SentryIssue,
        event: SentryEvent,
        result: AgentResult,
        sentry_client: SentryClient,
        **kwargs: Any,
    ) -> None: ...
