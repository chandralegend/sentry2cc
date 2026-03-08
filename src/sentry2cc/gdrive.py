"""
Google Drive integration for sentry2cc.

Provides OAuth2 user-credential authentication (browser-based, stored token),
folder existence checks, folder creation, and recursive directory upload.

Usage
-----
    from sentry2cc.gdrive import GoogleDriveClient

    client = GoogleDriveClient(
        credentials_file="~/.sentry2cc/gdrive_credentials.json",
        token_file="~/.sentry2cc/gdrive_token.json",
    )

    # Check if a folder named "WOVAR-BACKEND-2SW" exists under the target Drive folder
    exists = client.folder_exists(parent_id="12DggA5sG1Q...", name="WOVAR-BACKEND-2SW")

    # Upload a local directory to Drive under the target folder
    client.upload_directory(
        local_dir="/path/to/.idea/Sentry/WOVAR-BACKEND-2SW",
        parent_id="12DggA5sG1Q...",
        folder_name="WOVAR-BACKEND-2SW",
    )
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# OAuth2 scopes — drive.file allows creating/reading files this app created;
# use drive scope if you need to read folders created by other apps/humans.
_SCOPES = ["https://www.googleapis.com/auth/drive"]


class GoogleDriveClient:
    """
    Thin wrapper around the Google Drive API v3.

    Authentication uses OAuth2 user credentials (browser-based first-run,
    then token refresh from a stored JSON file on subsequent runs).

    Parameters
    ----------
    credentials_file:
        Path to the OAuth2 client secrets JSON downloaded from Google Cloud
        Console (type: "Desktop app").
    token_file:
        Path where the obtained OAuth2 token is persisted between runs.
        Created automatically after the first successful auth.
    """

    def __init__(
        self,
        credentials_file: str | Path,
        token_file: str | Path,
    ) -> None:
        self._credentials_file = Path(credentials_file).expanduser().resolve()
        self._token_file = Path(token_file).expanduser().resolve()
        self._service: Any = None  # googleapiclient.discovery.Resource

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Authenticate with Google Drive.

        On first run, opens a browser for the OAuth2 consent flow and saves
        the resulting token to ``token_file``. On subsequent runs, loads and
        refreshes the token automatically.

        Raises
        ------
        FileNotFoundError
            If ``credentials_file`` does not exist.
        """
        # Defer heavy imports so the rest of sentry2cc still works if google
        # libraries are not installed.
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise ImportError(
                "Google API libraries are required for Drive integration. "
                "Install them with: uv add google-auth google-auth-oauthlib google-api-python-client"
            ) from exc

        if not self._credentials_file.exists():
            raise FileNotFoundError(
                f"Google OAuth2 credentials file not found: {self._credentials_file}\n"
                "Download it from Google Cloud Console → APIs & Services → Credentials "
                "→ OAuth 2.0 Client IDs → Download JSON."
            )

        creds: Credentials | None = None

        # Load existing token if available
        if self._token_file.exists():
            logger.debug("Loading Drive token from %s", self._token_file)
            creds = Credentials.from_authorized_user_file(
                str(self._token_file), _SCOPES
            )

        # Refresh or re-authenticate as needed
        if creds is None or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.debug("Refreshing expired Drive token")
                creds.refresh(Request())
            else:
                logger.info(
                    "No valid Drive token found — starting OAuth2 browser flow. "
                    "A browser window will open to authorize access."
                )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._credentials_file), _SCOPES
                )
                creds = flow.run_local_server(port=0)

            # Persist token for next run
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(creds.to_json())
            logger.debug("Drive token saved to %s", self._token_file)

        self._service = build("drive", "v3", credentials=creds)
        logger.debug("Google Drive API client ready")

    def _svc(self) -> Any:
        """Return the Drive service, authenticating if not already done."""
        if self._service is None:
            self.authenticate()
        return self._service

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def folder_exists(self, parent_id: str, name: str) -> bool:
        """
        Return True if a folder with ``name`` exists directly under ``parent_id``.

        Parameters
        ----------
        parent_id:
            Google Drive folder ID of the parent.
        name:
            Exact folder name to look for.
        """
        results = (
            self._svc()
            .files()
            .list(
                q=(
                    f"'{parent_id}' in parents "
                    f"and mimeType='application/vnd.google-apps.folder' "
                    f"and name='{_escape(name)}' "
                    f"and trashed=false"
                ),
                fields="files(id, name)",
                pageSize=1,
            )
            .execute()
        )
        files = results.get("files", [])
        if files:
            logger.debug(
                "Drive folder '%s' found under parent %s (id=%s)",
                name,
                parent_id,
                files[0]["id"],
            )
            return True
        logger.debug("Drive folder '%s' not found under parent %s", name, parent_id)
        return False

    def get_or_create_folder(self, parent_id: str, name: str) -> str:
        """
        Return the Drive folder ID for ``name`` under ``parent_id``,
        creating it if it does not exist.

        Parameters
        ----------
        parent_id:
            Google Drive folder ID of the parent.
        name:
            Folder name.

        Returns
        -------
        str
            Google Drive folder ID of the (possibly new) folder.
        """
        # Check if it already exists
        results = (
            self._svc()
            .files()
            .list(
                q=(
                    f"'{parent_id}' in parents "
                    f"and mimeType='application/vnd.google-apps.folder' "
                    f"and name='{_escape(name)}' "
                    f"and trashed=false"
                ),
                fields="files(id, name)",
                pageSize=1,
            )
            .execute()
        )
        files = results.get("files", [])
        if files:
            folder_id = files[0]["id"]
            logger.debug("Reusing existing Drive folder '%s' (id=%s)", name, folder_id)
            return folder_id

        # Create new folder
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = self._svc().files().create(body=metadata, fields="id").execute()
        folder_id = folder["id"]
        logger.info(
            "Created Drive folder '%s' (id=%s) under parent %s",
            name,
            folder_id,
            parent_id,
        )
        return folder_id

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: str | Path,
        parent_id: str,
        *,
        mime_type: str = "text/plain",
    ) -> str:
        """
        Upload a single file to Drive under ``parent_id``.

        Parameters
        ----------
        local_path:
            Path to the local file.
        parent_id:
            Drive folder ID where the file will be placed.
        mime_type:
            MIME type of the file. Defaults to text/plain.

        Returns
        -------
        str
            Drive file ID of the uploaded file.
        """
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise ImportError("google-api-python-client is required") from exc

        local_path = Path(local_path)
        # Guess MIME type from extension
        detected_mime = _mime_for(local_path)

        metadata = {
            "name": local_path.name,
            "parents": [parent_id],
        }
        media = MediaFileUpload(
            str(local_path),
            mimetype=detected_mime,
            resumable=False,
        )
        result = (
            self._svc()
            .files()
            .create(body=metadata, media_body=media, fields="id")
            .execute()
        )
        file_id = result["id"]
        logger.debug("Uploaded '%s' → Drive id=%s", local_path.name, file_id)
        return file_id

    def upload_directory(
        self,
        local_dir: str | Path,
        parent_id: str,
        folder_name: str | None = None,
    ) -> str:
        """
        Recursively upload a local directory to Google Drive.

        Creates a top-level folder named ``folder_name`` (or the directory's
        own name if not specified) under ``parent_id``, then uploads all files
        and sub-directories recursively.

        Parameters
        ----------
        local_dir:
            Local directory to upload.
        parent_id:
            Drive folder ID of the destination parent.
        folder_name:
            Name for the top-level folder in Drive. Defaults to local dir name.

        Returns
        -------
        str
            Drive folder ID of the created top-level folder.
        """
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            raise ValueError(f"Not a directory: {local_dir}")

        name = folder_name or local_dir.name
        root_id = self.get_or_create_folder(parent_id, name)

        self._upload_dir_contents(local_dir, root_id)
        logger.info(
            "Uploaded directory '%s' (%d files) to Drive folder id=%s",
            local_dir,
            sum(1 for _ in local_dir.rglob("*") if _.is_file()),
            root_id,
        )
        return root_id

    def _upload_dir_contents(self, local_dir: Path, drive_parent_id: str) -> None:
        """Recursively upload the contents of local_dir into drive_parent_id."""
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
