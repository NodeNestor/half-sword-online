"""
Half Sword Online — Session & Lobby Manager

Player-driven team assignment. No map detection.
Players pick their team in the lobby; the host translates
to Half Sword's internal Team Int and syncs via bridge.

Flow:
    1. Client connects → enters lobby
    2. Client picks team (Allies/Enemies/FFA/Team A/B)
    3. Client sets ready
    4. Host translates TeamChoice → Team Int, writes bridge
    5. Lua mod reads bridge, sets Team Int on pawns
    6. When all ready (or host forces), game streams start
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from shared.lobby import LobbyState, LobbyPlayer, TeamChoice

logger = logging.getLogger(__name__)


@dataclass
class PlayerSession:
    """Server-side state for one connected player."""
    slot: int
    name: str
    address: tuple
    team: TeamChoice = TeamChoice.UNDECIDED
    ready: bool = False
    ping_ms: int = 0
    connected_at: float = 0.0
    is_host: bool = False
    streaming: bool = False


class SessionManager:
    """
    Manages the lobby and team assignments.
    Players decide their teams — no map detection.
    """

    def __init__(self, host_name: str = "Host", max_players: int = 8,
                 bridge_dir: Optional[Path] = None):
        self.host_name = host_name
        self.max_players = max_players
        self.bridge_dir = bridge_dir or Path(
            os.environ.get("LOCALAPPDATA", ".")) / "HalfSwordUE5" / "Saved" / "HalfSwordOnline"

        self.players: dict[int, PlayerSession] = {}
        self.allow_team_change = True
        self.allow_ffa = True
        self.status_message = "Waiting for players..."
        self.game_active = False

        self._lock = threading.Lock()
        self._lobby_version = 0

    def on_player_join(self, slot: int, name: str, address: tuple) -> PlayerSession:
        with self._lock:
            session = PlayerSession(
                slot=slot, name=name, address=address,
                team=TeamChoice.ALLIES, connected_at=time.monotonic(),
            )
            self.players[slot] = session
            self._lobby_version += 1
            logger.info(f"[Lobby] {name} joined slot {slot}, default: Allies")
            self._sync_teams_to_bridge()
            return session

    def on_player_leave(self, slot: int):
        with self._lock:
            session = self.players.pop(slot, None)
            if session:
                logger.info(f"[Lobby] {session.name} left slot {slot}")
                self._lobby_version += 1
                self._sync_teams_to_bridge()

    def set_player_team(self, slot: int, team: TeamChoice) -> bool:
        with self._lock:
            if not self.allow_team_change:
                return False
            if team == TeamChoice.FFA and not self.allow_ffa:
                return False
            session = self.players.get(slot)
            if not session:
                return False
            old = session.team
            session.team = team
            self._lobby_version += 1
            logger.info(f"[Lobby] {session.name}: {old.display_name} → {team.display_name}")
            self._sync_teams_to_bridge()
            return True

    def set_player_ready(self, slot: int, ready: bool) -> bool:
        with self._lock:
            session = self.players.get(slot)
            if not session:
                return False
            session.ready = ready
            self._lobby_version += 1
            ready_count = sum(1 for p in self.players.values() if p.ready)
            total = len(self.players)
            if self._all_ready():
                self.status_message = "All players ready! Starting..."
            else:
                self.status_message = f"Waiting... ({ready_count}/{total} ready)"
            return True

    def update_ping(self, slot: int, ping_ms: int):
        with self._lock:
            session = self.players.get(slot)
            if session:
                session.ping_ms = ping_ms

    def should_start_game(self) -> bool:
        with self._lock:
            if self.game_active:
                return True
            if self.players and self._all_ready():
                self.game_active = True
                return True
            return False

    def force_start(self):
        with self._lock:
            self.game_active = True
            self.status_message = "Game starting!"
            self._lobby_version += 1

    def get_lobby_state(self) -> LobbyState:
        with self._lock:
            lobby_players = [LobbyPlayer(
                slot=1, name=self.host_name, team=TeamChoice.ALLIES,
                ready=True, ping_ms=0, is_host=True,
            )]
            for slot, s in sorted(self.players.items()):
                lobby_players.append(LobbyPlayer(
                    slot=s.slot, name=s.name, team=s.team,
                    ready=s.ready, ping_ms=s.ping_ms, is_host=False,
                ))
            return LobbyState(
                players=lobby_players, game_mode="choose",
                host_name=self.host_name, message=self.status_message,
                allow_team_change=self.allow_team_change,
                allow_ffa=self.allow_ffa, max_players=self.max_players,
            )

    @property
    def lobby_version(self) -> int:
        return self._lobby_version

    def _sync_teams_to_bridge(self):
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        lines = []
        for slot, s in self.players.items():
            lines.append(f"slot_{slot}_team={s.team.to_team_int()}")
        try:
            (self.bridge_dir / "team_config.txt").write_text("\n".join(lines) + "\n")
        except Exception as e:
            logger.error(f"Bridge write failed: {e}")

    def send_spawn_commands(self):
        cmd_file = self.bridge_dir / "host_commands.txt"
        commands = []
        with self._lock:
            for slot, s in self.players.items():
                if not s.streaming:
                    commands.append("spawn 1920 1080")
                    s.streaming = True
        if commands:
            try:
                with open(cmd_file, "a") as f:
                    for cmd in commands:
                        f.write(cmd + "\n")
            except Exception as e:
                logger.error(f"Spawn command write failed: {e}")

    def _all_ready(self) -> bool:
        return bool(self.players) and all(p.ready for p in self.players.values())
