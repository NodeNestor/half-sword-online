"""
Half Sword Online — Remote Client

Connects to a host server, receives video stream, decodes and displays it,
and sends input back to the host.

Usage:
    python -m client.app --host 192.168.1.100:8080 --name "Player2"

Architecture:
    1. Sends ConnectRequest to host
    2. Receives ConnectAccept with assigned slot and stream params
    3. Receives H.264 video packets → reassembles → decodes via FFmpeg
    4. Displays decoded frames in a SDL2/pygame window
    5. Captures keyboard/mouse/gamepad input → sends to host via UDP
"""

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.protocol import (
    PacketType, PacketFlags, HEADER_SIZE, MAX_MTU, PROTOCOL_VERSION,
    pack_header, unpack_header, parse_packet_type, now_ms,
    ConnectRequest, ConnectAccept,
    VideoPacket,
    MouseInput, KeyboardInput,
)

logger = logging.getLogger(__name__)

# We use pygame for display + input capture (SDL2 under the hood)
try:
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False


# ---------------------------------------------------------------------------
# Frame Reassembler
# ---------------------------------------------------------------------------

class FrameReassembler:
    """
    Reassembles fragmented video packets into complete H.264 access units.

    Video frames are split into multiple UDP packets (fragments).
    This class collects fragments and emits complete frames.
    """

    def __init__(self):
        # { frame_number: { frag_index: data, ... } }
        self._pending: dict[int, dict[int, bytes]] = {}
        self._pending_counts: dict[int, int] = {}
        self._pending_keyframe: dict[int, bool] = {}
        self._last_complete = 0
        self.frames_completed = 0
        self.frames_dropped = 0

    def add_packet(self, pkt: VideoPacket) -> Optional[tuple[bytes, bool]]:
        """
        Add a video packet. Returns (frame_data, is_keyframe) if the
        frame is now complete, else None.
        """
        fn = pkt.frame_number

        # Discard old frames
        if fn <= self._last_complete:
            return None

        # Hard cap: prevent memory leak from incomplete frames
        if len(self._pending) > 100:
            cutoff = fn - 5
            stale = [k for k in self._pending if k < cutoff]
            for k in stale:
                del self._pending[k]
                del self._pending_counts[k]
                self._pending_keyframe.pop(k, None)
                self.frames_dropped += 1

        # Init frame entry
        if fn not in self._pending:
            self._pending[fn] = {}
            self._pending_counts[fn] = pkt.fragment_count
            self._pending_keyframe[fn] = pkt.is_keyframe

        if pkt.is_keyframe:
            self._pending_keyframe[fn] = True

        # Store fragment
        self._pending[fn][pkt.fragment_index] = pkt.data

        # Check if complete
        expected = self._pending_counts[fn]
        if len(self._pending[fn]) == expected:
            # Reassemble in order
            frame_data = b"".join(
                self._pending[fn][i] for i in range(expected)
            )
            is_kf = self._pending_keyframe.get(fn, False)

            # Cleanup
            del self._pending[fn]
            del self._pending_counts[fn]
            del self._pending_keyframe[fn]

            # Drop any older pending frames
            stale = [k for k in self._pending if k < fn]
            for k in stale:
                del self._pending[k]
                del self._pending_counts[k]
                self._pending_keyframe.pop(k, None)
                self.frames_dropped += 1

            self._last_complete = fn
            self.frames_completed += 1
            return frame_data, is_kf

        # Garbage collect very old pending frames (> 30 frames behind)
        stale = [k for k in self._pending if fn - k > 30]
        for k in stale:
            del self._pending[k]
            del self._pending_counts[k]
            self._pending_keyframe.pop(k, None)
            self.frames_dropped += 1

        return None


# ---------------------------------------------------------------------------
# Video Decoder (FFmpeg subprocess)
# ---------------------------------------------------------------------------

class VideoDecoder:
    """
    Decodes H.264 stream via FFmpeg subprocess.

    Input:  H.264 Annex B NAL units via stdin pipe
    Output: Raw RGB24 frames via stdout pipe
    """

    def __init__(self, width: int, height: int,
                 on_decoded_frame: callable):
        self.width = width
        self.height = height
        self.on_decoded_frame = on_decoded_frame
        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self.frames_decoded = 0

    def start(self):
        if self._running:
            return

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",

            # Input: H.264 from stdin
            "-f", "h264",
            "-i", "pipe:0",

            # Output: raw RGB24 to stdout
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-video_size", f"{self.width}x{self.height}",
            "pipe:1",
        ]

        # Try hardware decode first
        hwaccel_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-hwaccel", "auto",

            "-f", "h264",
            "-i", "pipe:0",

            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-video_size", f"{self.width}x{self.height}",
            "pipe:1",
        ]

        self._running = True

        # Try hardware decode, fall back to software
        try:
            self._process = subprocess.Popen(
                hwaccel_cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            logger.info("Decoder started with hardware acceleration")
        except Exception:
            self._process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            logger.info("Decoder started (software)")

        self._reader_thread = threading.Thread(
            target=self._read_frames, daemon=True, name="decoder-out")
        self._reader_thread.start()

        threading.Thread(target=self._read_stderr, daemon=True, name="decoder-err").start()

    def stop(self):
        self._running = False
        if self._process:
            if self._process.stdin:
                try: self._process.stdin.close()
                except: pass
            self._process.terminate()
            try: self._process.wait(timeout=3)
            except: self._process.kill()
            self._process = None

    def feed(self, h264_data: bytes):
        """Feed H.264 data to the decoder."""
        if not self._running or not self._process or not self._process.stdin:
            return
        try:
            self._process.stdin.write(h264_data)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            self._running = False

    def _read_frames(self):
        """Read decoded raw frames from FFmpeg stdout."""
        frame_size = self.width * self.height * 3  # RGB24
        buffer = bytearray()

        while self._running and self._process:
            try:
                chunk = self._process.stdout.read(min(65536, frame_size - len(buffer)))
                if not chunk:
                    break
                buffer.extend(chunk)

                while len(buffer) >= frame_size:
                    frame = bytes(buffer[:frame_size])
                    buffer = buffer[frame_size:]
                    self.frames_decoded += 1
                    self.on_decoded_frame(frame)
            except Exception as e:
                if self._running:
                    logger.error(f"Decoder read error: {e}")
                break

    def _read_stderr(self):
        while self._running and self._process:
            try:
                line = self._process.stderr.readline()
                if not line: break
                logger.debug(f"ffmpeg decoder: {line.decode(errors='replace').strip()}")
            except: break


# ---------------------------------------------------------------------------
# Display + Input (pygame/SDL2)
# ---------------------------------------------------------------------------

class GameWindow:
    """
    Fullscreen game window that displays decoded frames and captures input.
    """

    def __init__(self, width: int, height: int, title: str = "Half Sword Online"):
        if not HAS_PYGAME:
            raise RuntimeError("pygame not installed. Run: pip install pygame")

        self.width = width
        self.height = height
        self.title = title
        self._surface: Optional[pygame.Surface] = None
        self._screen: Optional[pygame.Surface] = None
        self._fullscreen = False
        self._running = False
        self._latest_frame: Optional[bytes] = None
        self._frame_lock = threading.Lock()
        self._frame_ready = threading.Event()

        # Input state
        self._mouse_grabbed = True
        self._input_callbacks: dict = {}

    def start(self):
        """Initialize pygame and create the window."""
        pygame.init()
        pygame.display.set_caption(self.title)

        self._screen = pygame.display.set_mode(
            (self.width, self.height),
            pygame.RESIZABLE | pygame.DOUBLEBUF
        )
        self._surface = pygame.Surface((self.width, self.height))

        # Grab mouse for FPS-style input
        pygame.event.set_grab(True)
        pygame.mouse.set_visible(False)

        self._running = True
        logger.info(f"Window created: {self.width}x{self.height}")

    def set_input_callback(self, event_type: str, callback):
        """Register input callbacks: 'mouse', 'keyboard', 'quit'"""
        self._input_callbacks[event_type] = callback

    def update_frame(self, rgb_data: bytes):
        """Queue a new frame for display (called from decoder thread)."""
        with self._frame_lock:
            self._latest_frame = rgb_data
        self._frame_ready.set()

    def run_loop(self):
        """
        Main display loop. Must run on the main thread (SDL requirement).
        Processes input events and renders frames.
        """
        clock = pygame.time.Clock()

        while self._running:
            # Process events
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._running = False
                    cb = self._input_callbacks.get("quit")
                    if cb: cb()
                    return

                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_F11:
                        self._toggle_fullscreen()
                    elif event.key == pygame.K_q and event.mod & pygame.KMOD_CTRL:
                        self._running = False
                        cb = self._input_callbacks.get("quit")
                        if cb: cb()
                        return
                    elif event.key == pygame.K_ESCAPE:
                        # Toggle mouse grab
                        self._mouse_grabbed = not self._mouse_grabbed
                        pygame.event.set_grab(self._mouse_grabbed)
                        pygame.mouse.set_visible(not self._mouse_grabbed)
                    else:
                        cb = self._input_callbacks.get("keyboard")
                        if cb: cb(self._sdl_to_vk(event.key), True)

                elif event.type == pygame.KEYUP:
                    cb = self._input_callbacks.get("keyboard")
                    if cb: cb(self._sdl_to_vk(event.key), False)

                elif event.type == pygame.MOUSEMOTION:
                    if self._mouse_grabbed:
                        cb = self._input_callbacks.get("mouse_move")
                        if cb: cb(event.rel[0], event.rel[1])

                elif event.type == pygame.MOUSEBUTTONDOWN:
                    cb = self._input_callbacks.get("mouse_button")
                    if cb: cb(event.button, True)

                elif event.type == pygame.MOUSEBUTTONUP:
                    cb = self._input_callbacks.get("mouse_button")
                    if cb: cb(event.button, False)

            # Render latest frame
            with self._frame_lock:
                frame = self._latest_frame
                self._latest_frame = None

            if frame and len(frame) == self.width * self.height * 3:
                try:
                    # Create surface from RGB data
                    frame_surface = pygame.image.frombuffer(frame, (self.width, self.height), "RGB")
                    # Scale to window size if needed
                    screen_size = self._screen.get_size()
                    if screen_size != (self.width, self.height):
                        frame_surface = pygame.transform.scale(frame_surface, screen_size)
                    self._screen.blit(frame_surface, (0, 0))
                    pygame.display.flip()
                except Exception as e:
                    logger.error(f"Render error: {e}")

            clock.tick(120)  # Cap at 120fps render loop (actual fps depends on stream)

    def stop(self):
        self._running = False
        pygame.quit()

    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self._screen = pygame.display.set_mode(
                (0, 0), pygame.FULLSCREEN | pygame.DOUBLEBUF)
        else:
            self._screen = pygame.display.set_mode(
                (self.width, self.height), pygame.RESIZABLE | pygame.DOUBLEBUF)

    @staticmethod
    def _sdl_to_vk(sdl_key: int) -> int:
        """Convert SDL key code to Windows virtual key code."""
        # Common mappings (pygame uses SDL keycodes)
        SDL_TO_VK = {
            pygame.K_w: 0x57, pygame.K_a: 0x41, pygame.K_s: 0x53, pygame.K_d: 0x44,
            pygame.K_q: 0x51, pygame.K_e: 0x45, pygame.K_r: 0x52, pygame.K_x: 0x58,
            pygame.K_SPACE: 0x20, pygame.K_LSHIFT: 0x10, pygame.K_RSHIFT: 0x10,
            pygame.K_ESCAPE: 0x1B, pygame.K_TAB: 0x09, pygame.K_RETURN: 0x0D,
            pygame.K_1: 0x31, pygame.K_2: 0x32, pygame.K_3: 0x33, pygame.K_4: 0x34,
            pygame.K_f: 0x46, pygame.K_g: 0x47, pygame.K_c: 0x43, pygame.K_v: 0x56,
        }
        return SDL_TO_VK.get(sdl_key, sdl_key & 0xFF)


# ---------------------------------------------------------------------------
# Client App
# ---------------------------------------------------------------------------

class ClientApp:
    """
    Main client application.
    Connects to host, receives video, displays it, sends input.
    """

    def __init__(self, host: str, port: int, name: str,
                 width: int, height: int, fps: int):
        self.host_addr = (host, port)
        self.name = name
        self.requested_width = width
        self.requested_height = height
        self.requested_fps = fps

        self.sock: Optional[socket.socket] = None
        self.slot = 0
        self.actual_width = width
        self.actual_height = height
        self.actual_fps = fps
        self.codec = "h264"

        self.reassembler = FrameReassembler()
        self.decoder: Optional[VideoDecoder] = None
        self.window: Optional[GameWindow] = None

        self._running = False
        self._connected = False
        self._seq = 0
        self._mouse_buttons = 0

        # Stats
        self._stats_start = 0.0
        self._packets_received = 0
        self._input_sent = 0

    def run(self):
        """Main entry point. Blocks until disconnected."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        self.sock.settimeout(0.1)

        # Connect to host
        if not self._connect():
            logger.error("Failed to connect to host")
            return

        self._running = True
        self._stats_start = time.monotonic()

        # Start decoder
        self.decoder = VideoDecoder(
            self.actual_width, self.actual_height,
            on_decoded_frame=self._on_decoded_frame,
        )
        self.decoder.start()

        # Start network receiver
        self._recv_thread = threading.Thread(
            target=self._receive_loop, daemon=True, name="net-recv")
        self._recv_thread.start()

        # Start keepalive
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True, name="keepalive")
        self._keepalive_thread.start()

        # Create display window and run (must be on main thread)
        self.window = GameWindow(self.actual_width, self.actual_height,
                                 f"Half Sword Online — {self.name}")
        self.window.start()

        # Wire up input callbacks
        self.window.set_input_callback("keyboard", self._on_keyboard)
        self.window.set_input_callback("mouse_move", self._on_mouse_move)
        self.window.set_input_callback("mouse_button", self._on_mouse_button)
        self.window.set_input_callback("quit", self._on_quit)

        logger.info(f"Connected to {self.host_addr} as slot {self.slot}")
        logger.info(f"Stream: {self.actual_width}x{self.actual_height}@{self.actual_fps}fps {self.codec}")
        logger.info("Controls: Esc=toggle mouse grab, F11=fullscreen, Ctrl+Q=quit")

        # Run display loop (blocks)
        self.window.run_loop()

        # Cleanup
        self._disconnect()

    def _connect(self) -> bool:
        """Send connect request and wait for accept/reject."""
        req = ConnectRequest(
            protocol_version=PROTOCOL_VERSION,
            player_name=self.name,
            requested_width=self.requested_width,
            requested_height=self.requested_height,
            requested_fps=self.requested_fps,
        )

        # Send request (retry a few times)
        for attempt in range(5):
            self._seq += 1
            self.sock.sendto(req.to_bytes(self._seq), self.host_addr)
            logger.info(f"Connect request sent (attempt {attempt + 1})")

            # Wait for response
            try:
                self.sock.settimeout(2.0)
                data, addr = self.sock.recvfrom(MAX_MTU)
                ptype = parse_packet_type(data)

                if ptype == PacketType.CONTROL_ACCEPT:
                    accept = ConnectAccept.from_bytes(data)
                    self.slot = accept.assigned_slot
                    self.actual_width = accept.actual_width
                    self.actual_height = accept.actual_height
                    self.actual_fps = accept.actual_fps
                    self.codec = accept.codec
                    self._connected = True
                    self.sock.settimeout(0.1)
                    return True

                elif ptype == PacketType.CONTROL_REJECT:
                    reason = data[HEADER_SIZE:].decode("utf-8", errors="replace")
                    logger.error(f"Connection rejected: {reason}")
                    return False

            except socket.timeout:
                continue

        return False

    def _disconnect(self):
        """Send disconnect and cleanup."""
        if self._connected:
            header = pack_header(PacketType.CONTROL_DISCONNECT, PacketFlags.NONE, 0, now_ms())
            try:
                self.sock.sendto(header, self.host_addr)
            except Exception:
                pass
            self._connected = False

        self._running = False

        if self.decoder:
            self.decoder.stop()
        if self.window:
            self.window.stop()
        if self.sock:
            self.sock.close()

        elapsed = time.monotonic() - self._stats_start
        if elapsed > 0:
            logger.info(f"Session: {elapsed:.0f}s, "
                        f"{self._packets_received} packets recv, "
                        f"{self._input_sent} input sent, "
                        f"{self.reassembler.frames_completed} frames decoded, "
                        f"{self.reassembler.frames_dropped} dropped")

    # -----------------------------------------------------------------------
    # Network
    # -----------------------------------------------------------------------

    def _receive_loop(self):
        """Receive UDP packets from host."""
        while self._running:
            try:
                data, addr = self.sock.recvfrom(MAX_MTU)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < HEADER_SIZE:
                continue

            self._packets_received += 1

            try:
                ptype = parse_packet_type(data)
            except ValueError:
                continue

            if ptype == PacketType.VIDEO_FRAME:
                self._handle_video(data)
            elif ptype == PacketType.CONTROL_KEEPALIVE_ACK:
                pass  # RTT measurement could go here
            elif ptype == PacketType.CONTROL_DISCONNECT:
                logger.info("Host disconnected us")
                self._running = False
                break

    def _handle_video(self, data: bytes):
        """Process a video packet."""
        try:
            pkt = VideoPacket.from_bytes(data)
        except Exception:
            return

        result = self.reassembler.add_packet(pkt)
        if result:
            frame_data, is_keyframe = result
            # Feed complete frame to decoder
            if self.decoder:
                self.decoder.feed(frame_data)

    def _keepalive_loop(self):
        while self._running:
            time.sleep(1.0)
            header = pack_header(PacketType.CONTROL_KEEPALIVE, PacketFlags.NONE, 0, now_ms())
            try:
                self.sock.sendto(header, self.host_addr)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Input → Host
    # -----------------------------------------------------------------------

    def _send_input(self, packet_bytes: bytes):
        if not self._connected:
            return
        try:
            self.sock.sendto(packet_bytes, self.host_addr)
            self._input_sent += 1
        except Exception:
            pass

    def _on_keyboard(self, vk_code: int, pressed: bool):
        kb = KeyboardInput(keycode=vk_code, pressed=pressed, timestamp_ms=now_ms())
        self._seq += 1
        self._send_input(kb.to_bytes(self._seq))

    def _on_mouse_move(self, dx: int, dy: int):
        mouse = MouseInput(dx=dx, dy=dy, buttons=self._mouse_buttons,
                           scroll=0, timestamp_ms=now_ms())
        self._seq += 1
        self._send_input(mouse.to_bytes(self._seq))

    def _on_mouse_button(self, button: int, pressed: bool):
        # pygame: 1=left, 2=middle, 3=right
        bit_map = {1: 0x01, 3: 0x02, 2: 0x04}
        bit = bit_map.get(button, 0)
        if pressed:
            self._mouse_buttons |= bit
        else:
            self._mouse_buttons &= ~bit

        mouse = MouseInput(dx=0, dy=0, buttons=self._mouse_buttons,
                           scroll=0, timestamp_ms=now_ms())
        self._seq += 1
        self._send_input(mouse.to_bytes(self._seq))

    def _on_decoded_frame(self, rgb_data: bytes):
        """Called from decoder thread with a decoded RGB frame."""
        if self.window:
            self.window.update_frame(rgb_data)

    def _on_quit(self):
        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Half Sword Online — Client")
    parser.add_argument("--host", required=True, help="Host address (IP:PORT)")
    parser.add_argument("--name", default="Player", help="Player name")
    parser.add_argument("--width", type=int, default=1920, help="Requested width")
    parser.add_argument("--height", type=int, default=1080, help="Requested height")
    parser.add_argument("--fps", type=int, default=60, help="Requested FPS")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Parse host:port
    if ":" in args.host:
        host, port = args.host.rsplit(":", 1)
        port = int(port)
    else:
        host = args.host
        port = 8080

    app = ClientApp(
        host=host, port=port, name=args.name,
        width=args.width, height=args.height, fps=args.fps,
    )
    app.run()


if __name__ == "__main__":
    main()
