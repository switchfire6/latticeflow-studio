"""Exceptions raised by the LatticeFlow client."""

from __future__ import annotations

from typing import Any, Optional


class StudioError(Exception):
    """The server answered with ``ERROR`` (WebSocket) or an ``ErrorResponse`` (REST).

    ``code`` is an ``ErrorCode`` value from ``backend/app/schemas/messages.py``
    (``INVALID_PAYLOAD``, ``NO_PHONON_ANALYSIS``, ``HULL_UNAVAILABLE``, ...).
    ``reply`` holds whatever the command had produced before the error (e.g. the
    final relaxation frame before ``RELAXATION_DIVERGED``); ``status`` is the HTTP
    status for REST errors.
    """

    def __init__(self, code: str, message: str, *, command: Optional[str] = None,
                 status: Optional[int] = None, reply: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.command = command
        self.status = status
        self.reply = reply


class StreamStopped(StudioError):
    """A streaming command was cancelled (``STREAM_STOPPED``), e.g. by :meth:`Studio.stop`.

    The structure of the last frame that was sent stays active on the server.
    """

    def __init__(self, command: Optional[str], reply: Any = None) -> None:
        super().__init__("STREAM_STOPPED", f"{command} was stopped", command=command, reply=reply)


class StudioConnectionError(StudioError):
    """The WebSocket closed (or never opened) while a command was waiting."""

    def __init__(self, message: str) -> None:
        super().__init__("CONNECTION_CLOSED", message)


class StudioTimeout(StudioError):
    """No terminal message arrived within the call's ``timeout``."""

    def __init__(self, command: Optional[str], timeout: float) -> None:
        super().__init__("TIMEOUT", f"{command}: no terminal message within {timeout:g} s", command=command)
