"""Command line interface: ``skill-atlas scan <git repo url>``."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .models import ScanResult
from .report import format_console, write_html
from .repo import RepoError, clone, web_base_url
from .scanner import find_skills
from .similarity import DEFAULT_THRESHOLD, MAX_COMPARABLE, find_similar, pairwise

DEFAULT_REPORT = "report.html"
DEFAULT_PORT = 8888

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
    """Whether a graphical browser can be handed the report's URL.

    On Linux with no display, :mod:`webbrowser` falls back to terminal browsers
    such as lynx, which would seize the terminal rather than show a report.
    """
    if sys.platform.startswith(("win", "darwin")):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _should_open(args: argparse.Namespace) -> bool:
    """Only open a report a human is waiting for and a browser can display.

    A piped or redirected stdout means a script or CI is driving us, where a
    browser window is at best unseen -- and since serving blocks until Ctrl+C,
    a step nobody is watching would hang until the job times out.
    """
    if args.no_open or args.no_html:
        return False
    isatty = getattr(sys.stdout, "isatty", None)
    if not (callable(isatty) and isatty()):
        return False
    return _has_display()


def _open_in_browser(url: str) -> None:
    """Best effort: the report on disk is the deliverable, the window a convenience."""
    try:
        webbrowser.open(url)
    except (webbrowser.Error, OSError):
        pass


class _ReportHandler(BaseHTTPRequestHandler):
    """Serve one in-memory document at ``/``, and 404 for everything else.

    The report is standalone -- no external assets -- so there is nothing to gain
    from rooting a :class:`~http.server.SimpleHTTPRequestHandler` at the report's
    directory, which by default is the working directory: that would publish every
    sibling file to anything that can reach the port.
    """

    def do_GET(self) -> None:
        self._respond(with_body=True)

    def do_HEAD(self) -> None:
        self._respond(with_body=False)

    def _respond(self, *, with_body: bool) -> None:
        if self.path.split("?", 1)[0] not in {"/", "/index.html"}:
            self.send_error(HTTPStatus.NOT_FOUND)  # a browser asking after /favicon.ico
            return
        document: bytes = self.server.document
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(document)))
        self.end_headers()
        if with_body:
            self.wfile.write(document)

    def log_message(self, *_args: object) -> None:
        """Silence the per-request log: it would bury the scan output above it."""


class _ReportServer(ThreadingHTTPServer):
    """Localhost-only HTTP server holding the rendered report in memory."""

    # HTTPServer turns SO_REUSEADDR on, which POSIX wants so that a second scan can
    # rebind the port while the previous session's sockets sit in TIME_WAIT. Windows
    # reads the same flag as permission to bind a port another process is already
    # listening on: it would be stolen silently, and _serve_report would never get
    # to report it as busy.
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, address: tuple[str, int], document: bytes) -> None:
        self.document = document  # set first: super() binds and a request may then arrive
        super().__init__(address, _ReportHandler)

    def handle_error(self, request: object, client_address: object) -> None:
        """A tab closed mid-response resets the connection; that is not news.

        socketserver's default is to print the traceback, which would look like a
        crash in the middle of an otherwise finished scan.
        """


def _serve_report(path: Path, port: int) -> None:
    """Serve the report at ``http://localhost:<port>/`` until interrupted.

    Over http:// the report is an ordinary page with a real origin, where a
    ``file://`` URL is not: browsers deny local files module scripts, ``fetch``
    and storage. Serving costs nothing here -- the scan is done, the report is on
    disk -- so blocking until Ctrl+C is only the terminal's time, and a failure to
    bind or to launch a browser is a message, not a non-zero exit.
    """
    try:
        server = _ReportServer(("127.0.0.1", port), path.read_bytes())
    except OSError as exc:
        reason = exc.strerror or exc  # EADDRINUSE, typically: something else holds the port
        print(f"skill-atlas: not serving the report on port {port}: {reason}", file=sys.stderr)
        return

    with server:
        url = f"http://localhost:{server.server_address[1]}/"  # ask the socket: port 0 means "any"
        print(f"Serving it at {url} (Ctrl+C to stop)")
        _open_in_browser(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()  # the echoed ^C left the cursor mid-line; stopping is not an error


def _port(value: str) -> int:
    """Parse ``--port``, rejecting what the socket layer would only refuse later.

    A bad port is a bad argument, so it belongs to argparse and exit code 2. Left to
    ``bind()`` it would instead surface as a stderr line after the clone and the scan
    had already run, looking like a busy port rather than a typo.
    """
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{port} is outside the port range 0-65535")
    return port


def _ratio(value: str) -> float:
    """Parse ``--similarity``, which is a fraction of 1 and not a percentage.

    Same reasoning as :func:`_port`: a value no score could ever equal is a typo, and
    saying so costs exit code 2 up front rather than an empty "Similar skills" section
    after the clone and the scan have already run. ``nan`` and ``inf`` parse as floats
    and fail the range test, which is where they belong.
    """
    try:
        ratio = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not 0.0 <= ratio <= 1.0:
        raise argparse.ArgumentTypeError(f"{value} is outside the range 0-1")
    return ratio


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
    scan.add_argument(
        "--port",
        type=_port,
        # Read at parse time, not import time, so the default tracks the constant.
        default=DEFAULT_PORT,
        metavar="<n>",
        help=f"port for the report server (default: {DEFAULT_PORT}; 0 picks any free port)",
    )
    scan.add_argument(
        "--similarity",
        type=_ratio,
        metavar="<0-1>",
        default=DEFAULT_THRESHOLD,
        help=f"how alike two skills must read to be grouped (default: {DEFAULT_THRESHOLD}; 1 is identical)",
    )
    scan.add_argument(
        "--no-similar",
        action="store_true",
        help="do not compare skills: no near-duplicate groups, and no matrix in the report",
    )
    scan.add_argument(
        "--no-open",
        action="store_true",
        help="do not serve the report and open it in a browser",
    )
    scan.set_defaults(func=cmd_scan)

    return parser


def cmd_scan(args: argparse.Namespace) -> int:
    try:
        with clone(args.url, ref=args.ref) as checkout:
            skills = find_skills(checkout.path)
            compare = not args.no_similar
            result = ScanResult(
                source=args.url,
                scanned_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                skills=skills,
                commit=checkout.commit,
                ref=checkout.ref,
                web_base_url=web_base_url(args.url),
                similar=find_similar(skills, args.similarity) if compare else [],
                matrix=pairwise(skills) if compare and len(skills) <= MAX_COMPARABLE else [],
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
            _serve_report(path, args.port)

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
