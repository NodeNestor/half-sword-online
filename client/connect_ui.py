"""
Half Sword Online — Client Connect Screen

The first thing players see when they launch the client.
No command line needed — everything is visual.

Flow:
    1. App launches → shows connect screen
    2. Auto-scans LAN for hosts (UDP broadcast)
    3. Player can also type an IP manually
    4. Enter name → Connect → Lobby screen → Game stream
"""

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable

logger = logging.getLogger(__name__)

try:
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False


# ============================================================================
# LAN Discovery
# ============================================================================

DISCOVERY_PORT = 9001
DISCOVERY_MAGIC = b"HSONLINE"
DISCOVERY_INTERVAL = 2.0  # seconds between broadcasts


@dataclass
class DiscoveredHost:
    """A Half Sword Online host found on the LAN."""
    ip: str
    port: int
    host_name: str
    player_count: int
    max_players: int
    game_version: str
    last_seen: float = 0.0
    ping_ms: int = 0


class LANScanner:
    """
    Discovers Half Sword Online hosts on the local network.

    The host broadcasts a beacon packet on UDP port 9001 every 2 seconds.
    This scanner listens for those beacons and maintains a list of found hosts.
    """

    def __init__(self, on_host_found: Optional[Callable[[DiscoveredHost], None]] = None):
        self.on_host_found = on_host_found
        self.hosts: dict[str, DiscoveredHost] = {}  # ip:port → host
        self._running = False
        self._sock: Optional[socket.socket] = None

    def start(self):
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.settimeout(0.5)

        try:
            self._sock.bind(("", DISCOVERY_PORT))
        except OSError:
            # Port in use — try a random port and send broadcast queries instead
            self._sock.bind(("", 0))

        threading.Thread(target=self._listen, daemon=True, name="lan-scan").start()
        threading.Thread(target=self._send_queries, daemon=True, name="lan-query").start()

    def stop(self):
        self._running = False
        if self._sock:
            self._sock.close()

    def get_hosts(self) -> list[DiscoveredHost]:
        """Get list of recently seen hosts (last 10 seconds)."""
        now = time.monotonic()
        return [h for h in self.hosts.values() if now - h.last_seen < 10.0]

    def _listen(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(256)
                self._parse_beacon(data, addr)
            except socket.timeout:
                continue
            except OSError:
                break

    def _send_queries(self):
        """Send broadcast queries to find hosts."""
        while self._running:
            try:
                # Send "looking for hosts" broadcast
                query = DISCOVERY_MAGIC + b"\x01"  # type 1 = query
                self._sock.sendto(query, ("<broadcast>", DISCOVERY_PORT))
            except OSError:
                pass
            time.sleep(DISCOVERY_INTERVAL)

    def _parse_beacon(self, data: bytes, addr: tuple):
        if len(data) < 9 or data[:8] != DISCOVERY_MAGIC:
            return

        msg_type = data[8]

        if msg_type == 0x02:  # Beacon response
            try:
                # Format: magic(8) + type(1) + port(2) + players(1) + max(1) + name_len(1) + name + version_len(1) + version
                offset = 9
                game_port = struct.unpack("!H", data[offset:offset + 2])[0]
                offset += 2
                player_count = data[offset]
                offset += 1
                max_players = data[offset]
                offset += 1
                name_len = data[offset]
                offset += 1
                host_name = data[offset:offset + name_len].decode("utf-8", errors="replace")
                offset += name_len
                ver_len = data[offset]
                offset += 1
                game_version = data[offset:offset + ver_len].decode("utf-8", errors="replace")

                host = DiscoveredHost(
                    ip=addr[0],
                    port=game_port,
                    host_name=host_name,
                    player_count=player_count,
                    max_players=max_players,
                    game_version=game_version,
                    last_seen=time.monotonic(),
                )

                key = f"{addr[0]}:{game_port}"
                is_new = key not in self.hosts
                self.hosts[key] = host

                if is_new and self.on_host_found:
                    self.on_host_found(host)

            except (IndexError, struct.error):
                pass


class LANBeacon:
    """
    Broadcasts presence on LAN so clients can discover this host.
    Run this on the host side.
    """

    def __init__(self, game_port: int, host_name: str, max_players: int):
        self.game_port = game_port
        self.host_name = host_name
        self.max_players = max_players
        self.player_count = 0
        self._running = False
        self._sock: Optional[socket.socket] = None

    def start(self):
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Also listen for queries
        self._listen_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._listen_sock.settimeout(0.5)
        try:
            self._listen_sock.bind(("", DISCOVERY_PORT))
        except OSError:
            logger.warning("Could not bind discovery port — LAN discovery may not work")

        threading.Thread(target=self._broadcast_loop, daemon=True, name="beacon").start()
        threading.Thread(target=self._listen_loop, daemon=True, name="beacon-listen").start()

    def stop(self):
        self._running = False
        if self._sock:
            self._sock.close()
        if self._listen_sock:
            self._listen_sock.close()

    def _build_beacon(self) -> bytes:
        name_b = self.host_name.encode("utf-8")[:32]
        ver_b = b"1.0"
        return (DISCOVERY_MAGIC +
                bytes([0x02]) +  # type = beacon
                struct.pack("!H", self.game_port) +
                bytes([self.player_count, self.max_players]) +
                bytes([len(name_b)]) + name_b +
                bytes([len(ver_b)]) + ver_b)

    def _broadcast_loop(self):
        while self._running:
            try:
                beacon = self._build_beacon()
                self._sock.sendto(beacon, ("<broadcast>", DISCOVERY_PORT))
            except OSError:
                pass
            time.sleep(DISCOVERY_INTERVAL)

    def _listen_loop(self):
        """Respond to direct queries."""
        while self._running:
            try:
                data, addr = self._listen_sock.recvfrom(64)
                if len(data) >= 9 and data[:8] == DISCOVERY_MAGIC and data[8] == 0x01:
                    # Query received — respond directly
                    beacon = self._build_beacon()
                    self._listen_sock.sendto(beacon, addr)
            except socket.timeout:
                continue
            except OSError:
                break


# ============================================================================
# Colors
# ============================================================================

class C:
    BG = (18, 18, 28)
    PANEL = (28, 28, 42)
    CARD = (38, 38, 58)
    CARD_HOVER = (50, 50, 75)
    TEXT = (210, 210, 220)
    DIM = (130, 130, 150)
    BRIGHT = (255, 255, 255)
    ACCENT = (100, 140, 255)
    GREEN = (50, 200, 80)
    RED = (200, 60, 60)
    GOLD = (200, 180, 100)
    INPUT_BG = (22, 22, 35)
    INPUT_BORDER = (70, 70, 100)
    INPUT_ACTIVE = (100, 140, 255)
    BUTTON = (70, 80, 130)
    BUTTON_HOVER = (90, 100, 160)


# ============================================================================
# Connect Screen
# ============================================================================

class ConnectScreen:
    """
    The main connect screen UI. Players see this when they launch the app.
    """

    def __init__(self, width: int = 700, height: int = 650):
        if not HAS_PYGAME:
            raise RuntimeError("pygame not installed")

        self.width = width
        self.height = height

        # Input fields
        self.name_input = "Player"
        self.ip_input = ""
        self.active_field: Optional[str] = None  # "name", "ip"

        # Settings
        self.resolution_options = ["1920x1080", "2560x1440", "1280x720", "960x540"]
        self.fps_options = ["60", "30", "120"]
        self.selected_resolution = 0
        self.selected_fps = 0

        # State
        self.status = "Scanning LAN for games..."
        self.status_color = C.DIM
        self.connecting = False

        # LAN scanner
        self.scanner = LANScanner()

        # Result
        self.result: Optional[dict] = None  # Set when user connects

        # Pygame
        self._screen: Optional[pygame.Surface] = None
        self._font: Optional[pygame.font.Font] = None
        self._font_big: Optional[pygame.font.Font] = None
        self._font_small: Optional[pygame.font.Font] = None
        self._running = False

        # Button rects
        self._connect_btn: Optional[pygame.Rect] = None
        self._lan_host_rects: list[tuple[pygame.Rect, DiscoveredHost]] = []
        self._res_btn: Optional[pygame.Rect] = None
        self._fps_btn: Optional[pygame.Rect] = None

    def run(self) -> Optional[dict]:
        """
        Show the connect screen. Blocks until user connects or quits.

        Returns dict with connection info, or None if user quit:
        {
            "host": "192.168.1.5",
            "port": 8080,
            "name": "Player2",
            "width": 1920,
            "height": 1080,
            "fps": 60,
        }
        """
        pygame.init()
        pygame.display.set_caption("Half Sword Online")

        self._screen = pygame.display.set_mode(
            (self.width, self.height), pygame.RESIZABLE)
        self._font = pygame.font.SysFont("Segoe UI", 18)
        self._font_big = pygame.font.SysFont("Segoe UI", 32, bold=True)
        self._font_small = pygame.font.SysFont("Segoe UI", 14)

        self._running = True
        self.scanner.start()

        clock = pygame.time.Clock()

        while self._running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._running = False
                    self.scanner.stop()
                    pygame.quit()
                    return None

                elif event.type == pygame.KEYDOWN:
                    self._handle_key(event)

                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self._handle_click(event.pos)

                elif event.type == pygame.VIDEORESIZE:
                    self.width, self.height = event.size

            self._render()
            pygame.display.flip()
            clock.tick(30)

            if self.result:
                self.scanner.stop()
                return self.result

        self.scanner.stop()
        return None

    def _handle_key(self, event):
        if event.key == pygame.K_ESCAPE:
            if self.active_field:
                self.active_field = None
            else:
                self._running = False
            return

        if event.key == pygame.K_TAB:
            # Cycle fields
            fields = ["name", "ip"]
            if self.active_field in fields:
                idx = fields.index(self.active_field)
                self.active_field = fields[(idx + 1) % len(fields)]
            else:
                self.active_field = "name"
            return

        if event.key == pygame.K_RETURN:
            if not self.active_field:
                self._try_connect()
            elif self.active_field == "ip":
                self._try_connect()
            else:
                self.active_field = None
            return

        if not self.active_field:
            return

        # Text input
        field_map = {"name": "name_input", "ip": "ip_input"}
        attr = field_map.get(self.active_field)
        if not attr:
            return

        current = getattr(self, attr)
        if event.key == pygame.K_BACKSPACE:
            setattr(self, attr, current[:-1])
        elif event.unicode and len(current) < 64:
            setattr(self, attr, current + event.unicode)

    def _handle_click(self, pos):
        # Check LAN host join buttons
        for rect, host in self._lan_host_rects:
            if rect.collidepoint(pos):
                self.ip_input = f"{host.ip}:{host.port}"
                self._try_connect()
                return

        # Check connect button
        if self._connect_btn and self._connect_btn.collidepoint(pos):
            self._try_connect()
            return

        # Check resolution toggle
        if self._res_btn and self._res_btn.collidepoint(pos):
            self.selected_resolution = (self.selected_resolution + 1) % len(self.resolution_options)
            return

        # Check FPS toggle
        if self._fps_btn and self._fps_btn.collidepoint(pos):
            self.selected_fps = (self.selected_fps + 1) % len(self.fps_options)
            return

        # Check field clicks
        # (We check during render and store rects)
        self.active_field = None
        for field_name, rect in getattr(self, '_field_rects', {}).items():
            if rect.collidepoint(pos):
                self.active_field = field_name
                return

    def _try_connect(self):
        if self.connecting:
            return

        ip_str = self.ip_input.strip()
        if not ip_str:
            self.status = "Enter a host IP address or select a LAN game"
            self.status_color = C.RED
            return

        # Parse host:port
        if ":" in ip_str:
            host, port_str = ip_str.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                self.status = "Invalid port number"
                self.status_color = C.RED
                return
        else:
            host = ip_str
            port = 8080

        if not (1 <= port <= 65535):
            self.status = "Port must be between 1 and 65535"
            self.status_color = C.RED
            return

        # Parse resolution
        res = self.resolution_options[self.selected_resolution]
        w, h = map(int, res.split("x"))
        fps = int(self.fps_options[self.selected_fps])

        name = self.name_input.strip() or "Player"

        self.status = f"Connecting to {host}:{port}..."
        self.status_color = C.ACCENT
        self.connecting = True

        self.result = {
            "host": host,
            "port": port,
            "name": name,
            "width": w,
            "height": h,
            "fps": fps,
        }

    def _render(self):
        self._screen.fill(C.BG)
        w = self.width
        mouse = pygame.mouse.get_pos()
        self._field_rects = {}
        self._lan_host_rects = []

        y = 20

        # Title
        title = self._font_big.render("HALF SWORD ONLINE", True, C.GOLD)
        self._screen.blit(title, (w // 2 - title.get_width() // 2, y))
        y += 50

        subtitle = self._font_small.render(
            "Only the HOST needs the game. You just need this app.", True, C.ACCENT)
        self._screen.blit(subtitle, (w // 2 - subtitle.get_width() // 2, y))
        y += 30

        # LAN Games
        hosts = self.scanner.get_hosts()
        panel_x = 40
        panel_w = w - 80

        label = self._font.render("LAN Games Found", True, C.DIM)
        self._screen.blit(label, (panel_x, y))
        y += 25

        if hosts:
            for host in hosts:
                card = pygame.Rect(panel_x, y, panel_w, 50)
                hover = card.collidepoint(mouse)
                pygame.draw.rect(self._screen, C.CARD_HOVER if hover else C.CARD,
                                 card, border_radius=6)

                # Host info
                name_s = self._font.render(f"{host.host_name}'s Game", True, C.BRIGHT)
                self._screen.blit(name_s, (card.x + 12, card.y + 5))

                info = f"{host.player_count}/{host.max_players} players  •  {host.ip}:{host.port}"
                info_s = self._font_small.render(info, True, C.DIM)
                self._screen.blit(info_s, (card.x + 12, card.y + 28))

                # Join button
                join_rect = pygame.Rect(card.right - 80, card.y + 10, 65, 30)
                join_hover = join_rect.collidepoint(mouse)
                pygame.draw.rect(self._screen, C.GREEN if join_hover else C.BUTTON,
                                 join_rect, border_radius=5)
                join_text = self._font_small.render("JOIN", True, C.BRIGHT)
                self._screen.blit(join_text, (join_rect.centerx - join_text.get_width() // 2,
                                              join_rect.centery - join_text.get_height() // 2))
                self._lan_host_rects.append((join_rect, host))

                y += 55
        else:
            no_hosts = self._font_small.render("No games found on LAN. Try connecting manually below.",
                                                True, C.DIM)
            self._screen.blit(no_hosts, (panel_x + 10, y + 5))
            y += 30

        y += 15

        # Divider
        pygame.draw.line(self._screen, C.PANEL, (panel_x, y), (panel_x + panel_w, y))
        y += 15

        # Manual connect
        label = self._font.render("Connect Manually", True, C.DIM)
        self._screen.blit(label, (panel_x, y))
        y += 28

        # IP input
        y = self._render_input_field("Host IP:", "ip", self.ip_input,
                                      "192.168.1.100:8080", panel_x, y, panel_w)
        y += 10

        # Divider
        pygame.draw.line(self._screen, C.PANEL, (panel_x, y), (panel_x + panel_w, y))
        y += 15

        # Player settings
        label = self._font.render("Settings", True, C.DIM)
        self._screen.blit(label, (panel_x, y))
        y += 28

        # Name
        y = self._render_input_field("Your Name:", "name", self.name_input,
                                      "Player", panel_x, y, panel_w)
        y += 12

        # Resolution + FPS on same line
        res_label = self._font_small.render("Resolution:", True, C.DIM)
        self._screen.blit(res_label, (panel_x, y + 4))

        res_btn = pygame.Rect(panel_x + 90, y, 120, 28)
        self._res_btn = res_btn
        res_hover = res_btn.collidepoint(mouse)
        pygame.draw.rect(self._screen, C.CARD_HOVER if res_hover else C.CARD,
                         res_btn, border_radius=4)
        res_text = self._font_small.render(self.resolution_options[self.selected_resolution],
                                            True, C.BRIGHT)
        self._screen.blit(res_text, (res_btn.x + 8, res_btn.y + 5))

        fps_label = self._font_small.render("FPS:", True, C.DIM)
        self._screen.blit(fps_label, (panel_x + 240, y + 4))

        fps_btn = pygame.Rect(panel_x + 280, y, 55, 28)
        self._fps_btn = fps_btn
        fps_hover = fps_btn.collidepoint(mouse)
        pygame.draw.rect(self._screen, C.CARD_HOVER if fps_hover else C.CARD,
                         fps_btn, border_radius=4)
        fps_text = self._font_small.render(self.fps_options[self.selected_fps],
                                            True, C.BRIGHT)
        self._screen.blit(fps_text, (fps_btn.x + 8, fps_btn.y + 5))

        y += 45

        # Connect button
        btn_w = 200
        btn_h = 48
        btn_rect = pygame.Rect(w // 2 - btn_w // 2, y, btn_w, btn_h)
        self._connect_btn = btn_rect
        btn_hover = btn_rect.collidepoint(mouse)

        if self.connecting:
            btn_color = C.DIM
        elif btn_hover:
            btn_color = C.GREEN
        else:
            btn_color = C.BUTTON

        pygame.draw.rect(self._screen, btn_color, btn_rect, border_radius=10)
        btn_label = "CONNECTING..." if self.connecting else "CONNECT"
        btn_text = self._font_big.render(btn_label, True, C.BRIGHT)
        self._screen.blit(btn_text, (btn_rect.centerx - btn_text.get_width() // 2,
                                     btn_rect.centery - btn_text.get_height() // 2))

        y += btn_h + 15

        # Status
        status_s = self._font_small.render(self.status, True, self.status_color)
        self._screen.blit(status_s, (w // 2 - status_s.get_width() // 2, y))

    def _render_input_field(self, label: str, field_name: str, value: str,
                             placeholder: str, x: int, y: int, panel_w: int) -> int:
        """Render a labeled text input field. Returns new y position."""
        is_active = self.active_field == field_name

        # Label
        label_s = self._font_small.render(label, True, C.DIM)
        self._screen.blit(label_s, (x, y + 4))

        # Input box
        input_x = x + 90
        input_w = panel_w - 90
        input_rect = pygame.Rect(input_x, y, input_w, 28)
        self._field_rects[field_name] = input_rect

        border_color = C.INPUT_ACTIVE if is_active else C.INPUT_BORDER
        pygame.draw.rect(self._screen, C.INPUT_BG, input_rect, border_radius=4)
        pygame.draw.rect(self._screen, border_color, input_rect, 1, border_radius=4)

        # Text or placeholder
        if value:
            display = value
            if is_active and int(time.time() * 2) % 2:
                display += "│"
            text_s = self._font.render(display, True, C.BRIGHT)
        else:
            display = placeholder
            if is_active:
                display = "│"
            text_s = self._font.render(display, True, C.DIM)

        # Clip text to input box
        self._screen.blit(text_s, (input_rect.x + 6, input_rect.y + 3),
                          area=pygame.Rect(0, 0, input_w - 12, 24))

        return y + 32
