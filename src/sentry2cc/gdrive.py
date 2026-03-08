"""
Google Drive integration for sentry2cc.

Supports two authentication modes:

1. **API key** (read-only, for public folders):
   - Can check if a folder exists under a public parent folder.
   - Cannot upload files (Drive API requires OAuth or service account for writes).

2. **OAuth2 user credentials** (read + write):
   - Full access: folder checks, creation, and recursive directory upload.
   - First run opens a browser for the consent flow; token is stored locally.

The client auto-detects which mode to use based on what credentials are provided.
If only an API key is given, upload calls will log a warning and no-op.

Usage
-----
    # API key only (read-only dedup check):
    client = GoogleDriveClient(api_key="AIzaSy...")
    exists = client.folder_exists("12DggA5sG1Q...", "WOVAR-BACKEND-2SW")

    # OAuth2 (full access):
    client = GoogleDriveClient(
        credentials_file="~/.sentry2cc/gdrive_credentials.json",
        token_file="~/.sentry2cc/gdrive_token.json",
    )
    client.upload_directory("/path/to/WOVAR-BACKEND-2SW", "12DggA5sG1Q...", "WOVAR-BACKEND-2SW")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from loguru import logger

# OAuth2 scopes needed for full Drive access (folder creation + file upload)
_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Drive API v3 base URL
_DRIVE_API = "https://www.googleapis.com/drive/v3"
_DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


class GoogleDriveClient:
    """
    Google Drive API v3 client supporting API key (read-only) and OAuth2 (read/write).

    Parameters
    ----------
    api_key:
        Google Cloud API key. Sufficient for listing/checking public folders.
        Cannot be used for uploads.
    credentials_file:
        Path to OAuth2 client secrets JSON (Desktop app type) from Google Cloud
        Console. Required for uploads.
    token_file:
        Path where the OAuth2 token is persisted after first browser auth.
        Defaults to credentials_file parent / "gdrive_token.json".
    """

    def __init__(
        self,
        api_key: str | None = None,
        credentials_file: str | Path | None = None,
        token_file: str | Path | None = None,
    ) -> None:
        self._api_key = api_key
        self._credentials_file = (
            Path(credentials_file).expanduser().resolve() if credentials_file else None
        )
        if token_file:
            self._token_file = Path(token_file).expanduser().resolve()
        elif self._credentials_file:
            self._token_file = self._credentials_file.parent / "gdrive_token.json"
        else:
            self._token_file = None

        self._oauth_service: Any = None  # googleapiclient resource, lazily initialized
        self._http = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _get_oauth_service(self) -> Any:
        """
        Return an authenticated googleapiclient Drive v3 service.
        Performs browser OAuth2 flow on first call; refreshes token on subsequent calls.

        Raises
        ------
        RuntimeError
            If credentials_file is not configured.
        """
        if self._oauth_service is not None:
            return self._oauth_service

        if self._credentials_file is None:
            raise RuntimeError(
                "OAuth2 credentials_file is required for Drive write operations. "
                "Either provide credentials_file or use API key for read-only access."
            )

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise ImportError(
                "Google API libraries are required. "
                "Install: uv add google-auth google-auth-oauthlib google-api-python-client"
            ) from exc

        if not self._credentials_file.exists():
            raise FileNotFoundError(
                f"Google OAuth2 credentials file not found: {self._credentials_file}\n"
                "Download from: Google Cloud Console → APIs & Services → "
                "Credentials → OAuth 2.0 Client IDs → Download JSON"
            )

        creds = None
        if self._token_file and self._token_file.exists():
            logger.debug("Loading Drive OAuth token from {}", self._token_file)
            creds = Credentials.from_authorized_user_file(
                str(self._token_file), _SCOPES
            )

        if creds is None or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.debug("Refreshing expired Drive OAuth token")
                creds.refresh(Request())
            else:
                logger.info(
                    "Starting Google Drive OAuth2 browser flow — "
                    "a browser window will open for authorization."
                )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._credentials_file), _SCOPES
                )
                creds = flow.run_local_server(port=0)

            if self._token_file:
                self._token_file.parent.mkdir(parents=True, exist_ok=True)
                self._token_file.write_text(creds.to_json())
                logger.debug("Drive OAuth token saved to {}", self._token_file)

        self._oauth_service = build("drive", "v3", credentials=creds)
        logger.debug("Google Drive OAuth2 service ready")
        return self._oauth_service

    # ------------------------------------------------------------------
    # Folder existence check (API key — works on public folders)
    # ------------------------------------------------------------------

    def folder_exists(self, parent_id: str, name: str) -> bool:
        """
        Return True if a folder named ``name`` exists directly under ``parent_id``.

        Uses the API key if available (works for public folders), otherwise
        falls back to the OAuth2 service.

        Parameters
        ----------
        parent_id:
            Drive folder ID of the parent.
        name:
            Exact folder name to search for.
        """
        if self._api_key:
            return self._folder_exists_api_key(parent_id, name)
        else:
            return self._folder_exists_oauth(parent_id, name)

    def _folder_exists_api_key(self, parent_id: str, name: str) -> bool:
        """Check folder existence using the API key (no OAuth required)."""
        params = {
            "q": (
                f"'{parent_id}' in parents "
                f"and mimeType='application/vnd.google-apps.folder' "
                f"and name='{_escape(name)}' "
                f"and trashed=false"
            ),
            "fields": "files(id,name)",
            "pageSize": "1",
            "key": self._api_key,
        }
        resp = self._http.get(f"{_DRIVE_API}/files", params=params)
        if not resp.is_success:
            logger.warning(
                "Drive API key folder check failed (status={}): {}",
                resp.status_code,
                resp.text[:200],
            )
            return False
        files = resp.json().get("files", [])
        found = len(files) > 0
        logger.debug(
            "Drive folder '{}' under {}: {} (API key)",
            name,
            parent_id,
            "found" if found else "not found",
        )
        return found

    def _folder_exists_oauth(self, parent_id: str, name: str) -> bool:
        """Check folder existence using OAuth2."""
        svc = self._get_oauth_service()
        results = (
            svc.files()
            .list(
                q=(
                    f"'{parent_id}' in parents "
                    f"and mimeType='application/vnd.google-apps.folder' "
                    f"and name='{_escape(name)}' "
                    f"and trashed=false"
                ),
                fields="files(id,name)",
                pageSize=1,
            )
            .execute()
        )
        files = results.get("files", [])
        found = len(files) > 0
        logger.debug(
            "Drive folder '{}' under {}: {} (OAuth2)",
            name,
            parent_id,
            "found" if found else "not found",
        )
        return found

    # ------------------------------------------------------------------
    # Folder creation and file upload (OAuth2 required)
    # ------------------------------------------------------------------

    def get_or_create_folder(self, parent_id: str, name: str) -> str:
        """
        Return the Drive folder ID for ``name`` under ``parent_id``,
        creating it if it does not already exist. Requires OAuth2.
        """
        svc = self._get_oauth_service()
        results = (
            svc.files()
            .list(
                q=(
                    f"'{parent_id}' in parents "
                    f"and mimeType='application/vnd.google-apps.folder' "
                    f"and name='{_escape(name)}' "
                    f"and trashed=false"
                ),
                fields="files(id,name)",
                pageSize=1,
            )
            .execute()
        )
        files = results.get("files", [])
        if files:
            folder_id = files[0]["id"]
            logger.debug("Reusing existing Drive folder '{}' (id={})", name, folder_id)
            return folder_id

        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = svc.files().create(body=metadata, fields="id").execute()
        folder_id = folder["id"]
        logger.info(
            "Created Drive folder '{}' (id={}) under parent {}",
            name,
            folder_id,
            parent_id,
        )
        return folder_id

    def upload_file(self, local_path: str | Path, parent_id: str) -> str:
        """
        Upload a single file to Drive. Requires OAuth2.

        Returns the Drive file ID of the uploaded file.
        """
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise ImportError("google-api-python-client is required") from exc

        local_path = Path(local_path)
        svc = self._get_oauth_service()
        metadata = {"name": local_path.name, "parents": [parent_id]}
        media = MediaFileUpload(
            str(local_path),
            mimetype=_mime_for(local_path),
            resumable=False,
        )
        result = (
            svc.files().create(body=metadata, media_body=media, fields="id").execute()
        )
        file_id = result["id"]
        logger.debug("Uploaded '{}' → Drive id={}", local_path.name, file_id)
        return file_id

    def upload_directory(
        self,
        local_dir: str | Path,
        parent_id: str,
        folder_name: str | None = None,
    ) -> str:
        """
        Recursively upload a local directory to Google Drive. Requires OAuth2.

        Creates a top-level folder under ``parent_id`` and uploads all files
        and sub-directories recursively.

        Returns the Drive folder ID of the created top-level folder.
        """
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            raise ValueError(f"Not a directory: {local_dir}")

        name = folder_name or local_dir.name
        root_id = self.get_or_create_folder(parent_id, name)
        self._upload_dir_contents(local_dir, root_id)

        file_count = sum(1 for f in local_dir.rglob("*") if f.is_file())
        logger.info(
            "Uploaded directory '{}' ({} files) to Drive folder id={}",
            local_dir,
            file_count,
            root_id,
        )
        return root_id

    def _upload_dir_contents(self, local_dir: Path, drive_parent_id: str) -> None:
        for entry in sorted(local_dir.iterdir()):
            if entry.is_dir():
                sub_id = self.get_or_create_folder(drive_parent_id, entry.name)
                self._upload_dir_contents(entry, sub_id)
            elif entry.is_file():
                self.upload_file(entry, drive_parent_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape(name: str) -> str:
    """Escape single quotes in a Drive query string value."""
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _mime_for(path: Path) -> str:
    """Return a sensible MIME type based on file extension."""
    _MAP = {
        ".md": "text/markdown",
        ".json": "application/json",
        ".txt": "text/plain",
        ".py": "text/x-python",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".html": "text/html",
        ".js": "text/javascript",
        ".ts": "text/typescript",
        ".css": "text/css",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".pdf": "application/pdf",
    }
    return _MAP.get(path.suffix.lower(), "application/octet-stream")
