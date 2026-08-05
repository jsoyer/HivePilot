"""Noise in a reviewed diff is summarised, never silently dropped.

Raising the review subject cap to 200 000 characters (#421) took a 72 KB PR
from $8.26 to $1.83. The first PR that touches a lockfile would spend that
budget on 14 000 lines of dependency hashes and crowd out the code.

But **excluding** such a file would be worse than the cost it saves. A
changed `package-lock.json` is a supply-chain signal: dependency
substitution hides exactly there, and hiding it from the CISO is the one
review this gate cannot afford to skip. The same goes for a vendored
directory or a generated bundle — "this file changed" is information even
when its 14 000 lines are not.

So the filename stays, with a stated line count, and only the body goes. A
reviewer that sees `package-lock.json — 4213 lines changed (body omitted)`
can ask for it. One that never sees the name cannot.
"""

from __future__ import annotations

from hivepilot.orchestrator import _build_review_challenge_prompt
from hivepilot.services.diff_filter import summarise_noisy_files

_LOCKFILE = "\n".join(
    ["diff --git a/package-lock.json b/package-lock.json", "--- a/x", "+++ b/x"]
    + [f'+      "integrity": "sha512-{i}"' for i in range(500)]
)
_CODE = "\n".join(
    [
        "diff --git a/src/auth.py b/src/auth.py",
        "--- a/src/auth.py",
        "+++ b/src/auth.py",
        "+def check(token):",
        "+    return True",
    ]
)


class TestNoiseIsSummarisedNotRemoved:
    def test_the_filename_survives(self) -> None:
        """A changed lockfile is a supply-chain signal even unread."""
        out = summarise_noisy_files(_LOCKFILE)

        assert "package-lock.json" in out

    def test_the_body_is_gone(self) -> None:
        out = summarise_noisy_files(_LOCKFILE)

        assert "sha512-499" not in out
        assert len(out) < len(_LOCKFILE) / 10

    def test_it_states_how_much_was_withheld(self) -> None:
        """ "Omitted" alone leaves the reviewer unable to judge whether to ask.

        Two changed lines in a lockfile is a targeted edit worth reading; four
        thousand is a routine regeneration.
        """
        out = summarise_noisy_files(_LOCKFILE)

        assert "500" in out
        assert "omitted" in out.lower()


class TestCodeIsNeverTouched:
    def test_source_files_pass_through_verbatim(self) -> None:
        assert summarise_noisy_files(_CODE) == _CODE

    def test_a_mixed_diff_keeps_the_code_and_trims_the_noise(self) -> None:
        out = summarise_noisy_files(f"{_CODE}\n{_LOCKFILE}")

        assert "def check(token):" in out, "the reviewable change must survive intact"
        assert "sha512-499" not in out
        assert "package-lock.json" in out

    def test_an_empty_diff_stays_empty(self) -> None:
        assert summarise_noisy_files("") == ""

    def test_text_that_is_not_a_diff_is_returned_unchanged(self) -> None:
        """The subject is not always a diff — never mangle what it does not
        recognise."""
        assert summarise_noisy_files("just some prose") == "just some prose"


class TestWhatCountsAsNoise:
    def test_the_usual_lockfiles(self) -> None:
        for name in (
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "Cargo.lock",
            "poetry.lock",
            "go.sum",
            "composer.lock",
            "Gemfile.lock",
        ):
            diff = f"diff --git a/{name} b/{name}\n" + "+x\n" * 400
            assert "+x" not in summarise_noisy_files(diff), name

    def test_vendored_and_generated_paths(self) -> None:
        for path in ("vendor/lib.go", "node_modules/x/index.js", "dist/app.min.js"):
            diff = f"diff --git a/{path} b/{path}\n" + "+x\n" * 400
            assert "+x" not in summarise_noisy_files(diff), path

    def test_a_small_lockfile_change_is_shown_in_full(self) -> None:
        """A two-line lockfile edit is the interesting case, not the noisy one.

        Summarising it would hide a targeted dependency substitution behind
        the same treatment as a routine regeneration — the precise failure
        this filter must not cause.
        """
        diff = "diff --git a/yarn.lock b/yarn.lock\n+  resolved evil-registry.example\n"

        assert "evil-registry.example" in summarise_noisy_files(diff)

    def test_a_minified_bundle_is_caught_by_its_shape(self) -> None:
        """No basename rule covers every generated asset.

        This repo commits its own built bundle under a hashed filename that
        no lockfile list would match. Machine-written lines are long — that
        holds whatever the path.
        """
        diff = (
            "diff --git a/webui/static/index-Dht.js b/webui/static/index-Dht.js\n"
            + ("+" + "a" * 400 + "\n") * 60
        )

        assert "aaaa" not in summarise_noisy_files(diff)


class TestThePromptNeverClaimsItSawWhatWasWithheld:
    """#419 told a reviewer a truncated diff was COMPLETE while forbidding it
    to look further. Withholding file bodies reopens exactly that hole unless
    the prompt says so."""

    @staticmethod
    def _with_lockfile() -> str:
        return f"{_CODE}\n{_LOCKFILE}"

    def test_it_drops_the_completeness_claim_when_bodies_were_withheld(self) -> None:
        prompt = _build_review_challenge_prompt(self._with_lockfile())

        assert "COMPLETE change" not in prompt

    def test_it_names_the_files_it_withheld(self) -> None:
        prompt = _build_review_challenge_prompt(self._with_lockfile())

        assert "package-lock.json" in prompt

    def test_it_says_how_to_get_them(self) -> None:
        prompt = _build_review_challenge_prompt(self._with_lockfile())

        assert "gh pr diff" in prompt

    def test_the_perimeter_still_holds_for_everything_else(self) -> None:
        """The saving must survive: withholding a lockfile is not a licence
        to roam the repository."""
        prompt = _build_review_challenge_prompt(self._with_lockfile()).lower()

        assert "do not explore" in prompt

    def test_an_ordinary_diff_is_unaffected(self) -> None:
        prompt = _build_review_challenge_prompt(_CODE)

        assert "COMPLETE change" in prompt
        assert "omitted" not in prompt.lower()

    def test_the_reviewable_code_reaches_the_prompt(self) -> None:
        prompt = _build_review_challenge_prompt(self._with_lockfile())

        assert "def check(token):" in prompt
