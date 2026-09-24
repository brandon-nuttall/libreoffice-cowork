#!/usr/bin/env python3
"""Inject synthetic mouse-wheel events over a point on an X display.

Wheel verification without human hands: LibreOffice receives these as real
SalWheelMouseEvents (XTEST Button 4/5), so this drives the full production
event path -- the very path a wheel implementation must survive.

    python3 tests/wheel-inject.py [--display :99] <x> <y> <--up|-n|--down|+n>

Examples:
    python3 tests/wheel-inject.py :99 1170 500 -5     # 5 notches up
    python3 tests/wheel-inject.py :99 1170 500 +5     # 5 notches down
"""

import argparse
import sys
import time

try:
    import Xlib
    import Xlib.X as Xlib_X
    from Xlib import display as xdisplay
    from Xlib.ext import xtest
except ImportError:
    print("python-xlib is required: python3 -m pip install xlib")
    sys.exit(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("x", type=int)
    parser.add_argument("--display", default=":99")
    parser.add_argument("y", type=int)
    parser.add_argument("--notches", type=int, default=1,
                        help="positive scrolls down, negative up")
    parser.add_argument("--click", action="store_true",
                        help="single left click at the point instead of wheel")
    args = parser.parse_args()

    disp = xdisplay.Display(args.display)
    root = disp.screen().root

    # Park the pointer on the target so the event lands on the intended window.
    xtest.fake_input(disp, Xlib_X.MotionNotify, x=int(args.x), y=int(args.y))
    disp.sync()
    time.sleep(0.1)

    if args.click:
        xtest.fake_input(disp, Xlib_X.ButtonPress, 1)
        disp.sync(); time.sleep(0.03)
        xtest.fake_input(disp, Xlib_X.ButtonRelease, 1)
        disp.sync()
        print("clicked at (%d, %d) on %s" % (args.x, args.y, args.display))
        return
    for _ in range(abs(args.notches)):
        xtest.fake_input(disp, Xlib_X.ButtonPress, button)
        disp.sync()
        time.sleep(0.02)
        xtest.fake_input(disp, Xlib_X.ButtonRelease, button)
        disp.sync()
        time.sleep(0.08)

    print("injected %d notches %s at (%d, %d) on %s"
          % (abs(args.notches), "down" if args.notches > 0 else "up",
             args.x, args.y, args.display))


if __name__ == "__main__":
    main()
