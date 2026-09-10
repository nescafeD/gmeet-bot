"""Creative Workspace backend client.

This is the boundary to CW, not a utility module. Everything the bot needs from
CW is expressed here as a method with typed arguments and a typed result; no
other package builds a CW URL or knows a CW payload shape.

Responsibilities:
  * Issue resumable-upload URLs for meeting recordings.
  * Upload finished files directly (transcripts, converted audio).
  * Confirm an upload so CW begins processing.
  * Request meeting-mode artifact generation from an uploaded recording.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.base import BaseHTTPClient
from app.core.config import CWUtilsSettings
from app.core.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResumableUploadTarget:
    """Where a recording is uploaded and where it will be readable afterwards."""

    upload_url: str
    public_url: str

    @property
    def filename(self) -> str:
        return self.public_url.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """One file registered with CW."""

    filename: str
    url: str

    def as_payload(self) -> dict[str, str]:
        return {"filename": self.filename, "url": self.url}


class CWUtilsClient(BaseHTTPClient):
    """Client for the CW backend."""

    service_name = "cw-utils"

    _ENDPOINT_SIGNED_URLS = "/backend/gcs/generate-upload-signed-urls"
    _ENDPOINT_UPLOAD_FILES = "/backend/gcs/upload-files/"
    _ENDPOINT_CONFIRM_UPLOAD = "/backend/gcs/confirm-upload"
    _ENDPOINT_MEETING_ARTIFACT = "/backend/meeting_mode_artifact/gen_mm_artifact_latest_bg"
    _ENDPOINT_STARTUP_MH_SERVICES = "/backend/meeting_mode_artifact/startup_mh_services"

    def __init__(self, settings: CWUtilsSettings) -> None:
        super().__init__(
            settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )
        self._settings = settings

    # ------------------------------------------------------------------
    # Resumable uploads
    # ------------------------------------------------------------------

    async def create_resumable_upload(
        self,
        *,
        project_id: str,
        filename: str,
        content_type: str,
    ) -> ResumableUploadTarget:
        """Reserve an object and return its resumable upload URL.

        Called once per recording, when the first chunk arrives.

        Raises:
            ExternalServiceError: If CW declines or returns an unusable response.
        """
        payload = {
            "project_id": project_id,
            "filenames": [filename],
            "resumable": True,
            "content_type": content_type,
        }

        body = await self.post_json(
            self._ENDPOINT_SIGNED_URLS,
            operation="create_resumable_upload",
            json=payload,
        )

        files = body.get("files") or []
        if not files:
            raise ExternalServiceError(
                self.service_name,
                "signed-url response contained no files",
                details={"project_id": project_id, "filename": filename},
            )

        entry = files[0]
        if entry.get("status") != "ready":
            raise ExternalServiceError(
                self.service_name,
                f"signed URL not ready (status={entry.get('status')!r})",
                details={"project_id": project_id, "filename": filename},
            )

        signed_url = entry.get("signed_url")
        public_url = entry.get("public_url")
        if not signed_url or not public_url:
            raise ExternalServiceError(
                self.service_name,
                "signed-url response was missing signed_url or public_url",
                details={"project_id": project_id, "filename": filename},
            )

        logger.info(
            "Resumable upload target created",
            extra={"project_id": project_id, "object_name": filename, "content_type": content_type},
        )
        return ResumableUploadTarget(upload_url=signed_url, public_url=public_url)

    # ------------------------------------------------------------------
    # Whole-file upload
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        *,
        file_path: Path,
        project_id: str,
        display_name: str | None = None,
        is_ai: bool = True,
    ) -> dict[str, Any]:
        """Upload a complete local file in one request.

        For artefacts small enough not to need the resumable protocol — an
        exported transcript, a converted audio file. Recordings do not come
        through here: they are streamed while the meeting runs, because holding
        a whole meeting's audio in memory to upload at the end is what the
        streaming pipeline exists to avoid.

        The file is read in a worker thread. Reading it inline would block the
        event loop, and this process is concurrently polling captions and
        receiving audio chunks from the page — both of which stall if it does.

        Raises:
            FileNotFoundError: If the path does not exist or is not a file.
            ExternalServiceError: If CW rejects the upload.
        """
        path = Path(file_path)
        payload = await asyncio.to_thread(_read_file, path)

        form: dict[str, Any] = {"project_id": project_id, "isAI": is_ai}
        if display_name:
            form["displayName"] = display_name

        response = await self.request(
            "PUT",
            self._ENDPOINT_UPLOAD_FILES,
            operation="upload_file",
            data=form,
            files={"files": (path.name, payload)},
        )

        result = response.json() if response.content else {}
        logger.info(
            "File uploaded",
            extra={
                "project_id": project_id,
                "object_name": path.name,
                "size_bytes": len(payload),
                "uploaded_count": len(result.get("uploaded", [])),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Post-upload processing
    # ------------------------------------------------------------------

    async def confirm_upload(
        self,
        *,
        project_id: str,
        files: list[UploadedFile],
        is_ai: bool = False,
        display_name: str = "",
        user_name: str = "",
        user_email: str = "",
        auto_ingest: bool = True,
    ) -> dict[str, Any]:
        """Tell CW an upload is complete so it can begin processing.

        Raises:
            ValueError: If ``project_id`` or ``files`` is empty.
            ExternalServiceError: If CW rejects the confirmation.
        """
        if not project_id or not files:
            raise ValueError("project_id and at least one file are required to confirm an upload")

        payload: dict[str, Any] = {
            "project_id": project_id,
            "files": [item.as_payload() for item in files],
            "isAI": is_ai,
            "auto_ingest": auto_ingest,
        }
        if user_name:
            payload["artifactUserName"] = user_name
        if user_email:
            payload["artifactUserEmail"] = user_email
        if display_name:
            payload["displayName"] = display_name

        result = await self.post_json(
            self._ENDPOINT_CONFIRM_UPLOAD,
            operation="confirm_upload",
            json=payload,
            params={"auto_ingest": "true" if auto_ingest else "false"},
        )

        logger.info(
            "Upload confirmed",
            extra={
                "project_id": project_id,
                "file_count": len(files),
                "auto_ingest": auto_ingest,
            },
        )
        return result

    async def generate_meeting_artifact(
        self,
        *,
        audio_name: str,
        project_id: str,
        display_name: str,
        user_email: str,
        user_name: str,
        model_type: str | None = None,
        large_language_model: str | None = None,
        request_id: str | None = None,
        incremental_mh_metadata: dict[str, Any] | None = None,
        mode_ids: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, Any]:
        """Ask CW to derive a meeting artifact (summary, highlights) from a recording.

        Args:
            incremental_mh_metadata: Set when this request covers one segment of a
                meeting rather than a whole recording. It tells CW where the
                segment sits in the meeting, so several artifacts from the same
                meeting can be ordered and the last one recognised as the end.
                Omitted entirely for a whole-recording request, which keeps the
                payload unchanged for callers that do not segment.

        Raises:
            ValueError: If any required identifier is missing.
            ExternalServiceError: If CW rejects the request.
        """
        required = {
            "audio_name": audio_name,
            "project_id": project_id,
            "display_name": display_name,
            "user_email": user_email,
            "user_name": user_name,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing required fields for artifact generation: {', '.join(missing)}")
        payload: dict[str, Any] = {
            **required,
            "model_type": model_type or self._settings.artifact_model_type,
            "large_language_model": large_language_model or self._settings.artifact_llm,
            "request_id": request_id or str(uuid.uuid4()),
        }

        premade_artifact_id: str | None = None
        segment_number = (
            incremental_mh_metadata.get("segment_number")
            if incremental_mh_metadata is not None
            else None
        )
        if segment_number in (None, 1):
            premade_artifact_id = str(uuid.uuid4())
            payload["premade_artifact_id"] = premade_artifact_id
            logger.info(
                "Premade artifact ID created for first recording segment",
                extra={
                    "premade_artifact_id": premade_artifact_id,
                    "project_id": project_id,
                    "audio_name": audio_name,
                },
            )

        if incremental_mh_metadata is not None:
            payload["incremental_mh_metadata"] = incremental_mh_metadata
        if mode_ids:
            payload["selected_modes"] = list(mode_ids)

        result = await self.post_json(
            self._ENDPOINT_MEETING_ARTIFACT,
            operation="generate_meeting_artifact",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if premade_artifact_id is not None:
            # The upstream response does not have to echo a caller-provided ID,
            # but the recording finalizer needs it to build the email link.
            result = {**result, "premade_artifact_id": premade_artifact_id}

        logger.info(
            "Meeting artifact generation requested",
            extra={
                "request_id": payload["request_id"],
                "project_id": project_id,
                "audio_name": audio_name,
            },
        )
        return result

    def project_url(self, project_id: str) -> str:
        """Deep link to a project in the CW UI."""
        return self._settings.project_url_template.format(project_id=project_id)

    def transcription_url(self, artifact_id: str) -> str:
        """Deep link to a premade artifact in Transcription Mode."""
        return self._settings.transcription_url_template.format(artifact_id=artifact_id)

    # ------------------------------------------------------------------
    # MH services startup
    # ------------------------------------------------------------------

    def startup_mh_services(self) -> None:
        """Start MH services in the background without blocking the caller.

        Fire-and-forget operation that sends a simple POST request.
        """
        asyncio.create_task(self._startup_mh_services_request())

    async def _startup_mh_services_request(self) -> None:
        """Internal coroutine to request MH services startup."""
        try:
            await self.get_json(
                self._ENDPOINT_STARTUP_MH_SERVICES,
                operation="startup_mh_services",
            )
            logger.info("MH services startup requested")
        except Exception:
            logger.exception("Failed to start MH services")


def _read_file(path: Path) -> bytes:
    """Read a file's bytes. Runs in a worker thread; see :meth:`CWUtilsClient.upload_file`.

    Raises:
        FileNotFoundError: If the path is missing or is not a regular file.
    """
    if not path.is_file():
        raise FileNotFoundError(f"not a readable file: {path}")
    return path.read_bytes()
