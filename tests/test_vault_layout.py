"""
Tests for ``hivepilot.services.vault_layout`` — the config-owned vault folder
taxonomy.

The engine used to hardcode one organisation's numbered Obsidian filing scheme
(``02 - Artifacts``, ``03 - Decisions``, ``08 - Security``, ``12 - HivePilot``
and a 22-entry expected-layout list). HivePilot is a generic orchestrator for
ANY organisation, so a vault's folder names are that organisation's to declare.

These tests assert the MECHANISM (folders resolve from ``vault.yaml``; per-slot
semantics; unsafe values rejected loudly) plus content assertions that only ever
get STRICTER: no numbered folder literal may return to engine code.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.config import settings
from hivepilot.services import vault_layout as vl


@pytest.fixture
def vault_yaml(tmp_path: Path):
    """Point ``settings.vault_file`` at a throwaway vault.yaml.

    Yields a writer taking raw YAML text. Not calling the writer leaves the file
    absent, which is the "nothing configured at all" case. ``config_repo`` is
    pinned to None so the resolution chain cannot pick up a real config repo on
    the machine running the tests.
    """
    path = tmp_path / "vault.yaml"

    def write(content: str) -> Path:
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    with (
        patch.object(settings, "vault_file", path),
        patch.object(settings, "config_repo", None),
    ):
        yield write


# ---------------------------------------------------------------------------
# folders: — the write/read target slots
# ---------------------------------------------------------------------------


class TestLoadVaultFolders:
    """Folder names must come from config, not from engine code."""

    def test_configured_folder_names_are_used(self, vault_yaml):
        vault_yaml("""
            folders:
              hivepilot: "Robot Workspace"
              artifacts: "Deliverables"
              decisions: "ADRs"
              security: "InfoSec"
        """)
        assert vl.load_vault_folders() == {
            "hivepilot": "Robot Workspace",
            "artifacts": "Deliverables",
            "decisions": "ADRs",
            "security": "InfoSec",
        }

    def test_slots_are_independent_declaring_one_does_not_fill_another(self, vault_yaml):
        """Per-slot resolution: an omitted slot with no engine default stays
        empty rather than borrowing a sibling's value."""
        vault_yaml('folders:\n  artifacts: "Deliverables"\n')
        resolved = vl.load_vault_folders()
        assert resolved["artifacts"] == "Deliverables"
        assert resolved.get("decisions", "") == ""
        assert resolved.get("security", "") == ""

    def test_declaring_one_slot_does_not_drop_the_hivepilot_engine_default(self, vault_yaml):
        """`folders:` MERGES per slot. Declaring `artifacts` must not silently
        delete the only slot the engine does default, or a deployment that
        configured artifacts would lose its run logs."""
        vault_yaml('folders:\n  artifacts: "Deliverables"\n')
        resolved = vl.load_vault_folders()
        assert resolved["hivepilot"] == vl.ENGINE_DEFAULT_VAULT_FOLDERS[vl.SLOT_HIVEPILOT]

    def test_declaring_the_hivepilot_slot_replaces_the_engine_default(self, vault_yaml):
        vault_yaml('folders:\n  hivepilot: "12 - Orchestrator"\n')
        assert vl.load_vault_folders()["hivepilot"] == "12 - Orchestrator"

    def test_whitespace_around_a_valid_folder_name_is_stripped(self, vault_yaml):
        vault_yaml('folders:\n  artifacts: "  Deliverables  "\n')
        assert vl.load_vault_folders()["artifacts"] == "Deliverables"

    def test_returns_a_fresh_dict_callers_cannot_mutate_shared_state(self, vault_yaml):
        vault_yaml('folders:\n  artifacts: "A"\n')
        first = vl.load_vault_folders()
        first["artifacts"] = "mutated"
        assert vl.load_vault_folders()["artifacts"] == "A"


class TestVaultFoldersEngineDefault:
    """No config -> only the engine's own subtree, and never a partial path."""

    def test_missing_vault_file_yields_only_the_engine_default(self, vault_yaml):
        # Fixture applied but never written -> the file does not exist.
        assert vl.load_vault_folders() == dict(vl.ENGINE_DEFAULT_VAULT_FOLDERS)

    def test_file_without_the_key_yields_only_the_engine_default(self, vault_yaml):
        vault_yaml("expected_folders: []\n")
        assert vl.load_vault_folders() == dict(vl.ENGINE_DEFAULT_VAULT_FOLDERS)

    def test_bare_null_key_yields_only_the_engine_default(self, vault_yaml):
        vault_yaml("folders:\n")
        assert vl.load_vault_folders() == dict(vl.ENGINE_DEFAULT_VAULT_FOLDERS)

    def test_engine_default_covers_only_the_engine_owned_subtree(self):
        """The engine may name its OWN workspace; it may not guess an
        organisation's pre-existing filing scheme."""
        assert dict(vl.ENGINE_DEFAULT_VAULT_FOLDERS) == {vl.SLOT_HIVEPILOT: "HivePilot"}

    def test_engine_default_is_an_immutable_mapping(self):
        with pytest.raises(TypeError):
            vl.ENGINE_DEFAULT_VAULT_FOLDERS[vl.SLOT_ARTIFACTS] = "x"  # type: ignore[index]

    def test_engine_default_folder_name_carries_no_numbering_convention(self):
        """`12 - HivePilot` was the customer's numbering. The engine's own
        default must be a bare, self-describing name."""
        for name in vl.ENGINE_DEFAULT_VAULT_FOLDERS.values():
            assert not re.match(r"^\d", name), f"engine default {name!r} encodes a filing scheme"


class TestFolderLookupNeverBuildsAPartialPath:
    """THE trap: an absent folder name must never degrade into the vault root."""

    def test_unconfigured_slot_returns_the_empty_string_sentinel(self):
        layout = vl.VaultLayout(folders={}, expected_folders=(), frozen_folders=())
        for slot in vl.VAULT_FOLDER_SLOTS:
            assert layout.folder(slot) == ""

    def test_unconfigured_slot_never_yields_a_none_or_root_path(self):
        layout = vl.VaultLayout(folders={}, expected_folders=(), frozen_folders=())
        for slot in vl.VAULT_FOLDER_SLOTS:
            resolved = layout.folder(slot)
            assert "None" not in resolved
            assert not resolved.startswith("/")
            assert not resolved.endswith("/")

    def test_require_folder_raises_rather_than_returning_the_vault_root(self):
        """A writer must be refused loudly, not silently redirected to the
        vault root — a vault is typically a synced git repo."""
        layout = vl.VaultLayout(folders={}, expected_folders=(), frozen_folders=())
        with pytest.raises(vl.VaultLayoutError) as exc:
            layout.require_folder(vl.SLOT_ARTIFACTS)
        assert vl.SLOT_ARTIFACTS in str(exc.value)
        assert vl.VAULT_FOLDERS_KEY in str(exc.value)

    def test_require_folder_returns_the_configured_name(self):
        layout = vl.VaultLayout(
            folders={vl.SLOT_ARTIFACTS: "Deliverables"},
            expected_folders=(),
            frozen_folders=(),
        )
        assert layout.require_folder(vl.SLOT_ARTIFACTS) == "Deliverables"

    def test_unknown_slot_lookup_is_refused_not_silently_empty(self):
        layout = vl.VaultLayout(folders={"artifacts": "A"}, expected_folders=(), frozen_folders=())
        with pytest.raises(vl.VaultLayoutError):
            layout.folder("not_a_slot")


class TestVaultFoldersRejectsBadInput:
    """A typo must be loud, and must never widen where the engine writes."""

    @pytest.mark.parametrize(
        "body",
        [
            "folders: 'a string, not a mapping'\n",
            "folders:\n  - artifacts\n",
            "folders:\n  artifacts: 42\n",
            "folders:\n  artifacts: true\n",
        ],
    )
    def test_malformed_value_falls_back_to_the_engine_default_and_warns(
        self, vault_yaml, body, caplog
    ):
        vault_yaml(body)
        with caplog.at_level("WARNING"):
            assert vl.load_vault_folders() == dict(vl.ENGINE_DEFAULT_VAULT_FOLDERS)
        assert "vault_folders_malformed" in caplog.text

    def test_unknown_slot_is_ignored_and_warns(self, vault_yaml, caplog):
        """A typo'd slot silently yielding "no folder" is exactly the
        empty-means-no-constraint failure — so it warns by name."""
        vault_yaml("""
            folders:
              artefacts: "TYPO"
              decisions: "ADRs"
        """)
        with caplog.at_level("WARNING"):
            resolved = vl.load_vault_folders()
        assert resolved.get("artifacts", "") == ""
        assert resolved["decisions"] == "ADRs"
        assert "vault_folders_unknown_slot" in caplog.text

    @pytest.mark.parametrize("blank", ['""', '"   "'])
    def test_blank_folder_name_is_dropped_and_warns(self, vault_yaml, blank, caplog):
        """A blank name would make `<vault>/` itself the write target."""
        vault_yaml(f"folders:\n  artifacts: {blank}\n")
        with caplog.at_level("WARNING"):
            resolved = vl.load_vault_folders()
        assert resolved.get("artifacts", "") == ""
        assert "vault_folders_blank" in caplog.text

    @pytest.mark.parametrize(
        "value",
        [
            "../../etc",
            "/etc",
            "sub/dir",
            ".",
            "..",
            "windows\\dir",
            "trailing/",
        ],
    )
    def test_escaping_folder_name_is_rejected_and_warns(self, vault_yaml, value, caplog):
        """Config names a folder INSIDE the vault. Letting the value escape it
        would turn a config file into an arbitrary-path WRITE."""
        vault_yaml(f"folders:\n  artifacts: '{value}'\n")
        with caplog.at_level("WARNING"):
            resolved = vl.load_vault_folders()
        assert resolved.get("artifacts", "") == ""
        assert "vault_folders_unsafe" in caplog.text

    def test_unparseable_yaml_falls_back_to_the_engine_default_and_warns(self, vault_yaml, caplog):
        vault_yaml("folders: [unclosed\nexpected_folders: :::\n")
        with caplog.at_level("WARNING"):
            assert vl.load_vault_folders() == dict(vl.ENGINE_DEFAULT_VAULT_FOLDERS)
        assert "vault_layout_unreadable" in caplog.text


# ---------------------------------------------------------------------------
# expected_folders: / frozen_folders: — the AUDIT lists
# ---------------------------------------------------------------------------


class TestLoadExpectedFolders:
    """The audit's expected layout is the OPERATOR's declaration."""

    def test_configured_expected_folders_are_used(self, vault_yaml):
        vault_yaml("""
            expected_folders:
              - "Inbox"
              - "Journal"
              - "Archive"
        """)
        assert vl.load_expected_folders() == ["Inbox", "Journal", "Archive"]

    def test_missing_file_yields_no_expected_folders(self, vault_yaml):
        assert vl.load_expected_folders() == []

    def test_absent_key_yields_no_expected_folders(self, vault_yaml):
        vault_yaml('folders:\n  artifacts: "A"\n')
        assert vl.load_expected_folders() == []

    def test_blank_entries_are_dropped_and_warn(self, vault_yaml, caplog):
        vault_yaml("""
            expected_folders:
              - "Inbox"
              - ""
              - "   "
        """)
        with caplog.at_level("WARNING"):
            assert vl.load_expected_folders() == ["Inbox"]
        assert "expected_folders_blank" in caplog.text

    @pytest.mark.parametrize("value", ["..", ".", "../secrets", "a/b", "back\\slash"])
    def test_escaping_entries_are_rejected_and_warn(self, vault_yaml, caplog, value):
        """`(vault / "..").is_dir()` is always True — an escaping entry would
        report a folder "present" that is not even in the vault."""
        vault_yaml(f"expected_folders:\n  - '{value}'\n")
        with caplog.at_level("WARNING"):
            assert vl.load_expected_folders() == []
        assert "expected_folders_unsafe" in caplog.text

    def test_duplicates_are_collapsed_preserving_order(self, vault_yaml):
        vault_yaml("""
            expected_folders:
              - "Inbox"
              - "Journal"
              - "Inbox"
        """)
        assert vl.load_expected_folders() == ["Inbox", "Journal"]

    @pytest.mark.parametrize(
        "body",
        [
            "expected_folders: 'a string'\n",
            "expected_folders:\n  key: value\n",
            "expected_folders:\n  - 42\n",
        ],
    )
    def test_malformed_value_yields_nothing_and_warns(self, vault_yaml, body, caplog):
        vault_yaml(body)
        with caplog.at_level("WARNING"):
            assert vl.load_expected_folders() == []
        assert "expected_folders_malformed" in caplog.text


class TestLoadFrozenFolders:
    def test_configured_frozen_folders_are_used(self, vault_yaml):
        vault_yaml("""
            frozen_folders:
              - "InfoSec"
              - "ADRs"
        """)
        assert vl.load_frozen_folders() == ["InfoSec", "ADRs"]

    def test_missing_file_yields_no_frozen_folders(self, vault_yaml):
        assert vl.load_frozen_folders() == []

    def test_malformed_value_yields_nothing_and_warns(self, vault_yaml, caplog):
        vault_yaml("frozen_folders: 'a string'\n")
        with caplog.at_level("WARNING"):
            assert vl.load_frozen_folders() == []
        assert "frozen_folders_malformed" in caplog.text


# ---------------------------------------------------------------------------
# The write taxonomy and the audit list are INDEPENDENT
# ---------------------------------------------------------------------------


class TestWriteTargetsAndAuditListStayIndependent:
    """Regression guard for the conflation bug this change must not re-create.

    ``EXPECTED_TOP_LEVEL_FOLDERS`` was for years missing ``02 - Artifacts`` —
    the one folder the engine actually writes deliverables into — so the audit
    never checked it. The tempting "fix" is to make one list derive from the
    other. That is wrong: the write targets are ENGINE behaviour and the
    expected layout is the OPERATOR's declaration of what the vault should look
    like (most entries have no writer at all). They are reported separately and
    neither is computed from the other.
    """

    def test_expected_folders_does_not_feed_the_write_slots(self, vault_yaml):
        vault_yaml("""
            expected_folders:
              - "Deliverables"
              - "ADRs"
        """)
        resolved = vl.load_vault_folders()
        assert resolved.get("artifacts", "") == ""
        assert resolved.get("decisions", "") == ""

    def test_write_slots_do_not_feed_expected_folders(self, vault_yaml):
        vault_yaml("""
            folders:
              artifacts: "Deliverables"
              decisions: "ADRs"
        """)
        assert vl.load_expected_folders() == []

    def test_layout_exposes_both_lists_separately(self, vault_yaml):
        vault_yaml("""
            folders:
              artifacts: "Deliverables"
            expected_folders:
              - "Inbox"
            frozen_folders:
              - "InfoSec"
        """)
        layout = vl.load_layout()
        assert layout.folder(vl.SLOT_ARTIFACTS) == "Deliverables"
        assert layout.expected_folders == ("Inbox",)
        assert layout.frozen_folders == ("InfoSec",)
        # "Deliverables" is a write target, NOT part of the declared layout.
        assert "Deliverables" not in layout.expected_folders


# ---------------------------------------------------------------------------
# Loud when a vault is configured but the taxonomy is not
# ---------------------------------------------------------------------------


class TestSilenceWithAVaultConfiguredIsLoud:
    def test_warns_when_a_vault_is_set_but_no_write_folders_declared(self, vault_yaml, caplog):
        with patch.object(settings, "obsidian_vault", Path("/abs/vault")):
            with caplog.at_level("WARNING"):
                vl.load_layout()
        assert "vault_folders_absent" in caplog.text

    def test_warns_when_a_vault_is_set_but_no_expected_layout_declared(self, vault_yaml, caplog):
        with patch.object(settings, "obsidian_vault", Path("/abs/vault")):
            with caplog.at_level("WARNING"):
                vl.load_layout()
        assert "expected_folders_absent" in caplog.text

    def test_does_not_warn_when_no_vault_is_configured_at_all(self, vault_yaml, caplog):
        """A deployment with no vault never asked for a vault taxonomy."""
        with patch.object(settings, "obsidian_vault", None):
            with caplog.at_level("WARNING"):
                vl.load_layout()
        assert "vault_folders_absent" not in caplog.text
        assert "expected_folders_absent" not in caplog.text

    def test_does_not_warn_when_the_taxonomy_is_fully_declared(self, vault_yaml, caplog):
        vault_yaml("""
            folders:
              hivepilot: "Robot"
              artifacts: "Deliverables"
              decisions: "ADRs"
              security: "InfoSec"
            expected_folders:
              - "Inbox"
        """)
        with patch.object(settings, "obsidian_vault", Path("/abs/vault")):
            with caplog.at_level("WARNING"):
                vl.load_layout()
        assert "vault_folders_absent" not in caplog.text
        assert "expected_folders_absent" not in caplog.text


# ---------------------------------------------------------------------------
# Regression guard: no organisation's taxonomy back in engine code
# ---------------------------------------------------------------------------


class TestNoOrganisationTaxonomyInEngineCode:
    """This is what the whole change exists to protect."""

    #: The numbered-folder shape: two digits, spaces, a hyphen, spaces, a word.
    NUMBERED_FOLDER = re.compile(r"\b\d{2}\s+-\s+[A-Z]")

    ENGINE_MODULES = (
        "hivepilot/services/vault_layout.py",
        "hivepilot/services/obsidian_service.py",
        "hivepilot/agent_rules.py",
        "hivepilot/pipelines.py",
    )

    def test_no_numbered_folder_literal_survives_in_engine_modules(self):
        repo_root = Path(vl.__file__).resolve().parents[2]
        offenders: list[str] = []
        for rel in self.ENGINE_MODULES:
            source = (repo_root / rel).read_text(encoding="utf-8")
            for lineno, line in enumerate(source.splitlines(), start=1):
                if self.NUMBERED_FOLDER.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert not offenders, (
            "A numbered vault folder name is back in engine code:\n"
            + "\n".join(offenders)
            + "\n\nVault folder names are config-owned (`folders:` in vault.yaml) — "
            "HivePilot is a generic orchestrator and cannot know how a deployment "
            "files its vault."
        )

    def test_slot_names_are_generic_not_organisation_specific(self):
        combined = " ".join(vl.VAULT_FOLDER_SLOTS).lower()
        assert not re.search(r"\d", combined), "a slot name must not encode a filing scheme"
        for marker in ("gtm", "acme", "legal", "compliance", "inbox"):
            assert marker not in combined

    def test_slot_vocabulary_is_closed_and_covers_every_engine_target(self):
        """Exactly the folders the engine reads or writes — no more."""
        assert set(vl.VAULT_FOLDER_SLOTS) == {
            vl.SLOT_HIVEPILOT,
            vl.SLOT_ARTIFACTS,
            vl.SLOT_DECISIONS,
            vl.SLOT_SECURITY,
        }


class TestModuleLevelLayoutMatchesTheLoader:
    def test_no_second_drifting_source_of_truth(self):
        assert vl.current_layout() is vl.VAULT_LAYOUT
