"""
Half Sword Online — Lobby Protocol

Extends the base protocol with lobby state that lets players:
- See who's connected
- Pick their team (Ally / Enemy / FFA)
- Set ready status
- Vote on game settings
- Chat

The host maintains authoritative lobby state and broadcasts updates.
Clients send requests; the host validates and applies them.

This replaces map-detection-based team assignment entirely.
Players decide their own teams.
"""

import enum
import struct
from dataclasses import dataclass, field
# Reuse base protocol helpers
from shared.protocol import (
    PacketType, PacketFlags, HEADER_SIZE,
    pack_header, unpack_header, now_ms,
)

# ============================================================================
# Extend PacketType for lobby messages
# We use the 0x50-0x5F range (unused in base protocol)
# ============================================================================

class LobbyPacketType(enum.IntEnum):
    # Lobby state (host → all clients, broadcast)
    LOBBY_STATE = 0x50

    # Player actions (client → host)
    LOBBY_SET_TEAM = 0x51
    LOBBY_SET_READY = 0x52
    LOBBY_SET_NAME = 0x53
    LOBBY_CHAT = 0x54

    # Host actions (host → specific client or broadcast)
    LOBBY_KICK = 0x55
    LOBBY_START_GAME = 0x56
    LOBBY_SETTINGS = 0x57


# ============================================================================
# Team choices — what the player WANTS, not internal Team Int
# The host translates these to Team Int values when spawning
# ============================================================================

class TeamChoice(enum.IntEnum):
    UNDECIDED = 0    # Hasn't picked yet
    ALLIES = 1       # Fight alongside host (Team Int = 1)
    ENEMIES = 2      # Fight against host team (Team Int = 2)
    FFA = 3          # Free for all (Team Int = 0)
    TEAM_A = 4       # Custom team A (for team vs team without host)
    TEAM_B = 5       # Custom team B

    def to_team_int(self) -> int:
        """Convert player's team choice to Half Sword's internal Team Int."""
        return {
            TeamChoice.UNDECIDED: 1,  # Default to allies
            TeamChoice.ALLIES: 1,
            TeamChoice.ENEMIES: 2,
            TeamChoice.FFA: 0,
            TeamChoice.TEAM_A: 1,
            TeamChoice.TEAM_B: 2,
        }[self]

    @property
    def display_name(self) -> str:
        return {
            TeamChoice.UNDECIDED: "Undecided",
            TeamChoice.ALLIES: "Allies",
            TeamChoice.ENEMIES: "Enemies",
            TeamChoice.FFA: "Free for All",
            TeamChoice.TEAM_A: "Team A",
            TeamChoice.TEAM_B: "Team B",
        }[self]

    @property
    def color(self) -> tuple[int, int, int]:
        """RGB color for UI display."""
        return {
            TeamChoice.UNDECIDED: (150, 150, 150),
            TeamChoice.ALLIES: (50, 200, 50),
            TeamChoice.ENEMIES: (200, 50, 50),
            TeamChoice.FFA: (200, 200, 50),
            TeamChoice.TEAM_A: (50, 100, 200),
            TeamChoice.TEAM_B: (200, 100, 50),
        }[self]


# ============================================================================
# Player info in the lobby
# ============================================================================

@dataclass
class LobbyPlayer:
    slot: int
    name: str
    team: TeamChoice = TeamChoice.UNDECIDED
    ready: bool = False
    ping_ms: int = 0
    is_host: bool = False

    def to_bytes(self) -> bytes:
        """Serialize to 48 bytes."""
        name_b = self.name.encode("utf-8")[:30].ljust(30, b"\x00")
        return struct.pack("!BB B ? H ?",
                           self.slot, self.team.value,
                           0,  # padding
                           self.ready, self.ping_ms,
                           self.is_host) + name_b

    @classmethod
    def from_bytes(cls, data: bytes) -> "LobbyPlayer":
        slot, team_val, _, ready, ping, is_host = struct.unpack("!BB B ? H ?", data[:7])
        name = data[7:37].rstrip(b"\x00").decode("utf-8", errors="replace")
        return cls(
            slot=slot, name=name, team=TeamChoice(team_val),
            ready=ready, ping_ms=ping, is_host=is_host,
        )

LOBBY_PLAYER_SIZE = 37  # 7 header + 30 name


# ============================================================================
# Full lobby state (broadcast by host)
# ============================================================================

@dataclass
class LobbyState:
    """Complete lobby state, broadcast to all clients whenever it changes."""
    players: list[LobbyPlayer] = field(default_factory=list)
    game_mode: str = "choose"  # "choose" = players pick teams freely
    host_name: str = "Host"
    message: str = ""  # Status message from host ("Waiting for players...", etc.)
    allow_team_change: bool = True
    allow_ffa: bool = True
    max_players: int = 8

    def to_bytes(self, seq: int) -> bytes:
        header = pack_header(LobbyPacketType.LOBBY_STATE, PacketFlags.RELIABLE, seq, now_ms())

        # State header: player_count(1) + mode(16) + host_name(16) + message(64) + flags(1)
        mode_b = self.game_mode.encode("utf-8")[:16].ljust(16, b"\x00")
        host_b = self.host_name.encode("utf-8")[:16].ljust(16, b"\x00")
        msg_b = self.message.encode("utf-8")[:64].ljust(64, b"\x00")
        flags = (self.allow_team_change & 1) | ((self.allow_ffa & 1) << 1)

        state_header = struct.pack("!BB", len(self.players), flags) + mode_b + host_b + msg_b

        # Player entries
        player_data = b"".join(p.to_bytes() for p in self.players)

        return header + state_header + player_data

    @classmethod
    def from_bytes(cls, data: bytes) -> "LobbyState":
        if len(data) < HEADER_SIZE + 2 + 16 + 16 + 64:
            raise ValueError("packet too short for LobbyState")
        offset = HEADER_SIZE
        count, flags = struct.unpack("!BB", data[offset:offset + 2])
        offset += 2

        mode = data[offset:offset + 16].rstrip(b"\x00").decode("utf-8", errors="replace")
        offset += 16
        host_name = data[offset:offset + 16].rstrip(b"\x00").decode("utf-8", errors="replace")
        offset += 16
        message = data[offset:offset + 64].rstrip(b"\x00").decode("utf-8", errors="replace")
        offset += 64

        players = []
        for _ in range(count):
            if offset + LOBBY_PLAYER_SIZE > len(data):
                break
            p = LobbyPlayer.from_bytes(data[offset:offset + LOBBY_PLAYER_SIZE])
            players.append(p)
            offset += LOBBY_PLAYER_SIZE

        return cls(
            players=players,
            game_mode=mode,
            host_name=host_name,
            message=message,
            allow_team_change=bool(flags & 1),
            allow_ffa=bool(flags & 2),
        )


# ============================================================================
# Client → Host actions
# ============================================================================

@dataclass
class SetTeamRequest:
    team: TeamChoice

    def to_bytes(self, seq: int) -> bytes:
        header = pack_header(LobbyPacketType.LOBBY_SET_TEAM, PacketFlags.RELIABLE, seq, now_ms())
        return header + struct.pack("!B", self.team.value)

    @classmethod
    def from_bytes(cls, data: bytes) -> "SetTeamRequest":
        if len(data) < HEADER_SIZE + 1:
            raise ValueError("packet too short for SetTeamRequest")
        team_val = struct.unpack("!B", data[HEADER_SIZE:HEADER_SIZE + 1])[0]
        return cls(team=TeamChoice(team_val))


@dataclass
class SetReadyRequest:
    ready: bool

    def to_bytes(self, seq: int) -> bytes:
        header = pack_header(LobbyPacketType.LOBBY_SET_READY, PacketFlags.RELIABLE, seq, now_ms())
        return header + struct.pack("!?", self.ready)

    @classmethod
    def from_bytes(cls, data: bytes) -> "SetReadyRequest":
        if len(data) < HEADER_SIZE + 1:
            raise ValueError("packet too short for SetReadyRequest")
        ready = struct.unpack("!?", data[HEADER_SIZE:HEADER_SIZE + 1])[0]
        return cls(ready=ready)


@dataclass
class ChatMessage:
    text: str

    def to_bytes(self, seq: int) -> bytes:
        header = pack_header(LobbyPacketType.LOBBY_CHAT, PacketFlags.RELIABLE, seq, now_ms())
        text_b = self.text.encode("utf-8")[:128]
        return header + struct.pack("!B", len(text_b)) + text_b

    @classmethod
    def from_bytes(cls, data: bytes) -> "ChatMessage":
        if len(data) < HEADER_SIZE + 1:
            raise ValueError("packet too short for ChatMessage")
        length = struct.unpack("!B", data[HEADER_SIZE:HEADER_SIZE + 1])[0]
        if len(data) < HEADER_SIZE + 1 + length:
            raise ValueError("packet too short for ChatMessage text")
        text = data[HEADER_SIZE + 1:HEADER_SIZE + 1 + length].decode("utf-8", errors="replace")
        return cls(text=text)
