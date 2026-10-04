"""Frame codec contract tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import protocol as P


def test_parse_hello():
    frame = P.parse_plugin_frame(
        json.dumps(
            {
                "t": "hello",
                "v": 1,
                "deviceId": "dev-1",
                "deviceName": "home-pc",
                "platform": "win32",
                "harness": {"version": "0.2.0-rc.2"},
                "capabilities": ["ops", "events"],
            }
        )
    )
    assert isinstance(frame, P.HelloFrame)
    assert frame.deviceId == "dev-1"
    assert frame.capabilities == ["ops", "events"]


def test_parse_res_ok_and_error():
    ok = P.parse_plugin_frame(json.dumps({"t": "res", "id": "r1", "ok": True, "value": {"a": 1}}))
    assert isinstance(ok, P.ResFrame) and ok.ok and ok.value == {"a": 1}

    bad = P.parse_plugin_frame(
        json.dumps({"t": "res", "id": "r2", "ok": False, "error": {"code": "timeout", "message": "late"}})
    )
    assert isinstance(bad, P.ResFrame) and not bad.ok
    assert bad.error is not None and bad.error.code == "timeout"


def test_parse_stream_and_evt_and_pong():
    stream = P.parse_plugin_frame(json.dumps({"t": "stream", "id": "s1", "phase": "chunk", "value": 1}))
    assert isinstance(stream, P.StreamFrame) and stream.phase == "chunk"

    evt = P.parse_plugin_frame(json.dumps({"t": "evt", "topic": "session.event", "payload": {"seq": 1}}))
    assert isinstance(evt, P.EvtFrame) and evt.payload["seq"] == 1

    pong = P.parse_plugin_frame(json.dumps({"t": "pong", "id": "p1"}))
    assert isinstance(pong, P.PongFrame)


def test_plugin_ping_parses():
    """The plugin pings on its own schedule, so `ping` must be an inbound type.

    Treating it as unknown used to make the relay drop the link once per
    heartbeat interval.
    """
    frame = P.parse_plugin_frame(json.dumps({"t": "ping", "id": "hb-1"}))
    assert isinstance(frame, P.PingFrame)
    assert frame.id == "hb-1"


def test_ping_and_pong_are_bidirectional():
    for frame in (P.PingFrame(id="x"), P.PongFrame(id="x")):
        # Constructible as an outbound frame and parseable as an inbound one.
        assert P.dump_relay_frame(frame) == {"t": frame.t, "id": "x"}
        assert P.parse_plugin_frame(json.dumps({"t": frame.t, "id": "x"})).id == "x"


def test_unknown_frame_type_is_ignorable_not_fatal():
    """Forward compatibility: an unrecognised type must be distinguishable from
    a broken frame, because the relay ignores one and drops the link for the other."""
    with pytest.raises(P.FrameError) as info:
        P.parse_plugin_frame(json.dumps({"t": "not-a-frame"}))
    assert info.value.code == P.ERR_UNKNOWN_FRAME
    assert P.ERR_UNKNOWN_FRAME != P.ERR_BAD_ARGS


def test_malformed_json_is_rejected():
    with pytest.raises(P.FrameError) as info:
        P.parse_plugin_frame("{not json")
    assert info.value.code == P.ERR_BAD_ARGS


def test_non_object_json_is_rejected():
    with pytest.raises(P.FrameError) as info:
        P.parse_plugin_frame("[]")
    assert info.value.code == P.ERR_BAD_ARGS


def test_missing_required_field_is_rejected():
    with pytest.raises(P.FrameError) as info:
        P.parse_plugin_frame(json.dumps({"t": "res", "ok": True}))  # no id
    assert info.value.code == P.ERR_BAD_ARGS


def test_unknown_fields_are_ignored_for_forward_compatibility():
    frame = P.parse_plugin_frame(
        json.dumps({"t": "pong", "id": "p1", "futureField": {"nested": True}})
    )
    assert isinstance(frame, P.PongFrame)


def test_relay_frames_dump_without_null_optional_fields():
    payload = P.dump_relay_frame(P.ReqFrame(id="r1", op="session.list", args={}))
    assert payload == {"t": "req", "id": "r1", "op": "session.list", "args": {}}
    assert "deadlineMs" not in payload

    approval = P.dump_relay_frame(P.ApprovalFrame(askId="a1", decision="approved"))
    assert "answers" not in approval


def test_stream_ops_are_a_subset_of_allowed_ops():
    assert P.STREAM_OPS <= P.ALLOWED_OPS


def test_python_and_plugin_op_allowlists_agree():
    """The relay and the plugin enforce the same op list.

    Drift is silent on both sides: the relay would refuse an op the plugin
    supports (or pass one it does not), and no other test would notice. The
    allowlists are hand-written in two languages, so this reads the JavaScript
    one and compares.

    Only the repository checkout has both halves: the deployed relay is uploaded
    as `backend/` alone, so there this test skips rather than fails.
    """
    plugin_file = Path(__file__).resolve().parents[2] / "plugin" / "src" / "protocol.js"
    if not plugin_file.exists():
        pytest.skip("the JavaScript half is not part of this deployment")
    source = plugin_file.read_text(encoding="utf8")

    table = re.search(r"export const OP_TABLE = Object\.freeze\(\{(.*?)\n\}\);", source, re.S)
    assert table is not None, "OP_TABLE not found in plugin/src/protocol.js"
    plugin_ops = set(re.findall(r"'([^']+)':\s*\{", table.group(1)))

    local = re.search(r"export const LOCAL_OPS = Object\.freeze\(new Set\(\[(.*?)\]\)\)", source, re.S)
    assert local is not None, "LOCAL_OPS not found in plugin/src/protocol.js"
    plugin_ops |= set(re.findall(r"'([^']+)'", local.group(1)))

    assert plugin_ops == set(P.ALLOWED_OPS)


def test_every_known_topic_is_handled_by_the_plugin_contract():
    # Guards against adding a topic constant without documenting a payload.
    assert P.TOPIC_SESSION_EVENT in P.KNOWN_TOPICS
    assert P.TOPIC_APPROVAL_ASK in P.KNOWN_TOPICS
    assert P.TOPIC_QUESTION_ASK in P.KNOWN_TOPICS
    assert len(P.KNOWN_CAPABILITIES) == 3
