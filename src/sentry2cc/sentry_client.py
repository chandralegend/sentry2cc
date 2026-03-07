"""
Async Sentry REST API client.

Wraps the key endpoints needed for polling issues and fetching event details.
Uses httpx for async HTTP with automatic retries on transient errors.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from sentry2cc.models import SentryEvent, SentryIssue

logger = logging.getLogger(__name__)

# Sentry REST API v0 base path
_API_BASE = "/api/0"

# HTTP status codes that are safe to retry
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Default timeouts (seconds)
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0


class SentryAPIError(Exception):
    """Raised when the Sentry API returns a non-success response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Sentry API error {status_code}: {detail}")


class SentryClient:
    """
    Async client for the Sentry REST API.

    Usage
    -----
    Use as an async context manager:

        async with SentryClient(auth_token=..., organization=...) as client:
            issues = await client.list_issues()

    Or manage the lifecycle manually:

        client = SentryClient(...)
        await client.__aenter__()
        ...
        await client.__aexit__(None, None, None)
    """

    def __init__(
        self,
        auth_token: str,
        organization: str,
        project: str,
        base_url: str = "https://sentry.io",
    ) -> None:
        self._auth_token = auth_token
        self._organization = organization
        self._project = project
        self._base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> SentryClient:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._auth_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT,
                read=_READ_TIMEOUT,
                write=10.0,
                pool=10.0,
            ),
            follow_redirects=True,
        )
        logger.debug(
            "SentryClient opened (org=%s, project=%s)",
            self._organization,
            self._project,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.debug("SentryClient closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "SentryClient is not open. Use it as an async context manager."
            )
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated API request and return parsed JSON."""
        client = self._require_client()
        url = f"{_API_BASE}{path}"
        logger.debug("%s %s", method.upper(), url)

        response = await client.request(method, url, **kwargs)

        if not response.is_success:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise SentryAPIError(response.status_code, str(detail))

        # 204 No Content
        if response.status_code == 204:
            return None

        return response.json()

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def list_issues(
        self,
        query: str = "is:unresolved",
        cursor: str | None = None,
        limit: int = 25,
        stats_period: str = "24h",
    ) -> tuple[list[SentryIssue], str | None]:
        """
        List issues (groups) for the configured project.

        Parameters
        ----------
        query:
            Sentry structured search query.
        cursor:
            Pagination cursor from a previous response.
        limit:
            Maximum number of issues to return per page.
        stats_period:
            Stats time window to include ("24h", "14d", or "" to disable).

        Returns
        -------
        tuple[list[SentryIssue], str | None]
            A list of issues and the next cursor (or None if no more pages).
        """
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "statsPeriod": stats_period,
        }
        if cursor:
            params["cursor"] = cursor

        client = self._require_client()
        url = f"{_API_BASE}/projects/{self._organization}/{self._project}/issues/"
        logger.debug("GET %s params=%s", url, params)

        response = await client.get(url, params=params)
        if not response.is_success:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise SentryAPIError(response.status_code, str(detail))

        data = response.json()
        issues = [SentryIssue.model_validate(item) for item in data]

        # Extract next cursor from Link header
        next_cursor = _parse_next_cursor(response.headers.get("Link", ""))

        logger.debug("Fetched %d issues (next_cursor=%s)", len(issues), next_cursor)
        return issues, next_cursor

    async def get_issue(self, issue_id: str) -> SentryIssue:
        """
        Retrieve full details for a single issue.

        Parameters
        ----------
        issue_id:
            The Sentry issue ID.
        """
        data = await self._request(
            "GET",
            f"/organizations/{self._organization}/issues/{issue_id}/",
        )
        return SentryIssue.model_validate(data)

    async def update_issue(self, issue_id: str, payload: dict[str, Any]) -> SentryIssue:
        """
        Update issue fields (e.g. status, assignedTo).

        Parameters
        ----------
        issue_id:
            The Sentry issue ID.
        payload:
            Dict of fields to update. Common keys: ``status``, ``assignedTo``,
            ``isBookmarked``, ``isSubscribed``.
        """
        data = await self._request(
            "PUT",
            f"/organizations/{self._organization}/issues/{issue_id}/",
            json=payload,
        )
        return SentryIssue.model_validate(data)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def get_latest_event(
        self,
        issue_id: str,
        environment: str | None = None,
    ) -> SentryEvent:
        """
        Retrieve the most recent event for an issue.

        Parameters
        ----------
        issue_id:
            The Sentry issue ID.
        environment:
            Optional environment filter (e.g. "production").
        """
        params: dict[str, Any] = {}
        if environment:
            params["environment"] = environment

        data = await self._request(
            "GET",
            f"/organizations/{self._organization}/issues/{issue_id}/events/latest/",
            params=params if params else None,
        )
        return SentryEvent.model_validate(data)

    async def get_oldest_event(
        self,
        issue_id: str,
        environment: str | None = None,
    ) -> SentryEvent:
        """Retrieve the oldest (first) event for an issue."""
        params: dict[str, Any] = {}
        if environment:
            params["environment"] = environment

        data = await self._request(
            "GET",
            f"/organizations/{self._organization}/issues/{issue_id}/events/oldest/",
            params=params if params else None,
        )
        return SentryEvent.model_validate(data)

    async def get_event(
        self,
        issue_id: str,
        event_id: str,
        environment: str | None = None,
    ) -> SentryEvent:
        """
        Retrieve a specific event by ID, or use ``"latest"`` / ``"oldest"``
        / ``"recommended"`` as the event_id.
        """
        params: dict[str, Any] = {}
        if environment:
            params["environment"] = environment

        data = await self._request(
            "GET",
            f"/organizations/{self._organization}/issues/{issue_id}/events/{event_id}/",
            params=params if params else None,
        )
        return SentryEvent.model_validate(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_next_cursor(link_header: str) -> str | None:
    """
    Extract the 'next' cursor value from a Sentry Link header.

    Sentry pagination uses the standard Link header format:
        <url>; rel="next"; results="true"; cursor="..."
    """
    if not link_header:
        return None

    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' not in part:
            continue
        # Check results="true" — if false, there are no more results
        if 'results="false"' in part:
            return None
        # Extract cursor value
        for segment in part.split(";"):
            segment = segment.strip()
            if segment.startswith("cursor="):
                return segment[len("cursor=") :].strip('"')
    return None
