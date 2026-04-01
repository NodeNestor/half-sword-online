"""
Half Sword Online — Input Injection

Receives input from remote clients and injects it into the host system
as virtual gamepad input via ViGEmBus.

ViGEmBus creates virtual Xbox 360 controllers that are indistinguishable
from real hardware to the game. Each remote player gets their own virtual
controller.

Dependencies:
    pip install vgamepad

ViGEmBus driver must be installed:
    https://github.com/nefarius/ViGEmBus/releases
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

try:
    import vgamepad as vg
    HAS_VGAMEPAD = True
except ImportError:
    HAS_VGAMEPAD = False

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from shared.protocol import GamepadInput, MouseInput, KeyboardInput, XButton

logger = logging.getLogger(__name__)


# Mapping from our XButton flags to vgamepad's XUSB_BUTTON
XBUTTON_TO_VGAMEPAD = {}

if HAS_VGAMEPAD:
    XBUTTON_TO_VGAMEPAD = {
        XButton.DPAD_UP: vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
        XButton.DPAD_DOWN: vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
        XButton.DPAD_LEFT: vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        XButton.DPAD_RIGHT: vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        XButton.START: vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        XButton.BACK: vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
        XButton.LEFT_THUMB: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
        XButton.RIGHT_THUMB: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
        XButton.LEFT_SHOULDER: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
        XButton.RIGHT_SHOULDER: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
        XButton.A: vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
        XButton.B: vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
        XButton.X: vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
        XButton.Y: vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
    }


@dataclass
class MouseToGamepadMapping:
    """Maps mouse movement and buttons to gamepad axes and buttons."""
    # Mouse sensitivity for camera control (maps to right stick)
    sensitivity_x: float = 5.0
    sensitivity_y: float = 5.0
    # Mouse buttons → gamepad buttons
    left_click: int = XButton.RIGHT_SHOULDER    # Right hand action
    right_click: int = XButton.LEFT_SHOULDER    # Left hand action
    middle_click: int = XButton.X               # Swap hands
    # Accumulated mouse movement (smoothed into stick position)
    _accum_x: float = 0.0
    _accum_y: float = 0.0
    # Decay rate for mouse → stick conversion
    decay: float = 0.8


@dataclass
class KeyboardToGamepadMapping:
    """Maps keyboard keys to gamepad inputs."""
    # WASD → left stick
    # Mouse → right stick (handled in MouseToGamepadMapping)
    # Common mappings (VK codes → gamepad)
    keymap: dict = field(default_factory=lambda: {
        0x57: ("left_stick", 0, 32767),     # W → left stick up
        0x53: ("left_stick", 0, -32767),    # S → left stick down
        0x41: ("left_stick", -32767, 0),    # A → left stick left
        0x44: ("left_stick", 32767, 0),     # D → left stick right
        0x20: ("button", XButton.A, 0),     # Space → A button
        0x10: ("button", XButton.B, 0),     # Shift → B button
        0x51: ("button", XButton.LEFT_SHOULDER, 0),   # Q → left hand
        0x45: ("button", XButton.RIGHT_SHOULDER, 0),  # E → right hand
        0x58: ("button", XButton.X, 0),     # X → swap hands
        0x52: ("button", XButton.Y, 0),     # R → Y button
        0x1B: ("button", XButton.START, 0), # Escape → Start
        0x09: ("button", XButton.BACK, 0),  # Tab → Back
    })


class VirtualGamepad:
    """
    A virtual Xbox 360 controller for one remote player.

    Translates mouse + keyboard input from the remote client into
    gamepad state that Half Sword can read.
    """

    def __init__(self, player_slot: int,
                 mouse_map: Optional[MouseToGamepadMapping] = None,
                 keyboard_map: Optional[KeyboardToGamepadMapping] = None):
        self.player_slot = player_slot
        self.mouse_map = mouse_map or MouseToGamepadMapping()
        self.keyboard_map = keyboard_map or KeyboardToGamepadMapping()

        self._gamepad: Optional[object] = None
        self._active_keys: set[int] = set()
        self._left_stick = [0, 0]   # [x, y]
        self._right_stick = [0, 0]  # [x, y]
        self._buttons: int = 0
        self._left_trigger: int = 0
        self._right_trigger: int = 0
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """Create the virtual gamepad device."""
        if not HAS_VGAMEPAD:
            logger.error(f"[Slot {self.player_slot}] vgamepad not installed. "
                         "Run: pip install vgamepad")
            return False

        try:
            self._gamepad = vg.VX360Gamepad()
            logger.info(f"[Slot {self.player_slot}] Virtual Xbox 360 controller connected")
            return True
        except Exception as e:
            logger.error(f"[Slot {self.player_slot}] Failed to create virtual gamepad: {e}")
            logger.error("Is ViGEmBus driver installed? "
                         "https://github.com/nefarius/ViGEmBus/releases")
            return False

    def disconnect(self):
        """Remove the virtual gamepad device."""
        if self._gamepad:
            try:
                self._gamepad.reset()
                self._gamepad.update()
            except Exception:
                pass
            self._gamepad = None
            logger.info(f"[Slot {self.player_slot}] Virtual gamepad disconnected")

    def apply_gamepad_input(self, state: GamepadInput):
        """Apply a raw gamepad state from a remote client using a real gamepad."""
        if not self._gamepad:
            return

        with self._lock:
            try:
                # Reset and apply full state
                self._gamepad.reset()

                # Buttons
                for flag, vg_button in XBUTTON_TO_VGAMEPAD.items():
                    if state.buttons & flag:
                        self._gamepad.press_button(button=vg_button)

                # Triggers (0-255 → 0-255)
                self._gamepad.left_trigger(value=state.left_trigger)
                self._gamepad.right_trigger(value=state.right_trigger)

                # Sticks (-32768 to 32767)
                self._gamepad.left_joystick(
                    x_value=state.left_stick_x,
                    y_value=state.left_stick_y,
                )
                self._gamepad.right_joystick(
                    x_value=state.right_stick_x,
                    y_value=state.right_stick_y,
                )

                self._gamepad.update()
            except Exception as e:
                logger.error(f"[Slot {self.player_slot}] Gamepad update error: {e}")

    def apply_mouse_input(self, mouse: MouseInput):
        """Convert mouse input to right-stick movement for camera control."""
        if not self._gamepad:
            return

        m = self.mouse_map

        with self._lock:
            # Accumulate mouse movement → right stick
            m._accum_x += mouse.dx * m.sensitivity_x
            m._accum_y += mouse.dy * m.sensitivity_y

            # Clamp to stick range
            stick_x = max(-32767, min(32767, int(m._accum_x)))
            stick_y = max(-32767, min(32767, int(-m._accum_y)))  # Invert Y

            self._right_stick = [stick_x, stick_y]

            # Decay accumulation (so stick returns to center when mouse stops)
            m._accum_x *= m.decay
            m._accum_y *= m.decay

            # Mouse buttons → gamepad buttons
            if mouse.buttons & 0x01:  # Left click
                self._buttons |= m.left_click
            else:
                self._buttons &= ~m.left_click

            if mouse.buttons & 0x02:  # Right click
                self._buttons |= m.right_click
            else:
                self._buttons &= ~m.right_click

            if mouse.buttons & 0x04:  # Middle click
                self._buttons |= m.middle_click
            else:
                self._buttons &= ~m.middle_click

            self._update_gamepad()

    def apply_keyboard_input(self, kb: KeyboardInput):
        """Convert keyboard input to gamepad state."""
        if not self._gamepad:
            return

        with self._lock:
            if kb.pressed:
                self._active_keys.add(kb.keycode)
            else:
                self._active_keys.discard(kb.keycode)

            # Rebuild left stick from WASD
            lx, ly = 0, 0
            for key in self._active_keys:
                mapping = self.keyboard_map.keymap.get(key)
                if mapping is None:
                    continue
                kind, val1, val2 = mapping
                if kind == "left_stick":
                    lx += val1
                    ly += val2
                elif kind == "button":
                    self._buttons |= val1

            # Handle key releases for buttons
            for key, mapping in self.keyboard_map.keymap.items():
                if mapping[0] == "button" and key not in self._active_keys:
                    self._buttons &= ~mapping[1]

            # Clamp stick values
            self._left_stick = [
                max(-32767, min(32767, lx)),
                max(-32767, min(32767, ly)),
            ]

            self._update_gamepad()

    def _update_gamepad(self):
        """Push current state to the virtual gamepad."""
        if not self._gamepad:
            return

        try:
            self._gamepad.reset()

            # Buttons
            for flag, vg_button in XBUTTON_TO_VGAMEPAD.items():
                if self._buttons & flag:
                    self._gamepad.press_button(button=vg_button)

            # Triggers
            self._gamepad.left_trigger(value=self._left_trigger)
            self._gamepad.right_trigger(value=self._right_trigger)

            # Sticks
            self._gamepad.left_joystick(
                x_value=self._left_stick[0],
                y_value=self._left_stick[1],
            )
            self._gamepad.right_joystick(
                x_value=self._right_stick[0],
                y_value=self._right_stick[1],
            )

            self._gamepad.update()
        except Exception as e:
            logger.error(f"[Slot {self.player_slot}] Gamepad state push error: {e}")


class InputInjectorManager:
    """Manages virtual gamepads for all remote players."""

    def __init__(self):
        self.gamepads: dict[int, VirtualGamepad] = {}

    def add_player(self, slot: int) -> VirtualGamepad:
        """Create a virtual gamepad for a remote player."""
        if slot in self.gamepads:
            raise ValueError(f"Slot {slot} already has a virtual gamepad")

        gp = VirtualGamepad(player_slot=slot)
        if gp.connect():
            self.gamepads[slot] = gp
            return gp
        else:
            raise RuntimeError(f"Failed to create virtual gamepad for slot {slot}")

    def remove_player(self, slot: int):
        """Remove a remote player's virtual gamepad."""
        if slot in self.gamepads:
            self.gamepads[slot].disconnect()
            del self.gamepads[slot]

    def remove_all(self):
        """Disconnect all virtual gamepads."""
        for gp in self.gamepads.values():
            gp.disconnect()
        self.gamepads.clear()

    def get_gamepad(self, slot: int) -> Optional[VirtualGamepad]:
        return self.gamepads.get(slot)
