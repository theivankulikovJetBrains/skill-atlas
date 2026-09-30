from __future__ import annotations

import pytest

from skill_atlas.models import Skill
from skill_atlas.similarity import DEFAULT_THRESHOLD, MAX_COMPARABLE, find_similar, pairwise, similarity


def skill(name: str, description: str = "", path: str | None = None) -> Skill:
    return Skill(name, description, path or f"{name}/SKILL.md")


def paths(groups) -> list[list[str]]:
    return [[s.path for s in group.skills] for group in groups]


class TestScore:
    def test_a_skill_is_identical_to_itself(self):
        one = skill("pdf-filler", "Fills in PDF forms from structured data.")
        assert similarity(one, one) == 1.0

    def test_unrelated_skills_score_near_zero(self):
        a = skill("pdf-filler", "Fills in PDF forms from structured data.")
        b = skill("slack-digest", "Summarise recent chatter in a workspace channel.")
        assert similarity(a, b) < 0.2

    def test_the_score_does_not_depend_on_the_order_of_the_pair(self):
        a = skill("pdf-filler", "Fills in PDF forms.")
        b = skill("pdf-fill", "Fills PDF forms in.")
        assert similarity(a, b) == similarity(b, a)

    def test_punctuation_and_case_in_a_name_are_not_a_difference(self):
        a = skill("pdf-filler", "")
        b = skill("PDF_Filler", "")
        assert similarity(a, b) == 1.0

    def test_a_reworded_description_still_scores_high(self):
        a = skill("deploy", "Deploy the service to staging and then to production.")
        b = skill("ship-it", "Deploy a service to the staging environment, then production.")
        assert similarity(a, b) >= DEFAULT_THRESHOLD

    def test_boilerplate_alone_does_not_make_two_skills_alike(self):
        """Every skill in a house style opens the same way; that must not count."""
        a = skill("alpha", "Use this skill whenever you want to resize an image.")
        b = skill("bravo", "Use this skill whenever you want to file a ticket.")
        assert similarity(a, b) < DEFAULT_THRESHOLD

    def test_a_shared_topic_is_not_a_duplicate(self):
        a = skill("slack-search", "Search Slack messages, files and channels.")
        b = skill("slack-messaging", "Compose well-formatted Slack messages in markdown.")
        assert similarity(a, b) < DEFAULT_THRESHOLD

    def test_a_missing_description_leaves_the_name_to_decide(self):
        """A vendored copy that lost its frontmatter is the duplicate, not an outlier."""
        a = skill("deploy", "Deploy the service to production.")
        b = skill("deploy", "")
        assert similarity(a, b) == 1.0

    def test_two_nameless_descriptions_are_compared_on_names_too(self):
        assert similarity(skill("alpha", ""), skill("beta", "")) < DEFAULT_THRESHOLD

    def test_non_ascii_descriptions_are_compared_not_discarded(self):
        a = skill("rechnung", "Erstellt eine Rechnung für den Kunden.")
        b = skill("rechnung-neu", "Erstellt eine Rechnung für den Kunden.")
        assert a.description  # guard: the fixture is the point of the test
        assert similarity(a, b) >= DEFAULT_THRESHOLD


class TestGrouping:
    def test_nothing_alike_produces_no_groups(self):
        skills = [
            skill("pdf-filler", "Fills in PDF forms."),
            skill("slack-digest", "Summarises a channel."),
            skill("docker-build", "Builds a container image."),
        ]
        assert find_similar(skills) == []

    def test_a_copy_of_one_skill_is_grouped_with_its_original(self):
        skills = [
            skill("deploy", "Deploy the service.", ".agents/skills/deploy/SKILL.md"),
            skill("deploy", "Deploy the service.", ".claude/skills/deploy/SKILL.md"),
        ]
        assert paths(find_similar(skills)) == [
            [".agents/skills/deploy/SKILL.md", ".claude/skills/deploy/SKILL.md"]
        ]

    def test_a_group_reports_its_strongest_link_as_a_percentage(self):
        skills = [
            skill("deploy", "Deploy the service.", "a/SKILL.md"),
            skill("deploy", "Deploy the service.", "b/SKILL.md"),
        ]
        (group,) = find_similar(skills)
        assert group.score == 1.0
        assert group.percent == 100

    def test_unrelated_skills_are_left_out_of_the_groups_entirely(self):
        skills = [
            skill("deploy", "Deploy the service.", "a/SKILL.md"),
            skill("unrelated", "Resizes images on disk.", "b/SKILL.md"),
            skill("deploy", "Deploy the service.", "c/SKILL.md"),
        ]
        assert paths(find_similar(skills)) == [["a/SKILL.md", "c/SKILL.md"]]

    def test_three_copies_land_in_one_group_not_three_pairs(self):
        skills = [skill("deploy", "Deploy the service.", f"{n}/SKILL.md") for n in "abc"]
        assert paths(find_similar(skills)) == [["a/SKILL.md", "b/SKILL.md", "c/SKILL.md"]]

    def test_resemblance_chains_through_a_shared_member(self):
        """A and C never matched each other, but both matched B, so all three group."""
        skills = [
            skill("report-builder", "Builds a PDF report from a spreadsheet.", "a/SKILL.md"),
            skill("report-builder", "Builds a PDF report from a spreadsheet of sales.", "b/SKILL.md"),
            skill("report-builder", "Builds a report of sales.", "c/SKILL.md"),
        ]
        assert paths(find_similar(skills)) == [["a/SKILL.md", "b/SKILL.md", "c/SKILL.md"]]

    def test_members_keep_the_scanned_order(self):
        skills = [
            skill("deploy", "Deploy the service.", "z/SKILL.md"),
            skill("deploy", "Deploy the service.", "a/SKILL.md"),
        ]
        assert paths(find_similar(skills)) == [["z/SKILL.md", "a/SKILL.md"]]

    def test_groups_are_ordered_by_score_descending(self):
        skills = [
            skill("alpha", "Compresses a video file for the web.", "a1/SKILL.md"),
            skill("alpha-two", "Compresses a video file for a website.", "a2/SKILL.md"),
            skill("bravo", "Renames photographs in bulk.", "b1/SKILL.md"),
            skill("bravo", "Renames photographs in bulk.", "b2/SKILL.md"),
        ]
        groups = find_similar(skills)
        assert paths(groups) == [["b1/SKILL.md", "b2/SKILL.md"], ["a1/SKILL.md", "a2/SKILL.md"]]
        assert groups[0].score > groups[1].score

    def test_equally_scored_groups_are_ordered_by_path(self):
        """Two runs over one repo must produce the same report, or diffing it is noise."""
        skills = [
            skill("zulu", "Sorts a list of names.", "z1/SKILL.md"),
            skill("zulu", "Sorts a list of names.", "z2/SKILL.md"),
            skill("alpha", "Merges two calendars.", "a1/SKILL.md"),
            skill("alpha", "Merges two calendars.", "a2/SKILL.md"),
        ]
        groups = find_similar(skills)
        assert [group.score for group in groups] == [1.0, 1.0]
        assert paths(groups) == [["a1/SKILL.md", "a2/SKILL.md"], ["z1/SKILL.md", "z2/SKILL.md"]]

    def test_the_group_names_the_distinct_skills_in_it(self):
        skills = [
            skill("pdf-filler", "Fills in PDF forms.", "a/SKILL.md"),
            skill("pdf-fill", "Fills in PDF forms.", "b/SKILL.md"),
            skill("pdf-filler", "Fills in PDF forms.", "c/SKILL.md"),
        ]
        (group,) = find_similar(skills)
        assert group.names == ("pdf-filler", "pdf-fill")

    @pytest.mark.parametrize("skills", [[], [skill("lonely", "All by itself.")]])
    def test_too_few_skills_to_compare(self, skills):
        assert find_similar(skills) == []

    def test_a_realistic_collection_yields_only_the_real_duplicate(self):
        """The calibration of the default threshold, pinned end to end.

        A family of skills on one topic is the case that makes a naive matcher useless:
        eleven Slack skills share a vocabulary without any two of them being the same
        skill. Only ``code-review`` and ``security-review``, which describe the same job
        on the same input, may come back. Weights or stopwords moving should have to
        answer to this.
        """
        collection = [
            ("slack-channel-digest", "Get a digest of recent activity across multiple Slack channels"),
            ("slack-summarize-channel", "Summarize recent activity in a Slack channel"),
            ("slack-standup", "Generate a standup update based on your recent Slack activity"),
            ("slack-find-discussions", "Find discussions about a specific topic across Slack channels"),
            ("slack-messaging", "Guidance for composing well-formatted Slack messages using markdown"),
            ("slack-search", "Guidance for searching Slack to find messages, files, channels and people"),
            ("slack-cli", "Use the Slack CLI to create, run and manage Slack apps from the terminal"),
            ("slack-api", "Discover, navigate and call Slack Web API methods"),
            ("code-review", "Review the pending changes on the current branch and report findings"),
            ("security-review", "Complete a security review of the pending changes on the current branch"),
            ("init", "Initialize a new CLAUDE.md file with codebase documentation"),
        ]
        skills = [skill(name, description) for name, description in collection]
        assert [group.names for group in find_similar(skills)] == [("code-review", "security-review")]


class TestPairwise:
    """The full triangle the report compares arbitrary pairs from."""

    THREE = [
        skill("deploy", "Deploy the service to production.", "a/SKILL.md"),
        skill("deploy", "Deploy the service to production.", "b/SKILL.md"),
        skill("resize", "Shrinks an image to fit a box.", "c/SKILL.md"),
    ]

    def test_a_row_per_skill_holding_the_pairs_before_it(self):
        """Row i has i entries, so row and column indexes are the skills' own."""
        assert [len(row) for row in pairwise(self.THREE)] == [0, 1, 2]

    def test_a_copy_scores_a_hundred_and_a_stranger_does_not(self):
        matrix = pairwise(self.THREE)
        assert matrix[1][0] == 100
        assert matrix[2][0] < 30
        assert matrix[2][1] == matrix[2][0]  # the same pair from the other side

    def test_every_cell_is_the_public_score_in_whole_percent(self):
        matrix = pairwise(self.THREE)
        for i, left in enumerate(self.THREE):
            for j in range(i):
                assert matrix[i][j] == round(similarity(left, self.THREE[j]) * 100)

    def test_no_pair_is_scored_outside_the_scale(self):
        assert all(0 <= cell <= 100 for row in pairwise(self.THREE) for cell in row)

    def test_the_threshold_does_not_reach_the_matrix(self):
        """A reader comparing two skills wants the number, grouped or not."""
        matrix = pairwise(self.THREE)
        assert find_similar(self.THREE) != []  # a and b group ...
        assert matrix[2][0] > 0  # ... and c, which groups with nothing, is still scored

    @pytest.mark.parametrize(("skills", "expected"), [([], []), ([skill("lonely", "Alone.")], [[]])])
    def test_too_few_skills_to_compare(self, skills, expected):
        assert pairwise(skills) == expected

    def test_the_cap_leaves_room_for_a_realistic_collection(self):
        assert MAX_COMPARABLE >= 100


class TestThreshold:
    def test_raising_it_breaks_a_loose_group_apart(self):
        skills = [
            skill("deploy", "Deploy the service to staging.", "a/SKILL.md"),
            skill("deploy-prod", "Deploy the service to production.", "b/SKILL.md"),
        ]
        assert find_similar(skills, 0.5) != []
        assert find_similar(skills, 0.99) == []

    def test_lowering_it_gathers_more(self):
        skills = [
            skill("slack-search", "Guidance for searching Slack to find messages, files and people.", "a/SKILL.md"),
            skill("slack-messaging", "Guidance for composing well-formatted Slack messages.", "b/SKILL.md"),
        ]
        assert find_similar(skills) == []
        assert paths(find_similar(skills, 0.3)) == [["a/SKILL.md", "b/SKILL.md"]]

    def test_one_means_identical_only(self):
        skills = [
            skill("deploy", "Deploy the service.", "a/SKILL.md"),
            skill("deploy", "Deploy the service.", "b/SKILL.md"),
            skill("deploy", "Deploy the service now.", "c/SKILL.md"),
        ]
        assert paths(find_similar(skills, 1.0)) == [["a/SKILL.md", "b/SKILL.md"]]

    def test_zero_puts_everything_in_one_group(self):
        skills = [
            skill("alpha", "Resizes images.", "a/SKILL.md"),
            skill("bravo", "Files tickets.", "b/SKILL.md"),
        ]
        assert paths(find_similar(skills, 0.0)) == [["a/SKILL.md", "b/SKILL.md"]]

    def test_the_shortcut_never_drops_a_pair_the_full_score_would_keep(self):
        """The cheap upper bound exists to skip work, not to change the answer."""
        import skill_atlas.similarity as module

        skills = [
            skill("pdf-filler", "Fills in PDF forms from data.", "a/SKILL.md"),
            skill("pdf-fill", "Fills in PDF forms from data files.", "b/SKILL.md"),
            skill("image-resize", "Resizes an image to fit a box.", "c/SKILL.md"),
            skill("deploy", "", "d/SKILL.md"),
            skill("deploy", "Ships the service.", "e/SKILL.md"),
        ]
        for threshold in (0.0, 0.3, DEFAULT_THRESHOLD, 0.9, 1.0):
            pruned = find_similar(skills, threshold)
            unpruned = _find_similar_without_shortcut(module, skills, threshold)
            assert paths(pruned) == paths(unpruned), f"the shortcut changed the answer at {threshold}"


def _find_similar_without_shortcut(module, skills, threshold):
    """Re-run the grouping with every pair scored in full, shortcut disabled."""
    original = module._ceiling
    module._ceiling = lambda _a, _b: 1.0
    try:
        return module.find_similar(skills, threshold)
    finally:
        module._ceiling = original
