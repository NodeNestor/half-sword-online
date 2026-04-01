"""Audio capture and Opus encoding for the host.

Captures game audio via WASAPI loopback (DirectShow) on Windows and encodes
to Opus via an FFmpeg subprocess, then delivers encoded packets to the
network layer through a callback.
"""

import logging
import subprocess
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Default audio device — common loopback names on Windows.
_DEFAULT_DEVICE = "Stereo Mix"


class AudioCapture:
    """Captures system audio via FFmpeg (DirectShow) and encodes to Opus.

    Parameters
    ----------
    device_name:
        DirectShow audio device name (e.g. ``"Stereo Mix"``,
        ``"CABLE Output"``).  Run ``ffmpeg -f dshow -list_devices true -i dummy``
        to discover available devices.
    sample_rate:
        Audio sample rate in Hz.
    bitrate_kbps:
        Opus encoder bitrate in kilobits per second.
    channels:
        Number of audio channels (1 = mono, 2 = stereo).
    on_audio_data:
        Callback invoked for every encoded Opus packet.
        Signature: ``(opus_bytes: bytes, timestamp_ms: int) -> None``.
    """

    def __init__(
        self,
        device_name: str = _DEFAULT_DEVICE,
        sample_rate: int = 48_000,
        bitrate_kbps: int = 128,
        channels: int = 2,
        on_audio_data: Optional[Callable[[bytes, int], None]] = None,
    ) -> None:
        self.device_name = device_name
        self.sample_rate = sample_rate
        self.bitrate_kbps = bitrate_kbps
        self.channels = channels
        self.on_audio_data = on_audio_data

        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._start_time_ms: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start audio capture and encoding."""
        with self._lock:
            if self._started:
                log.warning("AudioCapture already running")
                return

            cmd = self._build_ffmpeg_cmd()
            log.info("Starting FFmpeg: %s", " ".join(cmd))

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except FileNotFoundError:
                log.error("ffmpeg not found — make sure it is on PATH")
                raise
            except Exception:
                log.exception("Failed to launch FFmpeg")
                raise

            self._stop_event.clear()
            self._start_time_ms = _now_ms()
            self._started = True

            self._thread = threading.Thread(
                target=self._read_loop, daemon=True, name="audio-capture"
            )
            self._thread.start()
            log.info("AudioCapture started (device=%s)", self.device_name)

    def stop(self) -> None:
        """Stop audio capture, terminate FFmpeg, and join the reader thread."""
        with self._lock:
            if not self._started:
                return
            self._started = False

        self._stop_event.set()

        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("FFmpeg did not exit in time — killing")
                self._process.kill()
                self._process.wait()
            except Exception:
                log.exception("Error stopping FFmpeg")
            finally:
                self._process = None

        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

        log.info("AudioCapture stopped")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_ffmpeg_cmd(self) -> list[str]:
        """Build the FFmpeg command line."""
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            # Input — DirectShow loopback device
            "-f", "dshow",
            "-i", f"audio={self.device_name}",
            # Encoding
            "-c:a", "libopus",
            "-b:a", f"{self.bitrate_kbps}k",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-application", "audio",
            "-frame_duration", "20",  # 20 ms Opus frames
            # Output — raw Opus stream to stdout
            "-f", "opus",
            "pipe:1",
        ]

    def _read_loop(self) -> None:
        """Background thread that reads Opus data from FFmpeg stdout.

        The ``-f opus`` muxer wraps Opus frames in lightweight OGG-like
        pages.  We read fixed-size chunks and deliver them as-is; the
        receiver demuxes with the matching format.
        """
        assert self._process is not None
        assert self._process.stdout is not None

        READ_SIZE = 4096  # bytes per read — roughly a few Opus frames

        try:
            while not self._stop_event.is_set():
                chunk = self._process.stdout.read(READ_SIZE)
                if not chunk:
                    # FFmpeg exited or pipe closed
                    break

                timestamp_ms = _now_ms() - self._start_time_ms

                if self.on_audio_data is not None:
                    try:
                        self.on_audio_data(chunk, timestamp_ms)
                    except Exception:
                        log.exception("on_audio_data callback error")
        except Exception:
            log.exception("Audio read loop error")
        finally:
            log.debug("Audio read loop exited")

            # Drain stderr for diagnostics
            if self._process is not None and self._process.stderr is not None:
                try:
                    err = self._process.stderr.read()
                    if err:
                        log.debug("FFmpeg stderr: %s", err.decode(errors="replace"))
                except Exception:
                    pass


def list_devices() -> str:
    """Run FFmpeg device enumeration and return the raw output.

    Useful for discovering the correct ``device_name`` value.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-f", "dshow",
        "-list_devices", "true",
        "-i", "dummy",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # FFmpeg prints device list to stderr
    return result.stderr


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.monotonic() * 1000)
