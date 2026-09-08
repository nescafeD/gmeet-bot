"""HTTP request and response models for meeting endpoints.

These are the public API contract. Field names use the camelCase the existing
callers already send; internal code uses snake_case and the translation happens
at this boundary via aliases.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import CamelCaseModel


class JoinMeetingRequest(CamelCaseModel):
    """Ask the bot to join a meeting."""

    meeting_url: str = Field(
        ...,
        alias="meetingUrl",
        min_length=1,
        description="Full meeting URL, e.g. https://meet.google.com/abc-defg-hij",
    )
    meeting_id: str | None = Field(
        default=None,
        alias="meetingId",
        description=(
            "Optional caller-assigned identifier. Omit it and one is minted from "
            "the meeting code. Either way the id in the response — not the one "
            "sent here — is what all later calls must address the session by: a "
            "rejoin of a meeting already in progress is given a suffixed id "
            "rather than being rejected."
        ),
    )
    user_name: str | None = Field(default=None, alias="userName")
    user_email: str | None = Field(default=None, alias="userEmail")
    project_id: str | None = Field(default=None, alias="projectId")
    project_name: str | None = Field(default=None, alias="projectName")
    meeting_title: str | None = Field(default=None, alias="meetingTitle")

    @field_validator("meeting_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate.startswith(("http://", "https://")):
            raise ValueError("meetingUrl must be an absolute http(s) URL")
        return candidate

    @field_validator("meeting_id", mode="before")
    @classmethod
    def _normalise_meeting_id(cls, value: object) -> str | None:
        """Blank is 'not supplied', not an error — the server mints one instead.

        Coerced with ``str`` because a caller sending a numeric id would
        otherwise key the session under an int and never match its own string
        on the calls that follow.
        """
        if value is None:
            return None
        return str(value).strip() or None


class MeetingActionRequest(CamelCaseModel):
    """Address an existing session."""

    meeting_id: str = Field(..., alias="meetingId", min_length=1)


class PlayAudioRequest(CamelCaseModel):
    """Play audio into the meeting through the bot's virtual microphone."""

    meeting_id: str = Field(..., alias="meetingId", min_length=1)
    audio_url: str = Field(..., alias="audioUrl", min_length=1, description="Audio stream or data URL.")
    volume: float = Field(default=0.7, ge=0.0, le=1.0)


class JoinMeetingResponse(CamelCaseModel):
    """Accepted a join request. Joining continues in the background."""

    message: str
    meeting_id: str = Field(..., alias="meetingId")
    session_id: str = Field(..., alias="sessionId")


class MeetingActionResponse(CamelCaseModel):
    """Generic acknowledgement for a meeting action."""

    message: str
    meeting_id: str = Field(..., alias="meetingId")


class ParticipantSummary(CamelCaseModel):
    """Who the bot can see in the meeting."""

    count: int
    names: list[str] = Field(default_factory=list)


class MeetingStateResponse(BaseModel):
    """Persisted state for a meeting, as stored by the state repository."""

    model_config = ConfigDict(populate_by_name=True)

    meeting_id: str = Field(..., alias="meetingId")
    state: dict

    def model_dump_api(self) -> dict:
        return self.model_dump(by_alias=True)


class MeetingStateHistoryResponse(BaseModel):
    """Ordered history of state transitions, newest first."""

    model_config = ConfigDict(populate_by_name=True)

    meeting_id: str = Field(..., alias="meetingId")
    history: list[dict]
    count: int
