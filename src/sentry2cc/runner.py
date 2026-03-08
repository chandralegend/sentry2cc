"""
Main orchestration loop for sentry2cc.

Polls Sentry for new issues, evaluates user-defined trigger rules, renders
prompts, dispatches the Claude Code agent, and calls post-execution hooks.

The loop is intentionally sequential: issues are processed one at a time
to avoid concurrent writes to the same codebase.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from pathlib import Path
from typing import Any, Callable

from sentry2cc.agent import run_agent
from sentry2cc.config import FunctionRef, Sentry2CCConfig
from sentry2cc.formatter import format_issue
from sentry2cc.models import AgentResult, SentryEvent, SentryIssue
from sentry2cc.prompt import render_prompt
from sentry2cc.sentry_client import SentryClient, SentryAPIError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Function loader
# ---------------------------------------------------------------------------


def load_function(ref: FunctionRef) -> Callable:
    """
    Dynamically import and return a function identified by a FunctionRef.

    Parameters
    ----------
    ref:
        Module path and function name (e.g. ``module="my.module"``,
        ``function="my_func"``).

    Returns
    -------
    Callable
        The resolved function object.

    Raises
    ------
    ImportError
        If the module cannot be imported.
    AttributeError
        If the function is not found in the module.
    """
    try:
        module = importlib.import_module(ref.module)
    except ImportError as exc:
        raise ImportError(
            f"Cannot import trigger/post_execution module '{ref.module}': {exc}"
        ) from exc

    func = getattr(module, ref.function, None)
    if func is None:
        raise AttributeError(
            f"Function '{ref.function}' not found in module '{ref.module}'"
        )
    if not callable(func):
        raise TypeError(f"'{ref.module}.{ref.function}' is not callable")

    logger.debug("Loaded function: %s.%s", ref.module, ref.function)
    return func


async def _call_maybe_async(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """
    Call a function that may be either a regular function or a coroutine function.

    Sync functions are run in a thread pool executor via ``asyncio.to_thread``
    so they don't block the event loop.
    """
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    else:
        return await asyncio.to_thread(func, *args, **kwargs)


# ---------------------------------------------------------------------------
# Per-issue pipeline
# ---------------------------------------------------------------------------


async def process_issue(
    issue: SentryIssue,
    sentry_client: SentryClient,
    trigger_fn: Callable,
    post_exec_fn: Callable | None,
    config: Sentry2CCConfig,
) -> None:
    """
    Run the full pipeline for a single Sentry issue:
      1. Evaluate the trigger rule
      2. Fetch the latest event
      3. Render the prompt
      4. Run the Claude Code agent
      5. Call the post-execution hook

    Parameters
    ----------
    issue:
        The Sentry issue to process.
    sentry_client:
        Open Sentry REST API client.
    trigger_fn:
        User-supplied trigger rule function.
    post_exec_fn:
        User-supplied post-execution hook (optional).
    config:
        Full sentry2cc configuration.
    """
    logger.info("Evaluating trigger for issue %s (%s)", issue.id, issue.short_id)

    trigger_kwargs = config.trigger.kwargs

    # Step 1: Trigger rule
    try:
        should_trigger = await _call_maybe_async(
            trigger_fn, issue, sentry_client, **trigger_kwargs
        )
    except Exception:
        logger.exception(
            "Trigger rule raised an exception for issue %s — skipping", issue.id
        )
        return

    if not should_trigger:
        logger.debug("Issue %s did not pass the trigger rule — skipping", issue.id)
        return

    logger.info(
        "Issue %s triggered! title=%r level=%s occurrences=%s",
        issue.id,
        issue.title,
        issue.level,
        issue.count,
    )

    # Step 2: Fetch the latest event
    try:
        event: SentryEvent = await sentry_client.get_latest_event(issue.id)
        logger.debug("Fetched latest event %s for issue %s", event.event_id, issue.id)
    except SentryAPIError as exc:
        logger.error(
            "Failed to fetch latest event for issue %s: %s — skipping", issue.id, exc
        )
        return

    # Step 3: Render the prompt
    try:
        issue_markdown = format_issue(issue, event)
        logger.debug(
            "Issue markdown for %s (%d chars):\n%s",
            issue.id,
            len(issue_markdown),
            issue_markdown,
        )

        # Build extra context for templates from trigger kwargs so templates
        # can reference any user-defined value (e.g. {{ findings_dir }})
        extra_context: dict = dict(trigger_kwargs)

        prompt = render_prompt(
            issue, event, issue_markdown, config, extra_context=extra_context
        )
        logger.debug(
            "Rendered prompt for issue %s (%d chars):\n%s",
            issue.id,
            len(prompt),
            prompt,
        )
    except Exception:
        logger.exception("Failed to render prompt for issue %s — skipping", issue.id)
        return

    # Step 4: Run the Claude Code agent
    logger.info("Dispatching Claude Code agent for issue %s...", issue.id)
    try:
        result: AgentResult = await run_agent(prompt, config.claude_code)
    except Exception:
        logger.exception("Claude Code agent raised an exception for issue %s", issue.id)
        # Still call post-execution if available, with a synthetic error result
        result = AgentResult(
            session_id="error",
            is_error=True,
            num_turns=0,
            duration_ms=0,
            duration_api_ms=0,
            result=None,
        )

    status_str = "SUCCESS" if result.success else "ERROR"
    logger.info(
        "Agent finished for issue %s: %s | turns=%d | cost=$%.4f",
        issue.id,
        status_str,
        result.num_turns,
        result.total_cost_usd or 0.0,
    )

    # Step 5: Post-execution hook
    if post_exec_fn is not None:
        logger.info("Running post-execution hook for issue %s", issue.id)
        post_exec_kwargs = config.post_execution.kwargs if config.post_execution else {}
        try:
            await _call_maybe_async(
                post_exec_fn,
                issue,
                event,
                result,
                sentry_client,
                **post_exec_kwargs,
            )
        except Exception:
            logger.exception(
                "Post-execution hook raised an exception for issue %s", issue.id
            )


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


async def run_poll_loop(
    config: Sentry2CCConfig,
    *,
    run_once: bool = False,
) -> None:
    """
    Main poll loop. Polls Sentry at the configured interval and processes
    new issues through the full pipeline.

    Parameters
    ----------
    config:
        Fully loaded sentry2cc configuration.
    run_once:
        If True, poll exactly once and then return. Useful for one-shot
        invocations (``sentry2cc --once``).
    """
    # Load user functions eagerly so we fail fast on bad config
    trigger_fn = load_function(config.trigger)
    post_exec_fn = (
        load_function(config.post_execution) if config.post_execution else None
    )

    logger.info(
        "sentry2cc starting | org=%s project=%s interval=%ds query=%r",
        config.sentry.organization,
        config.sentry.project,
        config.sentry.poll_interval,
        config.sentry.query,
    )
    if post_exec_fn is None:
        logger.info("No post_execution hook configured")

    async with SentryClient(
        auth_token=config.sentry.auth_token,
        organization=config.sentry.organization,
        project=config.sentry.project,
        base_url=config.sentry.base_url,
    ) as sentry_client:
        while True:
            await _poll_once(
                config=config,
                sentry_client=sentry_client,
                trigger_fn=trigger_fn,
                post_exec_fn=post_exec_fn,
            )

            if run_once:
                logger.info("--once flag set; exiting after single poll")
                break

            logger.info("Sleeping %ds until next poll...", config.sentry.poll_interval)
            await asyncio.sleep(config.sentry.poll_interval)


async def _poll_once(
    config: Sentry2CCConfig,
    sentry_client: SentryClient,
    trigger_fn: Callable,
    post_exec_fn: Callable | None,
) -> None:
    """Perform a single poll: fetch issues and process each one."""
    logger.info("Polling Sentry for issues (query=%r)...", config.sentry.query)

    try:
        issues, next_cursor = await sentry_client.list_issues(
            query=config.sentry.query,
            limit=config.sentry.issue_limit,
            sort=config.sentry.sort,
        )
    except SentryAPIError as exc:
        logger.error("Sentry API error during poll: %s", exc)
        return
    except Exception:
        logger.exception("Unexpected error while polling Sentry")
        return

    if not issues:
        logger.info("No issues found matching query")
        return

    logger.info("Fetched %d issue(s)", len(issues))

    for issue in issues:
        await process_issue(
            issue=issue,
            sentry_client=sentry_client,
            trigger_fn=trigger_fn,
            post_exec_fn=post_exec_fn,
            config=config,
        )
