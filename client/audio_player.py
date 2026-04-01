"""
Half Sword Online — Client Audio Player

Receives Opus audio packets from the host and plays them via FFmpeg → SDL2.

Pipeline:
    UDP audio packets (Opus)
        → FFmpeg decodes Opus → raw PCM
            → pygame.mixer plays PCM

Alternative simpler approach: pipe Opus to FFplay or use PyAudio.
We use FFmpeg decode → pygame for consistency with the video pipeline.
"""

import logging
import queue
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import pygame
    import pygame.mixer
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False


class AudioPlayer:
    """
    Decodes and plays Opus audio from the host stream.

    Uses FFmpeg to decode Opus → raw PCM (s16le, 48000 Hz, stereo)
    and feeds it to pygame.mixer for playback.
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2,
                 buffer_ms: int = 40):
        self.sample_rate = sample_rate
        self.channels = channels
        self.buffer_ms = buffer_ms

        self._decoder_process: Optional[subprocess.Popen] = None
        self._running = False
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._playback_thread: Optional[threading.Thread] = None

        # PCM buffer size per chunk (buffer_ms worth of audio)
        # 48000 Hz * 2 channels * 2 bytes (s16le) * (buffer_ms/1000)
        self.chunk_bytes = int(sample_rate * channels * 2 * (buffer_ms / 1000))

    def start(self):
        """Start the audio decode + playback pipeline."""
        if not HAS_PYGAME:
            logger.warning("pygame not available — audio disabled")
            return

        self._running = True

        # Init pygame mixer
        try:
            pygame.mixer.init(
                frequency=self.sample_rate,
                size=-16,  # signed 16-bit
                channels=self.channels,
                buffer=1024,  # Small buffer for low latency
            )
        except Exception as e:
            logger.error(f"Failed to init audio mixer: {e}")
            return

        # Start FFmpeg Opus decoder
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",

            # Input: Opus from stdin
            "-f", "ogg",
            "-i", "pipe:0",

            # Output: raw PCM to stdout
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "pipe:1",
        ]

        try:
            self._decoder_process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        except FileNotFoundError:
            logger.error("FFmpeg not found — audio disabled")
            self._running = False
            return

        # Playback thread: reads decoded PCM from FFmpeg and plays it
        self._playback_thread = threading.Thread(
            target=self._playback_loop, daemon=True, name="audio-play")
        self._playback_thread.start()

        logger.info(f"Audio player started: {self.sample_rate}Hz, "
                     f"{self.channels}ch, {self.buffer_ms}ms buffer")

    def stop(self):
        self._running = False
        if self._decoder_process:
            if self._decoder_process.stdin:
                try: self._decoder_process.stdin.close()
                except: pass
            self._decoder_process.terminate()
            try: self._decoder_process.wait(timeout=2)
            except: self._decoder_process.kill()
            self._decoder_process = None

        try:
            pygame.mixer.quit()
        except:
            pass

    def feed_audio(self, opus_data: bytes):
        """Feed Opus audio data (from network) to the decoder."""
        if not self._running or not self._decoder_process:
            return
        try:
            self._decoder_process.stdin.write(opus_data)
            self._decoder_process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _playback_loop(self):
        """Read decoded PCM from FFmpeg and play via pygame."""
        if not self._decoder_process:
            return

        # Use a pygame.mixer.Channel for streaming playback
        # We create Sound objects from raw PCM chunks and queue them

        while self._running:
            try:
                # Read a chunk of decoded PCM
                pcm_data = self._decoder_process.stdout.read(self.chunk_bytes)
                if not pcm_data:
                    break

                # Create a Sound from raw bytes and play it
                try:
                    sound = pygame.mixer.Sound(buffer=pcm_data)
                    sound.play()
                except Exception as e:
                    logger.debug(f"Audio playback error: {e}")

            except Exception as e:
                if self._running:
                    logger.error(f"Audio read error: {e}")
                break


class SimpleAudioPlayer:
    """
    Even simpler approach: pipe everything to ffplay for playback.
    No pygame dependency needed. Less control but dead simple.
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2):
        self.sample_rate = sample_rate
        self.channels = channels
        self._process: Optional[subprocess.Popen] = None
        self._running = False

    def start(self):
        cmd = [
            "ffplay",
            "-hide_banner",
            "-loglevel", "warning",
            "-nodisp",           # No video window
            "-autoexit",
            "-f", "ogg",
            "-i", "pipe:0",
            "-af", f"aresample={self.sample_rate}",
        ]

        try:
            self._process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self._running = True
            logger.info("Audio player started (ffplay)")
        except FileNotFoundError:
            logger.error("ffplay not found — audio disabled")

    def stop(self):
        self._running = False
        if self._process:
            if self._process.stdin:
                try: self._process.stdin.close()
                except: pass
            self._process.terminate()
            try: self._process.wait(timeout=2)
            except: self._process.kill()
            self._process = None

    def feed_audio(self, opus_data: bytes):
        if not self._running or not self._process:
            return
        try:
            self._process.stdin.write(opus_data)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
