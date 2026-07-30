"""Every config file the Settings declare must be either SYNCED or explicitly excluded.

THE DEFECT THIS GUARDS: `vault.yaml` shipped in #376 with a `Settings.vault_file`
entry and a working `resolve_config_path` chain, but nobody added it to
`config_service.CONFIG_FILES`. `hivepilot config sync` therefore reported
`updated: []` on a live host whose config repo carried the file, the engine fell
back to its own folder defaults (`{'hivepilot': 'HivePilot'}` instead of the
operator's `'12 - HivePilot'`), and a deployment that HAD declared its taxonomy
ran without it. Only `vault_layout`'s fail-closed warnings surfaced it — no test
did.

Being resolvable by `resolve_config_path` is not sufficient. Sync is what places
a file where a deployment's config dir expects it, so a file missing from
`CONFIG_FILES` is never copied, never reported, and never noticed.

The exclusions below are deliberately a hardcoded literal rather than a
heuristic: a new config file being synced or not is a security-relevant
decision, so a new `*_file` setting must BREAK this test and force someone to
choose, rather than being silently swept into either bucket.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import Settings
from hivepilot.services.config_service import CONFIG_DIRS, CONFIG_FILES

#: Config filenames that must NEVER be synced from a config repo, with the
#: reason. A shared config repo is not a secret store.
DELIBERATELY_NOT_SYNCED: dict[str, str] = {
    "api_tokens.yaml": (
        "API tokens are per-deployment SECRETS. Syncing them from a shared "
        "config repo would distribute one deployment's credentials to every "
        "other consumer of that repo, and would overwrite a host's own tokens "
        "on every `config sync`."
    ),
}


def _settings_declared_yaml_filenames() -> dict[str, str]:
    """Map `<field name> -> <filename>` for every `*_file` setting naming a YAML file.

    Derived from the model rather than hardcoded, so a newly added setting is
    picked up automatically — that is the point of the guard.
    """
    found: dict[str, str] = {}
    for field_name, field in Settings.model_fields.items():
        if not field_name.endswith("_file"):
            continue
        default = field.default
        if not isinstance(default, Path):
            continue
        if default.suffix not in {".yaml", ".yml"}:
            continue
        found[field_name] = default.name
    return found


class TestEveryDeclaredConfigFileIsAccountedFor:
    def test_each_settings_yaml_file_is_synced_or_explicitly_excluded(self) -> None:
        declared = _settings_declared_yaml_filenames()
        assert declared, "no `*_file` YAML settings found — the derivation is broken"

        unaccounted = {
            field: name
            for field, name in declared.items()
            if name not in CONFIG_FILES and name not in DELIBERATELY_NOT_SYNCED
        }

        assert not unaccounted, (
            "these Settings declare a config file that `config sync` will NEVER "
            f"copy: {unaccounted}. Add each filename to "
            "`config_service.CONFIG_FILES`, or to `DELIBERATELY_NOT_SYNCED` in "
            "this test with the reason it must not be distributed. Leaving it in "
            "neither is how vault.yaml silently ran on engine defaults."
        )

    def test_vault_yaml_is_synced(self) -> None:
        """The specific regression. Kept as its own case so the failure message
        names the file rather than a diff of two sets."""
        assert "vault.yaml" in CONFIG_FILES

    def test_api_tokens_is_never_synced(self) -> None:
        """The exclusion is load-bearing, not an oversight: syncing a secrets
        file from a shared config repo would distribute credentials."""
        assert "api_tokens.yaml" not in CONFIG_FILES
        assert "api_tokens.yaml" in DELIBERATELY_NOT_SYNCED

    def test_exclusions_are_justified(self) -> None:
        """An exclusion with an empty reason is an exclusion nobody decided."""
        for name, reason in DELIBERATELY_NOT_SYNCED.items():
            assert reason.strip(), f"{name} is excluded with no stated reason"

    def test_exclusions_do_not_silently_overlap_the_synced_set(self) -> None:
        """A filename in BOTH sets means the two disagree and one is dead
        documentation — the reader cannot tell which is authoritative."""
        overlap = set(DELIBERATELY_NOT_SYNCED) & CONFIG_FILES
        assert not overlap, f"listed as both synced and excluded: {sorted(overlap)}"

    def test_prompts_is_still_the_only_synced_directory(self) -> None:
        """Pins the directory side too. `skills/` deliberately is NOT copied —
        it is resolved in place from the config repo (see hivepilot/skill_dirs.py),
        so adding it here would create a second, staleable copy."""
        assert CONFIG_DIRS == {"prompts"}
