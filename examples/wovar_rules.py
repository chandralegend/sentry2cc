"""
Trigger rule and post-execution hook for the wovar-backend Sentry project.

Trigger:  evaluate every unresolved error-level issue once per session.
Action:   Claude explores the codebase and writes findings to a Markdown file.
Post-exec: reads the findings file and prints it to stdout.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deduplication — in-memory set of issue IDs already queued this session
# ---------------------------------------------------------------------------
_seen_issue_ids: set[str] = set()

# Directory where findings markdown files are written.
# Can be overridden via the SENTRY2CC_FINDINGS_DIR environment variable.
FINDINGS_DIR = Path(
    os.environ.get("SENTRY2CC_FINDINGS_DIR", Path(__file__).parent / "findings")
)


def findings_path_for(issue_id: str) -> Path:
    """Return the path where the findings file for a given issue should live."""
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    return FINDINGS_DIR / f"{issue_id}.md"


# ---------------------------------------------------------------------------
# Trigger rule
# ---------------------------------------------------------------------------


async def should_investigate(
    issue: Any,  # SentryIssue
    sentry_client: Any,  # SentryClient
    **kwargs: Any,
) -> bool:
    """
    Return True if this issue should be investigated by Claude Code.

    Triggers on:
    - error or fatal level
    - unresolved status
    - not already processed this session
    - no findings file already exists on disk (survives restarts)
    """
    if issue.id in _seen_issue_ids:
        logger.debug("Issue %s already queued this session — skipping", issue.id)
        return False

    if issue.level not in ("error", "fatal"):
        logger.debug("Issue %s level=%s — skipping", issue.id, issue.level)
        return False

    if issue.status != "unresolved":
        logger.debug("Issue %s status=%s — skipping", issue.id, issue.status)
        return False

    # Skip if a findings file already exists from a previous run
    output_file = findings_path_for(issue.id)
    if output_file.exists():
        logger.info(
            "Findings already exist for issue %s at %s — skipping",
            issue.id,
            output_file,
        )
        return False

    _seen_issue_ids.add(issue.id)
    logger.info(
        "Triggering investigation for issue %s (%s): %r",
        issue.id,
        issue.short_id,
        issue.title[:80],
    )
    return True


# ---------------------------------------------------------------------------
# Post-execution hook
# ---------------------------------------------------------------------------


async def print_findings(
    issue: Any,  # SentryIssue
    event: Any,  # SentryEvent
    result: Any,  # AgentResult
    sentry_client: Any,  # SentryClient
    **kwargs: Any,
) -> None:
    """
    Read the findings markdown file written by Claude and print it to stdout.
    """
    output_file = findings_path_for(issue.id)

    separator = "=" * 80

    print()
    print(separator)
    print(f"  FINDINGS: Sentry Issue {issue.short_id}  ({issue.id})")
    print(separator)
    print()

    if not output_file.exists():
        print(f"[WARNING] Findings file not found: {output_file}")
        print(f"  Agent status : {'SUCCESS' if result.success else 'ERROR'}")
        print(f"  Turns used   : {result.num_turns}")
        print(f"  Stop reason  : {result.stop_reason}")
        if result.result:
            print()
            print("Agent final output:")
            print(result.result)
        print()
        print(separator)
        return

    content = output_file.read_text(encoding="utf-8")

    print(content)
    print()
    print(separator)
    print(f"  File: {output_file}")
    print(
        f"  Agent turns : {result.num_turns}  |  Cost: ${result.total_cost_usd or 0:.4f}"
    )
    print(separator)
    print()
