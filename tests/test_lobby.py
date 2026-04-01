"""Tests for lobby protocol and session manager."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.lobby import (
    LobbyState, LobbyPlayer, TeamChoice,
    SetTeamRequest, SetReadyRequest, ChatMessage,
)
from host.session_manager import SessionManager


def test_team_choice_to_team_int():
    assert TeamChoice.ALLIES.to_team_int() == 1
    assert TeamChoice.ENEMIES.to_team_int() == 2
    assert TeamChoice.FFA.to_team_int() == 0
    assert TeamChoice.TEAM_A.to_team_int() == 1
    assert TeamChoice.TEAM_B.to_team_int() == 2
    assert TeamChoice.UNDECIDED.to_team_int() == 1
    print("  team choice -> team int OK")


def test_lobby_state_roundtrip():
    state = LobbyState(
        players=[
            LobbyPlayer(slot=1, name="Host", team=TeamChoice.ALLIES, ready=True, is_host=True),
            LobbyPlayer(slot=2, name="Player2", team=TeamChoice.ENEMIES, ready=False, ping_ms=35),
        ],
        host_name="Host",
        message="Waiting...",
        allow_team_change=True,
        allow_ffa=True,
        max_players=8,
    )
    raw = state.to_bytes(0)
    parsed = LobbyState.from_bytes(raw)
    assert len(parsed.players) == 2
    assert parsed.players[0].name == "Host"
    assert parsed.players[0].is_host == True
    assert parsed.players[1].name == "Player2"
    assert parsed.players[1].team == TeamChoice.ENEMIES
    assert parsed.host_name == "Host"
    assert parsed.message == "Waiting..."
    print("  lobby state roundtrip OK")


def test_set_team_roundtrip():
    req = SetTeamRequest(team=TeamChoice.FFA)
    raw = req.to_bytes(5)
    parsed = SetTeamRequest.from_bytes(raw)
    assert parsed.team == TeamChoice.FFA
    print("  set team roundtrip OK")


def test_set_ready_roundtrip():
    req = SetReadyRequest(ready=True)
    raw = req.to_bytes(3)
    parsed = SetReadyRequest.from_bytes(raw)
    assert parsed.ready == True
    print("  set ready roundtrip OK")


def test_chat_roundtrip():
    msg = ChatMessage(text="hello everyone!")
    raw = msg.to_bytes(1)
    parsed = ChatMessage.from_bytes(raw)
    assert parsed.text == "hello everyone!"
    print("  chat roundtrip OK")


def test_session_manager_join_leave():
    import tempfile
    bridge = Path(tempfile.mkdtemp())

    sm = SessionManager(host_name="TestHost", max_players=4, bridge_dir=bridge)
    s1 = sm.on_player_join(2, "Alice", ("127.0.0.1", 5000))
    s2 = sm.on_player_join(3, "Bob", ("127.0.0.1", 5001))

    assert s1.team == TeamChoice.ALLIES  # Default
    assert len(sm.players) == 2

    lobby = sm.get_lobby_state()
    assert len(lobby.players) == 3  # Host + 2 players
    assert lobby.players[0].is_host == True

    sm.on_player_leave(2)
    assert len(sm.players) == 1
    print("  session manager join/leave OK")


def test_session_manager_teams():
    import tempfile
    bridge = Path(tempfile.mkdtemp())

    sm = SessionManager(host_name="Host", max_players=4, bridge_dir=bridge)
    sm.on_player_join(2, "Alice", ("127.0.0.1", 5000))
    sm.on_player_join(3, "Bob", ("127.0.0.1", 5001))

    assert sm.set_player_team(2, TeamChoice.ENEMIES) == True
    assert sm.players[2].team == TeamChoice.ENEMIES

    # Check bridge file was written
    team_file = bridge / "team_config.txt"
    assert team_file.exists()
    content = team_file.read_text()
    assert "slot_2_team=2" in content  # ENEMIES = team int 2
    assert "slot_3_team=1" in content  # ALLIES = team int 1
    print("  session manager teams OK")


def test_session_manager_ready():
    import tempfile
    bridge = Path(tempfile.mkdtemp())

    sm = SessionManager(host_name="Host", max_players=4, bridge_dir=bridge)
    sm.on_player_join(2, "Alice", ("127.0.0.1", 5000))

    assert sm.should_start_game() == False
    sm.set_player_ready(2, True)
    assert sm.should_start_game() == True  # All (1 player) ready
    print("  session manager ready OK")


if __name__ == "__main__":
    print("=== Lobby Tests ===")
    test_team_choice_to_team_int()
    test_lobby_state_roundtrip()
    test_set_team_roundtrip()
    test_set_ready_roundtrip()
    test_chat_roundtrip()
    test_session_manager_join_leave()
    test_session_manager_teams()
    test_session_manager_ready()
    print("\nAll lobby tests passed!")
