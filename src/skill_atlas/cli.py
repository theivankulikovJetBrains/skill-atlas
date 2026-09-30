"""Command line interface: ``skill-atlas scan <git repo url>``."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .models import ScanResult
from .report import format_console, write_html
from .repo import RepoError, clone, web_base_url
from .scanner import find_skills

DEFAULT_REPORT = "report.html"

EXIT_OK = 0
EXIT_ERROR = 1


def _harden_stdio() -> None:
    """Degrade unencodable characters instead of crashing on a legacy code page.

    Skill descriptions are arbitrary UTF-8, but a redirected Windows stdout is
    cp1252/surrogateescape, where an arrow or emoji raises UnicodeEncodeError.
    report.html is always written as UTF-8, so the report itself loses nothing.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding in {"utf-8", "utf8"}:
            continue
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # not a reconfigurable text stream (e.g. captured in tests)


def _has_display() -> bool:
    """Whether a graphical browser can be handed a ``file://`` URL.

    On Linux with no display, :mod:`webbrowser` falls back to terminal browsers
    such as lynx, which would seize the terminal rather than show a report.
    """
    if sys.platform.startswith(("win", "darwin")):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _should_open(args: argparse.Namespace) -> bool:
    """Only open a report a human is waiting for and a browser can display.

    A piped or redirected stdout means a script or CI is driving us, where a
    browser window is at best unseen and at worst a hung step.
    """
    if args.no_open or args.no_html:
        return False
    isatty = getattr(sys.stdout, "isatty", None)
    if not (callable(isatty) and isatty()):
        return False
    return _has_display()


def _open_in_browser(path: Path) -> None:
    """Best effort: the report on disk is the deliverable, the window a convenience.

    ``as_uri`` rejects a relative path, and ``--out`` is relative by default, so
    resolve first. ValueError is deliberately not caught: it would mean that
    slipped back in, and a silent no-op is how it hides.
    """
    try:
        webbrowser.open(path.resolve().as_uri())
    except (webbrowser.Error, OSError):
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill-atlas",
        description="Scan a git repository and find all AI agent skills.",
    )
    parser.add_argument("--version", action="version", version=f"skill-atlas {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    scan = subparsers.add_parser(
        "scan",
        help="scan a git repository and report the skills it describes",
        description="Clone a git repository, report every skill it describes, and write an HTML report.",
    )
    scan.add_argument("url", metavar="<git repo url>", help="repository to scan")
    scan.add_argument("--ref", metavar="<branch|tag>", help="branch or tag to scan (default: the repo's default branch)")
    scan.add_argument(
        "-o",
        "--out",
        default=DEFAULT_REPORT,
        metavar="<path>",
        help=f"where to write the HTML report (default: ./{DEFAULT_REPORT})",
    )
    scan.add_argument("--no-html", action="store_true", help="print to the terminal only")
    scan.add_argument("--no-open", action="store_true", help="do not open the report in a browser")
    scan.set_defaults(func=cmd_scan)

    return parser


def cmd_scan(args: argparse.Namespace) -> int:
    try:
        with clone(args.url, ref=args.ref) as checkout:
            skills = find_skills(checkout.path)
            result = ScanResult(
                source=args.url,
                scanned_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                skills=skills,
                commit=checkout.commit,
                ref=checkout.ref,
                web_base_url=web_base_url(args.url),
            )
    except RepoError as exc:
        print(f"skill-atlas: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(format_console(result))

    if not args.no_html:
        try:
            path = write_html(result, args.out)
        except OSError as exc:
            print(f"skill-atlas: could not write {args.out}: {exc.strerror}", file=sys.stderr)
            return EXIT_ERROR
        print(f"\nReport written to {path.resolve()}")
        if _should_open(args):
            _open_in_browser(path)

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    _harden_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nskill-atlas: interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
