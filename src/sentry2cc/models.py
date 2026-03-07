"""
Pydantic data models for Sentry issues, events, and Claude Code agent results.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sentry Models
# ---------------------------------------------------------------------------


class SentryProject(BaseModel):
    id: str
    name: str
    slug: str


class SentryIssueMetadata(BaseModel):
    title: str | None = None
    type: str | None = None
    value: str | None = None
    filename: str | None = None
    function: str | None = None

    model_config = {"extra": "allow"}


class SentryIssue(BaseModel):
    """Core issue (group) data returned by the Sentry issues endpoints."""

    id: str
    short_id: str = Field(alias="shortId")
    title: str
    culprit: str | None = None
    level: str
    status: str
    first_seen: datetime = Field(alias="firstSeen")
    last_seen: datetime = Field(alias="lastSeen")
    count: str  # Sentry returns event count as a string
    user_count: int = Field(alias="userCount", default=0)
    num_comments: int = Field(alias="numComments", default=0)
    permalink: str
    is_bookmarked: bool = Field(alias="isBookmarked", default=False)
    is_public: bool = Field(alias="isPublic", default=False)
    has_seen: bool = Field(alias="hasSeen", default=False)
    metadata: SentryIssueMetadata = Field(default_factory=SentryIssueMetadata)
    project: SentryProject
    annotations: list[str] = Field(default_factory=list)
    type: str = "default"

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def event_count(self) -> int:
        try:
            return int(self.count)
        except (ValueError, TypeError):
            return 0


class StackFrame(BaseModel):
    """A single frame in a stacktrace."""

    filename: str | None = None
    abs_path: str | None = Field(None, alias="absPath")
    module: str | None = None
    function: str | None = None
    line_no: int | None = Field(None, alias="lineNo")
    col_no: int | None = Field(None, alias="colNo")
    in_app: bool = Field(False, alias="inApp")
    context: list[list[Any]] = Field(default_factory=list)
    vars: dict[str, Any] | None = None

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def source_context(self) -> str:
        """Return formatted source context around the error line."""
        if not self.context:
            return ""
        lines = []
        for line_num, code in self.context:
            marker = ">" if line_num == self.line_no else " "
            lines.append(f"{marker} {line_num:4d} | {code}")
        return "\n".join(lines)


class Stacktrace(BaseModel):
    frames: list[StackFrame] = Field(default_factory=list)
    frames_omitted: list[int] | None = Field(None, alias="framesOmitted")
    has_system_frames: bool = Field(False, alias="hasSystemFrames")

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def app_frames(self) -> list[StackFrame]:
        """Return only in-app frames (most relevant to the user's code)."""
        in_app = [f for f in self.frames if f.in_app]
        return in_app if in_app else self.frames


class ExceptionValue(BaseModel):
    type: str | None = None
    value: str | None = None
    module: str | None = None
    stacktrace: Stacktrace | None = None
    mechanism: dict[str, Any] | None = None

    model_config = {"populate_by_name": True, "extra": "allow"}


class ExceptionEntry(BaseModel):
    type: str = "exception"
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}

    @property
    def values(self) -> list[ExceptionValue]:
        raw = self.data.get("values", [])
        return [ExceptionValue.model_validate(v) for v in raw]


class BreadcrumbEntry(BaseModel):
    type: str = "breadcrumbs"
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}

    @property
    def values(self) -> list[dict[str, Any]]:
        return self.data.get("values", [])


class RequestEntry(BaseModel):
    type: str = "request"
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class SentryTag(BaseModel):
    key: str
    value: str


class SentryEvent(BaseModel):
    """Detailed event data (the actual error occurrence)."""

    id: str
    event_id: str = Field(alias="eventID")
    group_id: str = Field(alias="groupID")
    title: str
    message: str = ""
    platform: str | None = None
    date_created: datetime = Field(alias="dateCreated")
    date_received: datetime | None = Field(None, alias="dateReceived")
    tags: list[SentryTag] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    contexts: dict[str, Any] = Field(default_factory=dict)
    user: dict[str, Any] | None = None
    sdk: dict[str, Any] | None = None
    release: dict[str, Any] | None = None
    culprit: str | None = None
    location: str | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def exception_entries(self) -> list[ExceptionEntry]:
        return [
            ExceptionEntry.model_validate(e)
            for e in self.entries
            if e.get("type") == "exception"
        ]

    @property
    def breadcrumb_entries(self) -> list[BreadcrumbEntry]:
        return [
            BreadcrumbEntry.model_validate(e)
            for e in self.entries
            if e.get("type") == "breadcrumbs"
        ]

    @property
    def request_entry(self) -> RequestEntry | None:
        for e in self.entries:
            if e.get("type") == "request":
                return RequestEntry.model_validate(e)
        return None

    def get_tag(self, key: str) -> str | None:
        for tag in self.tags:
            if tag.key == key:
                return tag.value
        return None

    @property
    def environment(self) -> str | None:
        return self.get_tag("environment")


# ---------------------------------------------------------------------------
# Agent Result Models
# ---------------------------------------------------------------------------


class AgentResult(BaseModel):
    """Result of a Claude Code Agent execution."""

    session_id: str
    is_error: bool
    num_turns: int
    duration_ms: int
    duration_api_ms: int
    total_cost_usd: float | None = None
    result: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None

    @property
    def success(self) -> bool:
        return not self.is_error
