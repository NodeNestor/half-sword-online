"""
Half Sword Online — Input Injection via Windows SendInput

Injects keyboard and mouse input from remote clients directly into
the host system using the Windows SendInput API (ctypes).

No drivers, no installs, no dependencies — just the Windows API.

Remote players send their keyboard/mouse state over the network.
This module converts that into SendInput calls that the game reads
as if someone was physically using the host's keyboard/mouse.
"""

import ctypes
import ctypes.wintypes
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from shared.protocol import MouseInput, KeyboardInput, GamepadInput, XButton

logger = logging.getLogger(__name__)

# ============================================================================
# Windows SendInput API via ctypes
# ============================================================================

user32 = ctypes.windll.user32

# Input type constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Mouse event flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

# Keyboard event flags
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("union", INPUT_UNION),
    ]


def send_input(*inputs: INPUT):
    """Send one or more input events via Windows SendInput."""
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


# ============================================================================
# Virtual Key Code to Scan Code mapping
# SendInput works better with scan codes for games
# ============================================================================

def vk_to_scan(vk: int) -> int:
    """Convert a virtual key code to a hardware scan code."""
    return user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC = 0


# ============================================================================
# Gamepad button to keyboard key mapping
# Half Sword's keyboard controls:
#   WASD = movement, Mouse = camera
#   Q = left hand pickup/drop
#   E = right hand pickup/drop
#   X = swap hands
#   Space = jump/dodge
#   Shift = sprint
#   R = inventory
# ============================================================================

GAMEPAD_TO_VK = {
    XButton.A: 0x20,              # A -> Space
    XButton.B: 0x10,              # B -> Shift
    XButton.X: 0x58,              # X -> X key (swap hands)
    XButton.Y: 0x52,              # Y -> R key
    XButton.LEFT_SHOULDER: 0x51,  # LB -> Q (left hand)
    XButton.RIGHT_SHOULDER: 0x45, # RB -> E (right hand)
    XButton.START: 0x1B,          # Start -> Escape
    XButton.BACK: 0x09,           # Back -> Tab
    XButton.DPAD_UP: 0x57,        # DPad Up -> W
    XButton.DPAD_DOWN: 0x53,      # DPad Down -> S
    XButton.DPAD_LEFT: 0x41,      # DPad Left -> A
    XButton.DPAD_RIGHT: 0x44,     # DPad Right -> D
}


# ============================================================================
# Input Injector — one per remote player
# ============================================================================

class InputInjector:
    """
    Injects input for one remote player using Windows SendInput.

    Converts remote keyboard/mouse/gamepad events into native input events
    that the game reads as real keyboard/mouse input.
    """

    def __init__(self, player_slot: int):
        self.player_slot = player_slot
        self._lock = threading.Lock()
        self._pressed_keys: set[int] = set()  # Track pressed VK codes
        self._mouse_buttons: int = 0           # Track mouse button state
        self._active = True

    def connect(self) -> bool:
        """No driver needed — always succeeds."""
        logger.info(f"[Slot {self.player_slot}] Input injector ready (SendInput)")
        return True

    def disconnect(self):
        """Release all held keys on disconnect."""
        with self._lock:
            # Release all pressed keys
            for vk in list(self._pressed_keys):
                self._send_key(vk, pressed=False)
            self._pressed_keys.clear()

            # Release mouse buttons
            if self._mouse_buttons & 0x01:
                self._send_mouse_button(MOUSEEVENTF_LEFTUP)
            if self._mouse_buttons & 0x02:
                self._send_mouse_button(MOUSEEVENTF_RIGHTUP)
            if self._mouse_buttons & 0x04:
                self._send_mouse_button(MOUSEEVENTF_MIDDLEUP)
            self._mouse_buttons = 0

        self._active = False
        logger.info(f"[Slot {self.player_slot}] Input injector disconnected")

    def apply_keyboard_input(self, kb: KeyboardInput):
        """Inject a keyboard key press/release."""
        if not self._active:
            return

        vk = kb.keycode
        with self._lock:
            if kb.pressed:
                if vk not in self._pressed_keys:
                    self._pressed_keys.add(vk)
                    self._send_key(vk, pressed=True)
            else:
                if vk in self._pressed_keys:
                    self._pressed_keys.discard(vk)
                    self._send_key(vk, pressed=False)

    def apply_mouse_input(self, mouse: MouseInput):
        """Inject mouse movement and button state."""
        if not self._active:
            return

        with self._lock:
            # Mouse movement (relative)
            if mouse.dx != 0 or mouse.dy != 0:
                self._send_mouse_move(mouse.dx, mouse.dy)

            # Mouse buttons — detect changes
            old = self._mouse_buttons
            new = mouse.buttons

            # Left button
            if (new & 0x01) and not (old & 0x01):
                self._send_mouse_button(MOUSEEVENTF_LEFTDOWN)
            elif not (new & 0x01) and (old & 0x01):
                self._send_mouse_button(MOUSEEVENTF_LEFTUP)

            # Right button
            if (new & 0x02) and not (old & 0x02):
                self._send_mouse_button(MOUSEEVENTF_RIGHTDOWN)
            elif not (new & 0x02) and (old & 0x02):
                self._send_mouse_button(MOUSEEVENTF_RIGHTUP)

            # Middle button
            if (new & 0x04) and not (old & 0x04):
                self._send_mouse_button(MOUSEEVENTF_MIDDLEDOWN)
            elif not (new & 0x04) and (old & 0x04):
                self._send_mouse_button(MOUSEEVENTF_MIDDLEUP)

            self._mouse_buttons = new

            # Scroll wheel
            if mouse.scroll != 0:
                self._send_mouse_wheel(mouse.scroll)

    def apply_gamepad_input(self, state: GamepadInput):
        """Convert gamepad input to keyboard/mouse and inject."""
        if not self._active:
            return

        with self._lock:
            # Gamepad buttons -> keyboard keys
            for btn_flag, vk in GAMEPAD_TO_VK.items():
                pressed = bool(state.buttons & btn_flag)
                if pressed and vk not in self._pressed_keys:
                    self._pressed_keys.add(vk)
                    self._send_key(vk, pressed=True)
                elif not pressed and vk in self._pressed_keys:
                    self._pressed_keys.discard(vk)
                    self._send_key(vk, pressed=False)

            # Left stick -> WASD
            threshold = 8000
            w_pressed = state.left_stick_y > threshold
            s_pressed = state.left_stick_y < -threshold
            a_pressed = state.left_stick_x < -threshold
            d_pressed = state.left_stick_x > threshold

            for vk, should_press in [(0x57, w_pressed), (0x53, s_pressed),
                                      (0x41, a_pressed), (0x44, d_pressed)]:
                if should_press and vk not in self._pressed_keys:
                    self._pressed_keys.add(vk)
                    self._send_key(vk, pressed=True)
                elif not should_press and vk in self._pressed_keys:
                    self._pressed_keys.discard(vk)
                    self._send_key(vk, pressed=False)

            # Right stick -> mouse movement (camera)
            # Scale stick values (-32768..32767) to mouse pixels
            sensitivity = 0.01
            dx = int(state.right_stick_x * sensitivity)
            dy = int(-state.right_stick_y * sensitivity)
            if dx != 0 or dy != 0:
                self._send_mouse_move(dx, dy)

    # -----------------------------------------------------------------------
    # Low-level SendInput wrappers
    # -----------------------------------------------------------------------

    def _send_key(self, vk: int, pressed: bool):
        """Send a keyboard key event."""
        scan = vk_to_scan(vk)
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        inp.union.ki.wScan = scan
        inp.union.ki.dwFlags = KEYEVENTF_SCANCODE | (0 if pressed else KEYEVENTF_KEYUP)
        inp.union.ki.time = 0
        inp.union.ki.dwExtraInfo = None
        send_input(inp)

    def _send_mouse_move(self, dx: int, dy: int):
        """Send relative mouse movement."""
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = dx
        inp.union.mi.dy = dy
        inp.union.mi.dwFlags = MOUSEEVENTF_MOVE
        inp.union.mi.mouseData = 0
        inp.union.mi.time = 0
        inp.union.mi.dwExtraInfo = None
        send_input(inp)

    def _send_mouse_button(self, flag: int):
        """Send a mouse button event."""
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = 0
        inp.union.mi.dy = 0
        inp.union.mi.dwFlags = flag
        inp.union.mi.mouseData = 0
        inp.union.mi.time = 0
        inp.union.mi.dwExtraInfo = None
        send_input(inp)

    def _send_mouse_wheel(self, clicks: int):
        """Send mouse wheel scroll."""
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = 0
        inp.union.mi.dy = 0
        inp.union.mi.dwFlags = MOUSEEVENTF_WHEEL
        inp.union.mi.mouseData = clicks * 120  # WHEEL_DELTA = 120
        inp.union.mi.time = 0
        inp.union.mi.dwExtraInfo = None
        send_input(inp)


# ============================================================================
# Manager — one injector per remote player
# ============================================================================

class InputInjectorManager:
    """Manages input injectors for all remote players."""

    def __init__(self):
        self.injectors: dict[int, InputInjector] = {}

    def add_player(self, slot: int) -> InputInjector:
        if slot in self.injectors:
            raise ValueError(f"Slot {slot} already has an injector")
        inj = InputInjector(player_slot=slot)
        inj.connect()
        self.injectors[slot] = inj
        return inj

    def remove_player(self, slot: int):
        if slot in self.injectors:
            self.injectors[slot].disconnect()
            del self.injectors[slot]

    def remove_all(self):
        for inj in self.injectors.values():
            inj.disconnect()
        self.injectors.clear()

    def get_gamepad(self, slot: int) -> Optional[InputInjector]:
        return self.injectors.get(slot)
