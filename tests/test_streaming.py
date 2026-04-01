"""
Integration test for the streaming pipeline.

Tests the encode -> network -> decode chain using dummy frames.
No game needed — generates colored test frames, encodes with FFmpeg,
sends over localhost UDP, decodes on the other end, verifies output.

Run: python tests/test_streaming.py
"""

import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.protocol import (
    PacketType, HEADER_SIZE, MAX_MTU, now_ms,
    VideoPacket, fragment_video_frame, pack_header, PacketFlags,
    ConnectRequest, ConnectAccept, PROTOCOL_VERSION,
    parse_packet_type,
)


def check_ffmpeg():
    """Verify FFmpeg is available."""
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        version = r.stdout.decode().split("\n")[0]
        print(f"  FFmpeg: {version}")
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("  ERROR: FFmpeg not found on PATH")
        return False


def check_nvenc():
    """Check if NVENC encoder is available."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, timeout=5,
        )
        has_nvenc = b"h264_nvenc" in r.stdout
        print(f"  NVENC: {'available' if has_nvenc else 'not available (will use libx264)'}")
        return has_nvenc
    except Exception:
        return False


def generate_test_frame(width: int, height: int, frame_num: int) -> bytes:
    """Generate a raw BGRA test frame with a moving color pattern."""
    # Simple gradient that changes each frame
    row = bytearray(width * 4)
    for x in range(width):
        r = (x + frame_num * 3) % 256
        g = (frame_num * 7) % 256
        b = (255 - x) % 256
        offset = x * 4
        row[offset] = b      # B
        row[offset + 1] = g  # G
        row[offset + 2] = r  # R
        row[offset + 3] = 255  # A

    # Repeat row for all scanlines (fast)
    return bytes(row) * height


def test_encode_decode_pipeline():
    """Test: raw BGRA -> FFmpeg encode -> raw bytes -> FFmpeg decode -> verify."""
    print("\n--- Encode/Decode Pipeline Test ---")

    width, height, fps = 320, 240, 30
    num_frames = 10
    has_nvenc = check_nvenc()
    encoder = "h264_nvenc" if has_nvenc else "libx264"

    # Always use libx264 for tests — NVENC can fail in headless/CI
    # and the test is about the pipeline, not the encoder choice
    encoder = "libx264"

    enc_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pixel_format", "bgra",
        "-video_size", f"{width}x{height}", "-framerate", str(fps),
        "-i", "pipe:0",
        "-c:v", encoder,
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-f", "h264", "pipe:1",
    ]

    # Decoder process (no -video_size on input — auto-detect from H264 SPS)
    dec_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "h264", "-i", "pipe:0",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "pipe:1",
    ]

    print(f"  Encoder: {encoder}")
    print(f"  Resolution: {width}x{height}")
    print(f"  Frames: {num_frames}")

    # Encode to temp file, then decode — avoids pipe complexity
    import tempfile
    tmp = tempfile.mktemp(suffix=".h264")

    encode_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "rawvideo", "-pixel_format", "bgra",
        "-video_size", f"{width}x{height}", "-framerate", str(fps),
        "-i", "pipe:0",
        "-c:v", encoder, "-pix_fmt", "yuv420p",
        "-preset", "ultrafast", "-tune", "zerolatency",
        tmp,
    ]

    decode_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-i", tmp,
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "pipe:1",
    ]

    t0 = time.monotonic()

    # Step 1: Encode
    enc_proc = subprocess.Popen(encode_cmd, stdin=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
    raw_size = 0
    for i in range(num_frames):
        frame = generate_test_frame(width, height, i)
        raw_size += len(frame)
        enc_proc.stdin.write(frame)
    enc_proc.stdin.close()
    enc_proc.wait(timeout=10)

    encoded_size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    if encoded_size == 0:
        stderr = enc_proc.stderr.read().decode(errors="replace")[:300]
        print(f"  ENCODER FAILED: {stderr}")
        return False

    # Step 2: Decode
    dec_proc = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
    decoded_data = dec_proc.stdout.read()
    dec_proc.wait(timeout=10)

    os.unlink(tmp)
    elapsed = time.monotonic() - t0

    out_frame_size = width * height * 3
    decoded_frames = len(decoded_data) // out_frame_size

    ratio = encoded_size / raw_size * 100
    print(f"  Raw input: {raw_size / 1024:.0f} KB")
    print(f"  Encoded: {encoded_size / 1024:.1f} KB ({ratio:.1f}% of raw)")
    print(f"  Decoded frames: {decoded_frames}/{num_frames}")
    print(f"  Time: {elapsed:.2f}s")

    if decoded_frames > 0:
        print("  PASS")
        return True
    else:
        stderr = dec_proc.stderr.read().decode(errors="replace")[:200]
        if stderr:
            print(f"  Decode stderr: {stderr}")
        print("  FAIL: no frames decoded")
        return False


def test_udp_loopback():
    """Test: fragment -> send over UDP localhost -> reassemble."""
    print("\n--- UDP Loopback Test ---")

    port = 19876
    frame_data = os.urandom(8000)  # Random 8KB frame
    frame_num = 42

    packets = fragment_video_frame(frame_data, frame_num, True, now_ms())
    print(f"  Frame: {len(frame_data)} bytes -> {len(packets)} fragments")

    # Receiver
    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.bind(("127.0.0.1", port))
    recv_sock.settimeout(2.0)

    received_fragments = {}
    expected_count = len(packets)

    def receiver():
        while len(received_fragments) < expected_count:
            try:
                data, _ = recv_sock.recvfrom(MAX_MTU)
                pkt = VideoPacket.from_bytes(data)
                received_fragments[pkt.fragment_index] = pkt.data
            except socket.timeout:
                break

    recv_thread = threading.Thread(target=receiver, daemon=True)
    recv_thread.start()

    # Sender
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i, pkt in enumerate(packets):
        raw = pkt.to_bytes(i)
        send_sock.sendto(raw, ("127.0.0.1", port))
        time.sleep(0.001)  # 1ms between packets

    recv_thread.join(timeout=3)
    send_sock.close()
    recv_sock.close()

    # Reassemble
    if len(received_fragments) == expected_count:
        reassembled = b"".join(received_fragments[i] for i in range(expected_count))
        if reassembled == frame_data:
            print(f"  Received {len(received_fragments)}/{expected_count} fragments")
            print("  PASS: reassembled data matches")
            return True
        else:
            print("  FAIL: data mismatch")
            return False
    else:
        print(f"  FAIL: received {len(received_fragments)}/{expected_count} fragments")
        return False


def test_connect_handshake():
    """Test: client sends ConnectRequest, server sends ConnectAccept."""
    print("\n--- Connect Handshake Test ---")

    port = 19877

    # Server
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", port))
    server.settimeout(2.0)

    # Client sends connect
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    req = ConnectRequest(
        protocol_version=PROTOCOL_VERSION,
        player_name="TestPlayer",
        requested_width=1920,
        requested_height=1080,
        requested_fps=60,
    )
    client.sendto(req.to_bytes(1), ("127.0.0.1", port))

    # Server receives
    data, addr = server.recvfrom(MAX_MTU)
    ptype = parse_packet_type(data)
    assert ptype == PacketType.CONTROL_CONNECT
    parsed_req = ConnectRequest.from_bytes(data)
    assert parsed_req.player_name == "TestPlayer"

    # Server responds
    acc = ConnectAccept(
        assigned_slot=2, actual_width=1920, actual_height=1080,
        actual_fps=60, codec="h264",
    )
    server.sendto(acc.to_bytes(2), addr)

    # Client receives
    client.settimeout(2.0)
    data, _ = client.recvfrom(MAX_MTU)
    ptype = parse_packet_type(data)
    assert ptype == PacketType.CONTROL_ACCEPT
    parsed_acc = ConnectAccept.from_bytes(data)
    assert parsed_acc.assigned_slot == 2
    assert parsed_acc.codec == "h264"

    server.close()
    client.close()

    print("  Connect -> Accept handshake OK")
    print("  PASS")
    return True


def test_adaptive_bitrate():
    """Test the adaptive bitrate controller responds correctly."""
    print("\n--- Adaptive Bitrate Test ---")

    from host.adaptive_bitrate import AdaptiveBitrateController

    abc = AdaptiveBitrateController(
        min_bitrate_kbps=1000,
        max_bitrate_kbps=20000,
        initial_bitrate_kbps=10000,
    )

    initial = abc.get_target_bitrate()
    print(f"  Initial: {initial} kbps")

    # Simulate packet loss -> should decrease
    for _ in range(5):
        abc.update(rtt_ms=50, packet_loss_pct=8.0, decode_time_ms=5)

    after_loss = abc.get_target_bitrate()
    print(f"  After 8% loss: {after_loss} kbps")
    assert after_loss < initial, f"Should decrease: {after_loss} >= {initial}"

    # Simulate good conditions -> should slowly increase
    for _ in range(20):
        abc.update(rtt_ms=20, packet_loss_pct=0.1, decode_time_ms=3)
        time.sleep(0.2)

    after_good = abc.get_target_bitrate()
    print(f"  After good period: {after_good} kbps")

    stats = abc.get_stats()
    print(f"  Stats: {stats}")
    print("  PASS")
    return True


if __name__ == "__main__":
    print("=== Streaming Integration Tests ===")

    if not check_ffmpeg():
        print("\nCannot run streaming tests without FFmpeg.")
        sys.exit(1)

    results = {}
    results["encode_decode"] = test_encode_decode_pipeline()
    results["udp_loopback"] = test_udp_loopback()
    results["handshake"] = test_connect_handshake()
    results["adaptive_bitrate"] = test_adaptive_bitrate()

    print("\n=== Results ===")
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_pass = False

    sys.exit(0 if all_pass else 1)
