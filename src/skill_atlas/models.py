"""Data types shared across the scanner, reporter and CLI."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Skill:
    """A single Claude Agent Skill discovered in a repository.

    ``name`` and ``description`` come from the SKILL.md YAML frontmatter. When the
    frontmatter is missing or unusable we still report the skill, fall back to the
    containing directory name, and record why in ``issues``.
    """

    name: str
    description: str
    path: str
    issues: tuple[str, ...] = ()

    @property
    def directory(self) -> str:
        """Repo-relative directory holding the SKILL.md, or ``.`` at the root."""
        parent, _, _ = self.path.rpartition("/")
        return parent or "."


@dataclass(frozen=True)
class SimilarGroup:
    """Skills that read as near-duplicates of one another.

    Produced by :mod:`skill_atlas.similarity`. ``score`` is the strongest pairwise
    resemblance inside the group, on the same ``0.0``-``1.0`` scale; the members keep
    the order they were scanned in, which is by path.
    """

    skills: tuple[Skill, ...]
    score: float

    @property
    def percent(self) -> int:
        """``score`` as whole percent, which is how both reports show it."""
        return round(self.score * 100)

    @property
    def names(self) -> tuple[str, ...]:
        """The distinct names in the group -- often just one, when a skill was copied."""
        return tuple(dict.fromkeys(skill.name for skill in self.skills))


@dataclass
class ScanResult:
    """Everything one ``skill-atlas scan`` run produced."""

    source: str
    scanned_at: str
    skills: list[Skill] = field(default_factory=list)
    commit: str | None = None
    ref: str | None = None
    web_base_url: str | None = None
    similar: list[SimilarGroup] = field(default_factory=list)

    @property
    def skill_count(self) -> int:
        return len(self.skills)

    @property
    def similar_count(self) -> int:
        return len(self.similar)

    def web_url_for(self, skill: Skill) -> str | None:
        """Browsable URL for a skill file, when the host layout is known."""
        if not self.web_base_url:
            return None
        rev = self.commit or self.ref or "HEAD"
        return f"{self.web_base_url}/blob/{rev}/{skill.path}"
