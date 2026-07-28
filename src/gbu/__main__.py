"""CLI entry: `gbu` / `python -m gbu`."""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gbu",
        description="Grok Build Usage — menu bar + always-on-top credit HUD",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=45.0,
        help="Seconds between billing refreshes (default: 45)",
    )
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="Start with the floating overlay hidden (menu bar only)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one usage snapshot to stdout and exit (no UI)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.once:
        from gbu.billing import fetch_snapshot

        snap = fetch_snapshot()
        for line in snap.summary_lines():
            print(line)
        return 1 if snap.error else 0

    if sys.platform != "darwin":
        print("Grok Build Usage is macOS-only.", file=sys.stderr)
        return 2

    from gbu.app import run_app

    run_app(poll_seconds=args.poll, start_hud_visible=not args.hidden)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
