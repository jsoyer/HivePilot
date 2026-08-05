"""The planning output must report what the reviewer will actually see.

`hivepilot review pr <N>` without `--execute` exists to let an operator
decide whether to spend: two of the three default reviewers resolve to opus.
The diff size is the number that decision rests on.

Since the diff filter landed, that number is wrong by up to fifty-fold. On
PR 417 the planner printed `1820113 bytes of diff`; the reviewers were sent
34 916 characters, because the built bundle was summarised away. An operator
reading 1.8 MB declines a review that would in fact have cost about as much
as a 35 KB one.

So the plan reports both: the raw diff, and the subject after filtering.
Reporting only the filtered size would hide that a rebuild happened at all
-- the same information the filter itself is careful to preserve.
"""

from __future__ import annotations

from hivepilot.services.diff_filter import summarise_noisy_files

_BUNDLE_LINE = "+" + "a" * 280_000
_NOISY_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "+def handler():\n"
    "+    return 1\n"
    "diff --git a/static/index-Dht.js b/static/index-Dht.js\n"
    f"{_BUNDLE_LINE}\n{_BUNDLE_LINE}\n"
)
_CLEAN_DIFF = "diff --git a/src/app.py b/src/app.py\n+def handler():\n+    return 1\n"


class TestReviewedBytesIsTheFilteredSize:
    def test_it_reports_what_the_reviewer_receives(self) -> None:
        from hivepilot.services.review_probe import plan_reviewers

        result = plan_reviewers(_NOISY_DIFF, reviewers=["reviewer"])

        assert result.reviewed_bytes == len(summarise_noisy_files(_NOISY_DIFF))

    def test_it_still_reports_the_raw_size(self) -> None:
        """Dropping the raw number would hide that a rebuild happened."""
        from hivepilot.services.review_probe import plan_reviewers

        result = plan_reviewers(_NOISY_DIFF, reviewers=["reviewer"])

        assert result.subject_bytes == len(_NOISY_DIFF)

    def test_the_two_differ_when_there_is_noise(self) -> None:
        """Guards the regression directly: a filter that silently stopped
        firing would make these equal and the test would say so."""
        from hivepilot.services.review_probe import plan_reviewers

        result = plan_reviewers(_NOISY_DIFF, reviewers=["reviewer"])

        assert result.reviewed_bytes < result.subject_bytes / 100

    def test_they_agree_on_an_ordinary_diff(self) -> None:
        from hivepilot.services.review_probe import plan_reviewers

        result = plan_reviewers(_CLEAN_DIFF, reviewers=["reviewer"])

        assert result.reviewed_bytes == result.subject_bytes
