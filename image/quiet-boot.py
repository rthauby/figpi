#!/usr/bin/env python3
"""Idempotently add the kernel/firmware options that keep the attached screen black during boot.

    quiet-boot.py cmdline /path/to/cmdline.txt
    quiet-boot.py config  /path/to/config.txt

Used by image/build.sh (on the extracted boot files) and scripts/install.sh (in place on a running Pi).
"""
import sys

CMDLINE_OPTS = ["quiet", "loglevel=3", "consoleblank=0", "vt.global_cursor_default=0", "logo.nologo"]
CONFIG_OPTS = ["disable_splash=1"]


def cmdline(path: str) -> None:
    with open(path) as fh:
        text = fh.read()
    line = text.strip().split("\n")[0] if text.strip() else ""
    words = line.split()
    keys = {w.split("=")[0] for w in words}
    for opt in CMDLINE_OPTS:
        if opt.split("=")[0] not in keys:
            words.append(opt)
    with open(path, "w") as fh:
        fh.write(" ".join(words) + "\n")


def config(path: str) -> None:
    with open(path) as fh:
        text = fh.read()
    # options before the first [section] apply to every board; the file starts with [all]-level settings
    head, sep, tail = text.partition("\n[")
    for opt in CONFIG_OPTS:
        key = opt.split("=")[0]
        if not any(l.strip().startswith(key + "=") for l in text.splitlines()):
            head = head.rstrip("\n") + "\n" + opt + "\n"
    with open(path, "w") as fh:
        fh.write(head + sep + tail)


if __name__ == "__main__":
    {"cmdline": cmdline, "config": config}[sys.argv[1]](sys.argv[2])
