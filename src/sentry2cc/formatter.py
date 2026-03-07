"""
Converts Sentry issue and event data into a rich Markdown document.

The output is used as the ``issue_markdown`` variable in Jinja2 prompt
templates, giving Claude a well-structured summary of the error.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentry2cc.models import (
        ExceptionValue,
        SentryEvent,
        SentryIssue,
        StackFrame,
        Stacktrace,
    )


def format_issue(issue: SentryIssue, event: SentryEvent) -> str:
    """
    Render a Sentry issue + its latest event as a Markdown document.

    Parameters
    ----------
    issue:
        The Sentry issue (group) metadata.
    event:
        A specific event occurrence for the issue (usually the latest).

    Returns
    -------
    str
        Multi-section Markdown string.
    """
    sections: list[str] = []

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    sections.append(_header(issue))

    # ------------------------------------------------------------------
    # Exception details
    # ------------------------------------------------------------------
    exception_entries = event.exception_entries
    if exception_entries:
        for exc_entry in exception_entries:
            for exc_val in reversed(exc_entry.values):  # innermost last = most relevant
                sections.append(_exception_section(exc_val))
    else:
        # Fallback: just show the event title/message
        sections.append(
            "## Error\n\n" + (event.message or event.title or "*(no message)*")
        )

    # ------------------------------------------------------------------
    # Request details (if present)
    # ------------------------------------------------------------------
    req_entry = event.request_entry
    if req_entry and req_entry.data:
        sections.append(_request_section(req_entry.data))

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------
    if event.tags:
        sections.append(_tags_section(event))

    # ------------------------------------------------------------------
    # Breadcrumbs (last 10)
    # ------------------------------------------------------------------
    breadcrumb_entries = event.breadcrumb_entries
    if breadcrumb_entries:
        all_crumbs: list[dict] = []
        for entry in breadcrumb_entries:
            all_crumbs.extend(entry.values)
        if all_crumbs:
            sections.append(_breadcrumbs_section(all_crumbs[-10:]))

    # ------------------------------------------------------------------
    # User context
    # ------------------------------------------------------------------
    if event.user:
        sections.append(_user_section(event.user))

    # ------------------------------------------------------------------
    # Release info
    # ------------------------------------------------------------------
    if event.release:
        sections.append(_release_section(event.release))

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _header(issue: SentryIssue) -> str:
    lines = [
        f"# Sentry Issue: {issue.title}",
        "",
        f"| Field | Value |",
        f"|---|---|",
        f"| **Issue ID** | `{issue.id}` |",
        f"| **Short ID** | `{issue.short_id}` |",
        f"| **Level** | `{issue.level}` |",
        f"| **Status** | `{issue.status}` |",
        f"| **First Seen** | {issue.first_seen.strftime('%Y-%m-%d %H:%M:%S UTC')} |",
        f"| **Last Seen** | {issue.last_seen.strftime('%Y-%m-%d %H:%M:%S UTC')} |",
        f"| **Occurrences** | {issue.event_count:,} |",
        f"| **Affected Users** | {issue.user_count:,} |",
        f"| **Project** | {issue.project.name} (`{issue.project.slug}`) |",
    ]
    if issue.culprit:
        lines.append(f"| **Culprit** | `{issue.culprit}` |")
    lines.append(f"| **Permalink** | {issue.permalink} |")
    return "\n".join(lines)


def _exception_section(exc: ExceptionValue) -> str:
    lines: list[str] = []

    exc_type = exc.type or "Exception"
    exc_value = exc.value or ""

    lines.append(f"## Exception: `{exc_type}`")
    if exc_value:
        lines.append(f"\n**Message:** {exc_value}")

    if exc.stacktrace:
        lines.append("")
        lines.append(_stacktrace_section(exc.stacktrace))

    return "\n".join(lines)


def _stacktrace_section(st: Stacktrace) -> str:
    lines = ["### Stacktrace"]

    frames = st.frames
    if not frames:
        return "\n".join(lines) + "\n\n*(no frames)*"

    # Show all frames but emphasise in-app ones
    app_frames = st.app_frames

    # If there are many frames, summarise and focus on the most relevant
    show_frames = frames[-15:]  # last 15 frames (nearest to the crash)

    for frame in show_frames:
        lines.append("")
        lines.append(_frame_block(frame))

    if len(frames) > 15:
        omitted = len(frames) - 15
        lines.insert(2, f"\n*({omitted} earlier frames omitted)*\n")

    _ = app_frames  # referenced to avoid unused variable warning
    return "\n".join(lines)


def _frame_block(frame: StackFrame) -> str:
    location_parts = []
    if frame.filename:
        location_parts.append(frame.filename)
    elif frame.abs_path:
        location_parts.append(frame.abs_path)

    location = ":".join(
        filter(
            None,
            [
                "/".join(location_parts),
                str(frame.line_no) if frame.line_no is not None else None,
            ],
        )
    )

    func = frame.function or "<unknown>"
    in_app_marker = " *(in-app)*" if frame.in_app else ""

    lines = [f"#### `{func}` — {location}{in_app_marker}"]

    ctx = frame.source_context
    if ctx:
        lines.append("")
        lines.append("```")
        lines.append(ctx)
        lines.append("```")

    if frame.vars:
        lines.append("")
        lines.append("**Local variables:**")
        lines.append("")
        for k, v in list(frame.vars.items())[:10]:  # limit to 10 vars
            v_repr = _safe_repr(v)
            lines.append(f"- `{k}` = `{v_repr}`")

    return "\n".join(lines)


def _request_section(data: dict) -> str:
    lines = ["## HTTP Request"]
    method = data.get("method") or ""
    url = data.get("url") or ""
    if method or url:
        lines.append(f"\n`{method} {url}`")

    query = data.get("query")
    if query:
        lines.append("\n**Query Parameters:**")
        if isinstance(query, list):
            for k, v in query:
                lines.append(f"- `{k}`: `{v}`")
        elif isinstance(query, dict):
            for k, v in query.items():
                lines.append(f"- `{k}`: `{v}`")

    headers = data.get("headers")
    if headers:
        lines.append("\n**Headers:**")
        shown_headers = {
            "user-agent",
            "content-type",
            "accept",
            "referer",
            "x-forwarded-for",
        }
        for h in headers:
            if isinstance(h, (list, tuple)) and len(h) == 2:
                k, v = h
                if k.lower() in shown_headers:
                    lines.append(f"- `{k}`: `{v}`")

    return "\n".join(lines)


def _tags_section(event: SentryEvent) -> str:
    lines = ["## Tags", ""]
    for tag in event.tags:
        lines.append(f"- **{tag.key}**: `{tag.value}`")
    return "\n".join(lines)


def _breadcrumbs_section(crumbs: list[dict]) -> str:
    lines = ["## Recent Breadcrumbs (last 10)", ""]
    for crumb in crumbs:
        ts = crumb.get("timestamp", "")
        if ts and len(ts) > 19:
            ts = ts[:19]
        category = crumb.get("category") or crumb.get("type") or "?"
        message = crumb.get("message") or ""
        level = crumb.get("level") or ""
        data = crumb.get("data")

        parts = [f"`{ts}`", f"[{category}]"]
        if level:
            parts.append(f"*{level}*")
        if message:
            parts.append(message)
        if data and isinstance(data, dict):
            data_str = ", ".join(f"{k}={v}" for k, v in list(data.items())[:3])
            parts.append(f"({data_str})")

        lines.append("- " + " ".join(parts))

    return "\n".join(lines)


def _user_section(user: dict) -> str:
    lines = ["## Affected User", ""]
    display_keys = ["id", "username", "email", "name", "ip_address"]
    for key in display_keys:
        val = user.get(key)
        if val:
            lines.append(f"- **{key}**: `{val}`")
    return "\n".join(lines)


def _release_section(release: dict) -> str:
    lines = ["## Release", ""]
    version = release.get("version") or release.get("shortVersion") or ""
    if version:
        lines.append(f"- **Version**: `{version}`")
    date_released = release.get("dateReleased")
    if date_released:
        lines.append(f"- **Released**: {date_released[:19]}")
    ref = release.get("ref")
    if ref:
        lines.append(f"- **Ref**: `{ref}`")
    url = release.get("url")
    if url:
        lines.append(f"- **Deploy URL**: {url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _safe_repr(value: object, max_len: int = 120) -> str:
    """Return a safe, truncated repr of a value."""
    try:
        r = repr(value)
    except Exception:
        r = "<unrepresentable>"
    if len(r) > max_len:
        r = r[:max_len] + "…"
    return r
