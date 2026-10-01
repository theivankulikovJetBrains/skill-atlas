---
name: review
description: Basic review rules for changes to skill-atlas.
---

# Review rules

Check the diff against these. Report what fails; do not fix it here.

1. **The spec moved with the behaviour.** Any change to flags, detection rules, output or
   exit codes must appear in `spec/cli.md` in the same change. Code and spec disagreeing is
   a finding.
2. **Tests cover the change, and they are green.** `uv run pytest` passes. New behaviour has
   a test; a loosened, skipped or deleted test is a finding, not a fix.
3. **Tests stay offline and off the desktop.** No network (local `file://` repos instead),
   no real browser tab, no writing to the repo's own `report.html`.
4. **Layering holds.** Data flows `repo` → `scanner` → `similarity` → `models` → `report` →
   `cli`. Nothing below `cli.py` prints; nothing outside `repo.py` reaches the network.
5. **Comments say why, not what.** A non-obvious choice without its reason is a finding —
   the next reader will simplify it away.
6. **Style matches the neighbours.** `from __future__ import annotations`, full type hints on
   public functions, stdlib before a new dependency, lines to ~110 characters.
7. **Failures are values.** Malformed input becomes a `Skill` with `issues`; only fetch and
   write failures raise or set a non-zero exit code.
8. **Nothing generated is committed.** No `report.html`, `demo-run/`, `test-shots/`, `dist/`,
   `__pycache__/`, `.venv/`, and no unrelated `.idea/workspace.xml` noise.
