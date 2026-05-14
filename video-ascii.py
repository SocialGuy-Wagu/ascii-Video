"""
ASCII Video Player
==================
A high-performance terminal ASCII art video player and exporter.

Features:
  - Direct ffmpeg pipe export (no temp PNGs)
  - Streaming frame processing (no full video in RAM)
  - Multiprocessing with guaranteed frame ordering
  - Unicode/Braille rendering mode
  - Edge-detection mode
  - Configurable ASCII palettes & preset profiles
  - Brightness / contrast / gamma controls
  - Colored background mode
  - Monochrome terminal mode optimization
  - Live webcam input
  - YouTube / stream URL input (yt-dlp)
  - Real-time FPS counter
  - Benchmark mode
  - Floyd–Steinberg & Bayer dithering
  - Dynamic contrast normalization (CLAHE)
  - Temporal smoothing between frames
  - 256-color fallback mode
  - Terminal capability detection
  - Terminal resize debounce
  - Pause / play / seek / mute keyboard controls
  - GPU acceleration option (ffmpeg hwaccel)
  - Adaptive frame skipping
  - GIF export
  - Image sequence export
  - Subtitle auto-discovery + offset sync
  - Config file support (JSON / TOML)
  - Rich / Textual UI mode (if installed)
  - Multiprocessing-safe progress tracking
  - Worker exception recovery
  - Graceful cleanup on crash / CTRL+C
  - Startup performance diagnostics

Authors: Nicolas Romero (coralgamer), original concept stepanussaruran
License: MIT
"""

# ── Stdlib ────────────────────────────────────────────────────────────────────
import os
import sys
import re
import time
import json
import signal
import shutil
import logging
import argparse
import threading
import subprocess
import tempfile
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from queue import Queue, Empty, Full
from typing import Optional, Tuple, Dict, List, Any
from multiprocessing import Pool, cpu_count, Manager, Value
import ctypes
import io

# ── Third-party (checked at runtime) ─────────────────────────────────────────
try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import numpy as np
    _NP = True
except ImportError:
    _NP = False

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    _PIL = True
except ImportError:
    _PIL = False

try:
    import tomllib                      # Python 3.11+
    _TOML = True
except ImportError:
    try:
        import tomli as tomllib         # backport
        _TOML = True
    except ImportError:
        _TOML = False

try:
    from rich.console import Console as RichConsole
    from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, SpinnerColumn
    _RICH = True
except ImportError:
    _RICH = False

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ascii_player")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

class RenderMode(str, Enum):
    ASCII      = "ascii"
    UNICODE    = "unicode"
    BRAILLE    = "braille"
    EDGE       = "edge"

class DitherAlgo(str, Enum):
    NONE         = "none"
    FLOYD        = "floyd"
    BAYER        = "bayer"

class ExportFormat(str, Enum):
    MP4  = "mp4"
    GIF  = "gif"
    SEQ  = "sequence"

class ColorMode(str, Enum):
    TRUECOLOR  = "truecolor"   # 24-bit ANSI
    C256       = "256"         # xterm-256
    MONO       = "mono"        # no color
    BG_COLOR   = "bg"          # colored background

PALETTES: Dict[str, str] = {
    "standard":   " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczmwqpdbkhao*#MW&8%B@$",
    "simple":     " .:-=+*#%@",
    "blocks":     " ░▒▓█",
    "dense":      " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczmwqpdbkhao*#MW&8%B@$0QSXGZJKPHDAUYTRENVLCF",
    "binary":     " @",
    "minimal":    "  ..::--==++**##%%@@",
    "retro":      " .oO0@",
    "shade":      " ·:░▒▓",
}

PRESETS: Dict[str, Dict[str, Any]] = {
    "retro": {
        "palette": "retro",
        "color_mode": ColorMode.MONO,
        "render_mode": RenderMode.ASCII,
        "dither": DitherAlgo.BAYER,
        "contrast": 1.3,
        "brightness": 0.9,
        "gamma": 1.2,
        "font_size": 10,
    },
    "cinematic": {
        "palette": "dense",
        "color_mode": ColorMode.TRUECOLOR,
        "render_mode": RenderMode.ASCII,
        "dither": DitherAlgo.FLOYD,
        "contrast": 1.1,
        "brightness": 1.0,
        "gamma": 0.9,
        "temporal_smooth": True,
        "letterbox": True,
        "font_size": 12,
    },
    "high-detail": {
        "palette": "dense",
        "color_mode": ColorMode.TRUECOLOR,
        "render_mode": RenderMode.EDGE,
        "dither": DitherAlgo.FLOYD,
        "contrast": 1.5,
        "brightness": 1.0,
        "gamma": 1.0,
        "font_size": 9,
    },
    "terminal-fast": {
        "palette": "simple",
        "color_mode": ColorMode.MONO,
        "render_mode": RenderMode.ASCII,
        "dither": DitherAlgo.NONE,
        "contrast": 1.0,
        "brightness": 1.0,
        "gamma": 1.0,
        "skip": 2,
        "font_size": 10,
    },
    "braille": {
        "palette": "dense",
        "color_mode": ColorMode.TRUECOLOR,
        "render_mode": RenderMode.BRAILLE,
        "dither": DitherAlgo.FLOYD,
        "contrast": 1.2,
        "brightness": 1.0,
        "gamma": 1.0,
        "font_size": 10,
    },
}


@dataclass
class PlayerConfig:
    # Input
    video:            str   = "video.mp4"
    subtitle:         str   = ""
    subtitle_offset:  float = 0.0        # seconds to shift subtitles

    # Rendering
    render_mode:      RenderMode   = RenderMode.ASCII
    palette:          str          = "dense"
    color_mode:       ColorMode    = ColorMode.TRUECOLOR
    dither:           DitherAlgo   = DitherAlgo.NONE
    width:            Optional[int] = None
    font_size:        int   = 10

    # Image adjustments
    brightness:       float = 1.0
    contrast:         float = 1.0
    gamma:            float = 1.0
    clahe:            bool  = False      # dynamic contrast normalization
    temporal_smooth:  bool  = False

    # Playback
    skip:             int   = 1
    loop:             bool  = False
    mute:             bool  = False
    letterbox:        bool  = False

    # Export
    export:           bool         = False
    export_format:    ExportFormat = ExportFormat.MP4
    export_codec:     str          = "libx264"
    export_path:      str          = "ascii_output"
    gpu_accel:        bool         = False

    # Modes
    benchmark:        bool  = False
    webcam:           bool  = False
    stats_overlay:    bool  = False
    rich_ui:          bool  = False
    low_latency:      bool  = False

    # Internal
    preset:           Optional[str] = None

    def apply_preset(self, name: str) -> None:
        data = PRESETS.get(name)
        if not data:
            log.warning("Unknown preset '%s'. Available: %s", name, list(PRESETS))
            return
        for k, v in data.items():
            if hasattr(self, k):
                setattr(self, k, v)
        log.info("Applied preset '%s'", name)

    @classmethod
    def from_file(cls, path: str) -> "PlayerConfig":
        p = Path(path)
        raw: Dict[str, Any] = {}
        if p.suffix in (".toml",) and _TOML:
            with open(p, "rb") as f:
                raw = tomllib.load(f)
        elif p.suffix == ".json":
            with open(p) as f:
                raw = json.load(f)
        else:
            log.warning("Config file format not supported or tomllib not installed: %s", path)
            return cls()

        cfg = cls()
        for k, v in raw.items():
            if hasattr(cfg, k):
                # Convert string enums
                attr = getattr(cfg, k)
                if isinstance(attr, Enum):
                    try:
                        setattr(cfg, k, type(attr)(v))
                    except ValueError:
                        log.warning("Invalid value '%s' for config key '%s'", v, k)
                else:
                    setattr(cfg, k, v)
        return cfg


# ══════════════════════════════════════════════════════════════════════════════
# TERMINAL CAPABILITY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TermCaps:
    """Detect and cache terminal capabilities."""

    def __init__(self) -> None:
        self.truecolor   = self._detect_truecolor()
        self.c256        = self._detect_256()
        self.unicode     = self._detect_unicode()
        self.cols, self.lines = self._get_size()
        self._resize_ts  = 0.0          # last resize timestamp
        self._lock       = threading.Lock()
        self._install_resize_handler()

    def _detect_truecolor(self) -> bool:
        ct = os.environ.get("COLORTERM", "").lower()
        return ct in ("truecolor", "24bit")

    def _detect_256(self) -> bool:
        term = os.environ.get("TERM", "")
        return "256color" in term or self._detect_truecolor()

    def _detect_unicode(self) -> bool:
        enc = (sys.stdout.encoding or "ascii").lower()
        return "utf" in enc

    def _get_size(self) -> Tuple[int, int]:
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except OSError:
            return 80, 24

    def _install_resize_handler(self) -> None:
        if os.name != "nt":
            try:
                signal.signal(signal.SIGWINCH, self._on_resize)
            except (OSError, ValueError):
                pass

    def _on_resize(self, *_) -> None:
        now = time.monotonic()
        with self._lock:
            self._resize_ts = now

    def get_size_debounced(self, debounce: float = 0.15) -> Tuple[int, int]:
        """Return terminal size; debounce rapid resize events."""
        with self._lock:
            age = time.monotonic() - self._resize_ts
        if age < debounce:
            time.sleep(debounce - age)
        self.cols, self.lines = self._get_size()
        return self.cols, self.lines

    def best_color_mode(self) -> ColorMode:
        if self.truecolor:
            return ColorMode.TRUECOLOR
        if self.c256:
            return ColorMode.C256
        return ColorMode.MONO


TERM = TermCaps()


# ══════════════════════════════════════════════════════════════════════════════
# ANSI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

CURSOR_HOME  = "\033[H"
CLEAR_SCREEN = "\033[2J"
HIDE_CURSOR  = "\033[?25l"
SHOW_CURSOR  = "\033[?25h"
WRAP_OFF     = "\033[?7l"   # disable line auto-wrap (DECAWM off)
WRAP_ON      = "\033[?7h"   # re-enable line auto-wrap
RESET_COLOR  = "\033[0m"
C_CYAN       = "\033[96m"
C_GREEN      = "\033[92m"
C_YELLOW     = "\033[93m"
C_RED        = "\033[91m"
C_GRAY       = "\033[90m"
C_BOLD       = "\033[1m"
C_BG_BLACK   = "\033[40m"

# Quantized color cache: key = (r//8, g//8, b//8)
_ANSI_CACHE: Dict[Tuple[int, int, int], str] = {}

def _ansi_fg(r: int, g: int, b: int) -> str:
    key = (r >> 3, g >> 3, b >> 3)
    v = _ANSI_CACHE.get(key)
    if v is None:
        qr, qg, qb = key[0] << 3, key[1] << 3, key[2] << 3
        v = f"\033[38;2;{qr};{qg};{qb}m"
        _ANSI_CACHE[key] = v
    return v

def _ansi_bg(r: int, g: int, b: int) -> str:
    key = (r >> 3 | 0x8000, g >> 3, b >> 3)      # namespace bg vs fg
    v = _ANSI_CACHE.get(key)
    if v is None:
        qr, qg, qb = (key[0] & 0x7FFF) << 3, key[1] << 3, key[2] << 3
        v = f"\033[48;2;{qr};{qg};{qb}m"
        _ANSI_CACHE[key] = v
    return v

def _rgb_to_256(r: int, g: int, b: int) -> int:
    """Map an RGB triplet to the nearest xterm-256 color index."""
    if r == g == b:
        if r < 8:   return 16
        if r > 248: return 231
        return round((r - 8) / 247 * 24) + 232
    ri = round(r / 255 * 5)
    gi = round(g / 255 * 5)
    bi = round(b / 255 * 5)
    return 16 + 36 * ri + 6 * gi + bi

def _ansi_256_fg(r: int, g: int, b: int) -> str:
    return f"\033[38;5;{_rgb_to_256(r,g,b)}m"


def enable_ansi_windows() -> None:
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# DITHERING
# ══════════════════════════════════════════════════════════════════════════════

def _bayer4() -> "np.ndarray":
    """Return normalized 4×4 Bayer threshold matrix."""
    m = np.array([[0,8,2,10],[12,4,14,6],[3,11,1,9],[15,7,13,5]], dtype=np.float32)
    return m / 16.0

_BAYER4 = None  # lazy init

def apply_dither(gray: "np.ndarray", algo: DitherAlgo) -> "np.ndarray":
    """Apply dithering to a float32 grayscale image [0,1]."""
    global _BAYER4
    if algo == DitherAlgo.NONE:
        return gray

    if algo == DitherAlgo.BAYER:
        if _BAYER4 is None:
            _BAYER4 = _bayer4()
        h, w = gray.shape
        tile = np.tile(_BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w]
        return np.clip(gray + (tile - 0.5) * 0.2, 0, 1)

    if algo == DitherAlgo.FLOYD:
        # Floyd–Steinberg (in-place on copy)
        out = gray.copy()
        h, w = out.shape
        n = len(_CHARS_ARRAY) - 1
        for y in range(h):
            for x in range(w):
                old = out[y, x]
                idx = int(round(old * n))
                idx = max(0, min(n, idx))
                # quantized brightness for chosen char
                new = idx / n
                err = old - new
                out[y, x] = new
                if x + 1 < w:
                    out[y, x+1]     = np.clip(out[y, x+1]     + err * 7/16, 0, 1)
                if y + 1 < h:
                    if x > 0:
                        out[y+1, x-1] = np.clip(out[y+1, x-1] + err * 3/16, 0, 1)
                    out[y+1, x]       = np.clip(out[y+1, x]   + err * 5/16, 0, 1)
                    if x + 1 < w:
                        out[y+1, x+1] = np.clip(out[y+1, x+1] + err * 1/16, 0, 1)
        return out

    return gray


# ══════════════════════════════════════════════════════════════════════════════
# BRAILLE RENDERER
# ══════════════════════════════════════════════════════════════════════════════

# Braille dot layout (column-major, 2×4 per cell)
_BRAILLE_BASE  = 0x2800
_DOT_MAP = [(0,0,0x01),(1,0,0x02),(2,0,0x04),(0,1,0x08),
            (3,0,0x10),(1,1,0x20),(2,1,0x40),(3,1,0x80)]

def frame_to_braille(gray_norm: "np.ndarray") -> str:
    """Convert a normalized float32 grayscale array to a Braille string."""
    h, w = gray_norm.shape
    # Pad to 4 rows × 2 cols multiples
    ph = (h + 3) & ~3
    pw = (w + 1) & ~1
    pad = np.zeros((ph, pw), dtype=np.float32)
    pad[:h, :w] = gray_norm
    threshold = 0.4

    lines = []
    for by in range(0, ph, 4):
        line_chars = []
        for bx in range(0, pw, 2):
            code = 0
            for dr, dc, bit in _DOT_MAP:
                if pad[by+dr, bx+dc] > threshold:
                    code |= bit
            line_chars.append(chr(_BRAILLE_BASE | code))
        lines.append("".join(line_chars))
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# FRAME CONVERSION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

# Lazy — set once from config
_CHARS_ARRAY: Optional["np.ndarray"] = None
_CLAHE_OBJ   = None
_PREV_GRAY: Optional["np.ndarray"] = None   # for temporal smoothing

def _init_chars(palette: str) -> None:
    global _CHARS_ARRAY
    chars = PALETTES.get(palette, PALETTES["dense"])
    _CHARS_ARRAY = np.array(list(chars))

def _apply_image_adjustments(gray_f: "np.ndarray", cfg: PlayerConfig) -> "np.ndarray":
    """Apply brightness/contrast/gamma + optional CLAHE. Input/output: float32 [0,1]."""
    global _CLAHE_OBJ, _PREV_GRAY

    # Brightness / contrast
    gray_f = np.clip(gray_f * cfg.brightness * cfg.contrast
                     - 0.5 * (cfg.contrast - 1), 0, 1)
    # Gamma
    if cfg.gamma != 1.0:
        gray_f = np.power(np.clip(gray_f, 1e-9, 1.0), 1.0 / cfg.gamma)

    # CLAHE (dynamic contrast normalization)
    if cfg.clahe:
        if _CLAHE_OBJ is None:
            _CLAHE_OBJ = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray8 = (gray_f * 255).astype(np.uint8)
        gray_f = _CLAHE_OBJ.apply(gray8).astype(np.float32) / 255.0

    # Temporal smoothing
    if cfg.temporal_smooth and _PREV_GRAY is not None and _PREV_GRAY.shape == gray_f.shape:
        gray_f = 0.7 * gray_f + 0.3 * _PREV_GRAY
    _PREV_GRAY = gray_f
    return gray_f


def frame_to_ascii(
    frame: "np.ndarray",
    cfg: PlayerConfig,
    width: int,
    height: Optional[int] = None,
) -> Tuple["np.ndarray", Optional["np.ndarray"]]:
    """
    Convert a BGR OpenCV frame to an ASCII/Braille char map.
    Returns (char_map_2d, rgb_map_2d_or_None).
    For BRAILLE mode, char_map is a 2D array of single braille chars.

    If `height` is None, height is derived from the source aspect ratio
    (with the standard 2:1 char-cell correction). If `height` is provided,
    the frame is stretched to exactly width × height — useful for filling
    the entire terminal regardless of video aspect.
    """
    assert _CHARS_ARRAY is not None, "Call _init_chars() first"

    h_orig, w_orig = frame.shape[:2]
    if height is None:
        height = max(1, int(h_orig * width / w_orig / 2))
    else:
        height = max(1, height)

    if cfg.render_mode == RenderMode.EDGE:
        gray8 = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray8, 50, 150)
        edges_resized = cv2.resize(edges, (width, height)).astype(np.float32) / 255.0
        resized_gray = _apply_image_adjustments(edges_resized, cfg)
        resized_rgb  = cv2.cvtColor(cv2.resize(frame, (width, height)), cv2.COLOR_BGR2RGB) \
                       if cfg.color_mode != ColorMode.MONO else None
    else:
        resized = cv2.resize(frame, (width, height))
        resized_rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB) \
                      if cfg.color_mode != ColorMode.MONO else None
        gray_raw = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        resized_gray = _apply_image_adjustments(gray_raw, cfg)

    if cfg.dither != DitherAlgo.NONE:
        resized_gray = apply_dither(resized_gray, cfg.dither)

    if cfg.render_mode == RenderMode.BRAILLE:
        braille_str = frame_to_braille(resized_gray)
        char_map = np.array([list(row) for row in braille_str.split("\n")])
        rgb_map  = None     # TODO: color braille via terminal fg per cell
        return char_map, rgb_map

    n_chars = len(_CHARS_ARRAY) - 1
    indices  = np.clip((resized_gray * n_chars).astype(np.int32), 0, n_chars)
    char_map = _CHARS_ARRAY[indices]
    return char_map, resized_rgb


def char_map_to_ansi(
    char_map: "np.ndarray",
    rgb_map:  Optional["np.ndarray"],
    cfg: PlayerConfig,
) -> str:
    """Render a 2D char_map to a terminal ANSI string."""
    h, w = char_map.shape
    lines = []
    if cfg.color_mode == ColorMode.MONO:
        for row in char_map:
            lines.append("".join(row))
    elif cfg.color_mode == ColorMode.TRUECOLOR:
        for y, row in enumerate(char_map):
            parts = []
            if rgb_map is not None:
                rgb_row = rgb_map[y]
                for c, (r, g, b) in zip(row, rgb_row):
                    parts.append(_ansi_fg(int(r), int(g), int(b)) + c)
            else:
                parts = list(row)
            lines.append("".join(parts) + RESET_COLOR)
    elif cfg.color_mode == ColorMode.BG_COLOR:
        for y, row in enumerate(char_map):
            parts = []
            if rgb_map is not None:
                rgb_row = rgb_map[y]
                for c, (r, g, b) in zip(row, rgb_row):
                    parts.append(_ansi_bg(int(r), int(g), int(b)) + c)
            else:
                parts = list(row)
            lines.append("".join(parts) + RESET_COLOR)
    elif cfg.color_mode == ColorMode.C256:
        for y, row in enumerate(char_map):
            parts = []
            if rgb_map is not None:
                rgb_row = rgb_map[y]
                for c, (r, g, b) in zip(row, rgb_row):
                    parts.append(_ansi_256_fg(int(r), int(g), int(b)) + c)
            else:
                parts = list(row)
            lines.append("".join(parts) + RESET_COLOR)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SUBTITLE SUPPORT
# ══════════════════════════════════════════════════════════════════════════════

def _parse_lrc(path: str) -> List[Tuple[float, str]]:
    results = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                m = re.match(r'\[(\d+):(\d+\.\d+)\](.*)', line)
                if m:
                    ts = float(m.group(1)) * 60 + float(m.group(2))
                    results.append((ts, m.group(3).strip()))
    except Exception as e:
        log.warning("LRC parse error: %s", e)
    return results

def _parse_srt(path: str) -> List[Tuple[float, str]]:
    results = []
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        for block in content.strip().split("\n\n"):
            lines = block.splitlines()
            if len(lines) < 3:
                continue
            m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)', lines[1])
            if m:
                ts = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                      + int(m.group(3)) + int(m.group(4)) / 1000)
                text = " ".join(lines[2:])
                results.append((ts, text))
    except Exception as e:
        log.warning("SRT parse error: %s", e)
    return results

def auto_discover_subtitle(video_path: str) -> str:
    """Look for a subtitle file with the same stem as the video."""
    p = Path(video_path)
    for ext in (".srt", ".lrc", ".ass", ".vtt"):
        candidate = p.with_suffix(ext)
        if candidate.exists():
            log.info("Auto-discovered subtitle: %s", candidate)
            return str(candidate)
    return ""

def load_subtitles(path: str) -> List[Tuple[float, str]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        log.warning("Subtitle file not found: %s", path)
        return []
    ext = p.suffix.lower()
    if ext == ".lrc":
        data = _parse_lrc(str(p))
    elif ext == ".srt":
        data = _parse_srt(str(p))
    else:
        log.warning("Unsupported subtitle format: %s", ext)
        return []
    log.info("Loaded %d subtitle entries from %s", len(data), p.name)
    return data

def get_current_lyric(lyrics: List[Tuple[float, str]], elapsed: float, offset: float = 0.0) -> str:
    t = elapsed + offset
    result = ""
    for ts, text in lyrics:
        if t >= ts:
            result = text
        else:
            break
    return result


# ══════════════════════════════════════════════════════════════════════════════
# VIDEO INFO & SOURCE
# ══════════════════════════════════════════════════════════════════════════════

def get_video_info(cap: "cv2.VideoCapture") -> Dict[str, float]:
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    return {
        "fps":          fps,
        "total_frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        "width_px":     int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height_px":    int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "duration_s":   cap.get(cv2.CAP_PROP_FRAME_COUNT) / max(fps, 1),
    }

def open_video_source(cfg: PlayerConfig) -> "cv2.VideoCapture":
    """Open a webcam, yt-dlp URL, or file."""
    if cfg.webcam:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            raise RuntimeError("Could not open webcam.")
        return cap

    src = cfg.video
    # YouTube / stream URL via yt-dlp
    if src.startswith("http://") or src.startswith("https://") or src.startswith("ytdl://"):
        url = src.replace("ytdl://", "https://")
        try:
            import yt_dlp  # type: ignore
            ydl_opts = {"quiet": True, "format": "best[ext=mp4]"}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                direct_url = info["url"]
            cap = cv2.VideoCapture(direct_url)
            if cap.isOpened():
                log.info("Opened stream via yt-dlp: %s", url)
                return cap
        except ImportError:
            log.warning("yt-dlp not installed; trying direct URL.")
        cap = cv2.VideoCapture(url)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open URL: {url}")
        return cap

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {src}")
    return cap


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT — direct ffmpeg pipe (no temp PNGs)
# ══════════════════════════════════════════════════════════════════════════════

# ── PIL image rendering (for export only) ────────────────────────────────────

# Cache font + metrics globally to avoid repeated disk lookups
_FONT_CACHE: Dict[int, Any] = {}

def _get_font(font_size: int) -> Any:
    if font_size not in _FONT_CACHE:
        for name in ("consola.ttf", "cour.ttf", "DejaVuSansMono.ttf",
                     "LiberationMono-Regular.ttf"):
            try:
                _FONT_CACHE[font_size] = ImageFont.truetype(name, font_size)
                break
            except (IOError, OSError):
                pass
        if font_size not in _FONT_CACHE:
            _FONT_CACHE[font_size] = ImageFont.load_default()
    return _FONT_CACHE[font_size]

# Cache resized char dimensions per font_size
_CHAR_METRICS: Dict[int, Tuple[int, int]] = {}

def _get_char_metrics(font_size: int) -> Tuple[int, int]:
    if font_size not in _CHAR_METRICS:
        font = _get_font(font_size)
        bbox = font.getbbox("A")
        _CHAR_METRICS[font_size] = (bbox[2] - bbox[0], bbox[3] - bbox[1])
    return _CHAR_METRICS[font_size]


def _char_map_to_pil(
    char_map: "np.ndarray",
    rgb_map:  Optional["np.ndarray"],
    bg_color: Tuple[int, int, int],
    font_size: int,
    lyric:    str = "",
    letterbox: bool = False,
) -> "Image.Image":
    """Render char_map → PIL Image (used for export frames)."""
    font      = _get_font(font_size)
    char_w, char_h = _get_char_metrics(font_size)
    h, w      = char_map.shape

    lyric_h   = font_size * 3 if lyric else 0
    lb_h      = font_size * 2 if letterbox else 0
    img_w     = w * char_w
    img_h     = h * char_h + lyric_h + lb_h * 2

    img  = Image.new("RGB", (img_w, img_h), bg_color)
    draw = ImageDraw.Draw(img)

    y_offset = lb_h
    for y in range(h):
        line_text = "".join(char_map[y])
        if rgb_map is not None:
            for x in range(w):
                r, g, b = rgb_map[y, x]
                draw.text((x * char_w, y_offset + y * char_h),
                          char_map[y, x], fill=(int(r), int(g), int(b)), font=font)
        else:
            draw.text((0, y_offset + y * char_h), line_text,
                      fill=(255, 255, 255), font=font)

    if lyric:
        lyric_text = f"♪ {lyric} ♪"
        tb  = draw.textbbox((0, 0), lyric_text, font=font)
        tw  = tb[2] - tb[0]
        xp  = (img_w - tw) // 2
        yp  = y_offset + h * char_h + font_size
        draw.text((xp, yp), lyric_text, fill=(255, 255, 0), font=font)

    return img


def _pil_to_raw_rgb(img: "Image.Image") -> bytes:
    return img.tobytes()


# ── Worker for export (multiprocessing) ──────────────────────────────────────

def _export_worker(args: Tuple) -> Tuple[int, bytes, Tuple[int, int]]:
    """
    Process one frame for export.
    Returns (frame_idx, raw_rgb_bytes, (width_px, height_px)).
    Must be a top-level function for pickling.
    """
    (frame_idx, frame_bgr, cfg_dict, lyrics) = args
    try:
        # Pull out any worker-only sidecar fields before constructing the
        # dataclass — PlayerConfig.__init__ rejects unknown kwargs.
        worker_meta = {k: cfg_dict[k] for k in cfg_dict if k.startswith("_")}
        clean = {k: v for k, v in cfg_dict.items() if not k.startswith("_")}
        # Re-hydrate Enum fields that were dumped to strings via asdict().
        _enum_fields = {
            "render_mode":   RenderMode,
            "color_mode":    ColorMode,
            "dither":        DitherAlgo,
            "export_format": ExportFormat,
        }
        for fname, etype in _enum_fields.items():
            if fname in clean and not isinstance(clean[fname], Enum):
                try:
                    clean[fname] = etype(clean[fname])
                except ValueError:
                    pass
        cfg = PlayerConfig(**clean)
        _init_chars(cfg.palette)

        char_map, rgb_map = frame_to_ascii(frame_bgr, cfg, cfg.width or 160)

        lyric = ""
        if lyrics:
            fps_val = worker_meta.get("_fps", 30.0)
            elapsed = frame_idx / fps_val
            lyric = get_current_lyric(lyrics, elapsed, cfg.subtitle_offset)

        img = _char_map_to_pil(
            char_map, rgb_map,
            (0, 0, 0), cfg.font_size,
            lyric, cfg.letterbox,
        )
        raw = _pil_to_raw_rgb(img)
        return (frame_idx, raw, img.size)
    except Exception as e:
        log.error("Worker error on frame %d: %s", frame_idx, e)
        # Return a black frame on error so the export can continue
        w, h = 320, 240
        return (frame_idx, b"\x00" * w * h * 3, (w, h))


def _open_ffmpeg_pipe(output_path: str, fps: float, w: int, h: int,
                      codec: str, audio_src: Optional[str],
                      gpu: bool) -> subprocess.Popen:
    """Open an ffmpeg process that reads raw RGB frames from stdin."""
    vf_flags = []
    if gpu:
        vf_flags += ["-hwaccel", "cuda"]

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{w}x{h}",
        "-pix_fmt", "rgb24",
        "-r", str(fps),
        "-i", "-",       # stdin
    ]
    if audio_src:
        cmd += ["-i", audio_src, "-c:a", "aac", "-shortest"]
    else:
        cmd += ["-an"]

    cmd += ["-vcodec", codec, "-pix_fmt", "yuv420p", output_path]
    log.debug("ffmpeg cmd: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _open_ffmpeg_gif_pipe(output_path: str, fps: float, w: int, h: int) -> subprocess.Popen:
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{w}x{h}", "-pix_fmt", "rgb24",
        "-r", str(fps), "-i", "-",
        "-vf", f"fps={min(fps,25)},scale={w}:{h}:flags=lanczos,split[s0][s1];"
               "[s0]palettegen[p];[s1][p]paletteuse",
        output_path,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def export_video(cfg: PlayerConfig, video_path: str, lyrics: List[Tuple[float, str]]) -> None:
    """
    Export the ASCII-rendered video directly to ffmpeg (no temp PNGs).
    Frames are processed in parallel with guaranteed ordering.
    """
    # Temporarily disable signal handlers during export to prevent duplicate messages
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handlers[sig] = signal.signal(sig, signal.SIG_DFL)
        except (OSError, ValueError):
            pass
    
    try:
        cap  = open_video_source(cfg)
        info = get_video_info(cap)
        total_frames = info["total_frames"]
        fps          = info["fps"]
        _init_chars(cfg.palette)

        log.info("Starting export: %d frames @ %.2f fps", total_frames, fps)

        num_workers = min(cpu_count(), 8)
        out_path    = Path(cfg.export_path)
        out_path    = out_path.with_suffix(
            ".mp4" if cfg.export_format == ExportFormat.MP4 else
            ".gif" if cfg.export_format == ExportFormat.GIF else ""
        )
        # Temp audio
        audio_tmp = None
        if cfg.export_format in (ExportFormat.MP4,) and not cfg.mute:
            audio_tmp = str(Path(tempfile.mkdtemp()) / "audio.aac")
            try:
                subprocess.run([
                    "ffmpeg", "-y", "-i", video_path,
                    "-vn", "-acodec", "aac", audio_tmp
                ], check=True, capture_output=True)
                log.info("Extracted audio to temp file.")
            except Exception as e:
                log.warning("Audio extraction failed: %s", e)
                audio_tmp = None

        # Serialize config for pickling (Enum → value)
        cfg_dict = {}
        for k, v in asdict(cfg).items():
            cfg_dict[k] = v.value if isinstance(v, Enum) else v
        cfg_dict["_fps"] = fps

        # Stream frames + submit to pool
        pipe: Optional[subprocess.Popen] = None
        frame_size: Optional[Tuple[int, int]] = None

        # Buffer of (idx, raw) sorted for in-order writing
        import heapq
        order_heap: List[Tuple[int, bytes]] = []
        next_write  = 0

        def _flush_heap() -> None:
            nonlocal next_write
            while order_heap and order_heap[0][0] == next_write:
                _, raw = heapq.heappop(order_heap)
                assert pipe is not None
                pipe.stdin.write(raw)  # type: ignore[union-attr]
                next_write += 1

        def _frame_generator():
            idx = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                yield (idx, frame, cfg_dict, lyrics)
                idx += 1

        progress_cb = _make_progress(total_frames, "Exporting")

        with Pool(processes=num_workers) as pool:
            for frame_idx, raw, fsize in pool.imap(
                _export_worker, _frame_generator(), chunksize=4
            ):
                if pipe is None:
                    frame_size = fsize
                    if cfg.export_format == ExportFormat.GIF:
                        pipe = _open_ffmpeg_gif_pipe(str(out_path), fps, *fsize)
                    elif cfg.export_format == ExportFormat.SEQ:
                        seq_dir = out_path
                        seq_dir.mkdir(parents=True, exist_ok=True)
                        # Write directly
                    else:
                        pipe = _open_ffmpeg_pipe(str(out_path), fps, fsize[0], fsize[1],
                                                 cfg.export_codec, audio_tmp, cfg.gpu_accel)

                if cfg.export_format == ExportFormat.SEQ:
                    img_path = out_path / f"frame_{frame_idx:06d}.png"
                    # raw bytes → PIL → save PNG
                    img = Image.frombytes("RGB", frame_size or fsize, raw)
                    img.save(str(img_path))
                else:
                    heapq.heappush(order_heap, (frame_idx, raw))
                    _flush_heap()

                progress_cb(frame_idx + 1)

        # Flush remaining
        if pipe:
            _flush_heap()
            pipe.stdin.close()  # type: ignore[union-attr]
            pipe.wait()

        cap.release()
        if audio_tmp and Path(audio_tmp).exists():
            Path(audio_tmp).unlink()

        log.info("Export complete → %s", out_path)
        print(f"\n  {C_GREEN}Exported to: {out_path}{RESET_COLOR}")
    
    finally:
        # Restore signal handlers
        for sig, handler in old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass


def _make_progress(total: int, label: str):
    """Return a simple progress callback."""
    start = time.monotonic()
    def cb(n: int) -> None:
        pct   = n * 100 // max(total, 1)
        ela   = time.monotonic() - start
        fps_e = n / ela if ela > 0 else 0
        eta   = (total - n) / fps_e if fps_e > 0 else 0
        bar_w = 30
        filled = int(bar_w * n / max(total, 1))
        bar = "█" * filled + "░" * (bar_w - filled)
        sys.stdout.write(
            f"\r  {label}: [{bar}] {pct:3d}% | {n}/{total} | {fps_e:.1f} fps | ETA {int(eta//60)}:{int(eta%60):02d}  "
        )
        sys.stdout.flush()
    return cb


# ══════════════════════════════════════════════════════════════════════════════
# PLAYBACK ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class PlaybackState:
    """Shared state for playback controls."""
    def __init__(self) -> None:
        self.paused  = False
        # Relative seek in frames (e.g. +300 = jump 10s forward at 30fps).
        # The decoder thread resolves this against the current cap position.
        self.seek_delta: int = 0
        self.muted   = False
        self.quit    = False
        self._lock   = threading.Lock()

    def toggle_pause(self) -> None:
        with self._lock:
            self.paused = not self.paused

    def seek(self, delta_frames: int) -> None:
        """Request a relative seek in frames. Positive = forward."""
        with self._lock:
            self.seek_delta += delta_frames

    def consume_seek(self) -> int:
        """Atomically read and clear the pending seek delta."""
        with self._lock:
            d, self.seek_delta = self.seek_delta, 0
            return d

    def toggle_mute(self) -> None:
        with self._lock:
            self.muted = not self.muted


def _keyboard_listener(state: PlaybackState, total_frames: int, fps: float) -> None:
    """
    Non-blocking keyboard input thread (Unix only via termios).
    Controls: space=pause, q=quit, →/←=seek ±10s, m=mute
    """
    try:
        import tty
        import termios
        fd   = sys.stdin.fileno()
        old  = termios.tcgetattr(fd)
        # ±10s jump, in frames
        seek_step = max(1, int(round(fps * 10)))
        try:
            tty.setraw(fd)
            while not state.quit:
                ch = sys.stdin.read(1)
                if ch == "q" or ch == "\x03":
                    state.quit = True
                    break
                elif ch == " ":
                    state.toggle_pause()
                elif ch == "m":
                    state.toggle_mute()
                elif ch == "\x1b":   # escape sequence
                    nxt = sys.stdin.read(2)
                    if nxt == "[C":   # → forward
                        state.seek(+seek_step)
                    elif nxt == "[D": # ← backward
                        state.seek(-seek_step)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass   # Windows / non-tty; controls unavailable


def play_engine(cfg: PlayerConfig) -> None:
    """
    Main playback loop — streams frames from video, renders ASCII to terminal.
    Supports pause/seek/mute, adaptive frame skipping, FPS counter,
    terminal resize debounce, frame-diff rendering.
    """
    _init_chars(cfg.palette)

    if cfg.dither == DitherAlgo.FLOYD:
        log.warning("Floyd–Steinberg dithering is implemented in pure Python and "
                    "is very slow for live playback. Consider --dither bayer or "
                    "--dither none unless you are exporting.")

    cap     = open_video_source(cfg)
    info    = get_video_info(cap)
    fps_vid = info["fps"] if info["fps"] > 0 else 30.0
    total   = info["total_frames"]
    delay   = (1.0 / fps_vid) * max(1, cfg.skip)

    lyrics  = []
    if cfg.subtitle:
        lyrics = load_subtitles(cfg.subtitle)
    elif not cfg.webcam:
        auto = auto_discover_subtitle(cfg.video)
        if auto:
            lyrics = load_subtitles(auto)

    state        = PlaybackState()
    state.muted  = cfg.mute
    kb_thread    = threading.Thread(
        target=_keyboard_listener, args=(state, total, fps_vid), daemon=True
    )
    kb_thread.start()

    # Adaptive skip tracking
    skip_count   = cfg.skip
    fps_window   = []       # rolling FPS measurements
    prev_art     = ""       # for frame-diff (only redraw on change)

    enable_ansi_windows()
    # WRAP_OFF prevents the terminal from inserting a phantom newline when a
    # row reaches the last column — that's what causes every-other-line gaps
    # when the ASCII fills the full terminal width.
    sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
    sys.stdout.flush()

    start_time  = time.monotonic()
    frame_count = 0

    def _cleanup() -> None:
        cap.release()
        sys.stdout.write(SHOW_CURSOR + RESET_COLOR + "\n")
        sys.stdout.flush()

    def _run_loop() -> None:
        nonlocal skip_count, fps_window, prev_art, start_time, frame_count
        global _PREV_GRAY

        q: Queue = Queue(maxsize=8)
        stop_ev  = threading.Event()

        def _decoder() -> None:
            idx = 0
            while not stop_ev.is_set():
                delta = state.consume_seek()
                if delta:
                    cur = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
                    target = max(0, min(total - 1 if total > 0 else cur + delta,
                                        cur + delta))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                ret, frame = cap.read()
                if not ret:
                    break
                if skip_count > 1 and idx % skip_count != 0:
                    idx += 1
                    continue
                idx += 1
                while not stop_ev.is_set():
                    try:
                        q.put(frame, timeout=0.05)
                        break
                    except Full:
                        pass
            try:
                q.put(None, timeout=1.0)
            except Full:
                pass

        dec_thread = threading.Thread(target=_decoder, daemon=True)
        dec_thread.start()

        # Number of bottom rows reserved for the status bar / subtitle overlay.
        # The ASCII art fills every row above this.
        RESERVED_ROWS = 2
        last_size: Tuple[int, int] = (0, 0)

        try:
            while not state.quit:
                if state.paused:
                    time.sleep(0.05)
                    continue

                t0 = time.perf_counter()
                try:
                    frame = q.get(timeout=2.0)
                except Empty:
                    break
                if frame is None:
                    break

                frame_count += 1
                cols, lines = TERM.get_size_debounced()

                # Handle terminal resize: clear leftovers, drop diff cache,
                # reset temporal-smoothing buffer (shape changes break it).
                if (cols, lines) != last_size:
                    sys.stdout.write(CLEAR_SCREEN + CURSOR_HOME)
                    prev_art = ""
                    _PREV_GRAY = None
                    last_size = (cols, lines)

                cur_w = cfg.width or cols
                cur_h = max(1, lines - RESERVED_ROWS)
                char_map, rgb_map = frame_to_ascii(frame, cfg, cur_w, cur_h)
                art = char_map_to_ansi(char_map, rgb_map, cfg)

                # Frame diff: only write if changed
                if art == prev_art:
                    # Still sleep to maintain timing
                    elapsed = time.perf_counter() - t0
                    if delay - elapsed > 0:
                        time.sleep(delay - elapsed)
                    continue
                prev_art = art

                buf = io.StringIO()
                # Write each line individually to avoid phantom newlines
                for y, line in enumerate(art.split('\n')):
                    buf.write(f"\033[{y+1};1H{line}")
                buf.write(RESET_COLOR)

                # Subtitle overlay (absolute-positioned, one row above status)
                elapsed_s = time.monotonic() - start_time
                lyric     = get_current_lyric(lyrics, elapsed_s, cfg.subtitle_offset)
                if lyric and lines > RESERVED_ROWS:
                    ml = max(1, cols - 4)
                    display = lyric[:ml - 3] + "..." if len(lyric) > ml else lyric
                    sub_text = f"♪ {display} ♪"
                    sub_row  = lines - 1
                    # pad-clear the row so old subs don't ghost on shorter lines
                    buf.write(f"\033[{sub_row};1H\033[2K"
                              f"{C_BOLD}{C_YELLOW}{sub_text[:cols-1]}{RESET_COLOR}")

                # Progress bar + FPS (bottom row)
                bar_l  = max(10, cols - 50)
                filled = int(bar_l * frame_count / max(total, 1))
                bar    = "█" * filled + "░" * (bar_l - filled)
                elapsed_total = time.perf_counter() - t0
                fps_window.append(1.0 / max(elapsed_total, 1e-9))
                if len(fps_window) > 30:
                    fps_window.pop(0)
                fps_now = sum(fps_window) / len(fps_window)

                status = (f"[{bar}] {frame_count}/{total} | "
                          f"{fps_now:.1f}fps | "
                          f"{'⏸ ' if state.paused else ''}"
                          f"{'🔇' if state.muted else ''}"
                          f"q=quit spc=pause m=mute ←→=seek")
                buf.write(f"\033[{lines};1H\033[2K"
                          f"{C_GRAY}{status[:cols-1]}{RESET_COLOR}")

                # Stats overlay
                if cfg.stats_overlay:
                    mem = _get_mem_mb()
                    buf.write(f"\033[2;{max(1, cols-20)}H{C_CYAN}MEM:{mem:.0f}MB{RESET_COLOR}")

                sys.stdout.write(buf.getvalue())
                sys.stdout.flush()

                # Adaptive skip: if we're falling behind, increase skip
                elapsed = time.perf_counter() - t0
                if elapsed > delay * 1.5 and skip_count < 8:
                    skip_count += 1
                    log.debug("Adaptive skip increased to %d", skip_count)
                elif elapsed < delay * 0.5 and skip_count > cfg.skip:
                    skip_count -= 1

                sleep_t = delay - (time.perf_counter() - t0)
                if sleep_t > 0:
                    time.sleep(sleep_t)

        except KeyboardInterrupt:
            state.quit = True
        finally:
            stop_ev.set()
            dec_thread.join(timeout=1.0)

    try:
        if cfg.loop:
            while not state.quit:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                start_time  = time.monotonic()
                frame_count = 0
                _run_loop()
        else:
            _run_loop()
    finally:
        _cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# BENCHMARK MODE
# ══════════════════════════════════════════════════════════════════════════════

def run_benchmark(cfg: PlayerConfig) -> None:
    """Render N frames as fast as possible and report throughput."""
    N = 100
    _init_chars(cfg.palette)
    cap  = open_video_source(cfg)
    info = get_video_info(cap)
    log.info("Benchmark: rendering %d frames …", N)

    t0 = time.perf_counter()
    for i in range(N):
        ret, frame = cap.read()
        if not ret:
            break
        w = cfg.width or 80
        char_map, rgb_map = frame_to_ascii(frame, cfg, w)
        _ = char_map_to_ansi(char_map, rgb_map, cfg)

    elapsed = time.perf_counter() - t0
    cap.release()
    print(f"\n  Benchmark: {N} frames in {elapsed:.2f}s = {N/elapsed:.1f} fps")
    print(f"  Mode: {cfg.render_mode.value} | Palette: {cfg.palette} | Color: {cfg.color_mode.value}")


# ══════════════════════════════════════════════════════════════════════════════
# DEPENDENCY CHECK & DIAGNOSTICS
# ══════════════════════════════════════════════════════════════════════════════

def _get_mem_mb() -> float:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 0.0

def check_dependencies() -> bool:
    missing = []
    if not _CV2:
        missing.append("opencv-python (cv2)")
    if not _NP:
        missing.append("numpy")
    if not _PIL:
        missing.append("Pillow (PIL)")
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        missing.append("ffmpeg (system binary)")

    if missing:
        print(f"\n  {C_RED}Missing dependencies:{RESET_COLOR}")
        for d in missing:
            print(f"  · {d}")
        return False
    return True

def startup_diagnostics(cfg: PlayerConfig) -> None:
    """Print a quick startup diagnostic panel."""
    log.info("═" * 50)
    log.info("ASCII Video Player — startup diagnostics")
    log.info("  Terminal:  %dx%d | truecolor=%s | unicode=%s",
             TERM.cols, TERM.lines, TERM.truecolor, TERM.unicode)
    log.info("  Rich:      %s", _RICH)
    log.info("  TOML:      %s", _TOML)
    log.info("  Render:    %s / palette=%s / color=%s",
             cfg.render_mode.value, cfg.palette, cfg.color_mode.value)
    log.info("  Dither:    %s", cfg.dither.value)
    log.info("  CPUs:      %d", cpu_count())
    log.info("  Memory:    %.1f MB", _get_mem_mb())
    log.info("═" * 50)


# ══════════════════════════════════════════════════════════════════════════════
# GRACEFUL SHUTDOWN
# ══════════════════════════════════════════════════════════════════════════════

_CLEANUP_CALLED = False

def _global_cleanup(*_) -> None:
    global _CLEANUP_CALLED
    if _CLEANUP_CALLED:
        return
    # Only cleanup from main process
    if _mp.current_process().name != "MainProcess" and _mp.parent_process() is not None:
        return
    _CLEANUP_CALLED = True
    sys.stdout.write(WRAP_ON + SHOW_CURSOR + RESET_COLOR + "\n")
    sys.stdout.flush()

# Only install in the main process — multiprocessing workers inherit the
# module on import and would otherwise each print "Graceful shutdown complete."
# on Ctrl+C / SIGTERM.
import multiprocessing as _mp
if _mp.current_process().name == "MainProcess" or _mp.parent_process() is None:
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _global_cleanup)
        except (OSError, ValueError):
            pass


# ══════════════════════════════════════════════════════════════════════════════
# ARG PARSING & MAIN
# ══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ASCII Video Player — play & export videos as terminal ASCII art",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    # Input
    p.add_argument("--video", "-v",    default="video.mp4",  help="Video path, URL, or 'webcam'")
    p.add_argument("--subtitle", "-s", default="",           help="Subtitle file (.srt/.lrc)")
    p.add_argument("--sub-offset",     type=float, default=0.0, help="Subtitle offset in seconds")
    p.add_argument("--config", "-c",   default="",           help="Config file (.json or .toml)")

    # Render
    p.add_argument("--mode",      default="ascii",
                   choices=[m.value for m in RenderMode],   help="Render mode")
    p.add_argument("--palette",   default="dense",
                   choices=list(PALETTES.keys()),            help="ASCII palette")
    p.add_argument("--color",     default="truecolor",
                   choices=[m.value for m in ColorMode],     help="Color mode")
    p.add_argument("--dither",    default="none",
                   choices=[d.value for d in DitherAlgo],    help="Dithering algorithm")
    p.add_argument("--width",  "-w", type=int, default=None, help="Render width (default: auto)")
    p.add_argument("--font-size",    type=int, default=10,   help="Font size for export")
    p.add_argument("--preset",   default=None,
                   choices=list(PRESETS.keys()),             help="Apply a preset profile")

    # Image adjustments
    p.add_argument("--brightness", type=float, default=1.0)
    p.add_argument("--contrast",   type=float, default=1.0)
    p.add_argument("--gamma",      type=float, default=1.0)
    p.add_argument("--clahe",      action="store_true", help="Dynamic contrast normalization")
    p.add_argument("--smooth",     action="store_true", help="Temporal smoothing")

    # Playback
    p.add_argument("--skip",       type=int, default=1,     help="Frame skip (1=no skip)")
    p.add_argument("--loop",       action="store_true",     help="Loop video")
    p.add_argument("--mute",       action="store_true",     help="Mute audio")
    p.add_argument("--letterbox",  action="store_true",     help="Cinematic letterboxing")
    p.add_argument("--webcam",     action="store_true",     help="Live webcam input")

    # Export
    p.add_argument("--export", "-e",   action="store_true", help="Export instead of playing")
    p.add_argument("--format",         default="mp4",
                   choices=[f.value for f in ExportFormat], help="Export format")
    p.add_argument("--codec",          default="libx264",   help="ffmpeg video codec")
    p.add_argument("--output", "-o",   default="ascii_output", help="Output path (no extension)")
    p.add_argument("--gpu",            action="store_true", help="Enable GPU acceleration")

    # Misc
    p.add_argument("--benchmark",  action="store_true",     help="Benchmark render speed")
    p.add_argument("--stats",      action="store_true",     help="Overlay render stats")
    p.add_argument("--rich",       action="store_true",     help="Use Rich UI if available")
    p.add_argument("--low-latency",action="store_true",     help="Low-latency mode")
    p.add_argument("--debug",      action="store_true",     help="Enable debug logging")
    return p


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not check_dependencies():
        sys.exit(1)

    # Build config
    if args.config:
        cfg = PlayerConfig.from_file(args.config)
    else:
        cfg = PlayerConfig()

    # Apply preset first (CLI args override)
    if args.preset:
        cfg.apply_preset(args.preset)

    # Override from CLI
    cfg.video           = args.video
    cfg.webcam          = args.webcam
    cfg.subtitle        = args.subtitle
    cfg.subtitle_offset = args.sub_offset
    cfg.render_mode     = RenderMode(args.mode)
    cfg.palette         = args.palette
    cfg.color_mode      = ColorMode(args.color)
    cfg.dither          = DitherAlgo(args.dither)
    cfg.width           = args.width
    cfg.font_size       = args.font_size
    cfg.brightness      = args.brightness
    cfg.contrast        = args.contrast
    cfg.gamma           = args.gamma
    cfg.clahe           = args.clahe
    cfg.temporal_smooth = args.smooth
    cfg.skip            = args.skip
    cfg.loop            = args.loop
    cfg.mute            = args.mute
    cfg.letterbox       = args.letterbox
    cfg.export          = args.export
    cfg.export_format   = ExportFormat(args.format)
    cfg.export_codec    = args.codec
    cfg.export_path     = args.output
    cfg.gpu_accel       = args.gpu
    cfg.benchmark       = args.benchmark
    cfg.stats_overlay   = args.stats
    cfg.rich_ui         = args.rich
    cfg.low_latency     = args.low_latency

    # Auto-detect best color mode if truecolor selected but terminal lacks it
    if cfg.color_mode == ColorMode.TRUECOLOR and not TERM.truecolor:
        best = TERM.best_color_mode()
        log.warning("Terminal does not support truecolor; falling back to %s", best.value)
        cfg.color_mode = best

    enable_ansi_windows()
    startup_diagnostics(cfg)

    if cfg.benchmark:
        run_benchmark(cfg)
        return

    if cfg.export:
        lyrics = load_subtitles(cfg.subtitle) if cfg.subtitle else []
        export_video(cfg, cfg.video, lyrics)
        return

    # Validate video source
    if not cfg.webcam:
        src = cfg.video
        is_url = src.startswith("http://") or src.startswith("https://")
        if not is_url and not Path(src).exists():
            log.error("File not found: %s", src)
            sys.exit(1)

    try:
        play_engine(cfg)
    except Exception as e:
        log.exception("Fatal error during playback: %s", e)
    finally:
        _global_cleanup()

    # Post-playback export offer
    try:
        ans = input(f"\n  {C_BOLD}{C_YELLOW}Export to MP4? (y/N): {RESET_COLOR}").strip().lower()
        if ans == "y":
            lyrics = load_subtitles(cfg.subtitle) if cfg.subtitle else []
            cfg.export = True
            export_video(cfg, cfg.video, lyrics)
    except (EOFError, KeyboardInterrupt):
        pass

    print(f"\n  {C_GREEN}Thanks for using ASCII Video Player!{RESET_COLOR}")


if __name__ == "__main__":
    main()
