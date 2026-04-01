"""
Half Sword Online — Client Lobby UI

A proper lobby screen rendered with pygame where players can:
- See all connected players and their teams
- Click to pick their team (Allies / Enemies / FFA / Team A / Team B)
- Toggle ready status
- See ping and player names
- Chat with other players
- Wait for host to start the game

This makes the client feel like a real game, not just a stream viewer.
The lobby is shown BEFORE the game stream starts.
"""

import logging
import time
from typing import Optional, Callable

logger = logging.getLogger(__name__)

try:
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.lobby import (
    LobbyState, TeamChoice,
)

# ============================================================================
# Colors & Layout
# ============================================================================

class Colors:
    BG = (20, 20, 30)
    BG_PANEL = (30, 30, 45)
    BG_CARD = (40, 40, 60)
    BG_CARD_HOVER = (50, 50, 75)
    BG_CARD_SELF = (45, 45, 70)
    TEXT = (220, 220, 230)
    TEXT_DIM = (140, 140, 160)
    TEXT_BRIGHT = (255, 255, 255)
    ACCENT = (100, 140, 255)
    READY_GREEN = (50, 200, 80)
    NOT_READY = (200, 80, 50)
    BORDER = (60, 60, 80)
    CHAT_BG = (25, 25, 40)
    BUTTON = (70, 70, 100)
    BUTTON_HOVER = (90, 90, 130)
    BUTTON_ACTIVE = (100, 140, 255)
    TITLE = (200, 180, 100)


# ============================================================================
# Lobby UI
# ============================================================================

class LobbyUI:
    """
    Renders and handles the pre-game lobby.

    Shows:
    ┌─────────────────────────────────────────┐
    │  HALF SWORD ONLINE                      │
    │  Lobby: HostName's Game                 │
    │                                         │
    │  ┌─────────────────────────────────┐    │
    │  │ Player1 (Host)    [Allies] ✓    │    │
    │  │ Player2            [Allies] ✓    │    │
    │  │ Player3            [Enemies]     │    │
    │  │ Player4            [FFA]    ✓    │    │
    │  │ (waiting...)                     │    │
    │  └─────────────────────────────────┘    │
    │                                         │
    │  YOUR TEAM:                             │
    │  [Allies] [Enemies] [FFA] [Team A] [B]  │
    │                                         │
    │  [ READY ]                              │
    │                                         │
    │  ┌── Chat ──────────────────────────┐   │
    │  │ Player2: gl hf                   │   │
    │  │ Player3: im going rogue          │   │
    │  └──────────────────────────────────┘   │
    └─────────────────────────────────────────┘
    """

    def __init__(self, width: int = 800, height: int = 600,
                 my_slot: int = 0, my_name: str = "Player"):
        if not HAS_PYGAME:
            raise RuntimeError("pygame required for lobby UI")

        self.width = width
        self.height = height
        self.my_slot = my_slot
        self.my_name = my_name

        self.lobby_state: Optional[LobbyState] = None
        self.my_team = TeamChoice.UNDECIDED
        self.my_ready = False
        self.chat_messages: list[tuple[str, str, float]] = []  # (name, text, time)
        self.chat_input = ""
        self.chat_active = False

        # Callbacks
        self.on_set_team: Optional[Callable[[TeamChoice], None]] = None
        self.on_set_ready: Optional[Callable[[bool], None]] = None
        self.on_send_chat: Optional[Callable[[str], None]] = None
        self.on_quit: Optional[Callable[[], None]] = None

        # UI state
        self._screen: Optional[pygame.Surface] = None
        self._font: Optional[pygame.font.Font] = None
        self._font_big: Optional[pygame.font.Font] = None
        self._font_small: Optional[pygame.font.Font] = None
        self._running = False
        self._game_started = False

        # Button rects (computed during render)
        self._team_buttons: dict[TeamChoice, pygame.Rect] = {}
        self._ready_button_rect: Optional[pygame.Rect] = None

    def start(self):
        """Initialize pygame and show the lobby window."""
        pygame.init()
        pygame.display.set_caption("Half Sword Online — Lobby")

        self._screen = pygame.display.set_mode(
            (self.width, self.height), pygame.RESIZABLE)

        self._font = pygame.font.SysFont("Segoe UI", 18)
        self._font_big = pygame.font.SysFont("Segoe UI", 28, bold=True)
        self._font_small = pygame.font.SysFont("Segoe UI", 14)

        self._running = True

    def update_state(self, state: LobbyState):
        """Called when new lobby state arrives from host."""
        self.lobby_state = state
        # Check if game started
        # (host sends a specific message or packet to trigger this)

    def add_chat_message(self, sender: str, text: str):
        """Add a chat message to the display."""
        self.chat_messages.append((sender, text, time.monotonic()))
        # Keep last 50 messages
        if len(self.chat_messages) > 50:
            self.chat_messages = self.chat_messages[-50:]

    @property
    def game_started(self) -> bool:
        return self._game_started

    def run_frame(self) -> bool:
        """
        Process one frame of the lobby UI.
        Returns False if the lobby should close (quit or game started).
        Must be called from the main thread.
        """
        if not self._running:
            return False

        # Handle events
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._running = False
                if self.on_quit:
                    self.on_quit()
                return False

            elif event.type == pygame.KEYDOWN:
                if self.chat_active:
                    self._handle_chat_key(event)
                else:
                    if event.key == pygame.K_RETURN:
                        self.chat_active = True
                    elif event.key == pygame.K_r:
                        self._toggle_ready()
                    elif event.key == pygame.K_ESCAPE:
                        self._running = False
                        if self.on_quit:
                            self.on_quit()
                        return False
                    elif event.key == pygame.K_1:
                        self._set_team(TeamChoice.ALLIES)
                    elif event.key == pygame.K_2:
                        self._set_team(TeamChoice.ENEMIES)
                    elif event.key == pygame.K_3:
                        self._set_team(TeamChoice.FFA)
                    elif event.key == pygame.K_4:
                        self._set_team(TeamChoice.TEAM_A)
                    elif event.key == pygame.K_5:
                        self._set_team(TeamChoice.TEAM_B)

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self._handle_click(event.pos)

            elif event.type == pygame.VIDEORESIZE:
                self.width, self.height = event.size

        # Render
        self._render()
        pygame.display.flip()

        return self._running and not self._game_started

    def stop(self):
        self._running = False

    # -----------------------------------------------------------------------
    # Input handlers
    # -----------------------------------------------------------------------

    def _set_team(self, team: TeamChoice):
        if self.lobby_state and not self.lobby_state.allow_team_change:
            return
        if team == TeamChoice.FFA and self.lobby_state and not self.lobby_state.allow_ffa:
            return
        self.my_team = team
        if self.on_set_team:
            self.on_set_team(team)

    def _toggle_ready(self):
        self.my_ready = not self.my_ready
        if self.on_set_ready:
            self.on_set_ready(self.my_ready)

    def _handle_chat_key(self, event):
        if event.key == pygame.K_RETURN:
            if self.chat_input.strip():
                if self.on_send_chat:
                    self.on_send_chat(self.chat_input.strip())
                self.add_chat_message(self.my_name, self.chat_input.strip())
                self.chat_input = ""
            self.chat_active = False
        elif event.key == pygame.K_ESCAPE:
            self.chat_input = ""
            self.chat_active = False
        elif event.key == pygame.K_BACKSPACE:
            self.chat_input = self.chat_input[:-1]
        elif event.unicode and len(self.chat_input) < 128:
            self.chat_input += event.unicode

    def _handle_click(self, pos):
        # Check team buttons
        for team, rect in self._team_buttons.items():
            if rect.collidepoint(pos):
                self._set_team(team)
                return

        # Check ready button
        if self._ready_button_rect and self._ready_button_rect.collidepoint(pos):
            self._toggle_ready()
            return

    # -----------------------------------------------------------------------
    # Rendering
    # -----------------------------------------------------------------------

    def _render(self):
        self._screen.fill(Colors.BG)
        w, h = self.width, self.height
        mouse_pos = pygame.mouse.get_pos()

        # Title
        title = self._font_big.render("HALF SWORD ONLINE", True, Colors.TITLE)
        self._screen.blit(title, (w // 2 - title.get_width() // 2, 20))

        # Subtitle
        host_name = self.lobby_state.host_name if self.lobby_state else "..."
        sub = self._font.render(f"Lobby — {host_name}'s Game", True, Colors.TEXT_DIM)
        self._screen.blit(sub, (w // 2 - sub.get_width() // 2, 55))

        # Status message
        if self.lobby_state and self.lobby_state.message:
            msg = self._font_small.render(self.lobby_state.message, True, Colors.ACCENT)
            self._screen.blit(msg, (w // 2 - msg.get_width() // 2, 80))

        # Player list panel
        panel_x = 40
        panel_y = 100
        panel_w = w - 80
        panel_h = 220
        pygame.draw.rect(self._screen, Colors.BG_PANEL,
                         (panel_x, panel_y, panel_w, panel_h), border_radius=8)
        pygame.draw.rect(self._screen, Colors.BORDER,
                         (panel_x, panel_y, panel_w, panel_h), 1, border_radius=8)

        # Column headers
        hdr_y = panel_y + 8
        self._screen.blit(self._font_small.render("Player", True, Colors.TEXT_DIM),
                          (panel_x + 15, hdr_y))
        self._screen.blit(self._font_small.render("Team", True, Colors.TEXT_DIM),
                          (panel_x + panel_w - 220, hdr_y))
        self._screen.blit(self._font_small.render("Ping", True, Colors.TEXT_DIM),
                          (panel_x + panel_w - 100, hdr_y))
        self._screen.blit(self._font_small.render("Ready", True, Colors.TEXT_DIM),
                          (panel_x + panel_w - 50, hdr_y))

        # Player entries
        players = self.lobby_state.players if self.lobby_state else []
        card_y = panel_y + 30
        for i, p in enumerate(players):
            is_me = p.slot == self.my_slot
            card_rect = pygame.Rect(panel_x + 5, card_y, panel_w - 10, 32)

            # Card background
            bg = Colors.BG_CARD_SELF if is_me else Colors.BG_CARD
            if card_rect.collidepoint(mouse_pos) and not is_me:
                bg = Colors.BG_CARD_HOVER
            pygame.draw.rect(self._screen, bg, card_rect, border_radius=4)

            # Name
            name_str = p.name
            if p.is_host:
                name_str += " (Host)"
            if is_me:
                name_str += " (You)"
            name_surf = self._font.render(name_str, True,
                                          Colors.TEXT_BRIGHT if is_me else Colors.TEXT)
            self._screen.blit(name_surf, (card_rect.x + 10, card_rect.y + 6))

            # Team badge
            team_color = p.team.color
            team_text = p.team.display_name
            team_surf = self._font_small.render(team_text, True, team_color)
            self._screen.blit(team_surf, (panel_x + panel_w - 220, card_rect.y + 8))

            # Ping
            ping_surf = self._font_small.render(f"{p.ping_ms}ms", True, Colors.TEXT_DIM)
            self._screen.blit(ping_surf, (panel_x + panel_w - 100, card_rect.y + 8))

            # Ready indicator
            ready_color = Colors.READY_GREEN if p.ready else Colors.NOT_READY
            ready_text = "✓" if p.ready else "✗"
            ready_surf = self._font.render(ready_text, True, ready_color)
            self._screen.blit(ready_surf, (panel_x + panel_w - 40, card_rect.y + 5))

            card_y += 36

        # Empty slots
        max_p = self.lobby_state.max_players if self.lobby_state else 8
        for i in range(len(players), max_p):
            empty_surf = self._font_small.render("(empty slot)", True, Colors.TEXT_DIM)
            self._screen.blit(empty_surf, (panel_x + 15, card_y + 8))
            card_y += 36

        # Team selection buttons
        team_y = panel_y + panel_h + 20
        label = self._font.render("YOUR TEAM:", True, Colors.TEXT)
        self._screen.blit(label, (panel_x, team_y))

        btn_x = panel_x + 140
        btn_w = 90
        btn_h = 34
        self._team_buttons.clear()

        teams_to_show = [TeamChoice.ALLIES, TeamChoice.ENEMIES, TeamChoice.FFA,
                         TeamChoice.TEAM_A, TeamChoice.TEAM_B]

        for team in teams_to_show:
            if team == TeamChoice.FFA and self.lobby_state and not self.lobby_state.allow_ffa:
                continue

            rect = pygame.Rect(btn_x, team_y - 3, btn_w, btn_h)
            self._team_buttons[team] = rect

            # Button style
            is_selected = self.my_team == team
            is_hover = rect.collidepoint(mouse_pos)

            if is_selected:
                bg_color = team.color
            elif is_hover:
                bg_color = Colors.BUTTON_HOVER
            else:
                bg_color = Colors.BUTTON

            pygame.draw.rect(self._screen, bg_color, rect, border_radius=6)
            if is_selected:
                pygame.draw.rect(self._screen, Colors.TEXT_BRIGHT, rect, 2, border_radius=6)

            text_color = Colors.TEXT_BRIGHT if is_selected else Colors.TEXT
            btn_text = self._font_small.render(team.display_name, True, text_color)
            self._screen.blit(btn_text,
                              (rect.centerx - btn_text.get_width() // 2,
                               rect.centery - btn_text.get_height() // 2))

            btn_x += btn_w + 8

        # Keyboard hints
        hint_y = team_y + 40
        hints = self._font_small.render(
            "Keys: 1=Allies  2=Enemies  3=FFA  4=Team A  5=Team B  R=Ready  Enter=Chat",
            True, Colors.TEXT_DIM)
        self._screen.blit(hints, (panel_x, hint_y))

        # Ready button
        ready_y = hint_y + 30
        ready_w = 160
        ready_h = 44
        ready_rect = pygame.Rect(w // 2 - ready_w // 2, ready_y, ready_w, ready_h)
        self._ready_button_rect = ready_rect

        ready_bg = Colors.READY_GREEN if self.my_ready else Colors.NOT_READY
        if ready_rect.collidepoint(mouse_pos):
            ready_bg = tuple(min(c + 30, 255) for c in ready_bg)
        pygame.draw.rect(self._screen, ready_bg, ready_rect, border_radius=8)

        ready_label = "READY ✓" if self.my_ready else "NOT READY"
        ready_surf = self._font_big.render(ready_label, True, Colors.TEXT_BRIGHT)
        self._screen.blit(ready_surf,
                          (ready_rect.centerx - ready_surf.get_width() // 2,
                           ready_rect.centery - ready_surf.get_height() // 2))

        # Chat panel
        chat_y = ready_y + ready_h + 20
        chat_h = h - chat_y - 10
        if chat_h > 40:
            chat_rect = pygame.Rect(panel_x, chat_y, panel_w, chat_h)
            pygame.draw.rect(self._screen, Colors.CHAT_BG, chat_rect, border_radius=6)
            pygame.draw.rect(self._screen, Colors.BORDER, chat_rect, 1, border_radius=6)

            # Chat label
            chat_label = self._font_small.render("Chat (Enter to type)", True, Colors.TEXT_DIM)
            self._screen.blit(chat_label, (chat_rect.x + 8, chat_rect.y + 4))

            # Chat messages (bottom-aligned)
            msg_y = chat_rect.bottom - 30
            for sender, text, t in reversed(self.chat_messages[-8:]):
                if msg_y < chat_rect.y + 20:
                    break
                line = self._font_small.render(f"{sender}: {text}", True, Colors.TEXT)
                self._screen.blit(line, (chat_rect.x + 8, msg_y))
                msg_y -= 18

            # Chat input
            if self.chat_active:
                input_rect = pygame.Rect(chat_rect.x + 4, chat_rect.bottom - 26,
                                         chat_rect.width - 8, 22)
                pygame.draw.rect(self._screen, Colors.BG_CARD, input_rect, border_radius=3)
                cursor = "│" if int(time.monotonic() * 2) % 2 == 0 else ""
                input_text = self._font_small.render(
                    f"> {self.chat_input}{cursor}", True, Colors.TEXT_BRIGHT)
                self._screen.blit(input_text, (input_rect.x + 4, input_rect.y + 3))
