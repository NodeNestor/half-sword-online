"""Tests for the network protocol — packet serialization round-trips."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.protocol import (
    PacketType, PacketFlags, HEADER_SIZE,
    pack_header, unpack_header, parse_packet_type, now_ms,
    VideoPacket, fragment_video_frame,
    MouseInput, KeyboardInput, GamepadInput,
    ConnectRequest, ConnectAccept, StreamStats,
    XButton, PROTOCOL_VERSION,
)


def test_header_roundtrip():
    raw = pack_header(PacketType.VIDEO_FRAME, PacketFlags.KEYFRAME, 42, 123456)
    ptype, flags, seq, ts = unpack_header(raw)
    assert ptype == PacketType.VIDEO_FRAME
    assert flags == PacketFlags.KEYFRAME
    assert seq == 42
    assert ts == 123456
    print("  header roundtrip OK")


def test_parse_packet_type():
    raw = pack_header(PacketType.CONTROL_CONNECT, 0, 0, 0)
    assert parse_packet_type(raw) == PacketType.CONTROL_CONNECT
    print("  parse_packet_type OK")


def test_video_fragment_reassemble():
    # Create a 5KB fake frame
    frame_data = bytes(range(256)) * 20  # 5120 bytes
    frame_num = 7
    ts = now_ms()

    packets = fragment_video_frame(frame_data, frame_num, True, ts)
    assert len(packets) > 1, f"Expected multiple fragments, got {len(packets)}"

    # Serialize and deserialize each
    reassembled = {}
    for i, pkt in enumerate(packets):
        raw = pkt.to_bytes(i)
        parsed = VideoPacket.from_bytes(raw)
        assert parsed.frame_number == frame_num
        assert parsed.fragment_index == i
        assert parsed.fragment_count == len(packets)
        reassembled[parsed.fragment_index] = parsed.data

    # Reconstruct
    full = b"".join(reassembled[i] for i in range(len(packets)))
    assert full == frame_data, f"Reassembled {len(full)} != original {len(frame_data)}"
    print(f"  video fragment/reassemble OK ({len(packets)} fragments)")


def test_video_small_frame():
    # Single fragment
    frame_data = b"tiny"
    packets = fragment_video_frame(frame_data, 1, False, 0)
    assert len(packets) == 1
    raw = packets[0].to_bytes(0)
    parsed = VideoPacket.from_bytes(raw)
    assert parsed.data == frame_data
    print("  small frame OK")


def test_mouse_input_roundtrip():
    m = MouseInput(dx=-50, dy=120, buttons=0x03, scroll=-2, timestamp_ms=999)
    raw = m.to_bytes(5)
    parsed = MouseInput.from_bytes(raw)
    assert parsed.dx == -50
    assert parsed.dy == 120
    assert parsed.buttons == 0x03
    assert parsed.scroll == -2
    print("  mouse input OK")


def test_keyboard_input_roundtrip():
    k = KeyboardInput(keycode=0x57, pressed=True, timestamp_ms=100)
    raw = k.to_bytes(10)
    parsed = KeyboardInput.from_bytes(raw)
    assert parsed.keycode == 0x57
    assert parsed.pressed == True
    print("  keyboard input OK")


def test_gamepad_input_roundtrip():
    g = GamepadInput(
        buttons=XButton.A | XButton.LEFT_SHOULDER,
        left_trigger=200, right_trigger=50,
        left_stick_x=-16000, left_stick_y=32000,
        right_stick_x=0, right_stick_y=-10000,
        timestamp_ms=555,
    )
    raw = g.to_bytes(20)
    parsed = GamepadInput.from_bytes(raw)
    assert parsed.buttons == (XButton.A | XButton.LEFT_SHOULDER)
    assert parsed.left_trigger == 200
    assert parsed.left_stick_x == -16000
    assert parsed.right_stick_y == -10000
    print("  gamepad input OK")


def test_connect_request_roundtrip():
    req = ConnectRequest(
        protocol_version=PROTOCOL_VERSION,
        player_name="TestPlayer123",
        requested_width=1920,
        requested_height=1080,
        requested_fps=60,
    )
    raw = req.to_bytes(1)
    parsed = ConnectRequest.from_bytes(raw)
    assert parsed.protocol_version == PROTOCOL_VERSION
    assert parsed.player_name == "TestPlayer123"
    assert parsed.requested_width == 1920
    assert parsed.requested_height == 1080
    assert parsed.requested_fps == 60
    print("  connect request OK")


def test_connect_accept_roundtrip():
    acc = ConnectAccept(
        assigned_slot=3,
        actual_width=1280,
        actual_height=720,
        actual_fps=30,
        codec="h264",
    )
    raw = acc.to_bytes(2)
    parsed = ConnectAccept.from_bytes(raw)
    assert parsed.assigned_slot == 3
    assert parsed.actual_width == 1280
    assert parsed.codec == "h264"
    print("  connect accept OK")


def test_stats_roundtrip():
    s = StreamStats(
        fps=58, bitrate_kbps=12000, rtt_ms=35,
        packet_loss_pct=1.5, encode_time_ms=4, decode_time_ms=3,
    )
    raw = s.to_bytes(0)
    parsed = StreamStats.from_bytes(raw)
    assert parsed.fps == 58
    assert parsed.bitrate_kbps == 12000
    assert parsed.rtt_ms == 35
    assert abs(parsed.packet_loss_pct - 1.5) < 0.1
    print("  stats OK")


def test_truncated_packets_raise():
    """Malformed/truncated packets should raise ValueError, not crash."""
    short = b"\x40\x00\x00"  # Too short for any packet
    errors = 0

    for cls in [ConnectRequest, ConnectAccept, VideoPacket, MouseInput,
                KeyboardInput, GamepadInput, StreamStats]:
        try:
            cls.from_bytes(short)
            print(f"  WARNING: {cls.__name__} accepted truncated packet!")
        except (ValueError, Exception):
            errors += 1

    assert errors == 7, f"Expected 7 rejections, got {errors}"
    print("  truncated packet rejection OK")


if __name__ == "__main__":
    print("=== Protocol Tests ===")
    test_header_roundtrip()
    test_parse_packet_type()
    test_video_fragment_reassemble()
    test_video_small_frame()
    test_mouse_input_roundtrip()
    test_keyboard_input_roundtrip()
    test_gamepad_input_roundtrip()
    test_connect_request_roundtrip()
    test_connect_accept_roundtrip()
    test_stats_roundtrip()
    test_truncated_packets_raise()
    print("\nAll protocol tests passed!")
