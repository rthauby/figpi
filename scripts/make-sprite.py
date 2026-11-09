#!/usr/bin/env python3
"""Convert an animated image (WebP/GIF/APNG) into figpi's tiny sprite format.

Runs on your computer, not on the Pi; needs Pillow (`pip install pillow`).

    scripts/make-sprite.py assets/bird.webp figpi/assets/bird.sprite [--fps 10]

Format (little-endian): magic b"FPS1", u16 width, u16 height, u16 frames, u16 ms-per-frame,
then zlib-compressed RGBA bytes for all frames, premultiplied over black (the screen background).
The Pi decodes it with `struct` and `zlib` only.
"""
import argparse
import struct
import sys
import zlib

from PIL import Image

MAGIC = b"FPS1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("output")
    ap.add_argument("--fps", type=float, default=0, help="override frame rate (default: from the file, else 10)")
    args = ap.parse_args()

    im = Image.open(args.source)
    frames, durations = [], []
    try:
        while True:
            frames.append(im.convert("RGBA").tobytes())
            durations.append(im.info.get("duration") or 0)
            im.seek(im.tell() + 1)
    except EOFError:
        pass
    w, h = im.size
    if args.fps:
        ms = round(1000 / args.fps)
    else:
        known = [d for d in durations if d]
        ms = round(sum(known) / len(known)) if known else 100
    # premultiply over black so the Pi can blit without blending
    out = bytearray()
    for raw in frames:
        px = bytearray(raw)
        for i in range(0, len(px), 4):
            a = px[i + 3]
            if a != 255:
                px[i] = px[i] * a // 255
                px[i + 1] = px[i + 1] * a // 255
                px[i + 2] = px[i + 2] * a // 255
        out += px
    blob = MAGIC + struct.pack("<HHHH", w, h, len(frames), ms) + zlib.compress(bytes(out), 9)
    with open(args.output, "wb") as fh:
        fh.write(blob)
    print(f"{args.output}: {w}x{h}, {len(frames)} frames @ {ms} ms, {len(blob)} bytes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
