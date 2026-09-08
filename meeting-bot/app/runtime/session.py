"""Session registry.

Tracks the sessions this process is running, keyed by meeting id. Small on
purpose: it holds references and enforces uniqueness, and knows nothing about
what a session does.

Uniqueness is enforced rather than tolerated. The previous implementation
returned the existing bot when asked to join a meeting twice, which quietly
turned a duplicate request into a success and left the caller believing a fresh
join had happened.

Rejoining the *same* meeting is a different case, and a legitimate one: a
meeting recorded twice, or rejoined after the first bot was evicted, is two
sessions that must be told apart. :meth:`SessionRegistry.reserve_meeting_id`
resolves that by handing out a suffixed key rather than rejecting the join, so
the caller-supplied id is a starting point rather than a constraint.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

from app.core.exceptions import MeetingAlreadyActiveError, MeetingNotFoundError

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.meeting.meeting_session import MeetingSession

logger = logging.getLogger(__name__)


class SessionRegistry:
    """The meeting sessions running in this process."""

    def __init__(self, *, max_sessions: int = 0) -> None:
        """
        Args:
            max_sessions: Hard cap on concurrent sessions. ``0`` means no cap.
                A bot pod normally runs one meeting; a cap turns a runaway
                dispatcher into a rejected request rather than an OOM kill.
        """
        self._sessions: dict[str, MeetingSession] = {}
        # Ids handed out by reserve_meeting_id but not yet registered. A session
        # cannot be constructed until its id is settled (MeetingRequest is
        # frozen), so there is a window between choosing a key and adding the
        # session; without this, two concurrent joins could be handed the same
        # "free" key and the second would fail on add.
        self._reserved: set[str] = set()
        self._max_sessions = max_sessions
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, meeting_id: object) -> bool:
        return meeting_id in self._sessions

    def __iter__(self) -> Iterator[MeetingSession]:
        return iter(list(self._sessions.values()))

    @property
    def meeting_ids(self) -> list[str]:
        return sorted(self._sessions)

    @property
    def is_full(self) -> bool:
        return bool(self._max_sessions) and len(self._sessions) >= self._max_sessions

    def _is_taken(self, meeting_id: str) -> bool:
        """True when an id is registered or spoken for. Caller holds the lock."""
        return meeting_id in self._sessions or meeting_id in self._reserved

    async def reserve_meeting_id(self, desired: str) -> str:
        """Claim a free session key, deriving one from ``desired`` if it is taken.

        Returns ``desired`` untouched when nothing holds it — the common case,
        and the one that keeps a caller's own id meaningful. When it is already
        in use the id is suffixed rather than rejected, because a second join of
        the same meeting is a real scenario and failing it is worse than
        renaming it.

        The returned id is reserved until :meth:`add` registers it or
        :meth:`release_meeting_id` gives it back, so concurrent joins cannot be
        handed the same key.
        """
        async with self._lock:
            candidate = desired
            while self._is_taken(candidate):
                candidate = f"{desired}-{uuid.uuid4().hex[:8]}"
            self._reserved.add(candidate)
            return candidate

    async def release_meeting_id(self, meeting_id: str) -> None:
        """Give back a reservation that will never be registered.

        Only needed on the failure path between reserving and adding; a
        successful :meth:`add` consumes the reservation itself.
        """
        async with self._lock:
            self._reserved.discard(meeting_id)

    async def add(self, session: MeetingSession) -> None:
        """Register a session.

        Raises:
            MeetingAlreadyActiveError: If this meeting already has a session, or
                the process is at capacity.
        """
        async with self._lock:
            meeting_id = session.meeting_id
            if meeting_id in self._sessions:
                raise MeetingAlreadyActiveError(meeting_id)
            if self.is_full:
                raise MeetingAlreadyActiveError(
                    f"session limit reached ({self._max_sessions}); cannot start {meeting_id}"
                )

            self._reserved.discard(meeting_id)
            self._sessions[meeting_id] = session
            logger.info(
                "Session registered",
                extra={
                    "meeting_id": meeting_id,
                    "session_id": session.session_id,
                    "active_sessions": len(self._sessions),
                },
            )

    async def remove(self, meeting_id: str) -> MeetingSession | None:
        """Deregister a session. Returns it, or ``None`` if it was not registered."""
        async with self._lock:
            session = self._sessions.pop(meeting_id, None)
            if session is not None:
                logger.info(
                    "Session deregistered",
                    extra={"meeting_id": meeting_id, "active_sessions": len(self._sessions)},
                )
            return session

    def get(self, meeting_id: str) -> MeetingSession | None:
        """Look up a session, or ``None``."""
        return self._sessions.get(meeting_id)

    def require(self, meeting_id: str) -> MeetingSession:
        """Look up a session.

        Raises:
            MeetingNotFoundError: If there is no session for this meeting.
        """
        session = self._sessions.get(meeting_id)
        if session is None:
            raise MeetingNotFoundError(meeting_id)
        return session

    def all(self) -> list[MeetingSession]:
        """Snapshot of every session, safe to iterate while sessions end."""
        return list(self._sessions.values())

    async def clear(self) -> list[MeetingSession]:
        """Deregister everything and return it. Used during shutdown."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            # A join still between reserving and adding is being cancelled too,
            # and its id would otherwise outlive the registry it belongs to.
            self._reserved.clear()
            return sessions
