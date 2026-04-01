"""
Half Sword Online — Frame Capture via Shared Memory

Reads raw pixel data from the FrameExport C++ plugin's shared memory
and pipes it to FFmpeg NVENC for H.264 encoding.

Pipeline:
    FrameExport (C++ plugin)
        → Shared Memory (BGRA pixels per slot)
            → This module reads pixels
                → Pipes raw frames to FFmpeg stdin
                    → FFmpeg NVENC encodes to H.264
                        → Encoded NAL units read from FFmpeg stdout
                            → Passed to network layer for streaming

Each remote player slot gets its own FFmpeg encoder process.
"""

import ctypes
import ctypes.wintypes
import subprocess
import threading
import time
import logging
from dataclasses import dataclass
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared Memory Structures (must match C++ FrameMeta)
# ---------------------------------------------------------------------------

FRAME_META_MAGIC = 0x46524D45  # "FRME"
FRAME_META_SIZE = 64

class FrameMeta(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("magic", ctypes.c_uint32),
        ("version", ctypes.c_uint32),
        ("slot", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("stride", ctypes.c_uint32),
        ("format", ctypes.c_uint32),       # 0=BGRA8
        ("frame_number", ctypes.c_uint32),
        ("timestamp_us", ctypes.c_uint64),
        ("data_size", ctypes.c_uint32),
        ("ready", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 4),
    ]

assert ctypes.sizeof(FrameMeta) == 64

# ---------------------------------------------------------------------------
# Shared Memory Reader
# ---------------------------------------------------------------------------

# Windows API for named shared memory
kernel32 = ctypes.windll.kernel32

OpenFileMappingW = kernel32.OpenFileMappingW
OpenFileMappingW.restype = ctypes.wintypes.HANDLE
OpenFileMappingW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]

MapViewOfFile = kernel32.MapViewOfFile
MapViewOfFile.restype = ctypes.c_void_p
MapViewOfFile.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
                          ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.c_size_t]

UnmapViewOfFile = kernel32.UnmapViewOfFile
UnmapViewOfFile.restype = ctypes.wintypes.BOOL
UnmapViewOfFile.argtypes = [ctypes.c_void_p]

CloseHandle = kernel32.CloseHandle

FILE_MAP_READ = 0x0004


class SharedMemoryReader:
    """Reads frame data from the FrameExport C++ plugin's shared memory."""

    def __init__(self, slot: int):
        self.slot = slot
        self._meta_handle: Optional[int] = None
        self._frame_handle: Optional[int] = None
        self._meta_ptr: Optional[int] = None
        self._frame_ptr: Optional[int] = None
        self._meta_view: Optional[FrameMeta] = None
        self._last_frame_number = 0
        self._connected = False

    def connect(self, timeout_s: float = 30.0) -> bool:
        """
        Wait for the C++ plugin to create shared memory for this slot,
        then map it into our process.
        """
        meta_name = f"HalfSwordOnline_Meta_Slot{self.slot}"
        frame_name = f"HalfSwordOnline_Frame_Slot{self.slot}"

        deadline = time.monotonic() + timeout_s
        logger.info(f"[Slot {self.slot}] Waiting for shared memory '{meta_name}'...")

        while time.monotonic() < deadline:
            # Try to open the meta shared memory
            self._meta_handle = OpenFileMappingW(FILE_MAP_READ, False, meta_name)
            if self._meta_handle:
                break
            time.sleep(0.1)

        if not self._meta_handle:
            logger.error(f"[Slot {self.slot}] Timed out waiting for shared memory")
            return False

        # Map meta region
        self._meta_ptr = MapViewOfFile(self._meta_handle, FILE_MAP_READ, 0, 0, FRAME_META_SIZE)
        if not self._meta_ptr:
            logger.error(f"[Slot {self.slot}] Failed to map meta shared memory")
            CloseHandle(self._meta_handle)
            return False

        # Read meta to get frame dimensions
        self._meta_view = FrameMeta.from_address(self._meta_ptr)

        # Verify magic
        if self._meta_view.magic != FRAME_META_MAGIC:
            logger.error(f"[Slot {self.slot}] Invalid magic: 0x{self._meta_view.magic:08x}")
            self.disconnect()
            return False

        # Open frame data shared memory
        self._frame_handle = OpenFileMappingW(FILE_MAP_READ, False, frame_name)
        if not self._frame_handle:
            logger.error(f"[Slot {self.slot}] Failed to open frame shared memory")
            self.disconnect()
            return False

        frame_size = self._meta_view.data_size
        self._frame_ptr = MapViewOfFile(self._frame_handle, FILE_MAP_READ, 0, 0, frame_size)
        if not self._frame_ptr:
            logger.error(f"[Slot {self.slot}] Failed to map frame shared memory")
            self.disconnect()
            return False

        self._connected = True
        logger.info(f"[Slot {self.slot}] Connected to shared memory "
                     f"({self._meta_view.width}x{self._meta_view.height})")
        return True

    def read_frame(self) -> Optional[tuple[bytes, int, int, int, bool]]:
        """
        Read a new frame if available.

        Returns (pixel_data, width, height, frame_number, is_new) or None.
        """
        if not self._connected or not self._meta_view:
            return None

        meta = self._meta_view

        # Check if a new frame is ready
        if meta.ready == 0 or meta.frame_number == self._last_frame_number:
            return None

        width = meta.width
        height = meta.height
        data_size = meta.data_size
        frame_num = meta.frame_number

        # Copy pixel data from shared memory
        pixel_data = (ctypes.c_ubyte * data_size).from_address(self._frame_ptr)
        frame_bytes = bytes(pixel_data)

        # Mark as consumed (if we had write access)
        self._last_frame_number = frame_num

        return frame_bytes, width, height, frame_num, True

    def disconnect(self):
        """Unmap and close shared memory handles."""
        if self._frame_ptr:
            UnmapViewOfFile(self._frame_ptr)
            self._frame_ptr = None
        if self._meta_ptr:
            UnmapViewOfFile(self._meta_ptr)
            self._meta_ptr = None
        if self._frame_handle:
            CloseHandle(self._frame_handle)
            self._frame_handle = None
        if self._meta_handle:
            CloseHandle(self._meta_handle)
            self._meta_handle = None
        self._connected = False
        self._meta_view = None

    @property
    def width(self) -> int:
        return self._meta_view.width if self._meta_view else 0

    @property
    def height(self) -> int:
        return self._meta_view.height if self._meta_view else 0


# ---------------------------------------------------------------------------
# Encoder Configuration
# ---------------------------------------------------------------------------

@dataclass
class EncoderConfig:
    codec: str = "h264"
    encoder: str = "h264_nvenc"
    preset: str = "p1"            # p1=fastest/lowest latency, p7=highest quality
    tune: str = "ull"             # ull=ultra low latency
    bitrate_kbps: int = 15000
    max_bitrate_kbps: int = 25000
    gop_size: int = 120           # Keyframe every 2s at 60fps
    bframes: int = 0
    rc_mode: str = "cbr"
    profile: str = "high"
    level: str = "4.2"


# ---------------------------------------------------------------------------
# NVENC Encoder (FFmpeg subprocess)
# ---------------------------------------------------------------------------

class NVENCEncoder:
    """
    Pipes raw BGRA frames to FFmpeg for NVENC H.264 encoding.

    Input:  Raw BGRA pixels via stdin pipe
    Output: H.264 Annex B NAL units via stdout pipe
    """

    def __init__(self, slot: int, width: int, height: int, fps: int,
                 config: EncoderConfig,
                 on_encoded_data: Callable[[bytes, bool], None]):
        self.slot = slot
        self.width = width
        self.height = height
        self.fps = fps
        self.config = config
        self.on_encoded_data = on_encoded_data

        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._frames_in = 0
        self._frames_out = 0

    def start(self):
        if self._running:
            return

        cmd = self._build_command()
        logger.info(f"[Slot {self.slot}] Starting NVENC encoder:")
        logger.info(f"  {' '.join(cmd)}")

        self._running = True
        self._frames_in = 0
        self._frames_out = 0

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        # Reader thread: pulls encoded data from FFmpeg stdout
        self._reader_thread = threading.Thread(
            target=self._read_output, daemon=True,
            name=f"encoder-out-{self.slot}")
        self._reader_thread.start()

        # Stderr logger
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True,
            name=f"encoder-err-{self.slot}")
        self._stderr_thread.start()

    def stop(self):
        self._running = False
        if self._process:
            if self._process.stdin:
                try:
                    self._process.stdin.close()
                except Exception:
                    pass
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        logger.info(f"[Slot {self.slot}] Encoder stopped. "
                     f"In: {self._frames_in}, Out: {self._frames_out}")

    def feed_frame(self, bgra_pixels: bytes):
        """Feed a raw BGRA frame to the encoder."""
        if not self._running or not self._process or not self._process.stdin:
            return

        try:
            self._process.stdin.write(bgra_pixels)
            self._process.stdin.flush()
            self._frames_in += 1
        except (BrokenPipeError, OSError) as e:
            logger.error(f"[Slot {self.slot}] Encoder pipe broken: {e}")
            self._running = False

    def force_keyframe(self):
        """Request an IDR frame. Requires encoder restart with FFmpeg pipe approach."""
        logger.info(f"[Slot {self.slot}] Forcing keyframe (encoder restart)")
        self.stop()
        self.start()

    def _build_command(self) -> list[str]:
        cfg = self.config
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",

            # Input: raw BGRA from stdin
            "-f", "rawvideo",
            "-pixel_format", "bgra",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", "pipe:0",

            # Encoding
            "-c:v", cfg.encoder,
            "-pix_fmt", "yuv420p",  # NVENC needs YUV input

            # NVENC settings for ultra-low latency
            "-preset", cfg.preset,
            "-tune", cfg.tune,
            "-profile:v", cfg.profile,
            "-level:v", cfg.level,
            "-rc", cfg.rc_mode,
            "-b:v", f"{cfg.bitrate_kbps}k",
            "-maxrate", f"{cfg.max_bitrate_kbps}k",
            "-bufsize", f"{cfg.bitrate_kbps // 4}k",  # Tiny buffer = lowest latency
            "-g", str(cfg.gop_size),
            "-bf", str(cfg.bframes),
            "-delay", "0",
            "-zerolatency", "1",
            "-forced-idr", "1",

            # No audio
            "-an",

            # Output: raw H.264 to stdout
            "-f", "h264",
            "pipe:1",
        ]

    def _read_output(self):
        """Read encoded H.264 data from FFmpeg stdout."""
        CHUNK = 65536
        while self._running and self._process:
            try:
                data = self._process.stdout.read(CHUNK)
                if not data:
                    break
                # Pass encoded data to network layer
                # The data contains H.264 NAL units in Annex B format
                is_keyframe = self._check_keyframe(data)
                self._frames_out += 1
                self.on_encoded_data(data, is_keyframe)
            except Exception as e:
                if self._running:
                    logger.error(f"[Slot {self.slot}] Encoder read error: {e}")
                break

    def _check_keyframe(self, data: bytes) -> bool:
        """Check if H.264 data contains an IDR frame."""
        # Look for NAL type 5 (IDR) or 7 (SPS)
        i = 0
        while i < len(data) - 4:
            if data[i] == 0 and data[i + 1] == 0:
                nal_offset = -1
                if data[i + 2] == 1:
                    nal_offset = i + 3
                elif data[i + 2] == 0 and i + 3 < len(data) and data[i + 3] == 1:
                    nal_offset = i + 4
                if nal_offset >= 0 and nal_offset < len(data):
                    nal_type = data[nal_offset] & 0x1F
                    if nal_type in (5, 7):
                        return True
                    i = nal_offset
                else:
                    i += 1
            else:
                i += 1
        return False

    def _read_stderr(self):
        while self._running and self._process:
            try:
                line = self._process.stderr.readline()
                if not line:
                    break
                logger.debug(f"[Slot {self.slot}] ffmpeg: {line.decode(errors='replace').strip()}")
            except Exception:
                break


# ---------------------------------------------------------------------------
# Per-Player Capture + Encode Pipeline
# ---------------------------------------------------------------------------

class PlayerPipeline:
    """
    Complete capture-to-encode pipeline for one remote player.

    SharedMemory → Read pixels → FFmpeg NVENC → Encoded frames → Callback
    """

    def __init__(self, slot: int, fps: int, encoder_config: EncoderConfig,
                 on_encoded_data: Callable[[bytes, bool], None]):
        self.slot = slot
        self.fps = fps
        self.encoder_config = encoder_config
        self.on_encoded_data = on_encoded_data

        self.shm = SharedMemoryReader(slot)
        self.encoder: Optional[NVENCEncoder] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._running = False
        self._stats_frames = 0
        self._stats_start = 0.0

    def start(self) -> bool:
        """Connect to shared memory and start the encode pipeline."""
        if not self.shm.connect(timeout_s=30):
            return False

        self.encoder = NVENCEncoder(
            slot=self.slot,
            width=self.shm.width,
            height=self.shm.height,
            fps=self.fps,
            config=self.encoder_config,
            on_encoded_data=self.on_encoded_data,
        )
        self.encoder.start()

        self._running = True
        self._stats_start = time.monotonic()

        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True,
            name=f"capture-{self.slot}")
        self._capture_thread.start()

        return True

    def stop(self):
        self._running = False
        if self.encoder:
            self.encoder.stop()
        self.shm.disconnect()

        elapsed = time.monotonic() - self._stats_start
        if elapsed > 0:
            logger.info(f"[Slot {self.slot}] Pipeline stopped. "
                        f"{self._stats_frames} frames, "
                        f"{self._stats_frames / elapsed:.1f} fps avg")

    def _capture_loop(self):
        """
        Main capture loop: poll shared memory for new frames,
        pipe them to the encoder.
        """
        target_interval = 1.0 / self.fps
        next_frame_time = time.monotonic()

        while self._running:
            now = time.monotonic()

            # Rate limiting: don't exceed target FPS
            if now < next_frame_time:
                sleep_time = next_frame_time - now
                if sleep_time > 0.0005:  # Don't sleep less than 0.5ms
                    time.sleep(sleep_time)
                continue

            next_frame_time = now + target_interval

            # Read frame from shared memory
            result = self.shm.read_frame()
            if result is None:
                continue  # No new frame yet

            pixel_data, width, height, frame_num, is_new = result
            if not is_new:
                continue

            # Feed to encoder
            self.encoder.feed_frame(pixel_data)
            self._stats_frames += 1
