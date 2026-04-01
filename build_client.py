"""
Build the client as a standalone .exe using PyInstaller.

Run: python build_client.py

Output: dist/HalfSwordOnline.exe
    - Single file, no Python needed
    - ~30MB (includes pygame + SDL2)
    - Just download and run

Requires: pip install pyinstaller pygame
"""

import subprocess
import sys
import os

def main():
    print("Building Half Sword Online client...")
    print()

    # Check deps
    try:
        import pygame
        print(f"  pygame: {pygame.version.ver}")
    except ImportError:
        print("  ERROR: pygame not installed. Run: pip install pygame")
        return 1

    try:
        import PyInstaller
        print(f"  PyInstaller: {PyInstaller.__version__}")
    except ImportError:
        print("  ERROR: PyInstaller not installed. Run: pip install pyinstaller")
        return 1

    # Build command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",                 # No console window
        "--name", "HalfSwordOnline",
        "--icon", "NONE",
        # Include all our modules
        "--add-data", f"shared{os.pathsep}shared",
        "--add-data", f"config.json{os.pathsep}.",
        # Hidden imports that PyInstaller might miss
        "--hidden-import", "pygame",
        "--hidden-import", "shared.protocol",
        "--hidden-import", "shared.lobby",
        "--hidden-import", "shared.fec",
        "--hidden-import", "client.connect_ui",
        "--hidden-import", "client.lobby_ui",
        "--hidden-import", "client.audio_player",
        # Entry point
        "client/__main__.py",
    ]

    print()
    print(f"  Running: {' '.join(cmd[:6])}...")
    print()

    result = subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))

    if result.returncode == 0:
        print()
        print("  ✓ Build successful!")
        print("  Output: dist/HalfSwordOnline.exe")
        print()
        print("  Share this file with your friends — they just double-click to play.")
        print("  No Python, no game purchase, no setup needed.")
    else:
        print()
        print("  ✗ Build failed. Check the output above.")

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
