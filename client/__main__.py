"""
Half Sword Online — Client Entry Point

Just run: python -m client
Or after packaging: HalfSwordOnline.exe

No command line args needed — everything is in the UI.

Flow:
    1. Connect Screen (pick server, enter name, settings)
    2. Lobby (pick team, ready up, chat)
    3. Game Stream (fullscreen gameplay + input)
"""

import logging
import os

# Silence pygame prompt
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

from client.connect_ui import ConnectScreen
from client.app import ClientApp


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print()
    print("  ⚔  HALF SWORD ONLINE  ⚔")
    print("  You DON'T need the game — only the host does.")
    print()

    # Phase 1: Connect Screen
    connect = ConnectScreen()
    result = connect.run()

    if not result:
        print("Cancelled.")
        return

    host = result["host"]
    port = result["port"]
    name = result["name"]
    width = result["width"]
    height = result["height"]
    fps = result["fps"]

    print(f"Connecting to {host}:{port} as '{name}' ({width}x{height}@{fps}fps)...")

    # Phase 2 + 3: The ClientApp handles lobby → game transition internally
    app = ClientApp(
        host=host,
        port=port,
        name=name,
        width=width,
        height=height,
        fps=fps,
    )
    app.run()

    print("Disconnected. Thanks for playing!")


if __name__ == "__main__":
    main()
