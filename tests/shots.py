"""Photograph the report at every check this suite makes against it.

The report is a document a human looks at, but every assertion about it here can only
read its markup. A green run therefore says the bytes are right and nothing about
whether the page draws. On CI that is cheap to fix -- the runner has a browser -- so
each of those checks can leave behind a picture of the document it judged, and a check
that goes red leaves the picture that shows why.

Three properties are deliberate. They are what keeps a screenshot layer from becoming a
second, flakier source of truth:

- **Off unless asked for.** No browser starts without ``--shots-dir`` (or
  ``SKILL_ATLAS_SHOTS_DIR``). ``AGENTS.md`` promises the suite finishes in seconds and
  touches neither the network nor the developer's desktop; a headless render per check
  costs about a minute of that. With the flag absent, :meth:`Page.check` is a bare
  ``assert`` and this module launches nothing.
- **The check decides, never the picture.** ``check()`` asserts exactly the condition
  the test computed from the markup -- the same condition that was there before -- and
  the screenshot is a side effect. Nothing here can turn a red check green, and a
  browser that will not start cannot fail a check that would otherwise pass.
  ``--shots-strict`` is how CI asks for the opposite: there, a browser that cannot be
  found or cannot render is the job's problem, because the alternative is an artifact
  that is quietly empty.
- **One launch per check moment.** The band naming the check and its verdict is drawn
  into the document *before* it is photographed, so a frame can never be paired with
  some other check's verdict. Same reason ``record_demo.py`` takes its screenshot and
  its DOM dump from one launch.

This is collection, not visual regression. Nothing is compared against a baseline
image: fonts differ from one runner to the next, and these frames are evidence for a
human reading a CI artifact, not an oracle. A check's verdict comes from the markup,
and only from the markup.

Why this lives here and not in ``.claude/skills/demo-video/scripts/record_demo.py``:
that script is host tooling outside the package, the suite must not depend on it, and
it answers a different question -- whether the report's *JavaScript* works. The Edge
facts both rely on are the same, and are written out again below because they are
exactly the kind that reads as arbitrary and gets tidied away.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html import escape
from pathlib import Path

import pytest

#: Both even, and the same shape record_demo.py films at, so a frame from either route
#: is comparable by eye.
WIDTH, HEIGHT = 1280, 900

#: A render that has not finished by now is hung, not slow: these documents are a few
#: kilobytes of static HTML. Well inside the 60s ``faulthandler_timeout`` in
#: pyproject.toml, which is per *test* -- so even a test whose first launch times out
#: still gets to report its own failure rather than having its stacks dumped.
RENDER_TIMEOUT = 30.0

#: Where Edge is on the platforms this suite runs on: a developer's Windows box, a mac,
#: and the GitHub runners. The Ubuntu runner image ships ``microsoft-edge``; the rest is
#: covered by ``PATH`` below.
#:
#: **Edge, not Chrome.** Chrome hands the command line to an already-running instance
#: and ``--screenshot`` then silently produces nothing -- no error, no file, a green run
#: with an empty artifact. ``--shots-browser`` will take any Chromium binary for the
#: cases where the caller knows better (a Linux box with only ``chromium``), but nothing
#: but Edge is ever picked automatically.
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
    "/opt/microsoft/msedge/microsoft-edge",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
EDGE_ON_PATH = ("msedge", "microsoft-edge", "microsoft-edge-stable")

#: Dropped in the output directory so a later run can tell a directory it wrote itself
#: from one that belongs to somebody else. See :meth:`Collector.prepare`.
MARKER = ".skill-atlas-shots"

BAND_CSS = """<style id="shot-band-css">
#shot-band { display: flex; align-items: baseline; gap: 16px; background: #0b1220;
  color: #eaf0fb; padding: 14px 24px; font: 600 17px/1.35 "Segoe UI", system-ui, sans-serif; }
#shot-band .seq { background: #2563eb; color: #fff; border-radius: 999px; padding: 3px 12px;
  font-size: 13px; flex: none; }
#shot-band .where { color: #8fa3c4; font-weight: 400; font-size: 13px; flex: none;
  font-family: Consolas, "Cascadia Mono", monospace; }
#shot-band .verdict { margin-left: auto; flex: none; font-weight: 500; font-size: 14px;
  font-family: Consolas, "Cascadia Mono", monospace; }
#shot-band .pass { color: #8ee6a8; }
#shot-band .fail { color: #ff9a9a; }
</style>
"""


def find_browser(explicit: str | None) -> str | None:
    """Edge, or whatever ``--shots-browser`` named. ``None`` when there is none."""
    if explicit:
        return explicit if Path(explicit).exists() else None
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in EDGE_ON_PATH:
        if found := shutil.which(name):
            return found
    return None


def slug(text: str, cap: int) -> str:
    """A filename fragment: safe on Windows, and short enough to stay under MAX_PATH.

    Truncation cannot collide two shots into one file -- every name carries the
    session-wide sequence number, which is unique on its own.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-._")
    return cleaned[:cap].strip("-._") or "x"


def name_of(nodeid: str) -> str:
    """``tests/test_report.py::TestHtmlReport::test_x`` to ``test_report-TestHtmlReport-test_x``."""
    trimmed = nodeid.removeprefix("tests/").replace(".py::", "-").replace("::", "-")
    return slug(trimmed, 60)


@dataclasses.dataclass(frozen=True)
class Shot:
    """One photographed check moment, as the index records it."""

    seq: int
    test: str
    label: str
    passed: bool
    png: str  # relative to the shots directory, so the artifact is self-contained
    source: str  # the report the check was made against


class Collector:
    """Session-wide: where shots go, what renders them, and what came out.

    Built in ``pytest_configure`` so a bad ``--shots-dir`` or a missing browser under
    ``--shots-strict`` is a usage error before a single test runs, rather than a
    surprise at the end of one.
    """

    def __init__(self, directory: Path | None, browser: str | None, strict: bool) -> None:
        self.directory = directory
        self.browser = browser
        self.strict = strict
        self.shots: list[Shot] = []
        self.seq = 0
        #: Set when a render fails and we are not strict: one broken browser is worth a
        #: warning, not the same failure repeated thirty times at 30 seconds each.
        self.broken: str | None = None
        self._profile: Path | None = None

    @property
    def enabled(self) -> bool:
        return self.directory is not None and self.browser is not None and self.broken is None

    #: Where ``--shots`` alone collects to. Inside the checkout, because that is where a
    #: developer will look for it; ``.gitignore`` covers it so it never reads as a change
    #: to commit.
    DEFAULT_DIRNAME = "test-shots"

    @classmethod
    def from_config(cls, config: pytest.Config) -> Collector:
        raw = config.getoption("shots_dir") or os.environ.get("SKILL_ATLAS_SHOTS_DIR") or ""
        asked = bool(config.getoption("shots")) or bool(os.environ.get("SKILL_ATLAS_SHOTS"))
        strict = bool(config.getoption("shots_strict")) or bool(
            os.environ.get("SKILL_ATLAS_SHOTS_STRICT")
        )
        named = config.getoption("shots_browser") or os.environ.get("SKILL_ATLAS_SHOTS_BROWSER")
        if raw:
            directory = Path(raw).resolve()
        elif asked or strict:
            # --shots-strict on its own reads as "collect, and mean it", so it implies
            # the default location rather than being a usage error for lacking a path.
            directory = config.rootpath / cls.DEFAULT_DIRNAME
        else:
            directory = None
        browser = find_browser(named) if directory else None

        if directory is None:
            return cls(None, None, strict)
        if browser is None and strict:
            where = (
                f"{named} does not exist"
                if named
                else f"looked for Microsoft Edge at {', '.join(EDGE_CANDIDATES)} and for "
                     f"{', '.join(EDGE_ON_PATH)} on PATH -- pass --shots-browser=<path> if it "
                     f"lives somewhere unusual"
            )
            raise pytest.UsageError(
                f"--shots-strict is set and no browser was found to collect screenshots with: "
                f"{where}"
            )
        collector = cls(directory, browser, strict)
        collector.prepare()
        return collector

    # ----------------------------------------------------------------- the directory

    def prepare(self) -> None:
        """Make an empty output directory, and refuse to empty somebody else's.

        A leftover PNG from an earlier run is the trap ``AGENTS.md`` warns about for
        ``report.html``: it reads as this run's evidence. So the directory is cleared --
        but only once it has proved to be one of ours, because a harness that deletes
        whatever path it is handed is a bad thing to have in a repository.
        """
        assert self.directory is not None
        if self.directory.exists() and any(self.directory.iterdir()):
            if not (self.directory / MARKER).exists():
                raise pytest.UsageError(
                    f"--shots-dir {self.directory} is not empty and was not written by a "
                    f"previous run (no {MARKER}); pass a path that is empty or does not exist"
                )
            for leftover in self.directory.iterdir():
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink(missing_ok=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / MARKER).write_text(
            "Written by tests/shots.py. Contents are replaced by the next run.\n",
            encoding="utf-8",
        )

    @property
    def staging(self) -> Path:
        """Where the document handed to the browser is written.

        Edge needs a URL, so the banded copy has to exist as a file. A copy is kept
        beside its PNG only when its check went red, which is the one case where the
        markup behind a frame is worth reading.
        """
        assert self.directory is not None
        staged = self.directory / "staged"
        staged.mkdir(parents=True, exist_ok=True)
        return staged

    @property
    def profile(self) -> Path:
        """One browser profile for the whole session, in the system temp directory.

        Not under the shots directory: Edge's crash handler outlives the browser by a
        few seconds and holds its ``--user-data-dir`` open, which would leave the
        artifact carrying a half-written profile -- or make the *next* run unable to
        clear it.
        """
        if self._profile is None:
            self._profile = Path(tempfile.mkdtemp(prefix="skill-atlas-shots-"))
        return self._profile

    # ----------------------------------------------------------------- capturing

    def album(self, nodeid: str) -> Album:
        return Album(self, name_of(nodeid))

    def capture(self, test: str, html: str, source: str, label: str, passed: bool) -> str | None:
        """Render one frame. Returns the path to it, or ``None`` if nothing was taken."""
        if not self.enabled:
            return None
        assert self.directory is not None and self.browser is not None
        self.seq += 1
        stem = f"{self.seq:03d}-{test}-{slug(label, 40)}-{'PASS' if passed else 'FAIL'}"
        staged = self.staging / f"{stem}.html"
        staged.write_text(band(html, self.seq, label, passed, source), encoding="utf-8")
        png = self.directory / f"{stem}.png"

        # Headless with a profile of its own in the system temp directory, so nothing
        # here reaches the developer's desktop or their real browser profile -- the same
        # rule the `opened` fixture in conftest.py enforces for webbrowser.open.
        #
        # And nothing here reaches the network either, which the suite is not allowed to
        # do: the report is a standalone document with no external assets (a check in
        # TestHtmlReport pins that), so there is nothing in the page to fetch, and the
        # four --disable flags below switch off the browser's own background chatter.
        argv = [
            self.browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            # Shared memory is small in a container, and Chromium's default is to put
            # its renderer heap there; without this it dies with a bad exit code and no
            # useful message on exactly the machines this is meant to run on.
            "--disable-dev-shm-usage",
            f"--user-data-dir={self.profile}",
            f"--window-size={WIDTH},{HEIGHT}",
            # Virtual, not wall-clock: it lets the page settle without the launch
            # actually taking four seconds.
            "--virtual-time-budget=4000",
            f"--screenshot={png}",
            *self.sandbox_flags(),
            staged.as_uri(),
        ]
        try:
            subprocess.run(
                argv, capture_output=True, text=True, errors="replace", timeout=RENDER_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired) as bad:
            return self.give_up(f"{type(bad).__name__}: {bad}", staged)
        if not png.exists():
            return self.give_up("the browser exited without writing a png", staged)

        if passed:
            staged.unlink(missing_ok=True)  # only a red check's markup is worth keeping
        self.shots.append(
            Shot(self.seq, test, label, passed, png.name, source)
        )
        return str(png)

    def sandbox_flags(self) -> tuple[str, ...]:
        """``--no-sandbox``, but only as root, where the sandbox genuinely cannot work.

        Chromium refuses to start as root without it. Everywhere else -- a developer's
        account, the ``agent`` user in this project's sandbox container, the ``runner``
        user on GitHub -- the sandbox works and switching it off would be a pointless
        loosening on the machine that browses the least trusted thing here.
        """
        if sys.platform != "win32" and getattr(os, "geteuid", lambda: 1)() == 0:
            return ("--no-sandbox",)
        return ()

    def give_up(self, why: str, staged: Path) -> None:
        """A render failed. Strict callers hear about it as an error, others as a note."""
        staged.unlink(missing_ok=True)
        if self.strict:
            raise RuntimeError(
                f"screenshot collection failed and --shots-strict is set: {why}. "
                f"Browser: {self.browser}"
            )
        self.broken = why
        return None

    # ----------------------------------------------------------------- the index

    def finish(self) -> None:
        """Write the index, and clear up what nothing needs any more."""
        if self.directory is None:
            return
        if self._profile is not None:
            # ignore_errors: the crash handler may still hold it, and a leftover
            # directory in %TEMP% is not worth failing a green run over.
            shutil.rmtree(self._profile, ignore_errors=True)
        staged = self.directory / "staged"
        if staged.is_dir() and not any(staged.iterdir()):
            staged.rmdir()
        if not self.shots and self.broken is None:
            return

        red = [shot for shot in self.shots if not shot.passed]
        lines = [
            "# Report screenshots",
            "",
            f"{len(self.shots)} check moments photographed, {len(self.shots) - len(red)} green.",
            "",
            "Each picture is the report document as the browser drew it, with a band naming "
            "the check that was made against it and how that check went. The verdict comes "
            "from the markup, not from the picture -- see `tests/shots.py`.",
            "",
        ]
        if self.broken:
            lines += [f"**Collection stopped early:** {self.broken}", ""]
        if red:
            lines += ["## Red", ""]
            lines += [f"- `{s.png}` — {s.test}: {s.label}" for s in red]
            lines += [""]
        lines += ["| # | Test | Check | Verdict | Screenshot |", "|---|---|---|---|---|"]
        for shot in self.shots:
            lines.append(
                f"| {shot.seq} | `{shot.test}` | {shot.label} | "
                f"{'PASS' if shot.passed else 'FAIL'} | `{shot.png}` |"
            )
        (self.directory / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.directory / "shots.json").write_text(
            json.dumps(
                {
                    "browser": self.browser,
                    "viewport": [WIDTH, HEIGHT],
                    "stopped_early": self.broken,
                    "shots": [dataclasses.asdict(shot) for shot in self.shots],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def band(html: str, seq: int, label: str, passed: bool, source: str) -> str:
    """Draw the check and its verdict into the document that is about to be shot.

    Inserted as ``<body>``'s first child rather than overlaid as a fixed bar, so it
    pushes the report down instead of covering the header some of these checks are
    about. ``--screenshot`` photographs the viewport at the top of the document, which
    is where that puts it.
    """
    marker = (
        BAND_CSS
        + f'<div id="shot-band"><span class="seq">{seq:03d}</span>'
        f'<span class="label">{escape(label)}</span>'
        f'<span class="where">{escape(source)}</span>'
        f'<span class="verdict {"pass" if passed else "fail"}">'
        f'{"PASS" if passed else "FAIL"}</span></div>'
    )
    # A function replacement, because the band is arbitrary text: re.sub would read a
    # backslash in an escaped label as a group reference and raise.
    staged, hits = re.subn(r"<body[^>]*>", lambda m: m.group(0) + marker, html, count=1)
    return staged if hits else marker + html


class Album:
    """The per-test handle the ``shots`` fixture hands out."""

    def __init__(self, collector: Collector, test: str) -> None:
        self.collector = collector
        self.test = test

    def page(self, source: str | Path, *, no_script: bool = False) -> Page:
        """The report a run of checks is about, from its path or its markup.

        ``no_script=True`` strips the report's own ``<script>`` from the copy that is
        photographed. That is not a trick to make a picture simpler: it is the document
        a reader without JavaScript actually gets, and it is the only honest subject for
        the checks about what ships hidden. Rendered as shipped, those same elements are
        revealed by the report's script before the shutter opens, and the frame would
        contradict the check beside it.
        """
        if isinstance(source, Path):
            html, where = source.read_text(encoding="utf-8"), source.name
        else:
            html, where = source, "(markup)"
        if no_script:
            # Stripped rather than switched off: --blink-settings=scriptEnabled=false
            # also breaks --screenshot.
            html = re.sub(r"<script>.*?</script>", "", html, flags=re.DOTALL)
            where = f"{where} (no javascript)"
        return Page(self, html, where)


class Page:
    """A report document, and the checks made against it."""

    def __init__(self, album: Album, html: str, source: str) -> None:
        self.album = album
        self.html = html
        self.source = source

    def check(self, label: str, condition: object) -> None:
        """Assert ``condition``, and photograph the page as it was when it was asserted.

        The assertion is the test; the frame is evidence. Note that a red check ends the
        test the way any failed assert does, so the checks after it in the same test are
        neither made nor photographed -- the failing frame is the last one.
        """
        passed = bool(condition)
        png = self.album.collector.capture(
            self.album.test, self.html, self.source, label, passed
        )
        if passed:
            return
        detail = f"screenshot: {png}" if png else "screenshot: not collected"
        raise AssertionError(f"{label}\nreport: {self.source}\n{detail}")
