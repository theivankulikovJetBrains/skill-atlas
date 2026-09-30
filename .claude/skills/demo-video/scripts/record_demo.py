"""Run skill-atlas through every user scenario in spec/cli.md and film the run.

Host tooling, like scripts/sbx-feature.sh -- not part of the shipped package, so it is not
bound by the runtime's "jinja2 and pyyaml only" rule. It is stdlib-only for a different
reason: it drives `uv run` rather than importing the app, so it must start under whatever
`python` happens to be on PATH (3.11+), not under the project's 3.14 venv. Pillow is used if
it is there, and only for the GIF fallback when no ffmpeg can be found.

Three things happen here, in order:

  1. Every CLI scenario runs for real -- real git fetches, real reports on disk, real HTTP
     against the real report server -- and each one carries the assertions that say what
     "worked" means, so a red run is a finding rather than a nice video of a broken app.
  2. Every report-UI scenario is driven headlessly in Edge. One launch per frame both
     screenshots the page and dumps the DOM, so the picture and the assertions come from the
     same render and cannot disagree.
  3. The frames become demo.mp4.

Why frames and not a screen recorder: nothing on this box can film a desktop, and a
recording of a window would be unreproducible anyway. A storyboard of deterministic frames
is diffable, survives CI, and every frame is evidence of a check that ran.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import escape, unescape
from pathlib import Path

WIDTH, HEIGHT = 1280, 900  # both even: libx264 + yuv420p rejects odd dimensions

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/microsoft-edge",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


# --------------------------------------------------------------------------- results


@dataclasses.dataclass
class Check:
    """One assertion. `pass_` is the whole point of the run; the rest is for the report."""

    label: str
    actual: str
    expected: str
    pass_: bool


@dataclasses.dataclass
class Scenario:
    id: str
    caption: str
    detail: str = ""  # the command line, or the UI action, as the video should show it
    output: str = ""
    checks: list[Check] = dataclasses.field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return all(c.pass_ for c in self.checks)

    @property
    def verdict(self) -> str:
        bad = [c for c in self.checks if not c.pass_]
        if bad:
            return f"FAIL  {bad[0].label}: {bad[0].actual} (expected {bad[0].expected})"
        return f"OK  {len(self.checks)} checks"


@dataclasses.dataclass
class Frame:
    """A still to be rendered by Edge. `html` is a complete document written next to the png."""

    name: str
    html: str
    duration: float
    extra_args: tuple[str, ...] = ()
    wants_dom: bool = False
    png: Path | None = None
    dom: str = ""


# --------------------------------------------------------------------------- preflight


def find_edge(explicit: str | None) -> str:
    if explicit:
        if not Path(explicit).exists():
            die(f"--edge {explicit} does not exist")
        return explicit
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("msedge") or shutil.which("microsoft-edge")
    if found:
        return found
    die(
        "Microsoft Edge not found. Chrome is not a substitute: it hands the command line to "
        "an already-running instance and --screenshot silently produces nothing. Pass --edge "
        "<path> if Edge lives somewhere unusual."
    )


def find_ffmpeg() -> str | None:
    """ffmpeg from PATH, from an installed imageio-ffmpeg, or downloaded through uv."""
    if found := shutil.which("ffmpeg"):
        return found
    try:  # already installed next to this interpreter?
        import imageio_ffmpeg  # type: ignore[import-not-found]

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    if not shutil.which("uv"):
        return None
    # imageio-ffmpeg ships a static ffmpeg in its wheel, so this needs PyPI but no system
    # package manager and nothing on PATH. --no-project keeps it out of the project's venv.
    probe = "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
    try:
        out = subprocess.run(
            ["uv", "run", "--no-project", "--quiet", "--with", "imageio-ffmpeg", "python", "-c", probe],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    return path if path and Path(path).exists() else None


def die(message: str) -> None:
    print(f"record_demo: {message}", file=sys.stderr)
    raise SystemExit(2)


def wipe(path: Path, strict: bool = True) -> Path | None:
    """Delete a file or tree, read-only files and all, and make sure it really went.

    git leaves the objects in the fixture repositories read-only, which is enough to stop
    shutil.rmtree on Windows. `ignore_errors=True` would hide that and leave the last run's
    reports on disk -- and a stale report is exactly what lets an assertion pass against
    nothing, which is the trap AGENTS.md warns about for report.html. So the failure is
    either fatal (`strict`) or returned for the caller to report; it is never swallowed.
    """
    def clear(_func: object, name: str, _exc: object) -> None:
        target = Path(name)
        try:
            target.chmod(stat.S_IWRITE)
            target.rmdir() if target.is_dir() else target.unlink()
        except OSError:
            pass  # the existence check below is what actually decides

    # Three passes, because a Windows lock is often a process on its way out rather than a
    # permission: whatever still had the file open gets a moment to let go.
    for attempt in range(3):
        if not path.exists():
            return None
        if path.is_file():
            try:
                path.chmod(stat.S_IWRITE)
                path.unlink()
            except OSError:
                pass
        elif sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=clear)  # type: ignore[call-overload]
        else:
            shutil.rmtree(path, onerror=clear)  # type: ignore[call-overload]
        if not path.exists():
            return None
        time.sleep(1.0 + attempt)
    if strict:
        die(f"could not delete {path} -- something still has it open (a media player holding "
            f"demo.mp4 will do it); remove it and re-run")
    return path


def check_ignored(out: Path, project: Path) -> None:
    """Warn if the video would land somewhere git can see it.

    The point of the default `demo-run/` is that `.gitignore` covers it, so a demo never shows
    up as a change to commit. A `--out-dir` pointed somewhere else inside the checkout quietly
    loses that, which is worth a word rather than a surprise in `git status`.
    """
    if not out.is_relative_to(project):
        return
    try:
        seen = subprocess.run(["git", "check-ignore", "-q", str(out)], cwd=str(project),
                              capture_output=True, timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired):
        return
    if seen != 0:
        print(f"record_demo: warning: {out} is inside the checkout and not gitignored, so the "
              f"video will show up in `git status`. Add it to .gitignore or pass --out-dir.",
              file=sys.stderr)


# --------------------------------------------------------------------------- fixtures


SKILL_MD = """---
name: {name}
description: {description}
---

# {name}

{body}
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            GIT_AUTHOR_NAME="skill-atlas demo",
            GIT_AUTHOR_EMAIL="demo@example.invalid",
            GIT_COMMITTER_NAME="skill-atlas demo",
            GIT_COMMITTER_EMAIL="demo@example.invalid",
        ),
    )


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "commit.gpgsign", "false")
    return path


def write_skill(repo: Path, rel: str, name: str, description: str, body: str = "Steps.") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        SKILL_MD.format(name=name, description=description, body=body), encoding="utf-8"
    )


def build_fixtures(root: Path) -> dict[str, Path]:
    """Local repositories for the scenarios a public repo cannot be relied on to show.

    A remote's tags, its duplicate skill names and its exact skill count are all outside our
    control, and three spec behaviours turn on precisely those -- `--ref`, the directory shown
    under a name shared inside a comparison, and the 200-skill ceiling on the comparison. So
    they get repositories built to order, which also makes those scenarios work offline.
    """
    root.mkdir(parents=True, exist_ok=True)

    # `atlas`: the everyday repo. Two commits so --ref has an older tag to reach for, a skill
    # vendored under both .claude/ and .agents/ so grouping and the shared-name label have
    # something to find, and two broken files because "reported and flagged, not dropped" is
    # behaviour worth filming.
    atlas = init_repo(root / "atlas")
    write_skill(atlas, "skills/build-and-test/SKILL.md", "build-and-test",
                "Build the project and run its test suite before every commit.")
    write_skill(atlas, "skills/release-notes/SKILL.md", "release-notes",
                "Draft release notes from the commits since the last tag.")
    git(atlas, "add", "-A")
    git(atlas, "commit", "-qm", "Add the first two skills")
    git(atlas, "tag", "v0.1")

    shared = "Deploy the service to production and watch the rollout."
    write_skill(atlas, ".claude/skills/deploy/SKILL.md", "deploy", shared)
    write_skill(atlas, ".agents/skills/deploy/SKILL.md", "deploy", shared)
    write_skill(atlas, "skills/deploy-staging/SKILL.md", "deploy-staging",
                "Deploy the service to staging and watch the rollout.")
    write_skill(atlas, "skills/review-pr/SKILL.md", "review-pr",
                "Review an open pull request and leave comments on the diff.")
    (atlas / "skills/adrift/SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (atlas / "skills/adrift/SKILL.md").write_text(  # no frontmatter at all
        "# adrift\n\nSomebody forgot the frontmatter.\n", encoding="utf-8"
    )
    (atlas / "skills/mangled/SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (atlas / "skills/mangled/SKILL.md").write_text(  # frontmatter that will not parse
        "---\nname: [unclosed\ndescription: still broken\n---\n\n# mangled\n", encoding="utf-8"
    )
    git(atlas, "add", "-A")
    git(atlas, "commit", "-qm", "Vendor deploy twice and add two malformed skills")

    # `barren`: exit 0 on a repo with nothing to find is a documented success, not an error.
    barren = init_repo(root / "barren")
    (barren / "README.md").write_text("Nothing to see here.\n", encoding="utf-8")
    git(barren, "add", "-A")
    git(barren, "commit", "-qm", "A repository with no skills")

    # `bulk`: 201 skills, one over the ceiling above which the report drops the comparison.
    bulk = init_repo(root / "bulk")
    for i in range(201):
        write_skill(bulk, f"skills/skill-{i:03d}/SKILL.md", f"skill-{i:03d}",
                    f"Automate chore number {i} so nobody has to remember it.")
    git(bulk, "add", "-A")
    git(bulk, "commit", "-qm", "Add 201 skills, one past the comparison ceiling")

    return {"atlas": atlas, "barren": barren, "bulk": bulk}


# --------------------------------------------------------------------------- CLI scenarios


class Driver:
    """Runs the app the way a user does -- through `uv run` -- and keeps the evidence."""

    def __init__(self, project: Path, work: Path, timeout: float) -> None:
        self.project = project
        self.work = work
        self.timeout = timeout
        self.reports: dict[str, Path] = {}
        # Python's webbrowser honours $BROWSER on every platform, so pointing it at a recorder
        # proves the open happened -- and which URL it was handed -- without a real tab landing
        # on the user's desktop. shlex.split eats backslashes, hence the forward slashes.
        self.opened = work / "browser-opened.txt"
        recorder = work / "record_open.py"
        recorder.write_text(
            f"import pathlib, sys\n"
            f"pathlib.Path(r'{self.opened}').write_text(sys.argv[1], encoding='utf-8')\n",
            encoding="utf-8",
        )
        py = sys.executable.replace(os.sep, "/")
        self.env = dict(os.environ, BROWSER=f"{py} {str(recorder).replace(os.sep, '/')} %s")

    def argv(self, *args: str) -> list[str]:
        return ["uv", "run", "--project", str(self.project), "skill-atlas", *args]

    def pretty(self, args: tuple[str, ...]) -> str:
        """The command as the video should show it, with the scratch directory elided.

        The real argv carries two absolute temp-directory paths per scan, which wraps the line
        twice and buries the flag the frame is about. `uv run --project` goes the same way --
        it belongs in the docs, not in what this scenario is demonstrating.
        """
        elide = ((self.work.as_uri(), "file://…"), (str(self.work), "…"))
        shown = []
        for arg in args:
            for long, short in elide:
                arg = arg.replace(long, short)
            shown.append(arg.replace("\\", "/"))
        return "skill-atlas " + " ".join(shown)

    def run(self, *args: str) -> tuple[int, str, str, float]:
        started = time.monotonic()
        try:
            done = subprocess.run(
                self.argv(*args),
                cwd=str(self.project),
                env=self.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as expired:
            return 124, expired.stdout or "", f"timed out after {self.timeout:.0f}s", self.timeout
        return done.returncode, done.stdout or "", done.stderr or "", time.monotonic() - started


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def scenario_from_run(
    sid: str,
    caption: str,
    driver: Driver,
    args: list[str],
    *,
    rc: int = 0,
    stdout_has: tuple[str, ...] = (),
    stdout_lacks: tuple[str, ...] = (),
    stderr_has: tuple[str, ...] = (),
    writes: Path | None = None,
    absent: Path | None = None,
    html_has: tuple[str, ...] = (),
    html_lacks: tuple[str, ...] = (),
    remember: str | None = None,
) -> Scenario:
    if writes is not None and writes.exists():
        writes.unlink()  # a stale report can make every assertion pass against nothing
    code, out, err, seconds = driver.run(*args)
    step = Scenario(sid, caption, driver.pretty(tuple(args)), out or err, seconds=seconds)
    add = step.checks.append
    add(Check("exit code", str(code), str(rc), code == rc))
    for needle in stdout_has:
        add(Check(f"stdout has {needle!r}", str(needle in out), "True", needle in out))
    for needle in stdout_lacks:
        add(Check(f"stdout lacks {needle!r}", str(needle not in out), "True", needle not in out))
    for needle in stderr_has:
        add(Check(f"stderr has {needle!r}", str(needle in err), "True", needle in err))
    if writes is not None:
        add(Check(f"wrote {writes.name}", str(writes.exists()), "True", writes.exists()))
        if writes.exists():
            body = writes.read_text(encoding="utf-8", errors="replace")
            for needle in html_has:
                add(Check(f"report has {needle!r}", str(needle in body), "True", needle in body))
            for needle in html_lacks:
                ok = needle not in body
                add(Check(f"report lacks {needle!r}", str(ok), "True", ok))
            if remember:
                driver.reports[remember] = writes
    if absent is not None:
        add(Check(f"no {absent.name}", str(not absent.exists()), "True", not absent.exists()))
    return step


def cli_scenarios(driver: Driver, fixtures: dict[str, Path], repo_url: str | None) -> list[Scenario]:
    reports = driver.work / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    atlas = fixtures["atlas"].as_uri()
    steps: list[Scenario] = []

    steps.append(
        scenario_from_run("CLI-01", "--version prints the version and stops", driver,
                          ["--version"], stdout_has=("skill-atlas",))
    )
    steps.append(
        scenario_from_run(
            "CLI-02", "A plain scan: skills on the console, report on disk", driver,
            ["scan", atlas, "-o", str(reports / "atlas.html"), "--no-open"],
            stdout_has=("Found 8 skills", "deploy", "skills/build-and-test/SKILL.md",
                        "Similar skills", "Report written to"),
            writes=reports / "atlas.html",
            html_has=("<title>Skill Atlas", "deploy-staging", 'class="compare"'),
            remember="atlas",
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-03", "Malformed frontmatter is flagged, never dropped", driver,
            ["scan", atlas, "-o", str(reports / "flagged.html"), "--no-open"],
            stdout_has=("adrift", "mangled"),
            writes=reports / "flagged.html",
            html_has=("adrift", "mangled"),
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-04", "--ref scans a tag instead of the default branch", driver,
            ["scan", atlas, "--ref", "v0.1", "-o", str(reports / "tagged.html"), "--no-open"],
            stdout_has=("Found 2 skills", "build-and-test"),
            stdout_lacks=("deploy-staging",),
            writes=reports / "tagged.html",
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-05", "--no-html: the console alone, nothing written", driver,
            ["scan", atlas, "-o", str(reports / "never.html"), "--no-html", "--no-open"],
            stdout_has=("Found 8 skills",),
            stdout_lacks=("Report written",),
            absent=reports / "never.html",
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-06", "--similarity 0.9 wants near-identity, and finds the vendored copy", driver,
            ["scan", atlas, "--similarity", "0.9", "-o", str(reports / "strict.html"), "--no-open"],
            stdout_has=("Found 8 skills", "100%"),
            writes=reports / "strict.html",
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-07", "--similarity 0.2 groups anything that rhymes", driver,
            ["scan", atlas, "--similarity", "0.2", "-o", str(reports / "loose.html"), "--no-open"],
            stdout_has=("Similar skills",),
            writes=reports / "loose.html",
            remember="loose",
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-08", "--no-similar drops the groups and the comparison", driver,
            ["scan", atlas, "--no-similar", "-o", str(reports / "plain.html"), "--no-open"],
            stdout_has=("Found 8 skills",),
            stdout_lacks=("Similar skills",),
            writes=reports / "plain.html",
            html_lacks=('class="compare"',),
            remember="plain",
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-09", "A repo with no skills is a success, not an error", driver,
            ["scan", fixtures["barren"].as_uri(), "-o", str(reports / "barren.html"), "--no-open"],
            stdout_has=("No skills found",),
            writes=reports / "barren.html",
            html_lacks=('class="search"',),  # no rows to filter, so no search box
            remember="barren",
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-10", "Past 200 skills the comparison is left out; groups stay", driver,
            ["scan", fixtures["bulk"].as_uri(), "-o", str(reports / "bulk.html"), "--no-open"],
            stdout_has=("Found 201 skills",),
            writes=reports / "bulk.html",
            html_has=("skill-200",),
            html_lacks=('class="compare"',),
            remember="bulk",
        )
    )
    if repo_url:
        steps.append(
            scenario_from_run(
                "CLI-11", "The real thing: a public repository over the network", driver,
                ["scan", repo_url, "-o", str(reports / "public.html"), "--no-open"],
                stdout_has=("Scanned", "Found "),
                writes=reports / "public.html",
                html_has=("<title>Skill Atlas", "https://github.com"),
                remember="public",
            )
        )
    steps.append(
        scenario_from_run(
            "CLI-12", "A repository that is not there: exit 1", driver,
            ["scan", (driver.work / "no-such-repo").as_uri(), "-o", str(reports / "nope.html"),
             "--no-open"],
            rc=1, absent=reports / "nope.html",
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-13", "--similarity outside 0-1 is a usage error: exit 2", driver,
            ["scan", atlas, "--similarity", "5", "--no-open"],
            rc=2, stderr_has=("outside the range 0-1",),
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-14", "--port outside 0-65535 is caught before the scan: exit 2", driver,
            ["scan", atlas, "--port", "99999", "--no-open"],
            rc=2, stderr_has=("outside the port range",),
        )
    )
    steps.append(
        scenario_from_run(
            "CLI-15", "An unwritable report is exit 1, after the scan has printed", driver,
            ["scan", atlas, "-o", str(driver.work / "reports"), "--no-open"],
            rc=1, stdout_has=("Found 8 skills",),
        )
    )
    steps.append(piped_scenario(driver))
    steps.append(served_scenario(driver))
    steps.append(busy_port_scenario(driver))
    return steps


def piped_scenario(driver: Driver) -> Scenario:
    """Every run above was captured through a pipe, so none of them may have opened a tab.

    This is the one scenario with no command of its own: it reads the recorder that all of
    them shared. The rule it checks -- no browser and no wait when stdout is not a terminal --
    is what keeps the tool usable in CI, and a regression would hang a pipeline rather than
    fail it.
    """
    step = Scenario("CLI-16", "Piped or in CI: no server, no browser, no hanging",
                    "(every scan above ran with stdout on a pipe)")
    quiet = not driver.opened.exists()
    step.checks.append(Check("browser left alone for piped runs", str(quiet), "True", quiet))
    step.output = ("Nothing invoked $BROWSER across every scan above.\n"
                   "stdout was a pipe, so the report was written and the process exited.")
    return step


def served_scenario(driver: Driver) -> Scenario:
    """The serve-and-open path, which only happens when stdout is a terminal.

    CREATE_NO_WINDOW is what makes this testable from an agent's shell: the child gets a real
    console -- so isatty() is true and the server starts -- but no window appears, and $BROWSER
    catches the open instead of a browser. Elsewhere the same effect needs a pty, so on
    non-Windows the run is started in its own session with a pty if one can be had.
    """
    port = free_port()
    report = driver.work / "reports" / "served.html"
    atlas_uri = (driver.work / "fixtures" / "atlas").as_uri()
    argv = driver.argv("scan", atlas_uri, "--port", str(port), "-o", str(report))
    step = Scenario("CLI-17", "Served on 127.0.0.1 and opened in the browser",
                    f"skill-atlas scan <repo> --port {port} -o served.html")
    driver.opened.unlink(missing_ok=True)

    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    child = subprocess.Popen(argv, cwd=str(driver.project), env=driver.env, **kwargs)  # type: ignore[arg-type]

    url = f"http://localhost:{port}/"
    body, status, deadline = "", 0, time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as answer:
                status, body = answer.status, answer.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as bad:
            status = bad.code
            break
        except Exception:
            if child.poll() is not None:
                break
            time.sleep(0.4)

    other = 0
    try:
        with urllib.request.urlopen(url + "notes.txt", timeout=3) as answer:
            other = answer.status
    except urllib.error.HTTPError as bad:
        other = bad.code
    except Exception:
        other = -1

    opened = ""
    for _ in range(20):  # the open happens after the first bind, not necessarily before it
        if driver.opened.exists():
            opened = driver.opened.read_text(encoding="utf-8", errors="replace").strip()
            break
        time.sleep(0.3)

    still_up = child.poll() is None
    child.terminate()
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()

    add = step.checks.append
    add(Check("GET / status", str(status), "200", status == 200))
    add(Check("served the report", str("<title>Skill Atlas" in body), "True",
              "<title>Skill Atlas" in body))
    add(Check("GET /notes.txt status", str(other), "404", other == 404))
    add(Check("opened URL names the bound port", opened or "(nothing)", url, opened == url))
    add(Check("kept serving until stopped", str(still_up), "True", still_up))
    step.output = (
        f"$ curl -si {url}\nHTTP/1.0 {status}   {len(body)} bytes   <title>Skill Atlas ...\n"
        f"$ curl -si {url}notes.txt\nHTTP/1.0 {other}   (only / is published)\n"
        f"$BROWSER was handed {opened or '(nothing)'}\n"
        "The process was still serving when the demo stopped it."
    )
    return step


def busy_port_scenario(driver: Driver) -> Scenario:
    """A port already taken is a message on stderr, not a failure: the report is on disk."""
    report = driver.work / "reports" / "busy.html"
    report.unlink(missing_ok=True)
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = int(squatter.getsockname()[1])
        code, out, err, seconds = driver.run(
            "scan", (driver.work / "fixtures" / "atlas").as_uri(), "--port", str(port), "-o", str(report)
        )
    step = Scenario("CLI-18", "A port already in use costs a warning, not the report",
                    f"skill-atlas scan <repo> --port {port} -o busy.html",
                    out or err, seconds=seconds)
    step.checks.append(Check("exit code", str(code), "0", code == 0))
    step.checks.append(Check("report still written", str(report.exists()), "True", report.exists()))
    return step


# --------------------------------------------------------------------------- UI scenarios


DRIVER_JS = r"""
<style id="demo-chrome">
  /* Clicking Compare similarity scrolls the section into view smoothly, and --screenshot
     photographs the top of the document however far down the page has scrolled -- which comes
     out as a blank band where nothing has been painted. Instant scrolling plus a scroll back
     to the origin below makes every frame a picture of a settled page. */
  html { scroll-behavior: auto !important; }
  #demo-band { display: flex; align-items: baseline; gap: 16px; background: #0b1220;
    color: #eaf0fb; padding: 15px 26px; font: 600 18px/1.35 "Segoe UI", system-ui, sans-serif; }
  #demo-band .id { background: #2563eb; color: #fff; border-radius: 999px; padding: 3px 12px;
    font-size: 14px; letter-spacing: .02em; flex: none; }
  #demo-band .verdict { margin-left: auto; font-weight: 500; font-size: 14px; flex: none;
    font-family: Consolas, "Cascadia Mono", monospace; }
  #demo-band .verdict.bad { color: #ff9a9a; }
  #demo-band .verdict.good { color: #8ee6a8; }
</style>
<script>
(() => {
  const checks = [];
  const q = (sel) => document.querySelector(sel);
  const all = (sel) => [...document.querySelectorAll(sel)];
  const ok = (label, actual, expected) => {
    checks.push({label, actual: String(actual), expected: String(expected),
                 pass: String(actual) === String(expected)});
  };
  const rows = () => all('table.skills tbody tr[data-name]');
  const setAt = (indices, on) => {
    const list = rows();
    for (const i of indices) {
      const box = list[i].querySelector('.pick input');
      if (box.checked === on) continue;
      box.checked = on;
      box.dispatchEvent(new Event('change', {bubbles: true}));
    }
  };
  const ui = {
    ok, q, all, rows,
    shown: () => rows().filter((row) => !row.hidden),
    // Queries taken from the data, so a step reads the same whichever report it is filming --
    // the public repo or the fixture that stands in for it offline. `needle` matches at least
    // the row it came from; `pluralNeedle` is the longest prefix two names share, so it always
    // leaves more than one row on screen, which is what the selection steps need. Sorting
    // first is what makes the longest shared prefix an adjacent pair rather than every pair.
    needle: () => rows()[0].dataset.name.toLowerCase().split(/[-_. ]/)[0],
    pluralNeedle() {
      const names = rows().map((row) => row.dataset.name.toLowerCase()).sort();
      let best = '';
      for (let i = 1; i < names.length; i++) {
        const [a, b] = [names[i - 1], names[i]];
        let n = 0;
        while (n < a.length && n < b.length && a[n] === b[n]) n++;
        if (n > best.length) best = a.slice(0, n);
      }
      return best;
    },
    // Typed, not assigned: the report filters on the `input` event, which is what a keystroke
    // produces. Assigning .value alone would prove nothing about the listener.
    type(text) {
      const input = q('.search input');
      input.value = text;
      input.dispatchEvent(new Event('input', {bubbles: true}));
    },
    escape() {
      q('.search input').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    },
    // By position, which is the only way to tick exactly one row: a name is not unique --
    // the vendored copy is the whole point of the fixture -- so pick(['deploy']) ticks two.
    pickAt: (indices) => setAt(indices, true),
    unpickAt: (indices) => setAt(indices, false),
    // By name, for the one step that means "every copy of this skill".
    pick(names) {
      setAt(rows().flatMap((row, i) => (names.includes(row.dataset.name) ? [i] : [])), true);
    },
    pickShown() {
      const head = q('.pick-all');
      head.checked = true;
      head.dispatchEvent(new Event('change', {bubbles: true}));
    },
    compare() { q('.compare-btn').click(); },
    close() { q('.compare-close').click(); },
    // --screenshot photographs the viewport at the origin and ignores scrolling, so a section
    // further down the page has to be moved to the top of <main> to be in the picture.
    hoist(sel) {
      const node = q(sel);
      if (node) q('main').prepend(node);
    },
    matrix() { return all('.compare .matrix td'); },
    diagonal() { return all('.compare .matrix td.self'); },
  };

  try {
    __STEP__
  } catch (error) {
    checks.push({label: 'driver error', actual: String(error), expected: '(none)', pass: false});
  }
  window.scrollTo(0, 0);  // see the scroll-behavior note above

  const band = document.createElement('div');
  band.id = 'demo-band';
  const tag = document.createElement('span');
  tag.className = 'id';
  tag.textContent = __ID__;
  const caption = document.createElement('span');
  caption.textContent = __CAPTION__;
  const verdict = document.createElement('span');
  const bad = checks.filter((c) => !c.pass);
  verdict.className = 'verdict ' + (bad.length ? 'bad' : 'good');
  verdict.textContent = bad.length
    ? 'FAIL  ' + bad[0].label
    : 'OK  ' + checks.length + ' checks';
  band.append(tag, caption, verdict);
  document.body.prepend(band);

  const sink = document.createElement('pre');
  sink.id = 'demo-checks';
  // display:none rather than [hidden]: an author `display` rule beats the UA sheet's
  // [hidden] { display: none } at any specificity, and this page has plenty of them.
  sink.style.display = 'none';
  sink.textContent = JSON.stringify(checks);
  document.body.append(sink);
})();
</script>
"""


@dataclasses.dataclass
class UiStep:
    id: str
    caption: str
    report: str  # key into Driver.reports
    body: str  # JavaScript, run against `ui`
    duration: float = 2.4
    strip_script: bool = False
    dark: bool = False


def ui_steps() -> list[UiStep]:
    """The report's own behaviour, as spec/cli.md describes it, one step per claim."""
    return [
        UiStep("UI-01", "The report opens with every skill listed and the search box focused",
               "public", """
        ui.ok('rows listed', ui.rows().length > 0, true);
        ui.ok('all rows visible', ui.shown().length, ui.rows().length);
        ui.ok('search box shown', ui.q('.search').hidden, false);
        ui.ok('search box focused', document.activeElement === ui.q('.search input'), true);
        ui.ok('compare button offered', ui.q('.compare-btn').hidden, false);
        ui.ok('nothing ticked yet, so it is disabled', ui.q('.compare-btn').disabled, true);
        """, duration=3.0),
        UiStep("UI-02", "Typing narrows the table keystroke by keystroke", "public", """
        // Taken from the first row rather than written in, so the step reads the same against
        // the public repo and against the fixture that stands in for it offline.
        ui.type(ui.needle().slice(0, 2));
        ui.ok('some rows left', ui.shown().length > 0, true);
        ui.ok('fewer than all', ui.shown().length < ui.rows().length, true);
        ui.ok('count keeps up', /\\d+ of \\d+ match/.test(ui.q('.search-status').textContent), true);
        """, duration=1.1),
        UiStep("UI-03", "...and the count beside the box says how many matched", "public", """
        const needle = ui.needle();
        ui.type(needle);
        const shown = ui.shown().length;
        ui.ok('something matched', shown > 0, true);
        ui.ok('every visible row matches the query',
              ui.shown().every((row) => row.dataset.name.toLowerCase().includes(needle)), true);
        ui.ok('status agrees with the table',
              ui.q('.search-status').textContent, shown + ' of ' + ui.rows().length + ' match');
        """),
        UiStep("UI-04", "A query that matches nothing says so, and echoes what was typed",
               "public", """
        ui.type('zzz-nothing-here');
        ui.ok('no rows left', ui.shown().length, 0);
        ui.ok('the message is shown', ui.q('.no-matches').hidden, false);
        ui.ok('it quotes the query', ui.q('.no-matches .query').textContent, 'zzz-nothing-here');
        """),
        UiStep("UI-05", "Escape clears the query and brings every row back", "public", """
        ui.type('zzz-nothing-here');
        ui.escape();
        ui.ok('box emptied', ui.q('.search input').value, '');
        ui.ok('every row back', ui.shown().length, ui.rows().length);
        ui.ok('message gone', ui.q('.no-matches').hidden, true);
        ui.ok('count cleared', ui.q('.search-status').textContent, '');
        """),
        UiStep("UI-06", "One skill ticked is not a comparison, so the button stays disabled",
               "public", """
        ui.pickAt([0]);
        ui.ok('one ticked', ui.q('.compare-btn').textContent, 'Compare similarity (1)');
        ui.ok('still disabled', ui.q('.compare-btn').disabled, true);
        ui.ok('no grid yet', ui.q('.compare').hidden, true);
        """),
        UiStep("UI-07", "Tick a second and Compare similarity draws the matrix", "public", """
        ui.pickAt([0, 1]);
        ui.ok('button enabled', ui.q('.compare-btn').disabled, false);
        ui.compare();
        ui.ok('grid shown', ui.q('.compare').hidden, false);
        ui.ok('2x2 of cells', ui.matrix().length, 4);
        ui.ok('the diagonal is blank', ui.diagonal().length, 2);
        ui.ok('a percentage in the rest',
              ui.matrix().filter((td) => /^\\d+%$/.test(td.textContent)).length, 2);
        ui.hoist('.compare');
        """, duration=3.0),
        UiStep("UI-08", "The matrix follows the selection: six skills, thirty scores", "public", """
        ui.pickAt([0, 1, 2, 3, 4, 5]);
        ui.compare();
        ui.ok('6x6 of cells', ui.matrix().length, 36);
        ui.ok('six blank diagonal cells', ui.diagonal().length, 6);
        ui.ok('thirty percentages',
              ui.matrix().filter((td) => /^\\d+%$/.test(td.textContent)).length, 30);
        ui.ok('every cell names both skills',
              ui.matrix().filter((td) => td.title.includes('\\u2194')).length, 30);
        ui.ok('cells carry a tint band',
              ui.matrix().filter((td) => td.dataset.heat !== undefined).length, 30);
        ui.hoist('.compare');
        """, duration=3.4),
        UiStep("UI-09", "Untick to one and the grid waits; tick again and it is back, unasked",
               "public", """
        ui.pickAt([0, 1, 2]);
        ui.compare();
        ui.unpickAt([1, 2]);
        ui.ok('a selection of one has nothing to show', ui.q('.compare').hidden, true);
        ui.pickAt([1]);
        ui.ok('back without pressing the button again', ui.q('.compare').hidden, false);
        ui.ok('2x2 of cells', ui.matrix().length, 4);
        ui.hoist('.compare');
        """),
        UiStep("UI-10", "The header checkbox ticks what the query left on screen", "public", """
        ui.type(ui.pluralNeedle());
        const visible = ui.shown().map((row) => row.dataset.name);
        ui.ok('the query left something to compare', visible.length > 1, true);
        ui.pickShown();
        ui.compare();
        ui.ok('ticked the matches', ui.q('.compare-btn').textContent,
              'Compare similarity (' + visible.length + ')');
        ui.ok('and only the matches', ui.matrix().length, visible.length * visible.length);
        ui.hoist('.compare');
        """),
        UiStep("UI-11", "A row the next query hides keeps its place in the comparison", "public", """
        ui.type(ui.pluralNeedle());
        const kept = ui.shown().map((row) => row.dataset.name);
        ui.ok('the query left something to compare', kept.length > 1, true);
        ui.pickShown();
        ui.compare();
        ui.type('zzz-nothing-here');
        ui.ok('the table is empty', ui.shown().length, 0);
        ui.ok('the comparison is not', ui.q('.compare').hidden, false);
        ui.ok('all of them still in the grid', ui.matrix().length, kept.length * kept.length);
        ui.hoist('.compare');
        """),
        UiStep("UI-12", "A name used twice gets its directory printed under it", "atlas", """
        ui.pick(['deploy', 'deploy-staging']);
        ui.compare();
        const labels = ui.all('.compare .matrix th');
        const withDir = labels.filter((th) => th.querySelector('small'));
        ui.ok('two skills are called deploy',
              ui.rows().filter((row) => row.dataset.name === 'deploy').length, 2);
        ui.ok('both copies of deploy are ticked', ui.q('.compare-btn').textContent,
              'Compare similarity (3)');
        ui.ok('the shared name carries a directory', withDir.length, 4);
        ui.ok('the unique name does not',
              withDir.every((th) => th.textContent.startsWith('deploy') &&
                                    !th.textContent.startsWith('deploy-staging')), true);
        ui.ok('the vendored pair scores 100%',
              ui.matrix().some((td) => td.textContent === '100%'), true);
        ui.hoist('.compare');
        """, duration=3.4),
        UiStep("UI-13", "Hide closes the matrix and leaves the list as it was", "public", """
        ui.pickAt([0, 1, 2]);
        ui.compare();
        ui.close();
        ui.ok('grid closed', ui.q('.compare').hidden, true);
        ui.ok('ticks kept', ui.q('.compare-btn').textContent, 'Compare similarity (3)');
        ui.ok('rows untouched', ui.shown().length, ui.rows().length);
        """),
        UiStep("UI-14", "Near-duplicates are grouped below the table, strongest first", "atlas", """
        const cards = ui.all('.similar .group');
        const scores = cards.map((card) => parseInt(card.querySelector('.score').textContent, 10));
        ui.ok('at least one group', cards.length > 0, true);
        ui.ok('the vendored copy scores 100%', scores.includes(100), true);
        ui.ok('strongest group first',
              scores.every((score, i) => i === 0 || scores[i - 1] >= score), true);
        ui.ok('every skill is still listed on its own', ui.rows().length, 8);
        ui.hoist('.similar');
        """, duration=3.2),
        UiStep("UI-15", "--no-similar: no groups, no comparison, the list untouched", "plain", """
        ui.ok('no comparison section', ui.q('.compare'), null);
        ui.ok('no compare button', ui.q('.compare-btn'), null);
        ui.ok('no checkbox column', ui.q('table.skills .pick'), null);
        ui.ok('the search box still works', ui.q('.search').hidden, false);
        ui.type('deploy');
        ui.ok('and still filters', ui.shown().length, 3);
        """),
        UiStep("UI-16", "Past 200 skills the comparison is dropped; the list is not", "bulk", """
        ui.ok('201 rows', ui.rows().length, 201);
        ui.ok('no comparison at this size', ui.q('.compare'), null);
        ui.ok('search survives the size', ui.q('.search').hidden, false);
        ui.type('skill-19');
        ui.ok('filtering 201 rows', ui.shown().length, 10);
        """),
        UiStep("UI-17", "A repo with no skills: a report that says so", "barren", """
        ui.ok('no table', ui.q('table.skills'), null);
        ui.ok('no search box for an empty list', ui.q('.search'), null);
        ui.ok('it says nothing was found',
              document.body.textContent.toLowerCase().includes('no skills'), true);
        """),
        UiStep("UI-18", "Without JavaScript: the whole table, and no dead controls", "public", """
        // `pickable` is added by the report's own script and is what reveals the checkbox
        // column, so its absence is the proof that none of that script ran.
        ui.ok('the report script did not run',
              ui.q('table.skills').classList.contains('pickable'), false);
        ui.ok('the table is all there', ui.rows().length > 0, true);
        ui.ok('every row is visible', ui.shown().length, ui.rows().length);
        ui.ok('the search box is hidden rather than dead', ui.q('.search').hidden, true);
        ui.ok('and it really is out of the picture',
              getComputedStyle(ui.q('.search')).display, 'none');
        ui.ok('the compare button is hidden too', ui.q('.compare-btn').hidden, true);
        """, strip_script=True),
        UiStep("UI-19", "The same report in dark mode", "public", """
        ui.ok('dark preferred', matchMedia('(prefers-color-scheme: dark)').matches, true);
        ui.ok('the page follows it',
              getComputedStyle(document.body).backgroundColor !== 'rgb(255, 255, 255)', true);
        ui.ok('rows still listed', ui.rows().length > 0, true);
        """, dark=True, duration=3.0),
    ]


def js_literal(text: str) -> str:
    return json.dumps(text)


#: What to film a step against when its first choice was never written. The public repo is the
#: better subject -- twenty real skills, real descriptions -- but it needs the network, so under
#: --offline the atlas fixture stands in. Every step that names `public` is written to assert
#: against whatever the report actually holds rather than against a count only that repo has.
UI_FALLBACK = {"public": "atlas"}


def build_ui_frames(steps: list[UiStep], reports: dict[str, Path]) -> list[tuple[UiStep, Frame]]:
    pairs: list[tuple[UiStep, Frame]] = []
    for step in steps:
        source = reports.get(step.report)
        if source is None or not source.exists():
            stand_in = UI_FALLBACK.get(step.report)
            source = reports.get(stand_in) if stand_in else None
            if source is None or not source.exists():
                continue
            step = dataclasses.replace(step, report=stand_in)  # so the transcript says which
        html = source.read_text(encoding="utf-8")
        if step.strip_script:
            # Strip the report's script rather than switching scripting off:
            # --blink-settings=scriptEnabled=false also breaks --screenshot, and the driver
            # itself still has to run to assert against the bare markup it leaves behind.
            html = re.sub(r"<script>.*?</script>", "", html, flags=re.DOTALL)
        driver = (
            DRIVER_JS.replace("__STEP__", step.body)
            .replace("__ID__", js_literal(step.id))
            .replace("__CAPTION__", js_literal(step.caption))
        )
        html = html.replace("</body>", driver + "\n</body>")
        extra = ("--blink-settings=preferredColorScheme=0",) if step.dark else ()
        pairs.append(
            (step, Frame(step.id, html, step.duration, extra_args=extra, wants_dom=True))
        )
    return pairs


def read_checks(dom: str) -> list[Check]:
    match = re.search(r'<pre id="demo-checks"[^>]*>(.*?)</pre>', dom, flags=re.DOTALL)
    if not match:
        return [Check("driver reported back", "no #demo-checks in the DOM", "checks", False)]
    try:
        # The DOM dump is markup, so the JSON comes back with its quotes as &quot;.
        payload = json.loads(unescape(match.group(1)))
    except json.JSONDecodeError as bad:
        return [Check("driver reported back", f"unparsable: {bad}", "JSON", False)]
    if not payload:
        return [Check("driver asserted something", "no checks", "at least one", False)]
    return [
        Check(item["label"], item["actual"], item["expected"], bool(item["pass"]))
        for item in payload
    ]


# --------------------------------------------------------------------------- storyboard


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title><style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; width: {w}px; height: {h}px; background: #06080f; color: #eaf0fb;
    font: 400 16px/1.5 "Segoe UI", system-ui, sans-serif; overflow: hidden; }}
  .band {{ display: flex; align-items: baseline; gap: 16px; background: #0b1220;
    padding: 15px 26px; font-weight: 600; font-size: 18px; }}
  .band .id {{ background: #2563eb; color: #fff; border-radius: 999px; padding: 3px 12px;
    font-size: 14px; flex: none; }}
  .band .verdict {{ margin-left: auto; font-size: 14px; font-weight: 500; flex: none;
    font-family: Consolas, "Cascadia Mono", monospace; }}
  .good {{ color: #8ee6a8; }} .bad {{ color: #ff9a9a; }}
  main {{ padding: 24px 30px; }}
  .prompt {{ font: 600 18px/1.5 Consolas, "Cascadia Mono", monospace; color: #cfe0ff;
    white-space: pre-wrap; word-break: break-all; }}
  .prompt .sigil {{ color: #6ee7a8; }}
  .caret {{ display: inline-block; width: 10px; height: 19px; background: #cfe0ff;
    vertical-align: -3px; }}
  /* Output on the left, the assertions that judged it on the right: the frame has the width
     for both, and dropping either one costs the picture its point. */
  .split {{ display: flex; gap: 28px; margin-top: 18px; align-items: flex-start; }}
  .split > table.tally {{ flex: 1; min-width: 0; }}
  pre.out {{ flex: 1; min-width: 0; margin: 0;
    font: 400 14px/1.45 Consolas, "Cascadia Mono", monospace;
    color: #b9c6dd; white-space: pre-wrap; word-break: break-word; }}
  .side {{ flex: none; width: 352px; }}
  .took {{ font-size: 13px; color: #6f7f99; margin-bottom: 8px;
    font-family: Consolas, "Cascadia Mono", monospace; }}
  .title {{ display: flex; flex-direction: column; justify-content: center; height: {h}px;
    padding: 0 90px; }}
  .title h1 {{ margin: 0; font-size: 62px; letter-spacing: -.02em; }}
  .title p {{ margin: 18px 0 0; font-size: 23px; color: #9fb0cc; max-width: 62ch; }}
  .title .meta {{ margin-top: 42px; font: 400 16px/1.9 Consolas, "Cascadia Mono", monospace;
    color: #7f8fa8; }}
  table.tally {{ border-collapse: collapse; font-size: 13.5px; width: 100%; }}
  table.tally td {{ padding: 2px 12px 2px 0; vertical-align: top;
    font-family: Consolas, "Cascadia Mono", monospace; }}
  table.tally td.id {{ color: #7f8fa8; }}
  table.tally td.what {{ color: #cfe0ff; font-family: inherit; }}
  table.tally td.mark {{ white-space: nowrap; }}
  main.wide table.tally {{ font-size: 15px; }}
  main.wide table.tally td.id {{ white-space: nowrap; }}
</style></head><body>{body}</body></html>
"""


def page(title: str, body: str) -> str:
    return PAGE.format(title=escape(title), body=body, w=WIDTH, h=HEIGHT)


def band(sid: str, caption: str, verdict: str = "") -> str:
    mark = ""
    if verdict:
        mark = f'<span class="verdict {"bad" if verdict.startswith("FAIL") else "good"}">{escape(verdict)}</span>'
    return f'<div class="band"><span class="id">{escape(sid)}</span><span>{escape(caption)}</span>{mark}</div>'


def clip(text: str, lines: int = 26, width: int = 118) -> str:
    kept: list[str] = []
    for raw in text.replace("\r", "").split("\n"):
        kept.append(raw if len(raw) <= width else raw[: width - 1] + "\u2026")
        if len(kept) >= lines:
            kept.append("\u2026")
            break
    return "\n".join(kept).rstrip()


def title_frame(repo: str | None, project: Path) -> Frame:
    body = f"""<div class="title">
      <h1>skill&#8209;atlas, end to end</h1>
      <p>Every user scenario in <code>spec/cli.md</code>, run for real against real
      repositories, with the assertion that decides each one shown beside it.</p>
      <div class="meta">checkout &nbsp;{escape(str(project))}<br>
      network repo &nbsp;{escape(repo or "(skipped: --offline)")}<br>
      local fixtures &nbsp;atlas &middot; barren &middot; bulk (201 skills)</div>
    </div>"""
    return Frame("TITLE", page("skill-atlas demo", body), 4.2)


def terminal_frames(step: Scenario) -> list[Frame]:
    """Two stills per command: the line as typed, then what it printed.

    The half-second typed frame is the only theatre in here, and it earns its place -- without
    it a reader has to find the command inside a screen of output that arrives in the same
    instant it does.
    """
    typed = escape(step.detail)
    frames = [
        Frame(
            f"{step.id}-cmd",
            page(step.id, band(step.id, step.caption)
                 + f'<main><div class="prompt"><span class="sigil">$</span> {typed}'
                   f'<span class="caret"></span></div></main>'),
            0.85,
        )
    ]
    took = f"{step.seconds:.1f}s" if step.seconds else ""
    checks = "".join(
        f'<tr><td class="mark {"good" if c.pass_ else "bad"}">{"PASS" if c.pass_ else "FAIL"}</td>'
        f'<td class="what">{escape(c.label)}'
        + ("" if c.pass_ else f' &mdash; got {escape(c.actual)}, wanted {escape(c.expected)}')
        + "</td></tr>"
        for c in step.checks
    )
    frames.append(
        Frame(
            f"{step.id}-out",
            page(step.id, band(step.id, step.caption, step.verdict)
                 + f'<main><div class="prompt"><span class="sigil">$</span> {typed}</div>'
                   f'<div class="split"><pre class="out">{escape(clip(step.output, 34, 100))}</pre>'
                   f'<div class="side"><div class="took">{escape(took)}</div>'
                   f'<table class="tally">{checks}</table></div></div></main>'),
            3.1,
        )
    )
    return frames


def summary_frame(steps: list[Scenario]) -> Frame:
    passed = sum(1 for s in steps if s.ok)
    total_checks = sum(len(s.checks) for s in steps)

    def table(some: list[Scenario]) -> str:
        rows = "".join(
            f'<tr><td class="id">{escape(s.id)}</td>'
            f'<td class="what">{escape(s.caption)}</td>'
            f'<td class="mark {"good" if s.ok else "bad"}">{"PASS" if s.ok else "FAIL"}</td></tr>'
            for s in some
        )
        return f'<table class="tally">{rows}</table>'

    # Two columns: a single list of every scenario is taller than the frame.
    half = (len(steps) + 1) // 2
    heading = f"{passed} of {len(steps)} scenarios green &middot; {total_checks} assertions"
    body = (
        band("SUMMARY", "Every scenario, and how it went",
             "OK" if passed == len(steps) else f"FAIL  {len(steps) - passed} red")
        + f'<main class="wide"><div class="took" style="font-size:17px;color:#cfe0ff">{heading}</div>'
          f'<div class="split">{table(steps[:half])}{table(steps[half:])}</div></main>'
    )
    return Frame("SUMMARY", page("summary", body), 6.5)


# --------------------------------------------------------------------------- capture


def capture(
    frames: list[Frame], edge: str, work: Path, profiles: Path, workers: int, quiet: bool,
    offset: int = 0,
) -> None:
    """Render every frame. One Edge launch per frame, a few at a time.

    The launch both screenshots and dumps the DOM, so the assertions a frame reports and the
    picture it shows come out of the same render -- a frame that looks right can no longer be
    paired with checks from some other state of the page.
    """
    work.mkdir(parents=True, exist_ok=True)
    # One profile per worker, not per frame: Edge will not share a --user-data-dir between
    # concurrent launches, and building a fresh profile for every frame costs more than the
    # screenshot does. They live in the system temp directory and not under the output, because
    # Edge's crash handler outlives the browser by a few seconds and holds its profile open --
    # inside the output directory that turns into the *next* run failing to clear it.
    free: queue.SimpleQueue[Path] = queue.SimpleQueue()
    for i in range(workers):
        free.put(profiles / f"profile-{i}")

    def take(index: int, frame: Frame) -> None:
        profile = free.get()
        try:
            stem = f"frame-{index + offset:03d}-{re.sub(r'[^A-Za-z0-9-]', '_', frame.name)}"
            source = work / f"{stem}.html"
            source.write_text(frame.html, encoding="utf-8")
            png = work / f"{stem}.png"
            argv = [
                edge,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-component-update",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--user-data-dir={profile}",
                f"--window-size={WIDTH},{HEIGHT}",
                "--virtual-time-budget=5000",
                f"--screenshot={png}",
                *(("--dump-dom",) if frame.wants_dom else ()),
                *frame.extra_args,
                source.as_uri(),
            ]
            done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=180)
            if png.exists():
                frame.png = png
            frame.dom = done.stdout or ""
            if not quiet:
                mark = "ok " if png.exists() else "MISSING"
                print(f"  [{index + 1:>2}/{len(frames)}] {mark} {frame.name}", flush=True)
        finally:
            free.put(profile)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda pair: take(*pair), enumerate(frames)))


# --------------------------------------------------------------------------- video


def assemble(
    frames: list[Frame], out: Path, work: Path, ffmpeg: str | None, fps: int
) -> tuple[Path | None, str]:
    shots = [f for f in frames if f.png]
    if not shots:
        return None, "no frames were captured"
    if ffmpeg:
        listing = work / "frames.txt"  # scratch, so it goes with the rest of the scratch
        lines: list[str] = []
        for frame in shots:
            lines.append(f"file '{frame.png.as_posix()}'")
            lines.append(f"duration {frame.duration:.2f}")
        lines.append(f"file '{shots[-1].png.as_posix()}'")  # concat needs the last one twice
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        argv = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-vf", f"fps={fps},format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-movflags", "+faststart",
            str(out),
        ]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
        if done.returncode == 0 and out.exists():
            return out, f"{len(shots)} frames, {sum(f.duration for f in shots):.0f}s"
        print(f"record_demo: ffmpeg failed ({done.returncode}): {done.stderr.strip()[:400]}",
              file=sys.stderr)
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return None, ("no ffmpeg and no Pillow: the frames are on disk, assemble them with "
                      "`ffmpeg -f concat -safe 0 -i frames.txt -vf fps=30,format=yuv420p out.mp4`")
    gif = out.with_suffix(".gif")
    pictures = [Image.open(f.png).convert("P", palette=Image.ADAPTIVE, colors=128) for f in shots]
    pictures[0].save(
        gif, save_all=True, append_images=pictures[1:], loop=0, optimize=True,
        duration=[int(f.duration * 1000) for f in shots],
    )
    return gif, f"{len(shots)} frames, GIF fallback (no ffmpeg)"


# --------------------------------------------------------------------------- transcript


def write_transcript(path: Path, steps: list[Scenario], video: Path | None, note: str) -> None:
    passed = sum(1 for s in steps if s.ok)
    lines = [
        "# skill-atlas demo run",
        "",
        f"{passed} of {len(steps)} scenarios green, "
        f"{sum(len(s.checks) for s in steps)} assertions.",
        "",
        f"Video: `{video}`  ({note})" if video else f"No video: {note}",
        "",
        "| Scenario | What it proves | Checks | Verdict |",
        "|---|---|---|---|",
    ]
    for step in steps:
        lines.append(
            f"| {step.id} | {step.caption} | {len(step.checks)} | "
            f"{'PASS' if step.ok else 'FAIL'} |"
        )
    for step in steps:
        lines += ["", f"## {step.id} — {step.caption}", ""]
        if step.detail:
            lines += ["```", step.detail, "```", ""]
        for check in step.checks:
            got = check.actual if check.pass_ else f"{check.actual} (expected {check.expected})"
            lines.append(f"- {'PASS' if check.pass_ else 'FAIL'} {check.label}: {got}")
        if step.output.strip():
            lines += ["", "<details><summary>output</summary>", "", "```",
                      clip(step.output, lines=80, width=200), "```", "", "</details>"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="record_demo.py",
        description="Run skill-atlas through every scenario in spec/cli.md and film the run.",
    )
    here = Path(__file__).resolve()
    parser.add_argument("--project", type=Path, default=here.parents[4],
                        help="checkout to run (default: the one this script lives in)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where the video and transcript go (default: <project>/demo-run)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="scratch directory for fixtures, reports and frames "
                             "(default: a fresh temp directory, deleted afterwards)")
    parser.add_argument("--repo", default="https://github.com/anthropics/skills.git",
                        help="public repository for the over-the-network scenario")
    parser.add_argument("--offline", action="store_true",
                        help="skip the network scenario and use the local fixtures only")
    parser.add_argument("--edge", default=None, help="path to msedge.exe")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--workers", type=int, default=4, help="concurrent Edge launches")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per CLI scenario")
    parser.add_argument("--keep", action="store_true",
                        help="leave the scratch directory in place for debugging instead of "
                             "deleting it once the video exists")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    project = args.project.resolve()
    if not (project / "pyproject.toml").exists():
        die(f"{project} does not look like the skill-atlas checkout (no pyproject.toml)")
    if not shutil.which("uv"):
        die("uv is not on PATH; the app runs through `uv run`")
    if not shutil.which("git"):
        die("git is not on PATH")
    edge = find_edge(args.edge)

    # Everything intermediate -- the fixture repositories (each with its own .git), the
    # reports, the frames, the browser profiles -- lives outside the checkout in a temp
    # directory, so none of it is ever visible to git even while the run is in progress. It is
    # deleted once the video exists; --keep is the debugging escape hatch.
    #
    # Settled before anything is deleted below, so a rejected --work-dir costs the caller a
    # message rather than last run's video.
    out = (args.out_dir or project / "demo-run").resolve()
    if args.work_dir:
        work = args.work_dir.resolve()
        # Deleted wholesale at the end, so it has to be ours: an existing directory with
        # anything in it is refused rather than emptied.
        if work.exists() and any(work.iterdir()):
            die(f"--work-dir {work} is not empty; pass a path that does not exist yet")
        if out.is_relative_to(work):
            die(f"--out-dir {out} is inside --work-dir {work}, which is deleted after the run")
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="skill-atlas-demo-"))

    # A stale demo.mp4 sitting beside a failed run would read as the film of that run, so the
    # last one goes first. By name and not by emptying the directory: --out-dir can point
    # anywhere, and a script that rm -rf's whatever it is handed is a bad thing to have around.
    # The scratch names are here too, so an output directory left over from an older version of
    # this script -- when the frames and fixtures were written next to the video -- gets tidied
    # up rather than lingering.
    for stale in ("demo.mp4", "demo.gif", "transcript.md", "frames.txt", "record_open.py",
                  "browser-opened.txt", "frames", "fixtures", "reports", "profiles",
                  ".edge-profiles"):
        wipe(out / stale)
    out.mkdir(parents=True, exist_ok=True)
    check_ignored(out, project)

    # flush: stdout is block-buffered when it is redirected to a file, which is how an agent
    # runs this -- without it the whole progress report arrives at once, after the event.
    def say(line: str = "") -> None:
        if not args.quiet:
            print(line, flush=True)

    say(f"Filming {project}")
    say(f"  video     {out}")
    say(f"  scratch   {work}{'' if args.keep else '  (deleted after the video)'}")
    say(f"  edge      {edge}")

    say("Building local fixture repositories...")
    fixtures = build_fixtures(work / "fixtures")

    driver = Driver(project, work, args.timeout)
    repo_url = None if args.offline else args.repo
    say("Running the CLI scenarios...")
    steps = cli_scenarios(driver, fixtures, repo_url)
    for step in steps:
        say(f"  {step.id}  {'PASS' if step.ok else 'FAIL'}  {step.caption}")
        if not step.ok:
            for bad in (c for c in step.checks if not c.pass_):
                say(f"        {bad.label}: {bad.actual}  (expected {bad.expected})")

    say("Driving the report in Edge...")
    pairs = build_ui_frames(ui_steps(), driver.reports)
    missing = {s.id for s in ui_steps()} - {step.id for step, _ in pairs}
    frames: list[Frame] = [title_frame(repo_url, project)]
    for step in steps:
        frames.extend(terminal_frames(step))
    frames.extend(frame for _, frame in pairs)
    capture(frames, edge, work / "frames", work / "profiles", max(1, args.workers), args.quiet)

    for step, frame in pairs:
        ui_step = Scenario(step.id, step.caption, f"report UI — {step.report}.html")
        ui_step.checks = read_checks(frame.dom)
        if frame.png is None:
            ui_step.checks.append(Check("frame captured", "no png", "a png", False))
        ui_step.output = "\n".join(
            f"{'PASS' if c.pass_ else 'FAIL'}  {c.label}: {c.actual}" for c in ui_step.checks
        )
        steps.append(ui_step)
        say(f"  {ui_step.id}  {'PASS' if ui_step.ok else 'FAIL'}  {ui_step.caption}")
        if not ui_step.ok:
            for bad in (c for c in ui_step.checks if not c.pass_):
                say(f"        {bad.label}: {bad.actual}  (expected {bad.expected})")
    for skipped in sorted(missing):
        ghost = Scenario(skipped, "skipped: its report was never written")
        ghost.checks = [Check("report available", "missing", "written", False)]
        steps.append(ghost)

    tail = summary_frame(steps)  # last, because it tallies up everything above
    capture([tail], edge, work / "frames", work / "profiles", 1, args.quiet, offset=len(frames))
    frames.append(tail)

    say("Assembling the video...")
    ffmpeg = find_ffmpeg()
    video, note = assemble(frames, out / "demo.mp4", work, ffmpeg, args.fps)
    transcript = out / "transcript.md"
    write_transcript(transcript, steps, video, note)

    if args.keep:
        say(f"Scratch kept at {work}")
    else:
        # The frames have been encoded and the reports have been asserted on, so all that is
        # left here is tens of MB of PNGs, fixture repositories and browser profile. Not
        # strict: this is a temp directory, so a file Edge's crash handler is still holding
        # costs a leftover in %TEMP% rather than a wrong answer, and saying so beats dying
        # after a green run.
        leftover = wipe(work, strict=False)
        say("Scratch deleted." if leftover is None else f"Scratch left behind: {leftover}")

    passed = sum(1 for s in steps if s.ok)
    say("")
    say(f"{passed}/{len(steps)} scenarios green, {sum(len(s.checks) for s in steps)} assertions")
    say(f"video      {video or '(none: ' + note + ')'}")
    say(f"transcript {transcript}")
    if passed != len(steps):
        say("")
        say("Red scenarios:")
        for step in steps:
            if not step.ok:
                say(f"  {step.id}  {step.caption}")
                for bad in (c for c in step.checks if not c.pass_):
                    say(f"      {bad.label}: {bad.actual}  (expected {bad.expected})")
    return 0 if passed == len(steps) and video else 1


if __name__ == "__main__":
    raise SystemExit(main())
