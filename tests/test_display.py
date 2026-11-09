import struct

from figpi.display import (
    ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID, ABS_X, ABS_Y, BTN_TOUCH, EV_ABS, EV_KEY, EV_SYN,
    XRGB8888, PixelFormat, Show, Sprite, TouchDecoder, TouchState, pick_scale, render_frames,
)


def ev(etype, code, value):
    return struct.pack("qqHHi", 0, 0, etype, code, value)


SYN = ev(EV_SYN, 0, 0)


def test_builtin_sprite_loads():
    s = Sprite.load()
    assert (s.width, s.height, len(s.frames)) == (64, 64, 8) and s.frame_ms == 125
    assert all(len(f) == 64 * 64 * 4 for f in s.frames)
    # premultiplied over black: fully transparent pixels are pure black
    assert s.frames[0][:4] == b"\0\0\0\0"


def test_pixel_packing():
    assert XRGB8888.pack(0x12, 0x34, 0x56) == bytes([0x56, 0x34, 0x12, 0x00])
    rgb565 = PixelFormat(2, (11, 5), (5, 6), (0, 5))
    assert rgb565.pack(255, 0, 0) == (0xF800).to_bytes(2, "little")
    assert rgb565.pack(0, 255, 0) == (0x07E0).to_bytes(2, "little")
    assert rgb565.pack(0, 0, 255) == (0x001F).to_bytes(2, "little")


def test_render_frames_scales_nearest_neighbour():
    red, blue = b"\xff\0\0\xff", b"\0\0\xff\xff"
    sprite = Sprite(2, 1, 100, [red + blue])
    rows = render_frames(sprite, 3, XRGB8888)[0]
    assert len(rows) == 3 and all(len(r) == 2 * 3 * 4 for r in rows)
    assert rows[0] == XRGB8888.pack(255, 0, 0) * 3 + XRGB8888.pack(0, 0, 255) * 3
    assert rows[0] == rows[2]


def test_touch_decoder_single_touch_and_rotation():
    d = TouchDecoder((0, 4095), (0, 4095), (1920, 1080))
    reports = d.feed(ev(EV_KEY, BTN_TOUCH, 1) + ev(EV_ABS, ABS_X, 2048) + ev(EV_ABS, ABS_Y, 0) + SYN)
    assert reports == [TouchState(True, 959, 0)]
    # partial writes are buffered until the report completes
    assert d.feed(ev(EV_KEY, BTN_TOUCH, 0)[:10]) == []
    assert d.feed(ev(EV_KEY, BTN_TOUCH, 0)[10:] + SYN) == [TouchState(False, 959, 0)]
    r = TouchDecoder((0, 100), (0, 100), (1920, 1080), rotate=90)
    (t,) = r.feed(ev(EV_KEY, BTN_TOUCH, 1) + ev(EV_ABS, ABS_X, 0) + ev(EV_ABS, ABS_Y, 0) + SYN)
    assert (t.x, t.y) == (1919, 0)  # raw top-left lands top-right after 90 degrees


def test_touch_decoder_multitouch_protocol():
    d = TouchDecoder((0, 1000), (0, 1000), (1000, 1000))
    down = ev(EV_ABS, ABS_MT_TRACKING_ID, 7) + ev(EV_ABS, ABS_MT_POSITION_X, 500) + ev(EV_ABS, ABS_MT_POSITION_Y, 250) + SYN
    up = ev(EV_ABS, ABS_MT_TRACKING_ID, -1) + SYN
    assert d.feed(down) == [TouchState(True, 499, 249)]
    assert d.feed(up)[0].down is False


def test_show_plays_only_while_held_on_the_bird():
    show = Show(n_frames=4, frame_ms=100, rect=(100, 100, 200, 200), margin=0.25, release="freeze")
    assert show.hit == (50, 50, 350, 350)
    show.touch(TouchState(True, 10, 10), now=0.0)  # outside: nothing
    assert not show.playing and show.tick(5.0) is False
    show.touch(TouchState(False, 10, 10), now=0.1)
    show.touch(TouchState(True, 60, 340), now=1.0)  # inside the margin
    assert show.playing
    assert show.tick(1.05) is False and show.frame == 0
    assert show.tick(1.1) is True and show.frame == 1
    assert show.tick(1.2) is True and show.frame == 2
    show.touch(TouchState(True, 900, 900), now=1.25)  # sliding off while held keeps playing
    assert show.playing
    show.touch(TouchState(False, 900, 900), now=1.3)
    assert not show.playing and show.frame == 2  # freeze
    assert show.tick(9.0) is False and show.timeout(9.0) == 1.0


def test_show_rest_on_release_and_wraparound():
    show = Show(2, 100, (0, 0, 10, 10), 0, "rest")
    show.touch(TouchState(True, 5, 5), 0.0)
    assert show.tick(0.1) and show.frame == 1
    assert show.tick(0.2) and show.frame == 0
    assert show.tick(0.3) and show.frame == 1
    show.touch(TouchState(False, 5, 5), 0.35)
    assert show.frame == 0


def test_pick_scale():
    assert pick_scale(4, 1080, 64) == 4
    assert pick_scale(0, 1080, 64) == 5
    assert pick_scale(0, 100, 64) == 1
