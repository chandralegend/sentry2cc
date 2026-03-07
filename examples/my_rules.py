"""
Example trigger rule and post-execution hook for sentry2cc.

Point to these functions from sentry2cc.yaml:

    trigger:
      module: "my_rules"
      function: "should_fix"

    post_execution:
      module: "my_rules"
      function: "after_fix"

Both sync and async functions are supported. sentry2cc automatically wraps
sync functions so they don't block the event loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sentry2cc.models import AgentResult, SentryEvent, SentryIssue
    from sentry2cc.sentry_client import SentryClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory deduplication set
# (replace with a persistent store — DB, file, Redis — for production use)
# ---------------------------------------------------------------------------
_processed_issue_ids: set[str] = set()


# ---------------------------------------------------------------------------
# Trigger rule
# ---------------------------------------------------------------------------


async def should_fix(
    issue: SentryIssue,
    sentry_client: SentryClient,
    **kwargs: Any,
) -> bool:
    """
    Return True if this issue should be passed to Claude Code for fixing.

    This example triggers on:
      - Error or fatal level issues
      - Unresolved status
      - Not previously processed in this session

    Customize this function to add your own business rules, e.g.:
      - Minimum occurrence threshold
      - Specific error types or culprits
      - Project-specific logic
      - Check external systems (JIRA, Linear, etc.)
    """
    # Skip already-processed issues (deduplication)
    if issue.id in _processed_issue_ids:
        logger.debug("Issue %s already processed — skipping", issue.id)
        return False

    # Only trigger for error-level and above
    if issue.level not in ("error", "fatal"):
        logger.debug(
            "Issue %s has level '%s' — skipping (only error/fatal)",
            issue.id,
            issue.level,
        )
        return False

    # Only trigger for unresolved issues
    if issue.status != "unresolved":
        logger.debug(
            "Issue %s has status '%s' — skipping (only unresolved)",
            issue.id,
            issue.status,
        )
        return False

    # Example: skip issues with too few occurrences (might be noise)
    if issue.event_count < 3:
        logger.debug(
            "Issue %s has only %d occurrences — skipping (threshold: 3)",
            issue.id,
            issue.event_count,
        )
        return False

    # Mark as "seen" immediately so re-polls don't trigger it again
    _processed_issue_ids.add(issue.id)

    logger.info(
        "Triggering for issue %s: level=%s occurrences=%d title=%r",
        issue.id,
        issue.level,
        issue.event_count,
        issue.title,
    )
    return True


# ---------------------------------------------------------------------------
# Post-execution hook
# ---------------------------------------------------------------------------


async def after_fix(
    issue: SentryIssue,
    event: SentryEvent,
    result: AgentResult,
    sentry_client: SentryClient,
    **kwargs: Any,
) -> None:
    """
    Called after Claude Code Agent finishes processing an issue.

    This example:
      - Logs the outcome
      - If the agent succeeded, marks the Sentry issue as resolved

    Customize this function to:
      - Post a comment to Sentry with the fix summary
      - Open a pull request
      - Update a JIRA/Linear ticket
      - Send a Slack/Teams notification
      - Record metrics
    """
    if result.success:
        logger.info(
            "Agent successfully processed issue %s in %d turns ($%.4f). "
            "Marking as resolved in Sentry.",
            issue.id,
            result.num_turns,
            result.total_cost_usd or 0.0,
        )

        try:
            # Mark the issue as resolved in Sentry
            await sentry_client.update_issue(issue.id, {"status": "resolved"})
            logger.info("Issue %s marked as resolved in Sentry", issue.id)
        except Exception:
            logger.exception("Failed to mark issue %s as resolved", issue.id)

    else:
        logger.warning(
            "Agent encountered an error processing issue %s "
            "(turns=%d, stop_reason=%s). Issue left unresolved.",
            issue.id,
            result.num_turns,
            result.stop_reason,
        )
