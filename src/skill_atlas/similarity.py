"""Group skills that read as near-duplicates of one another.

Repositories accumulate copies: one skill vendored under both ``.agents/`` and
``.claude/``, a fork renamed but not rewritten, two teams solving the same problem
twice. The scanner reports every one of them separately -- correctly, because the
copies drift and the drift is the finding -- so this module answers the question
that comes next: which of those entries are the same skill wearing different names?

The comparison reads only the two fields the scanner already has, the frontmatter
``name`` and ``description``, not the SKILL.md body. That keeps the sparse checkout
in ``repo.py`` worth having (no extra bytes are fetched to answer this) and keeps
the score focused on what a skill claims to do rather than on the boilerplate that
instructions share.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher
from typing import NamedTuple

from .models import SimilarGroup, Skill

#: How the two halves of a score are weighed. The description carries more because a
#: name is short enough for unrelated skills to collide on ("pdf-split" vs "pdf-sign"),
#: while two descriptions agreeing on their content words rarely happens by accident.
NAME_WEIGHT = 0.4
DESCRIPTION_WEIGHT = 0.6

#: Calibrated against a real skill collection rather than picked for roundness. Across
#: one, the highest-scoring pair that is merely on a shared topic ("summarize-channel"
#: and "standup") reached 0.42, and the lowest-scoring pair that really was one skill
#: twice ("code-review" and "security-review") reached 0.49; this is the middle of that
#: gap. Move it with ``--similarity``: the gap is a property of the repository, not a
#: constant, and a collection with a tighter house style will want a higher bar.
DEFAULT_THRESHOLD = 0.45

#: Words that say nothing about what a skill does. Descriptions follow a house style
#: ("Use this skill whenever the user wants to ..."), so without this every pair would
#: share a floor of boilerplate and the scores would all bunch up in the middle.
_STOPWORDS = frozenset(
    """
    a an and any are as at be been but by can do does for from has have how if in into is it
    its no not of on or should skill so such than that the their them then there these they
    this those to up use used user uses using want when whenever which while with within you
    your
    """.split()
)

#: Word characters only, so punctuation splits tokens and ``_`` does not glue them.
_TOKEN_RE = re.compile(r"[^\W_]+")
#: The separators skill names are built from; all of them read as a space here.
_SEPARATOR_RE = re.compile(r"[-_./\\]+")


class _Prepared(NamedTuple):
    """A skill with the two comparable forms of it derived once, not per pair."""

    skill: Skill
    name: str
    tokens: frozenset[str]


def _prepare(skill: Skill) -> _Prepared:
    return _Prepared(skill, _fold_name(skill.name), _tokens(skill.description))


def _fold_name(name: str) -> str:
    """Reduce a name to comparable text, so ``pdf-filler`` and ``PDF Filler`` are one name."""
    return " ".join(_SEPARATOR_RE.sub(" ", name).casefold().split())


def _tokens(description: str) -> frozenset[str]:
    """The content words of a description: lowercased, deduplicated, boilerplate dropped."""
    words = _TOKEN_RE.findall(description.casefold())
    return frozenset(word for word in words if len(word) > 1 and word not in _STOPWORDS)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Overlap of two token sets, as the share of all words seen that both use."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _similarity(a: _Prepared, b: _Prepared) -> float:
    if not (a.tokens and b.tokens):
        # One side has nothing to compare descriptions with. Scoring the pair as
        # partly mismatched would hide exactly the duplicates a missing description
        # marks -- a vendored copy that kept the name and lost the frontmatter -- so
        # the name carries the whole judgement instead.
        return _name_ratio(a.name, b.name)
    return NAME_WEIGHT * _name_ratio(a.name, b.name) + DESCRIPTION_WEIGHT * _jaccard(a.tokens, b.tokens)


def _name_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _ceiling(a: _Prepared, b: _Prepared) -> float:
    """The highest score this pair could reach, found without matching the names.

    The sweep below is quadratic and spends nearly all of its time in SequenceMatcher;
    intersecting two small sets costs almost nothing. Once the descriptions are known
    to barely overlap, no name however identical can lift the pair over a threshold
    above ``NAME_WEIGHT``, so that pair never needs the expensive half.
    """
    if not (a.tokens and b.tokens):
        return 1.0  # judged on names alone, which this shortcut cannot bound
    return NAME_WEIGHT + DESCRIPTION_WEIGHT * _jaccard(a.tokens, b.tokens)


def similarity(a: Skill, b: Skill) -> float:
    """How alike two skills read, from ``0.0`` (nothing in common) to ``1.0`` (identical)."""
    return _similarity(_prepare(a), _prepare(b))


def find_similar(skills: Sequence[Skill], threshold: float = DEFAULT_THRESHOLD) -> list[SimilarGroup]:
    """Cluster ``skills`` that score at least ``threshold`` against one another.

    Groups are connected components, not cliques: if A resembles B and B resembles C,
    all three land together even when A and C were never a match themselves. That is
    the useful behaviour for near-duplicates, where the point is to put every copy of
    one skill in front of the reader at once rather than to prove each pairing.

    Returns the groups of two or more, highest score first; a skill resembling nothing
    else is simply absent. ``score`` is the strongest link inside the group.
    """
    prepared = [_prepare(skill) for skill in skills]
    parents = list(range(len(prepared)))
    best = [0.0] * len(prepared)  # per set root: the strongest link found inside it

    for i, left in enumerate(prepared):
        for j in range(i + 1, len(prepared)):
            right = prepared[j]
            if _ceiling(left, right) < threshold:
                continue
            score = _similarity(left, right)
            if score < threshold:
                continue
            _union(parents, best, i, j, score)

    return _collect(prepared, parents, best)


def _root(parents: list[int], index: int) -> int:
    root = index
    while parents[root] != root:
        root = parents[root]
    while parents[index] != root:  # path compression, so a long chain is walked once
        parents[index], index = root, parents[index]
    return root


def _union(parents: list[int], best: list[float], i: int, j: int, score: float) -> None:
    left, right = _root(parents, i), _root(parents, j)
    if left == right:
        best[left] = max(best[left], score)
        return
    parents[right] = left
    best[left] = max(best[left], best[right], score)


def _collect(prepared: Iterable[_Prepared], parents: list[int], best: list[float]) -> list[SimilarGroup]:
    members: dict[int, list[Skill]] = {}
    for index, item in enumerate(prepared):
        members.setdefault(_root(parents, index), []).append(item.skill)

    groups = [SimilarGroup(tuple(skills), best[root]) for root, skills in members.items() if len(skills) > 1]
    # Strongest first, then by path so two equally-scored groups do not swap places
    # between runs -- a report diffed against yesterday's should show real changes only.
    groups.sort(key=lambda group: (-group.score, group.skills[0].path))
    return groups
