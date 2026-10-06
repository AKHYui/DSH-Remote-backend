"""Wire contracts for the plugin <-> relay channel.

This is the Python half of `docs/PROTOCOL.md`; the JavaScript half is
`plugin/src/protocol.js`. Bump `PROTOCOL_VERSION` in both places, together.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 1

# --- capabilities -----------------------------------------------------------

CAP_OPS = "ops"
CAP_EVENTS = "events"
CAP_APPROVALS = "approvals"
KNOWN_CAPABILITIES = frozenset({CAP_OPS, CAP_EVENTS, CAP_APPROVALS})

# --- WebSocket close codes --------------------------------------------------

CLOSE_REPLACED = 4001
CLOSE_PROTOCOL = 4400
CLOSE_UNAUTHORIZED = 4401

# --- error codes ------------------------------------------------------------

ERR_OP_NOT_SUPPORTED = "op_not_supported"
ERR_BAD_ARGS = "bad_args"
ERR_REMOTE_ERROR = "remote_error"
ERR_TIMEOUT = "timeout"
ERR_CANCELLED = "cancelled"
ERR_LINK_LOST = "link_lost"
ERR_DEVICE_OFFLINE = "device_offline"
ERR_DEVICE_BUSY = "device_busy"
ERR_SESSION_NOT_ALLOWED = "session_not_allowed"
ERR_GATEWAY_INTERNAL = "gateway_internal"
ERR_UNKNOWN_FRAME = "unknown_frame"

# --- event topics -----------------------------------------------------------

TOPIC_SESSION_EVENT = "session.event"
TOPIC_SESSION_STATUS = "session.status"
TOPIC_SESSION_ACTIVITY = "session.activity"
TOPIC_APPROVAL_ASK = "approval.ask"
TOPIC_QUESTION_ASK = "question.ask"
TOPIC_APPROVAL_SETTLED = "approval.settled"
TOPIC_DEVICE_STATUS = "device.status"

KNOWN_TOPICS = frozenset(
    {
        TOPIC_SESSION_EVENT,
        TOPIC_SESSION_STATUS,
        TOPIC_SESSION_ACTIVITY,
        TOPIC_APPROVAL_ASK,
        TOPIC_QUESTION_ASK,
        TOPIC_APPROVAL_SETTLED,
        TOPIC_DEVICE_STATUS,
    }
)

# --- op allowlist -----------------------------------------------------------
#
# Defence in depth: the plugin enforces the same list, but the relay refuses
# unknown ops before they ever reach a desktop. Neither side may forward an
# arbitrary namespace/method pair.

OP_HARNESS_INFO = "harness.info"
OP_SESSION_LIST = "session.list"
OP_SESSION_PAGE = "session.page"
OP_SESSION_FOLLOW = "session.follow"
OP_SESSION_PROMPT = "session.prompt"
OP_SESSION_CANCEL = "session.cancel"
OP_SESSION_CREATE = "session.create"
OP_SESSION_SELECT_MODEL = "session.selectModel"
OP_USER_QUESTIONS_ANSWER = "userQuestions.answer"
OP_FILE_UPLOADS_UPLOAD = "fileUploads.upload"
OP_MODEL_CATALOG = "model.catalog"
# Archiving belongs to the Host's Workspace registry, not to a Session, so these
# are the only two ops in the list outside the `session`/`fileUploads` namespaces.
# They are also the only *mutations* of the Host's own listing rather than of a
# conversation.
OP_WORKSPACE_ARCHIVE_SESSION = "workspace.archiveSession"
OP_WORKSPACE_UNARCHIVE_SESSION = "workspace.unarchiveSession"

ALLOWED_OPS = frozenset(
    {
        OP_HARNESS_INFO,
        OP_SESSION_LIST,
        OP_SESSION_PAGE,
        OP_SESSION_FOLLOW,
        OP_SESSION_PROMPT,
        OP_SESSION_CANCEL,
        OP_SESSION_CREATE,
        OP_SESSION_SELECT_MODEL,
        OP_USER_QUESTIONS_ANSWER,
        OP_FILE_UPLOADS_UPLOAD,
        OP_MODEL_CATALOG,
        OP_WORKSPACE_ARCHIVE_SESSION,
        OP_WORKSPACE_UNARCHIVE_SESSION,
    }
)

STREAM_OPS = frozenset({OP_SESSION_FOLLOW})


class FrameError(Exception):
    """A malformed or unsupported frame / frame field."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- plugin -> relay --------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- keepalive frames (shared by both directions) ---------------------------
#
# Either side may ping and the peer must answer `pong` with the same id. The
# plugin pings on its own schedule to notice a half-open socket, and the relay
# pings to notice a silent plugin, so BOTH unions contain BOTH frames.
#
# Getting this wrong is expensive: an unrecognised `ping` used to be treated as a
# malformed frame, which made the relay drop the link every heartbeat interval.


class PingFrame(_Base):
    t: Literal["ping"] = "ping"
    id: str


class PongFrame(_Base):
    t: Literal["pong"] = "pong"
    id: str


class HelloFrame(_Base):
    t: Literal["hello"]
    v: int
    deviceId: str
    deviceName: str = ""
    platform: str = ""
    harness: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)


class ErrorPayload(_Base):
    code: str
    message: str = ""


class ResFrame(_Base):
    t: Literal["res"]
    id: str
    ok: bool
    value: Any = None
    error: ErrorPayload | None = None


class StreamFrame(_Base):
    t: Literal["stream"]
    id: str
    phase: Literal["open", "chunk", "end", "error"]
    value: Any = None
    error: ErrorPayload | None = None


class EvtFrame(_Base):
    t: Literal["evt"]
    topic: str
    payload: dict[str, Any] = Field(default_factory=dict)


_PLUGIN_FRAME_TYPES = frozenset({"hello", "res", "stream", "evt", "ping", "pong"})

PluginFrame = Annotated[
    Union[HelloFrame, ResFrame, StreamFrame, EvtFrame, PingFrame, PongFrame],
    Field(discriminator="t"),
]

_PLUGIN_FRAME_ADAPTER: TypeAdapter[PluginFrame] = TypeAdapter(PluginFrame)


def parse_plugin_frame(raw: str | bytes) -> PluginFrame:
    """Decode one inbound frame.

    Raises `FrameError`. The `code` matters to the caller:
      * `ERR_UNKNOWN_FRAME` — a frame type this relay does not know. Safe to
        ignore, and how a newer plugin stays compatible with an older relay.
      * `ERR_BAD_ARGS` — structurally broken: not JSON, no `t`, or wrong field
        types. A protocol violation worth dropping the link for.
    """
    try:
        peek = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise FrameError(ERR_BAD_ARGS, "frame is not valid JSON") from exc

    if not isinstance(peek, dict) or not isinstance(peek.get("t"), str):
        raise FrameError(ERR_BAD_ARGS, 'frame must be an object with a string "t"')

    if peek["t"] not in _PLUGIN_FRAME_TYPES:
        raise FrameError(ERR_UNKNOWN_FRAME, f"unsupported frame type {peek['t']!r}")

    try:
        return _PLUGIN_FRAME_ADAPTER.validate_json(raw)
    except ValidationError as exc:
        raise FrameError(ERR_BAD_ARGS, f"invalid frame: {exc.error_count()} problem(s)") from exc


# --- relay -> plugin --------------------------------------------------------


class WelcomeFrame(_Base):
    t: Literal["welcome"] = "welcome"
    v: int = PROTOCOL_VERSION
    deviceId: str
    serverTime: int


class ByeFrame(_Base):
    t: Literal["bye"] = "bye"
    code: str
    message: str


class ReqFrame(_Base):
    t: Literal["req"] = "req"
    id: str
    op: str
    args: dict[str, Any] = Field(default_factory=dict)
    deadlineMs: int | None = None


class CancelFrame(_Base):
    t: Literal["cancel"] = "cancel"
    id: str


class SubFrame(_Base):
    t: Literal["sub"] = "sub"
    id: str
    topics: list[str]
    args: dict[str, Any] = Field(default_factory=dict)


class UnsubFrame(_Base):
    t: Literal["unsub"] = "unsub"
    id: str


class ApprovalFrame(_Base):
    t: Literal["approval"] = "approval"
    askId: str
    decision: Literal["approved", "denied", "cancelled"]
    answers: list[dict[str, Any]] | None = None


RelayFrame = Union[
    WelcomeFrame,
    ByeFrame,
    ReqFrame,
    CancelFrame,
    SubFrame,
    UnsubFrame,
    ApprovalFrame,
    PingFrame,
]


def dump_relay_frame(frame: RelayFrame) -> dict[str, Any]:
    return frame.model_dump(exclude_none=True)
