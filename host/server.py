"""
Half Sword Online — Host Server

Manages remote player connections, capture pipelines, input injection,
and streaming. This is the main entry point for the host.

Usage:
    python -m host.server --port 8080 --max-players 4

Architecture:
    1. Listens for client connections on UDP port
    2. On connect: tells the Lua mod to spawn a player (via bridge file)
    3. Waits for FrameExport C++ plugin to create shared memory for that slot
    4. Starts capture pipeline: SharedMemory → NVENC → UDP stream
    5. Receives input from client → injects via ViGEmBus virtual gamepad
    6. Streams encoded video + audio back to client
"""

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Add parent to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.protocol import (
    PacketType, PacketFlags, HEADER_SIZE, MAX_MTU, PROTOCOL_VERSION,
    pack_header, unpack_header, parse_packet_type, now_ms,
    ConnectRequest, ConnectAccept, StreamStats,
    VideoPacket, fragment_video_frame,
    MouseInput, KeyboardInput, GamepadInput,
)
from shared.lobby import LobbyPacketType, LobbyState, SetTeamRequest, SetReadyRequest, ChatMessage
from host.capture import PlayerPipeline, EncoderConfig
from host.input_injector import InputInjectorManager, VirtualGamepad
from host.session_manager import SessionManager
from host.dashboard import HostDashboard
from client.connect_ui import LANBeacon

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bridge to Lua Mod
# ---------------------------------------------------------------------------

BRIDGE_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "HalfSwordUE5" / "Saved" / "HalfSwordOnline"


def send_mod_command(command: str):
    """Write a command to the bridge file for the Lua mod to read."""
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    cmd_file = BRIDGE_DIR / "host_commands.txt"

    # Append command (mod reads and deletes the file)
    with open(cmd_file, "a") as f:
        f.write(command + "\n")
    logger.info(f"Sent mod command: {command}")


def read_mod_state() -> dict:
    """Read the current mod state from the bridge file."""
    state_file = BRIDGE_DIR / "mod_state.txt"
    if not state_file.exists():
        return {}

    result = {}
    try:
        with open(state_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    result[key] = val
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Connected Client
# ---------------------------------------------------------------------------

@dataclass
class ConnectedClient:
    slot: int
    address: tuple  # (ip, port)
    name: str
    width: int
    height: int
    fps: int
    codec: str
    connected_at: float
    last_seen: float
    pipeline: Optional[PlayerPipeline] = None
    gamepad: Optional[VirtualGamepad] = None
    seq_out: int = 0
    frame_number: int = 0
    # Stats
    frames_sent: int = 0
    bytes_sent: int = 0
    input_packets_received: int = 0


# ---------------------------------------------------------------------------
# Host Server
# ---------------------------------------------------------------------------

class HostServer:
    def __init__(self, port: int = 8080, max_players: int = 4,
                 default_width: int = 1920, default_height: int = 1080,
                 default_fps: int = 60, default_bitrate: int = 15000):
        self.port = port
        self.max_players = max_players
        self.default_width = default_width
        self.default_height = default_height
        self.default_fps = default_fps
        self.default_bitrate = default_bitrate

        self.sock: Optional[socket.socket] = None
        self.clients: dict[int, ConnectedClient] = {}  # slot → client
        self.addr_to_slot: dict[tuple, int] = {}        # (ip,port) → slot
        self.input_mgr = InputInjectorManager()

        # Lobby & session manager (player-driven teams)
        self.session = SessionManager(
            host_name="Host", max_players=max_players)

        # LAN discovery beacon
        self.beacon = LANBeacon(
            game_port=port, host_name="Host", max_players=max_players)

        # Host dashboard (tkinter window)
        self.dashboard = HostDashboard(
            server_port=port, max_players=max_players,
            on_kick=self._kick_player,
            on_force_start=self._force_start_game,
            on_lock_teams=self._lock_teams,
        )

        self._running = False
        self._lock = threading.Lock()
        self._start_time = 0.0

    def start(self):
        """Start the host server."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(0.1)  # 100ms timeout for clean shutdown

        self._running = True

        logger.info(f"=== Half Sword Online Host Server ===")
        logger.info(f"Listening on UDP port {self.port}")
        logger.info(f"Max players: {self.max_players}")
        logger.info(f"Default stream: {self.default_width}x{self.default_height} "
                     f"@ {self.default_fps}fps, {self.default_bitrate}kbps")

        # Start receiver thread
        self._recv_thread = threading.Thread(
            target=self._receive_loop, daemon=True, name="udp-recv")
        self._recv_thread.start()

        # Start keepalive/cleanup thread
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True, name="keepalive")
        self._keepalive_thread.start()

        # Start LAN beacon so clients can discover us
        self.beacon.start()
        logger.info("LAN discovery beacon active")

        # Start lobby broadcast thread
        self._lobby_thread = threading.Thread(
            target=self._lobby_broadcast_loop, daemon=True, name="lobby")
        self._lobby_thread.start()

        # Start host dashboard window
        self.dashboard.start()
        logger.info("Host dashboard started")

        self._start_time = time.monotonic()
        logger.info("Server running. Waiting for connections...")

    def stop(self):
        """Stop the server and disconnect all clients."""
        self._running = False

        self.beacon.stop()
        self.dashboard.stop()

        # Stop all pipelines
        with self._lock:
            for slot, client in list(self.clients.items()):
                self._disconnect_client(slot)

        self.input_mgr.remove_all()

        if self.sock:
            self.sock.close()

        logger.info("Server stopped.")

    # -----------------------------------------------------------------------
    # Receive Loop
    # -----------------------------------------------------------------------

    def _receive_loop(self):
        """Main UDP receive loop."""
        # Rate limiter: track packet counts per address per second
        _rate_counts: dict[tuple, int] = {}
        _rate_window_start: float = time.monotonic()
        _RATE_LIMIT = 5000  # max packets per address per second

        while self._running:
            try:
                data, addr = self.sock.recvfrom(MAX_MTU)
            except socket.timeout:
                continue
            except OSError:
                break

            # Rate limiting per source address
            now = time.monotonic()
            if now - _rate_window_start >= 1.0:
                _rate_counts.clear()
                _rate_window_start = now
            _rate_counts[addr] = _rate_counts.get(addr, 0) + 1
            if _rate_counts[addr] > _RATE_LIMIT:
                continue

            if len(data) < HEADER_SIZE:
                continue

            try:
                ptype = parse_packet_type(data)
            except ValueError:
                continue

            # Route packet (check lobby packets too)
            ptype_int = data[0]

            if ptype == PacketType.CONTROL_CONNECT:
                self._handle_connect(data, addr)
            elif ptype == PacketType.CONTROL_DISCONNECT:
                self._handle_disconnect(addr)
            elif ptype == PacketType.CONTROL_KEEPALIVE:
                self._handle_keepalive(addr)
            elif ptype == PacketType.CONTROL_REQUEST_IDR:
                self._handle_idr_request(addr)
            elif ptype == PacketType.CONTROL_STATS:
                self._handle_stats(data, addr)
            elif ptype_int == LobbyPacketType.LOBBY_SET_TEAM:
                self._handle_lobby_set_team(data, addr)
            elif ptype_int == LobbyPacketType.LOBBY_SET_READY:
                self._handle_lobby_set_ready(data, addr)
            elif ptype_int == LobbyPacketType.LOBBY_CHAT:
                self._handle_lobby_chat(data, addr)
            elif ptype in (PacketType.INPUT_MOUSE, PacketType.INPUT_KEYBOARD,
                           PacketType.INPUT_GAMEPAD):
                self._handle_input(ptype, data, addr)

    # -----------------------------------------------------------------------
    # Connection Management
    # -----------------------------------------------------------------------

    def _handle_connect(self, data: bytes, addr: tuple):
        """Process a connection request from a client."""
        try:
            req = ConnectRequest.from_bytes(data)
        except Exception as e:
            logger.warning(f"Invalid connect request from {addr}: {e}")
            return

        # Check protocol version
        if req.protocol_version != PROTOCOL_VERSION:
            self._send_reject(addr, "Unsupported protocol version")
            return

        # Check if already connected
        if addr in self.addr_to_slot:
            slot = self.addr_to_slot[addr]
            logger.info(f"Reconnect from {addr} → slot {slot}")
            # Send accept again
            self._send_accept(addr, slot)
            return

        # Find a free slot
        slot = self._find_free_slot()
        if slot is None:
            self._send_reject(addr, "Server full")
            return

        # Negotiate resolution
        width = min(req.requested_width, self.default_width)
        height = min(req.requested_height, self.default_height)
        fps = min(req.requested_fps, self.default_fps)

        logger.info(f"New connection from {addr}: {req.player_name} "
                     f"→ slot {slot} ({width}x{height}@{fps}fps)")

        # Create client entry
        now = time.monotonic()
        client = ConnectedClient(
            slot=slot,
            address=addr,
            name=req.player_name,
            width=width,
            height=height,
            fps=fps,
            codec="h264",
            connected_at=now,
            last_seen=now,
        )

        with self._lock:
            self.clients[slot] = client
            self.addr_to_slot[addr] = slot

        # Register in session manager (lobby)
        self.session.on_player_join(slot, req.player_name, addr)

        # DON'T spawn yet — wait until player is ready in lobby
        # The session manager spawns when all players are ready

        # Create virtual gamepad for this player
        try:
            client.gamepad = self.input_mgr.add_player(slot)
        except Exception as e:
            logger.error(f"Failed to create gamepad for slot {slot}: {e}")

        # Start the capture pipeline in background
        # (waits for shared memory from C++ plugin)
        threading.Thread(
            target=self._start_pipeline, args=(slot,),
            daemon=True, name=f"pipeline-init-{slot}"
        ).start()

        # Send acceptance
        self._send_accept(addr, slot)

    def _start_pipeline(self, slot: int):
        """Start the capture + encode pipeline for a slot (runs in background)."""
        with self._lock:
            client = self.clients.get(slot)
        if not client:
            return

        encoder_cfg = EncoderConfig(
            bitrate_kbps=self.default_bitrate,
            max_bitrate_kbps=self.default_bitrate * 2,
        )

        def on_encoded_data(data: bytes, is_keyframe: bool):
            self._send_video(slot, data, is_keyframe)

        pipeline = PlayerPipeline(
            slot=slot,
            fps=client.fps,
            encoder_config=encoder_cfg,
            on_encoded_data=on_encoded_data,
        )

        logger.info(f"[Slot {slot}] Starting capture pipeline...")

        if pipeline.start():
            with self._lock:
                if slot in self.clients:
                    self.clients[slot].pipeline = pipeline
            logger.info(f"[Slot {slot}] Pipeline active — streaming to {client.address}")
        else:
            logger.error(f"[Slot {slot}] Failed to start pipeline")

    def _handle_disconnect(self, addr: tuple):
        """Client disconnected."""
        slot = self.addr_to_slot.get(addr)
        if slot:
            logger.info(f"Client at {addr} (slot {slot}) disconnecting")
            self._disconnect_client(slot)

    def _disconnect_client(self, slot: int):
        """Clean up everything for a disconnected client."""
        with self._lock:
            client = self.clients.pop(slot, None)
            if client:
                self.addr_to_slot.pop(client.address, None)

        if not client:
            return

        # Deregister from session manager (lobby)
        self.session.on_player_leave(slot)

        # Stop pipeline
        if client.pipeline:
            client.pipeline.stop()

        # Remove gamepad
        self.input_mgr.remove_player(slot)

        # Tell mod to remove player
        send_mod_command(f"remove {slot}")

        logger.info(f"[Slot {slot}] Disconnected. "
                     f"Sent {client.frames_sent} frames, "
                     f"{client.bytes_sent / 1024 / 1024:.1f} MB")

    def _find_free_slot(self) -> Optional[int]:
        for i in range(2, self.max_players + 2):
            if i not in self.clients:
                return i
        return None

    # -----------------------------------------------------------------------
    # Video Streaming
    # -----------------------------------------------------------------------

    def _send_video(self, slot: int, encoded_data: bytes, is_keyframe: bool):
        """Fragment and send encoded video data to the client."""
        with self._lock:
            client = self.clients.get(slot)
        if not client:
            return

        client.frame_number += 1
        timestamp = now_ms()

        # Fragment into MTU-sized packets
        packets = fragment_video_frame(
            encoded_data, client.frame_number, is_keyframe, timestamp
        )

        for pkt in packets:
            client.seq_out += 1
            raw = pkt.to_bytes(client.seq_out)
            try:
                self.sock.sendto(raw, client.address)
                client.bytes_sent += len(raw)
            except OSError as e:
                logger.warning(f"[Slot {slot}] Send error: {e}")
                break

        client.frames_sent += 1

    # -----------------------------------------------------------------------
    # Input Handling
    # -----------------------------------------------------------------------

    def _handle_input(self, ptype: PacketType, data: bytes, addr: tuple):
        """Process input from a remote client."""
        slot = self.addr_to_slot.get(addr)
        if not slot:
            return

        with self._lock:
            client = self.clients.get(slot)
        if not client or not client.gamepad:
            return

        client.last_seen = time.monotonic()
        client.input_packets_received += 1

        try:
            if ptype == PacketType.INPUT_MOUSE:
                mouse = MouseInput.from_bytes(data)
                client.gamepad.apply_mouse_input(mouse)
            elif ptype == PacketType.INPUT_KEYBOARD:
                kb = KeyboardInput.from_bytes(data)
                client.gamepad.apply_keyboard_input(kb)
            elif ptype == PacketType.INPUT_GAMEPAD:
                gp = GamepadInput.from_bytes(data)
                client.gamepad.apply_gamepad_input(gp)
        except Exception as e:
            logger.warning(f"[Slot {slot}] Input parse error: {e}")

    # -----------------------------------------------------------------------
    # Control Packets
    # -----------------------------------------------------------------------

    def _send_accept(self, addr: tuple, slot: int):
        client = self.clients.get(slot)
        if not client:
            return
        accept = ConnectAccept(
            assigned_slot=slot,
            actual_width=client.width,
            actual_height=client.height,
            actual_fps=client.fps,
            codec=client.codec,
        )
        self.sock.sendto(accept.to_bytes(0), addr)

    def _send_reject(self, addr: tuple, reason: str):
        header = pack_header(PacketType.CONTROL_REJECT, PacketFlags.NONE, 0, now_ms())
        msg = reason.encode("utf-8")[:64]
        self.sock.sendto(header + msg, addr)

    def _handle_keepalive(self, addr: tuple):
        slot = self.addr_to_slot.get(addr)
        if slot and slot in self.clients:
            self.clients[slot].last_seen = time.monotonic()
            # Send keepalive ack
            ack = pack_header(PacketType.CONTROL_KEEPALIVE_ACK, PacketFlags.NONE, 0, now_ms())
            self.sock.sendto(ack, addr)

    def _handle_idr_request(self, addr: tuple):
        slot = self.addr_to_slot.get(addr)
        if slot:
            client = self.clients.get(slot)
            if client and client.pipeline and client.pipeline.encoder:
                client.pipeline.encoder.force_keyframe()

    def _handle_stats(self, data: bytes, addr: tuple):
        slot = self.addr_to_slot.get(addr)
        if not slot:
            return
        try:
            stats = StreamStats.from_bytes(data)
            # Could use these stats for adaptive bitrate
            logger.debug(f"[Slot {slot}] Client stats: "
                         f"{stats.fps}fps, {stats.rtt_ms}ms RTT, "
                         f"{stats.packet_loss_pct:.1f}% loss")
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Lobby Handling
    # -----------------------------------------------------------------------

    def _handle_lobby_set_team(self, data: bytes, addr: tuple):
        slot = self.addr_to_slot.get(addr)
        if not slot:
            return
        try:
            req = SetTeamRequest.from_bytes(data)
            self.session.set_player_team(slot, req.team)
        except Exception as e:
            logger.warning(f"[Slot {slot}] Set team error: {e}")

    def _handle_lobby_set_ready(self, data: bytes, addr: tuple):
        slot = self.addr_to_slot.get(addr)
        if not slot:
            return
        try:
            req = SetReadyRequest.from_bytes(data)
            self.session.set_player_ready(slot, req.ready)

            # Check if game should start
            if self.session.should_start_game():
                logger.info("All players ready — starting game streams!")
                self.session.send_spawn_commands()
        except Exception as e:
            logger.warning(f"[Slot {slot}] Set ready error: {e}")

    def _handle_lobby_chat(self, data: bytes, addr: tuple):
        slot = self.addr_to_slot.get(addr)
        if not slot:
            return
        try:
            msg = ChatMessage.from_bytes(data)
            client = self.clients.get(slot)
            name = client.name if client else "?"
            logger.info(f"[Chat] {name}: {msg.text}")
            # Broadcast to all clients (relay the chat message)
            for other_slot, other_client in self.clients.items():
                if other_slot != slot:
                    try:
                        self.sock.sendto(data, other_client.address)
                    except OSError:
                        pass
        except Exception:
            pass

    def _lobby_broadcast_loop(self):
        """Broadcast lobby state to all connected clients periodically."""
        last_version = -1
        while self._running:
            time.sleep(0.5)  # 2 updates per second

            # Only broadcast if state changed
            if self.session.lobby_version == last_version:
                # Still update dashboard
                self._update_dashboard()
                continue
            last_version = self.session.lobby_version

            # Update beacon player count
            self.beacon.player_count = len(self.clients)

            # Build and send lobby state
            state = self.session.get_lobby_state()
            state_bytes = state.to_bytes(0)

            with self._lock:
                for client in self.clients.values():
                    try:
                        self.sock.sendto(state_bytes, client.address)
                    except OSError:
                        pass

            self._update_dashboard()

    def _update_dashboard(self):
        """Push current state to the host dashboard UI."""
        player_list = []
        with self._lock:
            for slot, client in self.clients.items():
                session = self.session.players.get(slot)
                player_list.append({
                    "slot": slot,
                    "name": client.name,
                    "team": session.team.display_name if session else "?",
                    "ready": session.ready if session else False,
                    "ping_ms": session.ping_ms if session else 0,
                    "resolution": f"{client.width}x{client.height}",
                    "bitrate_kbps": self.default_bitrate,
                    "fps": client.fps,
                })

        self.dashboard.update_players(player_list)

        uptime = time.monotonic() - self._start_time
        total_sent = sum(c.bytes_sent for c in self.clients.values())
        self.dashboard.update_stats({
            "uptime": f"{uptime / 60:.0f}m",
            "total_sent": f"{total_sent / 1024 / 1024:.1f}MB",
            "active_streams": str(sum(1 for c in self.clients.values() if c.pipeline)),
            "total_recv": f"{sum(c.input_packets_received for c in self.clients.values())}pkts",
        })

    # -----------------------------------------------------------------------
    # Dashboard Callbacks
    # -----------------------------------------------------------------------

    def _kick_player(self, slot: int):
        logger.info(f"Host kicking slot {slot}")
        self._disconnect_client(slot)

    def _force_start_game(self):
        logger.info("Host forcing game start")
        self.session.force_start()
        self.session.send_spawn_commands()

    def _lock_teams(self, locked: bool):
        self.session.allow_team_change = not locked
        logger.info(f"Teams {'locked' if locked else 'unlocked'}")

    # -----------------------------------------------------------------------
    # Keepalive / Cleanup
    # -----------------------------------------------------------------------

    def _keepalive_loop(self):
        """Periodically check for timed-out clients."""
        while self._running:
            time.sleep(1.0)
            now = time.monotonic()
            timeout = 5.0

            with self._lock:
                stale = [slot for slot, c in self.clients.items()
                         if now - c.last_seen > timeout]

            for slot in stale:
                logger.warning(f"[Slot {slot}] Client timed out")
                self._disconnect_client(slot)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Half Sword Online — Host Server")
    parser.add_argument("--port", type=int, default=8080, help="UDP port (default: 8080)")
    parser.add_argument("--max-players", type=int, default=4, help="Max remote players (default: 4)")
    parser.add_argument("--width", type=int, default=1920, help="Stream width (default: 1920)")
    parser.add_argument("--height", type=int, default=1080, help="Stream height (default: 1080)")
    parser.add_argument("--fps", type=int, default=60, help="Target FPS (default: 60)")
    parser.add_argument("--bitrate", type=int, default=15000, help="Bitrate in kbps (default: 15000)")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    server = HostServer(
        port=args.port,
        max_players=args.max_players,
        default_width=args.width,
        default_height=args.height,
        default_fps=args.fps,
        default_bitrate=args.bitrate,
    )

    try:
        server.start()
        # Block main thread
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
