"""Touchscreen face: a black screen with a pixel-art sprite that animates while you press and hold it.

Draws straight into /dev/fb0 and reads the touchscreen from /dev/input, with the standard library only.
No X, no Wayland, no pygame: the Pi runs nothing but this loop.
"""

from __future__ import annotations

import array
import fcntl
import glob
import logging
import mmap
import os
import select
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger("figpi.display")

SPRITE_MAGIC = b"FPS1"
BUILTIN_SPRITE = Path(__file__).with_name("assets") / "bird.sprite"

# ioctls (generic Linux _IOC layout, same on arm64 and x86_64)
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FBIOBLANK = 0x4611
KDSETMODE = 0x4B3A
KD_TEXT, KD_GRAPHICS = 0, 1


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39
EVENT_FORMAT = "qqHHi"  # struct timeval (2 x 64-bit) + type + code + value
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)


def EVIOCGBIT(ev: int, length: int) -> int:
    return _ioc(2, "E", 0x20 + ev, length)


def EVIOCGABS(axis: int) -> int:
    return _ioc(2, "E", 0x40 + axis, 24)


EVIOCGNAME = _ioc(2, "E", 0x06, 256)


# --- sprite ------------------------------------------------------------------------


@dataclass
class Sprite:
    width: int
    height: int
    frame_ms: int
    frames: list[bytes]  # RGBA, premultiplied over black

    @classmethod
    def load(cls, path: Path | str = BUILTIN_SPRITE) -> "Sprite":
        data = Path(path).read_bytes()
        if data[:4] != SPRITE_MAGIC:
            raise ValueError(f"{path}: not a figpi sprite (see scripts/make-sprite.py)")
        w, h, n, ms = struct.unpack_from("<HHHH", data, 4)
        raw = zlib.decompress(data[12:])
        size = w * h * 4
        if len(raw) != size * n:
            raise ValueError(f"{path}: corrupt sprite data")
        return cls(w, h, ms, [raw[i * size : (i + 1) * size] for i in range(n)])


@dataclass
class PixelFormat:
    """How the framebuffer wants its pixels."""

    bytes_per_pixel: int
    red: tuple[int, int]  # (offset, length)
    green: tuple[int, int]
    blue: tuple[int, int]

    def pack(self, r: int, g: int, b: int) -> bytes:
        value = 0
        for (off, length), chan in ((self.red, r), (self.green, g), (self.blue, b)):
            value |= (chan >> (8 - length)) << off
        return value.to_bytes(self.bytes_per_pixel, "little")


XRGB8888 = PixelFormat(4, (16, 8), (8, 8), (0, 8))


def render_frames(sprite: Sprite, scale: int, fmt: PixelFormat) -> list[list[bytes]]:
    """Nearest-neighbour scale every frame and pack it into framebuffer rows (one bytes object per row)."""
    out: list[list[bytes]] = []
    cache: dict[bytes, bytes] = {}
    for frame in sprite.frames:
        rows: list[bytes] = []
        for y in range(sprite.height):
            row = bytearray()
            for x in range(sprite.width):
                px = frame[(y * sprite.width + x) * 4 : (y * sprite.width + x) * 4 + 3]
                packed = cache.get(px)
                if packed is None:
                    packed = cache[px] = fmt.pack(px[0], px[1], px[2]) * scale
                row += packed
            rows.extend([bytes(row)] * scale)
        out.append(rows)
    return out


# --- framebuffer -------------------------------------------------------------------------


class Framebuffer:
    def __init__(self, device: str = "/dev/fb0", tty: str | None = "/dev/tty1"):
        self.fd = os.open(device, os.O_RDWR)
        var = fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, b"\0" * 160)
        fix = fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, b"\0" * 128)
        v = struct.unpack_from("<8I", var)  # xres yres xres_virtual yres_virtual xoffset yoffset bpp grayscale
        self.width, self.height = v[0], v[1]
        self.xoffset, self.yoffset = v[4], v[5]
        bpp = v[6]
        # red/green/blue bitfields follow: (offset, length, msb_right) each
        ro, rl, _, go, gl, _, bo, bl, _ = struct.unpack_from("<9I", var, 32)
        self.fmt = PixelFormat(bpp // 8, (ro, rl), (go, gl), (bo, bl))
        self.line_length = struct.unpack_from("<I", fix, 48)[0]
        self.mem = mmap.mmap(self.fd, self.line_length * (self.yoffset + self.height), mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        self.tty_fd = None
        if tty:
            try:
                self.tty_fd = os.open(tty, os.O_RDWR | os.O_NOCTTY)
                fcntl.ioctl(self.tty_fd, KDSETMODE, KD_GRAPHICS)  # silence the text console on this screen
                os.write(self.tty_fd, b"\033[?25l")
            except OSError as exc:
                log.warning("could not switch %s to graphics mode: %s", tty, exc)
        try:
            fcntl.ioctl(self.fd, FBIOBLANK, 0)
        except OSError:
            pass
        log.info("framebuffer %dx%d, %d bpp", self.width, self.height, bpp)

    def clear(self) -> None:
        self.mem[:] = b"\0" * len(self.mem)

    def blit_rows(self, x: int, y: int, rows: list[bytes]) -> None:
        bpp = self.fmt.bytes_per_pixel
        for i, row in enumerate(rows):
            yy = y + i
            if 0 <= yy < self.height:
                off = (yy + self.yoffset) * self.line_length + (x + self.xoffset) * bpp
                self.mem[off : off + len(row)] = row

    def close(self) -> None:
        try:
            self.clear()
        except (OSError, ValueError):
            pass
        if self.tty_fd is not None:
            try:
                fcntl.ioctl(self.tty_fd, KDSETMODE, KD_TEXT)
                os.write(self.tty_fd, b"\033[?25h")
            except OSError:
                pass
            os.close(self.tty_fd)
        self.mem.close()
        os.close(self.fd)


# --- touch ------------------------------------------------------------------------------------


def _has_bit(bits: bytes, bit: int) -> bool:
    return bit // 8 < len(bits) and bool(bits[bit // 8] & (1 << (bit % 8)))


def find_touchscreen() -> str | None:
    """First /dev/input/event* that reports absolute X/Y and a touch button (i.e. not a mouse)."""
    for dev in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            abs_bits = fcntl.ioctl(fd, EVIOCGBIT(EV_ABS, 8), b"\0" * 8)
            key_bits = fcntl.ioctl(fd, EVIOCGBIT(EV_KEY, 96), b"\0" * 96)
            if (_has_bit(abs_bits, ABS_MT_POSITION_X) or _has_bit(abs_bits, ABS_X)) and _has_bit(key_bits, BTN_TOUCH):
                name = fcntl.ioctl(fd, EVIOCGNAME, b"\0" * 256).split(b"\0", 1)[0].decode(errors="replace")
                log.info("touchscreen: %s (%s)", name, dev)
                return dev
        except OSError:
            pass
        finally:
            os.close(fd)
    return None


@dataclass
class TouchState:
    down: bool = False
    x: int = 0
    y: int = 0


class TouchDecoder:
    """Turns raw evdev bytes into (down, x, y) screen coordinates on every SYN_REPORT."""

    def __init__(self, x_range: tuple[int, int], y_range: tuple[int, int], screen: tuple[int, int], rotate: int = 0):
        self.xmin, self.xmax = x_range
        self.ymin, self.ymax = y_range
        self.screen_w, self.screen_h = screen
        self.rotate = rotate % 360
        self.state = TouchState()
        self._raw_x = self.xmin
        self._raw_y = self.ymin
        self._down = False
        self._buf = b""

    def feed(self, data: bytes) -> list[TouchState]:
        """Feed bytes read from the device; returns one TouchState per completed report."""
        self._buf += data
        reports: list[TouchState] = []
        while len(self._buf) >= EVENT_SIZE:
            _, _, etype, code, value = struct.unpack_from(EVENT_FORMAT, self._buf)
            self._buf = self._buf[EVENT_SIZE:]
            if etype == EV_ABS:
                if code in (ABS_X, ABS_MT_POSITION_X):
                    self._raw_x = value
                elif code in (ABS_Y, ABS_MT_POSITION_Y):
                    self._raw_y = value
                elif code == ABS_MT_TRACKING_ID:
                    self._down = value != -1
            elif etype == EV_KEY and code == BTN_TOUCH:
                self._down = bool(value)
            elif etype == EV_SYN and code == SYN_REPORT:
                self.state = TouchState(self._down, *self._to_screen(self._raw_x, self._raw_y))
                reports.append(self.state)
        return reports

    def _to_screen(self, rx: int, ry: int) -> tuple[int, int]:
        fx = (rx - self.xmin) / max(1, self.xmax - self.xmin)
        fy = (ry - self.ymin) / max(1, self.ymax - self.ymin)
        if self.rotate == 90:
            fx, fy = 1 - fy, fx
        elif self.rotate == 180:
            fx, fy = 1 - fx, 1 - fy
        elif self.rotate == 270:
            fx, fy = fy, 1 - fx
        return int(fx * (self.screen_w - 1)), int(fy * (self.screen_h - 1))


def open_touch(device: str, screen: tuple[int, int], rotate: int) -> tuple[int, TouchDecoder]:
    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    abs_bits = fcntl.ioctl(fd, EVIOCGBIT(EV_ABS, 8), b"\0" * 8)
    ax, ay = (ABS_MT_POSITION_X, ABS_MT_POSITION_Y) if _has_bit(abs_bits, ABS_MT_POSITION_X) else (ABS_X, ABS_Y)
    xi = struct.unpack("<6i", fcntl.ioctl(fd, EVIOCGABS(ax), b"\0" * 24))
    yi = struct.unpack("<6i", fcntl.ioctl(fd, EVIOCGABS(ay), b"\0" * 24))
    log.info("touch axes x=%d..%d y=%d..%d rotate=%d", xi[1], xi[2], yi[1], yi[2], rotate)
    return fd, TouchDecoder((xi[1], xi[2]), (yi[1], yi[2]), screen, rotate)


# --- the show -------------------------------------------------------------------------------------


class Show:
    """Pure state: which frame to display, given touches and the clock."""

    def __init__(self, n_frames: int, frame_ms: int, rect: tuple[int, int, int, int], margin: float, release: str):
        self.n_frames = n_frames
        self.frame_s = frame_ms / 1000
        x, y, w, h = rect
        mx, my = int(w * margin), int(h * margin)
        self.hit = (x - mx, y - my, x + w + mx, y + h + my)
        self.release = release
        self.frame = 0
        self.playing = False
        self._next_at = 0.0

    def touch(self, t: TouchState, now: float) -> None:
        if t.down and not self.playing:
            x0, y0, x1, y1 = self.hit
            if x0 <= t.x < x1 and y0 <= t.y < y1:
                self.playing = True
                self._next_at = now + self.frame_s
        elif not t.down and self.playing:
            self.playing = False
            if self.release == "rest":
                self.frame = 0

    def tick(self, now: float) -> bool:
        """Advance the animation; returns True when the frame changed."""
        if not self.playing or now + 1e-6 < self._next_at:
            return False
        self.frame = (self.frame + 1) % self.n_frames
        self._next_at = max(self._next_at + self.frame_s, now)
        return True

    def timeout(self, now: float) -> float:
        return max(0.0, self._next_at - now) if self.playing else 1.0


def pick_scale(cfg_scale: int, screen_h: int, sprite_h: int) -> int:
    if cfg_scale > 0:
        return cfg_scale
    return max(1, screen_h // (sprite_h * 3))  # about a third of the screen tall


def run(cfg: Config) -> int:
    d = cfg.display
    sprite = Sprite.load(d.sprite or BUILTIN_SPRITE)
    if d.fps > 0:
        sprite.frame_ms = round(1000 / d.fps)
    fb = Framebuffer(d.framebuffer, d.tty or None)
    try:
        scale = pick_scale(d.scale, fb.height, sprite.height)
        frames = render_frames(sprite, scale, fb.fmt)
        w, h = sprite.width * scale, sprite.height * scale
        x = (fb.width - w) // 2 if d.x < 0 else d.x
        y = (fb.height - h) // 2 if d.y < 0 else d.y
        show = Show(len(frames), sprite.frame_ms, (x, y, w, h), d.hit_margin, d.release)
        fb.clear()
        fb.blit_rows(x, y, frames[0])
        log.info("sprite %dx%d at (%d,%d), scale %d, %d frames @ %d ms", w, h, x, y, scale, len(frames), sprite.frame_ms)

        touch_fd, decoder, next_probe = None, None, 0.0
        while True:
            now = time.monotonic()
            if touch_fd is None and now >= next_probe:
                dev = d.touch_device or find_touchscreen()
                if dev:
                    try:
                        touch_fd, decoder = open_touch(dev, (fb.width, fb.height), d.touch_rotate)
                    except OSError as exc:
                        log.warning("cannot open %s: %s", dev, exc)
                next_probe = now + 5.0
            wait = min(show.timeout(now), 5.0)
            readable = select.select([touch_fd] if touch_fd is not None else [], [], [], wait)[0]
            if readable:
                try:
                    data = os.read(touch_fd, EVENT_SIZE * 64)
                except BlockingIOError:
                    data = b""
                except OSError as exc:  # unplugged
                    log.warning("touch device lost: %s", exc)
                    os.close(touch_fd)
                    touch_fd, decoder = None, None
                    data = b""
                for t in decoder.feed(data) if data else []:
                    show.touch(t, time.monotonic())
            if show.tick(time.monotonic()):
                fb.blit_rows(x, y, frames[show.frame])
    except KeyboardInterrupt:
        return 0
    finally:
        fb.close()
