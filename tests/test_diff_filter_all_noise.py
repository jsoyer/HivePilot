"""A PR made entirely of generated files must not be filtered to nothing.

The filter (#422) withholds generated bodies "to leave room for the code".
That justification is the whole warrant for hiding anything -- and it does
not exist when there is no code. A PR that only regenerates
`package-lock.json` would become a prompt containing one placeholder and
nothing else, and a reviewer with nothing to read returns PASS.

That is the worst outcome this filter could produce: a dependency
substitution waved through *because* the one file that mattered was the one
we hid. The saving is zero in that case too -- nothing was competing for the
budget.

So the filter only fires when something survives it. When every file in the
change is generated, the diff goes through untouched and the existing
truncation path handles the size honestly: the reviewer is told it is
holding a fragment and sent to fetch the rest, which is the correct
instruction for "this PR is a 14 MB lockfile".
"""

from __future__ import annotations

from hivepilot.orchestrator import _build_review_challenge_prompt
from hivepilot.services.diff_filter import summarise_with_report


def _lockfile(name: str = "package-lock.json", lines: int = 400) -> str:
    body = "\n".join(f'+      "integrity": "sha512-{i}"' for i in range(lines))
    return f"diff --git a/{name} b/{name}\n{body}\n"


_CODE = "diff --git a/src/auth.py b/src/auth.py\n+def check(token):\n+    return True\n"


class TestNothingSurvivesMeansNoFiltering:
    def test_a_lockfile_only_pr_is_passed_through_whole(self) -> None:
        diff = _lockfile()

        out, omitted = summarise_with_report(diff)

        assert out == diff
        assert omitted == []

    def test_the_substance_reaches_the_reviewer(self) -> None:
        """The point of the exception: something must be readable."""
        diff = _lockfile()

        out, _ = summarise_with_report(diff)

        assert "sha512-399" in out

    def test_it_holds_for_several_generated_files_too(self) -> None:
        diff = _lockfile("yarn.lock") + _lockfile("pnpm-lock.yaml")

        out, omitted = summarise_with_report(diff)

        assert out == diff
        assert omitted == []

    def test_the_prompt_does_not_claim_an_omission_that_did_not_happen(
        self,
    ) -> None:
        prompt = _build_review_challenge_prompt(_lockfile())

        assert "omitted" not in prompt.lower()
        assert "sha512-399" in prompt


class TestFilteringStillAppliesWhenThereIsCode:
    def test_one_surviving_source_file_is_enough(self) -> None:
        """The exception must be narrow: as soon as there is code to protect,
        the budget is contested again and the filter earns its keep."""
        diff = _CODE + _lockfile()

        out, omitted = summarise_with_report(diff)

        assert omitted == ["package-lock.json"]
        assert "sha512-399" not in out
        assert "def check(token):" in out

    def test_a_small_generated_change_does_not_count_as_filtered(self) -> None:
        """A lockfile shown in full because it is small is content that
        survived -- it does not make the change 'all noise'."""
        small = "diff --git a/yarn.lock b/yarn.lock\n+  resolved evil.example\n"
        diff = small + _lockfile()

        out, omitted = summarise_with_report(diff)

        assert omitted == ["package-lock.json"]
        assert "evil.example" in out
