"""Meeting lifecycle endpoints.

Handlers are three lines by design: validate (done by the schema), call the
manager, shape the response. No orchestration, no error handling — errors are
translated centrally in :mod:`app.api.errors`.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import ManagerDep, SettingsDep
from app.meeting.models import MeetingRequest
from app.schemas.meeting import (
    JoinMeetingRequest,
    JoinMeetingResponse,
    MeetingActionRequest,
    MeetingActionResponse,
    PlayAudioRequest,
)

router = APIRouter(prefix="/meetings", tags=["Meeting"])


@router.post(
    "/join",
    response_model=JoinMeetingResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Join a meeting",
)
async def join_meeting(
    payload: JoinMeetingRequest,
    manager: ManagerDep,
    settings: SettingsDep,
) -> JoinMeetingResponse:
    """Send the bot into a meeting.

    Returns as soon as the session is registered. The bot may then wait several
    minutes in the lobby for a host to admit it, so progress is reported through
    ``GET /meetings/{meeting_id}/status`` rather than by blocking this call.

    ``meetingId`` in the response is the session's real id and may differ from
    the one sent: it is minted when the caller omits it, and suffixed when the
    caller's id is already in use. Address every later call by the returned id.
    """
    request = MeetingRequest.build(
        meeting_id=payload.meeting_id,
        meeting_url=payload.meeting_url,
        defaults=settings.project,
        user_name=payload.user_name,
        user_email=payload.user_email,
        project_id=payload.project_id,
        project_name=payload.project_name,
        meeting_title=payload.meeting_title,
    )
    session = await manager.join_meeting(request)

    return JoinMeetingResponse(
        message="Joining meeting",
        meeting_id=session.meeting_id,
        session_id=session.session_id,
    )


@router.post("/leave", response_model=MeetingActionResponse, summary="Leave a meeting")
async def leave_meeting(
    payload: MeetingActionRequest,
    manager: ManagerDep,
) -> MeetingActionResponse:
    """Leave the meeting and release the browser.

    Any recording in progress is finalized first, so calling this is the
    supported way to end a meeting cleanly.
    """
    await manager.leave_meeting(payload.meeting_id)
    return MeetingActionResponse(message="Left meeting", meeting_id=payload.meeting_id)


@router.post("/audio/play", response_model=MeetingActionResponse, summary="Play audio into a meeting")
async def play_audio(
    payload: PlayAudioRequest,
    manager: ManagerDep,
) -> MeetingActionResponse:
    """Play audio through the bot's virtual microphone. Unmutes if needed."""
    played = await manager.play_audio(payload.meeting_id, payload.audio_url, payload.volume)
    return MeetingActionResponse(
        message="Audio playback started" if played else "Audio playback could not be started",
        meeting_id=payload.meeting_id,
    )


@router.post("/audio/unmute", response_model=MeetingActionResponse, summary="Unmute the bot")
async def unmute(payload: MeetingActionRequest, manager: ManagerDep) -> MeetingActionResponse:
    """Turn the bot's microphone on."""
    await manager.set_microphone(payload.meeting_id, enabled=True)
    return MeetingActionResponse(message="Microphone enabled", meeting_id=payload.meeting_id)


@router.post("/audio/mute", response_model=MeetingActionResponse, summary="Mute the bot")
async def mute(payload: MeetingActionRequest, manager: ManagerDep) -> MeetingActionResponse:
    """Turn the bot's microphone off."""
    await manager.set_microphone(payload.meeting_id, enabled=False)
    return MeetingActionResponse(message="Microphone muted", meeting_id=payload.meeting_id)
