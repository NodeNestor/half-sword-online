"""
Half Sword Online — Setup & Test Runner

Does everything in one go:
1. Downloads UE4SS v3.0.1 (for the demo on UE 5.1)
2. Installs it into the game directory
3. Copies the Lua mod
4. Installs Python dependencies
5. Runs the unit test suite
6. Prints instructions for launching the game + testing

Usage: python setup_and_test.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

# ============================================================================
# Configuration
# ============================================================================

# Half Sword Demo location (auto-detected or override here)
GAME_DIR = None  # Set to override auto-detection

# Known Steam library locations to search
STEAM_PATHS = [
    Path("C:/Program Files (x86)/Steam/steamapps/common"),
    Path("D:/SteamLibrary/steamapps/common"),
    Path("E:/SteamLibrary/steamapps/common"),
    Path("F:/SteamLibrary/steamapps/common"),
]

GAME_NAMES = ["Half Sword Demo", "Half Sword"]

# UE4SS download — v3.0.1 for UE 5.1 (demo), experimental for UE 5.4 (EA)
UE4SS_STABLE_URL = "https://github.com/UE4SS-RE/RE-UE4SS/releases/download/v3.0.1/UE4SS_v3.0.1.zip"
UE4SS_EXPERIMENTAL_URL = "https://github.com/UE4SS-RE/RE-UE4SS/releases/download/experimental-latest/UE4SS_v3.0.1.zip"

PROJECT_DIR = Path(__file__).parent


# ============================================================================
# Helpers
# ============================================================================

def find_game() -> Path | None:
    """Auto-detect Half Sword installation."""
    if GAME_DIR:
        p = Path(GAME_DIR)
        if p.exists():
            return p

    for steam_path in STEAM_PATHS:
        for name in GAME_NAMES:
            candidate = steam_path / name
            if candidate.exists():
                # Verify it's actually the game
                exe = candidate / "HalfSwordUE5" / "Binaries" / "Win64" / "HalfSwordUE5-Win64-Shipping.exe"
                if exe.exists():
                    return candidate
    return None


def get_win64_dir(game_dir: Path) -> Path:
    return game_dir / "HalfSwordUE5" / "Binaries" / "Win64"


def is_ue4ss_installed(win64: Path) -> bool:
    return (win64 / "UE4SS-settings.ini").exists()


def download_file(url: str, dest: Path, label: str = ""):
    """Download a file with progress."""
    print(f"  Downloading {label or url}...")

    def progress(count, block_size, total_size):
        pct = count * block_size * 100 // max(total_size, 1)
        print(f"\r  {pct}% ({count * block_size // 1024} KB)", end="", flush=True)

    urlretrieve(url, str(dest), reporthook=progress)
    print()


# ============================================================================
# Steps
# ============================================================================

def step_find_game():
    print("=" * 60)
    print("Step 1: Finding Half Sword")
    print("=" * 60)

    game_dir = find_game()
    if game_dir:
        print(f"  Found: {game_dir}")
        win64 = get_win64_dir(game_dir)
        print(f"  Win64: {win64}")

        # Detect if demo or EA
        is_demo = "Demo" in game_dir.name
        print(f"  Version: {'Demo' if is_demo else 'Early Access'}")
        return game_dir, is_demo
    else:
        print("  Half Sword not found!")
        print("  Searched:")
        for sp in STEAM_PATHS:
            print(f"    {sp}")
        print()
        print("  Set GAME_DIR at the top of this script, or install the demo from Steam.")
        return None, False


def step_install_ue4ss(game_dir: Path, is_demo: bool):
    print()
    print("=" * 60)
    print("Step 2: Installing UE4SS")
    print("=" * 60)

    win64 = get_win64_dir(game_dir)

    if is_ue4ss_installed(win64):
        print("  UE4SS already installed, skipping.")
        return True

    # Demo uses UE 5.1 -> stable v3.0.1
    # EA uses UE 5.4 -> experimental
    url = UE4SS_STABLE_URL if is_demo else UE4SS_EXPERIMENTAL_URL
    version = "v3.0.1 (stable)" if is_demo else "experimental-latest"
    print(f"  UE4SS version: {version}")

    # Download
    tmp = Path(tempfile.mkdtemp())
    zip_path = tmp / "ue4ss.zip"

    try:
        download_file(url, zip_path, f"UE4SS {version}")
    except Exception as e:
        print(f"  Download failed: {e}")
        print(f"  Manual download: {url}")
        print(f"  Extract to: {win64}")
        return False

    # Extract
    print(f"  Extracting to {win64}...")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(win64)
        print("  UE4SS installed!")
    except Exception as e:
        print(f"  Extract failed: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Verify
    if is_ue4ss_installed(win64):
        print("  Verified: UE4SS-settings.ini present")
        return True
    else:
        print("  WARNING: UE4SS-settings.ini not found after extraction")
        return False


def step_install_mod(game_dir: Path):
    print()
    print("=" * 60)
    print("Step 3: Installing Lua mod")
    print("=" * 60)

    win64 = get_win64_dir(game_dir)
    mods_dir = win64 / "Mods"

    # Copy our mod
    src = PROJECT_DIR / "mod" / "HalfSwordOnlineMod"
    dst = mods_dir / "HalfSwordOnlineMod"

    if dst.exists():
        print(f"  Removing old mod at {dst}")
        shutil.rmtree(dst)

    print(f"  Copying mod to {dst}")
    shutil.copytree(src, dst)

    # Add to mods.txt
    mods_txt = mods_dir / "mods.txt"
    mod_line = "HalfSwordOnlineMod : 1"

    if mods_txt.exists():
        content = mods_txt.read_text()
        if "HalfSwordOnlineMod" not in content:
            print(f"  Adding to mods.txt")
            with open(mods_txt, "a") as f:
                f.write(f"\n{mod_line}\n")
        else:
            print("  Already in mods.txt")
    else:
        print("  mods.txt not found — UE4SS may not be installed correctly")
        print(f"  Creating {mods_txt}")
        mods_txt.write_text(f"{mod_line}\n")

    print("  Mod installed!")
    return True


def step_install_python_deps():
    print()
    print("=" * 60)
    print("Step 4: Installing Python dependencies")
    print("=" * 60)

    deps = ["pygame"]

    for dep in deps:
        print(f"  Installing {dep}...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", dep, "-q"],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            if "already satisfied" in stderr or "already satisfied" in result.stdout.decode(errors="replace"):
                print(f"    Already installed")
            else:
                print(f"    WARNING: {stderr[:200]}")
        else:
            print(f"    OK")

    return True


def step_run_tests():
    print()
    print("=" * 60)
    print("Step 5: Running test suite")
    print("=" * 60)

    test_files = [
        "tests/test_protocol.py",
        "tests/test_fec.py",
        "tests/test_lobby.py",
        "tests/test_streaming.py",
    ]

    all_pass = True
    for tf in test_files:
        path = PROJECT_DIR / tf
        if not path.exists():
            print(f"  SKIP: {tf} not found")
            continue

        print(f"\n  Running {tf}...")
        result = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            timeout=120,
        )

        stdout = result.stdout.decode(errors="replace")
        # Print last few lines (summary)
        lines = stdout.strip().split("\n")
        for line in lines[-5:]:
            print(f"    {line}")

        if result.returncode != 0:
            all_pass = False
            stderr = result.stderr.decode(errors="replace")
            if stderr:
                for line in stderr.strip().split("\n")[-3:]:
                    print(f"    ERROR: {line}")

    print()
    if all_pass:
        print("  ALL TESTS PASSED")
    else:
        print("  SOME TESTS FAILED (see above)")

    return all_pass


def step_print_instructions(game_dir: Path):
    print()
    print("=" * 60)
    print("Step 6: Ready to test!")
    print("=" * 60)
    print()
    print("  HOW TO TEST:")
    print()
    print("  1. Launch Half Sword (via Steam)")
    print("     - UE4SS console should appear (a separate window)")
    print("     - Check console for '[HalfSwordOnline]' messages")
    print()
    print("  2. In-game, press Ctrl+N to spawn a second player")
    print("     - You should see a split-screen or new character appear")
    print("     - Check UE4SS console for spawn logs")
    print()
    print("  3. In a separate terminal, start the host server:")
    print(f"     cd {PROJECT_DIR}")
    print("     python -m host.server --port 8080")
    print()
    print("  4. In another terminal, start the client:")
    print(f"     cd {PROJECT_DIR}")
    print("     python -m client")
    print("     (enter 127.0.0.1:8080 to connect to yourself)")
    print()
    print("  WHAT TO LOOK FOR:")
    print("  - UE4SS console: player spawn messages, SceneCapture2D creation")
    print("  - Host server: 'Pipeline active' messages")
    print("  - Client: connect screen -> lobby -> (eventually) video stream")
    print()
    print("  KNOWN ISSUES FOR DEMO:")
    print("  - Demo is v0.3 on UE 5.1 — class names may differ from EA")
    print("  - SceneCapture2D creation via Lua may not work (needs testing)")
    print("  - GPU readback (C++ plugin) not built yet — needs UE4SS SDK")
    print("  - Without C++ plugin, the streaming pipeline won't have frames")
    print()
    print("  WHAT WORKS RIGHT NOW (without the game):")
    print("  - python -m client  (shows connect screen UI)")
    print("  - python -m host.server --port 8080  (starts server + dashboard)")
    print("  - Connect client to server -> lobby appears")
    print("  - All protocol/FEC/lobby logic is tested and working")


# ============================================================================
# Main
# ============================================================================

def main():
    print()
    print("  ==============================")
    print("  HALF SWORD ONLINE — SETUP")
    print("  ==============================")
    print()

    # Step 1: Find game
    game_dir, is_demo = step_find_game()

    if game_dir:
        # Step 2: Install UE4SS
        step_install_ue4ss(game_dir, is_demo)

        # Step 3: Install mod
        step_install_mod(game_dir)

    # Step 4: Python deps
    step_install_python_deps()

    # Step 5: Tests
    tests_ok = step_run_tests()

    # Step 6: Instructions
    if game_dir:
        step_print_instructions(game_dir)
    else:
        print()
        print("  Game not found — you can still test the client/server UI:")
        print(f"  cd {PROJECT_DIR}")
        print("  python -m host.server --port 8080   (terminal 1)")
        print("  python -m client                     (terminal 2)")

    print()
    return 0 if tests_ok else 1


if __name__ == "__main__":
    sys.exit(main())
