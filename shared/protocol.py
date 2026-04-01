"""
Half Sword Online — Network Protocol

Simple UDP protocol for game streaming between host and clients.
Inspired by Sunshine/Moonlight and Parsec's BUD protocol, but simplified.

Packet Types:
    - VIDEO_FRAME: H.264 NAL units, fragmented into MTU-sized chunks
    - AUDIO_FRAME: Opus-encoded audio packets
    - INPUT_STATE: Keyboard/mouse/gamepad state from client
    - CONTROL: Connection management, config, keepalive
    - FEC_REPAIR: Forward error correction parity data

All packets are prefixed with an 8-byte header:
    [0:1]  packet_type (uint8)
    [1:2]  flags (uint8)
    [2:4]  sequence_number (uint16)
    [4:8]  timestamp_ms (uint32)
"""

import struct
import enum
import time
import dataclasses

# =============================================================================
# Constants
# =============================================================================

PROTOCOL_VERSION = 1
MAX_MTU = 1400  # Safe UDP MTU (under typical 1500 ethernet MTU)
HEADER_SIZE = 8
MAX_PAYLOAD = MAX_MTU - HEADER_SIZE
KEEPALIVE_INTERVAL_MS = 1000
CONNECTION_TIMEOUT_MS = 5000

# =============================================================================
# Packet Types
# =============================================================================

class PacketType(enum.IntEnum):
    # Video packets (0x10-0x1F)
    VIDEO_FRAME = 0x10
    VIDEO_FEC = 0x11

    # Audio packets (0x20-0x2F)
    AUDIO_FRAME = 0x20

    # Input packets (0x30-0x3F)
    INPUT_STATE = 0x30
    INPUT_MOUSE = 0x31
    INPUT_KEYBOARD = 0x32
    INPUT_GAMEPAD = 0x33

    # Control packets (0x40-0x4F)
    CONTROL_CONNECT = 0x40
    CONTROL_ACCEPT = 0x41
    CONTROL_REJECT = 0x42
    CONTROL_DISCONNECT = 0x43
    CONTROL_KEEPALIVE = 0x44
    CONTROL_KEEPALIVE_ACK = 0x45
    CONTROL_CONFIG = 0x46
    CONTROL_REQUEST_IDR = 0x47  # Client requests keyframe
    CONTROL_STATS = 0x48


class PacketFlags(enum.IntFlag):
    NONE = 0x00
    KEYFRAME = 0x01       # This video packet starts a keyframe
    FRAGMENT = 0x02       # This packet is a fragment of a larger message
    LAST_FRAGMENT = 0x04  # This is the last fragment
    RELIABLE = 0x08       # Requires acknowledgment


# =============================================================================
# Header
# =============================================================================

HEADER_FORMAT = "!BBHI"  # type(1) + flags(1) + seq(2) + timestamp(4)

def pack_header(packet_type: int, flags: int, seq: int, timestamp_ms: int) -> bytes:
    return struct.pack(HEADER_FORMAT, packet_type, flags, seq & 0xFFFF, timestamp_ms & 0xFFFFFFFF)


def unpack_header(data: bytes) -> tuple[int, int, int, int]:
    """Returns (packet_type, flags, sequence_number, timestamp_ms)"""
    return struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])


# =============================================================================
# Video Packet
# =============================================================================

# Video sub-header (after main header):
#   [0:4]  frame_number (uint32)
#   [4:6]  fragment_index (uint16)
#   [6:8]  fragment_count (uint16)
#   [8:]   payload (H.264 NAL data)

VIDEO_SUBHEADER_FORMAT = "!IHH"
VIDEO_SUBHEADER_SIZE = 8


@dataclasses.dataclass
class VideoPacket:
    frame_number: int
    fragment_index: int
    fragment_count: int
    is_keyframe: bool
    timestamp_ms: int
    data: bytes

    def to_bytes(self, seq: int) -> bytes:
        flags = PacketFlags.NONE
        if self.is_keyframe and self.fragment_index == 0:
            flags |= PacketFlags.KEYFRAME
        if self.fragment_count > 1:
            flags |= PacketFlags.FRAGMENT
        if self.fragment_index == self.fragment_count - 1:
            flags |= PacketFlags.LAST_FRAGMENT

        header = pack_header(PacketType.VIDEO_FRAME, flags, seq, self.timestamp_ms)
        subheader = struct.pack(VIDEO_SUBHEADER_FORMAT,
                                self.frame_number, self.fragment_index, self.fragment_count)
        return header + subheader + self.data

    @classmethod
    def from_bytes(cls, data: bytes) -> "VideoPacket":
        if len(data) < HEADER_SIZE + VIDEO_SUBHEADER_SIZE:
            raise ValueError("packet too short for VideoPacket")
        ptype, flags, seq, ts = unpack_header(data)
        frame_num, frag_idx, frag_count = struct.unpack(
            VIDEO_SUBHEADER_FORMAT,
            data[HEADER_SIZE:HEADER_SIZE + VIDEO_SUBHEADER_SIZE]
        )
        payload = data[HEADER_SIZE + VIDEO_SUBHEADER_SIZE:]
        return cls(
            frame_number=frame_num,
            fragment_index=frag_idx,
            fragment_count=frag_count,
            is_keyframe=bool(flags & PacketFlags.KEYFRAME),
            timestamp_ms=ts,
            data=payload,
        )


def fragment_video_frame(frame_data: bytes, frame_number: int,
                         is_keyframe: bool, timestamp_ms: int) -> list[VideoPacket]:
    """Split a complete encoded frame into MTU-sized VideoPackets."""
    max_frag_payload = MAX_PAYLOAD - VIDEO_SUBHEADER_SIZE
    fragments = []
    offset = 0

    while offset < len(frame_data):
        chunk = frame_data[offset:offset + max_frag_payload]
        fragments.append(chunk)
        offset += max_frag_payload

    if not fragments:
        fragments = [b""]

    return [
        VideoPacket(
            frame_number=frame_number,
            fragment_index=i,
            fragment_count=len(fragments),
            is_keyframe=is_keyframe and i == 0,
            timestamp_ms=timestamp_ms,
            data=frag,
        )
        for i, frag in enumerate(fragments)
    ]


# =============================================================================
# Input Packets
# =============================================================================

# Mouse input sub-header:
#   [0:2]   dx (int16) — relative X movement
#   [2:4]   dy (int16) — relative Y movement
#   [4:5]   buttons (uint8) — bitmask: bit0=left, bit1=right, bit2=middle
#   [5:6]   scroll (int8) — scroll wheel delta

MOUSE_FORMAT = "!hhBb"
MOUSE_SIZE = struct.calcsize(MOUSE_FORMAT)

@dataclasses.dataclass
class MouseInput:
    dx: int
    dy: int
    buttons: int  # bitmask
    scroll: int
    timestamp_ms: int

    def to_bytes(self, seq: int) -> bytes:
        header = pack_header(PacketType.INPUT_MOUSE, PacketFlags.NONE, seq, self.timestamp_ms)
        payload = struct.pack(MOUSE_FORMAT, self.dx, self.dy, self.buttons, self.scroll)
        return header + payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "MouseInput":
        if len(data) < HEADER_SIZE + MOUSE_SIZE:
            raise ValueError("packet too short for MouseInput")
        _, _, _, ts = unpack_header(data)
        dx, dy, buttons, scroll = struct.unpack(MOUSE_FORMAT, data[HEADER_SIZE:HEADER_SIZE + MOUSE_SIZE])
        return cls(dx=dx, dy=dy, buttons=buttons, scroll=scroll, timestamp_ms=ts)


# Keyboard input sub-header:
#   [0:2]  keycode (uint16) — virtual key code
#   [2:3]  pressed (uint8) — 1=pressed, 0=released

KEYBOARD_FORMAT = "!HB"
KEYBOARD_SIZE = struct.calcsize(KEYBOARD_FORMAT)

@dataclasses.dataclass
class KeyboardInput:
    keycode: int
    pressed: bool
    timestamp_ms: int

    def to_bytes(self, seq: int) -> bytes:
        header = pack_header(PacketType.INPUT_KEYBOARD, PacketFlags.NONE, seq, self.timestamp_ms)
        payload = struct.pack(KEYBOARD_FORMAT, self.keycode, 1 if self.pressed else 0)
        return header + payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "KeyboardInput":
        if len(data) < HEADER_SIZE + KEYBOARD_SIZE:
            raise ValueError("packet too short for KeyboardInput")
        _, _, _, ts = unpack_header(data)
        keycode, pressed = struct.unpack(KEYBOARD_FORMAT, data[HEADER_SIZE:HEADER_SIZE + KEYBOARD_SIZE])
        return cls(keycode=keycode, pressed=bool(pressed), timestamp_ms=ts)


# Gamepad input (full state snapshot):
#   [0:2]   buttons (uint16) — bitmask matching XInput GAMEPAD_* constants
#   [2:3]   left_trigger (uint8) — 0-255
#   [3:4]   right_trigger (uint8) — 0-255
#   [4:6]   left_stick_x (int16) — -32768 to 32767
#   [6:8]   left_stick_y (int16)
#   [8:10]  right_stick_x (int16)
#   [10:12] right_stick_y (int16)

GAMEPAD_FORMAT = "!HBBhhhh"
GAMEPAD_SIZE = struct.calcsize(GAMEPAD_FORMAT)

@dataclasses.dataclass
class GamepadInput:
    buttons: int
    left_trigger: int
    right_trigger: int
    left_stick_x: int
    left_stick_y: int
    right_stick_x: int
    right_stick_y: int
    timestamp_ms: int

    def to_bytes(self, seq: int) -> bytes:
        header = pack_header(PacketType.INPUT_GAMEPAD, PacketFlags.NONE, seq, self.timestamp_ms)
        payload = struct.pack(GAMEPAD_FORMAT,
                              self.buttons, self.left_trigger, self.right_trigger,
                              self.left_stick_x, self.left_stick_y,
                              self.right_stick_x, self.right_stick_y)
        return header + payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "GamepadInput":
        if len(data) < HEADER_SIZE + GAMEPAD_SIZE:
            raise ValueError("packet too short for GamepadInput")
        _, _, _, ts = unpack_header(data)
        vals = struct.unpack(GAMEPAD_FORMAT, data[HEADER_SIZE:HEADER_SIZE + GAMEPAD_SIZE])
        return cls(
            buttons=vals[0], left_trigger=vals[1], right_trigger=vals[2],
            left_stick_x=vals[3], left_stick_y=vals[4],
            right_stick_x=vals[5], right_stick_y=vals[6],
            timestamp_ms=ts,
        )


# XInput button constants (for reference)
class XButton(enum.IntFlag):
    DPAD_UP = 0x0001
    DPAD_DOWN = 0x0002
    DPAD_LEFT = 0x0004
    DPAD_RIGHT = 0x0008
    START = 0x0010
    BACK = 0x0020
    LEFT_THUMB = 0x0040
    RIGHT_THUMB = 0x0080
    LEFT_SHOULDER = 0x0100
    RIGHT_SHOULDER = 0x0200
    A = 0x1000
    B = 0x2000
    X = 0x4000
    Y = 0x8000


# =============================================================================
# Control Packets
# =============================================================================

@dataclasses.dataclass
class ConnectRequest:
    """Client → Host: request to join."""
    protocol_version: int
    player_name: str
    requested_width: int
    requested_height: int
    requested_fps: int

    def to_bytes(self, seq: int) -> bytes:
        name_bytes = self.player_name.encode("utf-8")[:32].ljust(32, b"\x00")
        header = pack_header(PacketType.CONTROL_CONNECT, PacketFlags.RELIABLE, seq, now_ms())
        payload = struct.pack("!BHH B", self.protocol_version,
                              self.requested_width, self.requested_height,
                              self.requested_fps)
        return header + payload + name_bytes

    @classmethod
    def from_bytes(cls, data: bytes) -> "ConnectRequest":
        if len(data) < HEADER_SIZE + 38:
            raise ValueError("packet too short for ConnectRequest")
        offset = HEADER_SIZE
        ver, w, h, fps = struct.unpack("!BHH B", data[offset:offset + 6])
        name = data[offset + 6:offset + 38].rstrip(b"\x00").decode("utf-8", errors="replace")
        # Strip control characters from name
        name = "".join(ch for ch in name if ch.isprintable())
        if not name:
            name = "Player"
        return cls(protocol_version=ver, player_name=name,
                   requested_width=w, requested_height=h, requested_fps=fps)


@dataclasses.dataclass
class ConnectAccept:
    """Host → Client: connection accepted."""
    assigned_slot: int
    actual_width: int
    actual_height: int
    actual_fps: int
    codec: str  # "h264" or "h265"

    def to_bytes(self, seq: int) -> bytes:
        codec_bytes = self.codec.encode("utf-8")[:8].ljust(8, b"\x00")
        header = pack_header(PacketType.CONTROL_ACCEPT, PacketFlags.RELIABLE, seq, now_ms())
        payload = struct.pack("!BHH B", self.assigned_slot,
                              self.actual_width, self.actual_height,
                              self.actual_fps)
        return header + payload + codec_bytes

    @classmethod
    def from_bytes(cls, data: bytes) -> "ConnectAccept":
        if len(data) < HEADER_SIZE + 14:
            raise ValueError("packet too short for ConnectAccept")
        offset = HEADER_SIZE
        slot, w, h, fps = struct.unpack("!BHH B", data[offset:offset + 6])
        codec = data[offset + 6:offset + 14].rstrip(b"\x00").decode("utf-8", errors="replace")
        return cls(assigned_slot=slot, actual_width=w, actual_height=h,
                   actual_fps=fps, codec=codec)


@dataclasses.dataclass
class StreamStats:
    """Bidirectional stats for adaptive bitrate."""
    fps: int
    bitrate_kbps: int
    rtt_ms: int
    packet_loss_pct: float  # 0.0 - 100.0
    encode_time_ms: int
    decode_time_ms: int

    def to_bytes(self, seq: int) -> bytes:
        header = pack_header(PacketType.CONTROL_STATS, PacketFlags.NONE, seq, now_ms())
        # Pack loss as uint16 (0-10000 for 0.00-100.00%)
        loss_int = int(self.packet_loss_pct * 100)
        payload = struct.pack("!BIHH HH",
                              self.fps, self.bitrate_kbps, self.rtt_ms, loss_int,
                              self.encode_time_ms, self.decode_time_ms)
        return header + payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "StreamStats":
        if len(data) < HEADER_SIZE + 13:
            raise ValueError("packet too short for StreamStats")
        offset = HEADER_SIZE
        fps, bitrate, rtt, loss_int, enc, dec = struct.unpack("!BIHH HH", data[offset:offset + 13])
        return cls(fps=fps, bitrate_kbps=bitrate, rtt_ms=rtt,
                   packet_loss_pct=loss_int / 100.0,
                   encode_time_ms=enc, decode_time_ms=dec)


# =============================================================================
# Helpers
# =============================================================================

def now_ms() -> int:
    """Current time in milliseconds."""
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def parse_packet_type(data: bytes) -> PacketType:
    """Peek at the packet type without full parsing."""
    if len(data) < 1:
        raise ValueError("Empty packet")
    return PacketType(data[0])
