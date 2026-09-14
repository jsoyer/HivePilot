"""HP-110 deterministic skill-evolution validator + safety load.

OpenSpace ``evolution/validator`` pattern, rewritten in Python. Regex
alone is insufficient: path containment, YAML parse, privilege-set
diff, encoding, and size are structural. This module does **not**
vendor OpenSpace, talk to OpenSpace cloud, persist pickle embeddings,
write skill files, apply HP-111 atomic accept, or auto-evolve.

Contracts:

- Result ∈ {approve, reject, needs_human_review}.
- Zero mutation: no skill write, no catalog ``record``, no PASS
  ``create_pending``, no workshop accept.
- Traversal / symlink, size, UTF-8, frontmatter, and secrets are
  fail-closed (reject).
- Extending allowed-tools / hooks / shell / permissions without a
  **specific** approval is refused (needs_human_review). A generic
  ``approve`` / ``*`` token is not specific.
- ``validate()`` is the HP-111 hook. Callers may run it before or
  after ``propose``; it never applies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

import yaml

from hivepilot.evidence import redact_evidence_text
from hivepilot.pass_store import PassProposal
from hivepilot.pass_store import get as get_proposal
from hivepilot.services.config_provenance import REDACTED, registered_secret_values
from hivepilot.services.token_service import ROLE_RANKS
from hivepilot.skill_dirs import MAX_SKILL_FILE_BYTES, MAX_SKILL_FILES, SKILL_MANIFEST

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
APPROVE = "approve"
REJECT = "reject"
NEEDS_HUMAN_REVIEW = "needs_human_review"
VALIDATION_RESULTS: tuple[str, ...] = (APPROVE, REJECT, NEEDS_HUMAN_REVIEW)

PRIVILEGE_AXES: tuple[str, ...] = ("allowed-tools", "hooks", "shell", "permissions")

GENERIC_APPROVALS: frozenset[str] = frozenset(
    {"approve", "approved", "all", "yes", "*", "lgtm", "ok", "any"}
)

SHELL_TOOLS: frozenset[str] = frozenset({"Bash", "Shell", "bash", "shell", "Terminal"})
PERMISSIVE_MODES: frozenset[str] = frozenset(
    {
        "bypassPermissions",
        "bypass",
        "acceptEdits",
        "dontAsk",
        "full_access",
        "full-access",
    }
)
SECRET_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        "credentials",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
    }
)
PICKLE_SUFFIXES: frozenset[str] = frozenset({".pkl", ".pickle"})

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_SPLIT_TOOLS = re.compile(r"[\s,]+")
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|authorization|cookie|secret|password|credential)",
    re.IGNORECASE,
)
_PEM_PRIVATE_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+)?PRIVATE KEY-----",
    re.IGNORECASE,
)
_ASSIGNED_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*"
    r"(?:sk-|ghp_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9_\-/+=]{8,}"
)
_PICKLE_MAGIC = (b"\x80\x03", b"\x80\x04", b"\x80\x05")


class SkillEvolutionValidatorError(ValueError):
    """Invalid validator arguments (not a skill-content verdict)."""


@dataclass(frozen=True)
class ValidationFinding:
    """One deterministic check result."""

    check: str
    result: str
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "result": self.result,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Pure validation outcome. ``mutated`` is always False."""

    result: str
    mutated: bool = False
    reason: str = ""
    findings: tuple[ValidationFinding, ...] = ()
    privilege_extensions: tuple[str, ...] = ()
    checks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "mutated": False,
            "reason": self.reason,
            "findings": [item.to_dict() for item in self.findings],
            "privilege_extensions": list(self.privilege_extensions),
            "checks": dict(self.checks),
        }


def validate(
    *,
    files: Mapping[str, str | bytes] | None = None,
    name: str = "",
    baseline_files: Mapping[str, str | bytes] | None = None,
    skill_root: Path | None = None,
    specific_approvals: Sequence[str] = (),
    proposal: PassProposal | None = None,
) -> ValidationResult:
    """Inspect proposed skill content/paths. Never writes or persists."""
    proposed, root_findings = _collect_proposed(files, skill_root, proposal)
    findings: list[ValidationFinding] = list(root_findings)
    findings.extend(_check_paths(proposed, skill_root))
    findings.extend(_check_size(proposed))
    findings.extend(_check_utf8_and_pickle(proposed))
    frontmatter, fm_findings = _check_frontmatter(proposed, name)
    findings.extend(fm_findings)
    findings.extend(_check_secrets(proposed, frontmatter))
    extensions, priv_findings = _check_privilege(
        proposed,
        baseline_files or {},
        frontmatter,
        specific_approvals,
    )
    findings.extend(priv_findings)
    return _finalize(findings, extensions)


def validate_proposal(proposal_id: str) -> ValidationResult:
    """HP-111 hook: validate a PASS draft. Never mutates disk or inbox."""
    row_id = (proposal_id or "").strip()
    if not row_id:
        raise SkillEvolutionValidatorError("proposal_id is required")
    proposal = get_proposal(row_id)
    if proposal is None:
        return ValidationResult(
            result=REJECT,
            reason="proposal_not_found",
            findings=(
                ValidationFinding(
                    check="proposal",
                    result=REJECT,
                    code="proposal_not_found",
                    detail=row_id,
                ),
            ),
            checks={"proposal": REJECT},
        )
    payload = proposal.payload
    files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    approvals = payload.get("specific_approvals") or ()
    if not isinstance(approvals, (list, tuple)):
        approvals = ()
    return validate(
        files=files,
        name=str(payload.get("name") or ""),
        specific_approvals=tuple(str(item) for item in approvals),
        proposal=proposal,
    )


def _collect_proposed(
    files: Mapping[str, str | bytes] | None,
    skill_root: Path | None,
    proposal: PassProposal | None,
) -> tuple[dict[str, str | bytes], list[ValidationFinding]]:
    proposed: dict[str, str | bytes] = {}
    findings: list[ValidationFinding] = []
    if files is not None:
        for rel, content in files.items():
            if not isinstance(rel, str):
                findings.append(
                    ValidationFinding(
                        check="traversal",
                        result=REJECT,
                        code="path_not_str",
                        detail=type(rel).__name__,
                    )
                )
                continue
            if not isinstance(content, (str, bytes)):
                findings.append(
                    ValidationFinding(
                        check="utf8",
                        result=REJECT,
                        code="content_not_text",
                        detail=rel,
                    )
                )
                continue
            proposed[rel] = content
    elif proposal is not None:
        payload_files = proposal.payload.get("files")
        if isinstance(payload_files, dict):
            for rel, content in payload_files.items():
                if isinstance(rel, str) and isinstance(content, (str, bytes)):
                    proposed[rel] = content
    if skill_root is not None:
        loaded, root_findings = _load_skill_root(skill_root)
        findings.extend(root_findings)
        if files is None and proposal is None:
            proposed.update(loaded)
    return proposed, findings


def _load_skill_root(skill_root: Path) -> tuple[dict[str, str | bytes], list[ValidationFinding]]:
    """Read-only safety load. Does not register a SkillSpec or write."""
    findings: list[ValidationFinding] = []
    loaded: dict[str, str | bytes] = {}
    try:
        root = skill_root.resolve()
    except OSError as exc:
        findings.append(
            ValidationFinding(
                check="symlink",
                result=REJECT,
                code="skill_root_unreadable",
                detail=str(exc),
            )
        )
        return loaded, findings
    if not root.is_dir():
        findings.append(
            ValidationFinding(
                check="symlink",
                result=REJECT,
                code="skill_root_not_dir",
                detail=str(skill_root),
            )
        )
        return loaded, findings
    for path in sorted(skill_root.rglob("*")):
        try:
            rel = path.relative_to(skill_root).as_posix()
        except ValueError:
            findings.append(
                ValidationFinding(
                    check="traversal",
                    result=REJECT,
                    code="path_traversal",
                    detail=str(path),
                )
            )
            continue
        if path.is_symlink():
            findings.append(_symlink_finding(path, root, rel))
            continue
        if not path.is_file():
            continue
        try:
            loaded[rel] = path.read_bytes()
        except OSError as exc:
            findings.append(
                ValidationFinding(
                    check="utf8",
                    result=REJECT,
                    code="file_unreadable",
                    detail=f"{rel}: {exc}",
                )
            )
    return loaded, findings


def _symlink_finding(path: Path, root: Path, rel: str) -> ValidationFinding:
    try:
        resolved = path.resolve()
    except OSError:
        return ValidationFinding(
            check="symlink",
            result=REJECT,
            code="symlink",
            detail=rel,
        )
    try:
        resolved.relative_to(root)
    except ValueError:
        return ValidationFinding(
            check="symlink",
            result=REJECT,
            code="symlink_escape",
            detail=rel,
        )
    return ValidationFinding(
        check="symlink",
        result=REJECT,
        code="symlink",
        detail=rel,
    )


def _check_paths(
    files: Mapping[str, str | bytes],
    skill_root: Path | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    root = None
    if skill_root is not None:
        try:
            root = skill_root.resolve()
        except OSError:
            root = None
    for rel in files:
        code = _unsafe_relpath(rel)
        if code:
            findings.append(
                ValidationFinding(
                    check="traversal",
                    result=REJECT,
                    code=code,
                    detail=rel,
                )
            )
            continue
        if root is None:
            continue
        candidate = skill_root / rel
        if candidate.is_symlink() or candidate.exists():
            try:
                resolved = candidate.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                findings.append(
                    ValidationFinding(
                        check="symlink",
                        result=REJECT,
                        code="symlink_escape",
                        detail=rel,
                    )
                )
    return findings


def _unsafe_relpath(rel: str) -> str:
    if not rel or "\x00" in rel:
        return "path_null" if "\x00" in rel else "path_empty"
    cleaned = unquote(rel.replace("\\", "/"))
    if cleaned.startswith("/") or cleaned.startswith("~"):
        return "path_absolute"
    if re.match(r"^[A-Za-z]:", cleaned):
        return "path_absolute"
    path = Path(cleaned)
    if path.is_absolute():
        return "path_absolute"
    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        return "path_traversal"
    if any(part.startswith(".") for part in parts):
        return "path_hidden"
    return ""


def _check_size(files: Mapping[str, str | bytes]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if len(files) > MAX_SKILL_FILES:
        findings.append(
            ValidationFinding(
                check="size",
                result=REJECT,
                code="too_many_files",
                detail=str(len(files)),
            )
        )
    for rel, content in files.items():
        size = _byte_size(content)
        if size > MAX_SKILL_FILE_BYTES:
            findings.append(
                ValidationFinding(
                    check="size",
                    result=REJECT,
                    code="oversized_file",
                    detail=f"{rel}:{size}",
                )
            )
    return findings


def _byte_size(content: str | bytes) -> int:
    if isinstance(content, bytes):
        return len(content)
    try:
        return len(content.encode("utf-8"))
    except UnicodeEncodeError:
        return len(content.encode("utf-8", errors="replace"))


def _check_utf8_and_pickle(files: Mapping[str, str | bytes]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for rel, content in files.items():
        suffix = Path(rel).suffix.lower()
        if suffix in PICKLE_SUFFIXES:
            findings.append(
                ValidationFinding(
                    check="utf8",
                    result=REJECT,
                    code="pickle_forbidden",
                    detail=rel,
                )
            )
            continue
        raw = _as_bytes(content)
        if raw is None:
            findings.append(
                ValidationFinding(
                    check="utf8",
                    result=REJECT,
                    code="invalid_utf8",
                    detail=rel,
                )
            )
            continue
        if raw.startswith(_PICKLE_MAGIC):
            findings.append(
                ValidationFinding(
                    check="utf8",
                    result=REJECT,
                    code="pickle_forbidden",
                    detail=rel,
                )
            )
            continue
        if b"\x00" in raw:
            findings.append(
                ValidationFinding(
                    check="utf8",
                    result=REJECT,
                    code="null_byte",
                    detail=rel,
                )
            )
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(
                ValidationFinding(
                    check="utf8",
                    result=REJECT,
                    code="invalid_utf8",
                    detail=rel,
                )
            )
    return findings


def _as_bytes(content: str | bytes) -> bytes | None:
    if isinstance(content, bytes):
        return content
    try:
        return content.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _check_frontmatter(
    files: Mapping[str, str | bytes],
    name: str,
) -> tuple[dict[str, Any], list[ValidationFinding]]:
    findings: list[ValidationFinding] = []
    text = _manifest_text(files)
    if text is None:
        return {}, findings
    stripped = text.lstrip()
    if stripped.startswith("---") and _FRONTMATTER_RE.match(text) is None:
        findings.append(
            ValidationFinding(
                check="frontmatter",
                result=REJECT,
                code="frontmatter_malformed",
                detail=SKILL_MANIFEST,
            )
        )
        return {}, findings
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, findings
    try:
        parsed = yaml.safe_load(match.group("body"))
    except yaml.YAMLError as exc:
        findings.append(
            ValidationFinding(
                check="frontmatter",
                result=REJECT,
                code="frontmatter_malformed",
                detail=str(exc),
            )
        )
        return {}, findings
    if parsed is None:
        return {}, findings
    if not isinstance(parsed, dict):
        findings.append(
            ValidationFinding(
                check="frontmatter",
                result=REJECT,
                code="frontmatter_not_mapping",
                detail=type(parsed).__name__,
            )
        )
        return {}, findings
    declared = parsed.get("name")
    skill_name = (name or "").strip()
    if declared is not None and skill_name and str(declared) != skill_name:
        findings.append(
            ValidationFinding(
                check="frontmatter",
                result=REJECT,
                code="frontmatter_name_mismatch",
                detail=f"{declared!r}!={skill_name!r}",
            )
        )
    min_role = parsed.get("min_role")
    if min_role is not None and (not isinstance(min_role, str) or min_role not in ROLE_RANKS):
        findings.append(
            ValidationFinding(
                check="frontmatter",
                result=REJECT,
                code="frontmatter_invalid_min_role",
                detail=repr(min_role),
            )
        )
    return parsed, findings


def _manifest_text(files: Mapping[str, str | bytes]) -> str | None:
    raw = files.get(SKILL_MANIFEST)
    if raw is None:
        raw = files.get("skill.md")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return raw


def _check_secrets(
    files: Mapping[str, str | bytes],
    frontmatter: Mapping[str, Any],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    registered = registered_secret_values()
    for rel, content in files.items():
        base = Path(rel).name.lower()
        if base in SECRET_FILENAMES or base.endswith(".pem") or base.startswith(".env"):
            findings.append(
                ValidationFinding(
                    check="secrets",
                    result=REJECT,
                    code="secret_filename",
                    detail=rel,
                )
            )
        text = _text_or_empty(content)
        if text is None:
            continue
        if any(secret and secret in text for secret in registered):
            findings.append(
                ValidationFinding(
                    check="secrets",
                    result=REJECT,
                    code="secret_registered",
                    detail=rel,
                )
            )
            continue
        redacted = redact_evidence_text(text)
        if redacted != text and REDACTED in redacted:
            findings.append(
                ValidationFinding(
                    check="secrets",
                    result=REJECT,
                    code="secret_registered",
                    detail=rel,
                )
            )
            continue
        if _PEM_PRIVATE_RE.search(text) or _ASSIGNED_SECRET_RE.search(text):
            findings.append(
                ValidationFinding(
                    check="secrets",
                    result=REJECT,
                    code="secret_material",
                    detail=rel,
                )
            )
    for key in frontmatter:
        if _SECRET_KEY_RE.search(str(key)):
            findings.append(
                ValidationFinding(
                    check="secrets",
                    result=REJECT,
                    code="secret_key",
                    detail=str(key),
                )
            )
    return findings


def _text_or_empty(content: str | bytes) -> str | None:
    if isinstance(content, str):
        return content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _check_privilege(
    files: Mapping[str, str | bytes],
    baseline_files: Mapping[str, str | bytes],
    frontmatter: Mapping[str, Any],
    specific_approvals: Sequence[str],
) -> tuple[tuple[str, ...], list[ValidationFinding]]:
    proposed = _privilege_grants(files, frontmatter)
    baseline_meta, _ = _parse_frontmatter_only(baseline_files)
    baseline = _privilege_grants(baseline_files, baseline_meta)
    added = tuple(sorted(proposed - baseline))
    if not added:
        return (), []
    approved = _specific_approval_set(specific_approvals)
    uncovered = [grant for grant in added if grant not in approved]
    if not uncovered:
        return added, []
    findings = [
        ValidationFinding(
            check="privilege",
            result=NEEDS_HUMAN_REVIEW,
            code=_privilege_code(grant),
            detail=grant,
        )
        for grant in uncovered
    ]
    return added, findings


def _parse_frontmatter_only(
    files: Mapping[str, str | bytes],
) -> tuple[dict[str, Any], None]:
    text = _manifest_text(files)
    if not text:
        return {}, None
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, None
    try:
        parsed = yaml.safe_load(match.group("body"))
    except yaml.YAMLError:
        return {}, None
    if isinstance(parsed, dict):
        return parsed, None
    return {}, None


def _privilege_grants(
    files: Mapping[str, str | bytes],
    frontmatter: Mapping[str, Any],
) -> set[str]:
    grants: set[str] = set()
    for token in _tool_tokens(frontmatter):
        grants.add(f"allowed-tools:{token}")
        if _bare_tool(token) in SHELL_TOOLS:
            grants.add("shell")
    for hook in _hook_names(files, frontmatter):
        grants.add(f"hooks:{hook}")
    for mode in _permission_modes(frontmatter):
        grants.add(f"permissions:{mode}")
        if mode in PERMISSIVE_MODES:
            grants.add("shell")
    if _has_shell_files(files):
        grants.add("shell")
    return grants


def _tool_tokens(frontmatter: Mapping[str, Any]) -> tuple[str, ...]:
    value = frontmatter.get("allowed-tools", frontmatter.get("allowed_tools"))
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in _SPLIT_TOOLS.split(value) if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _hook_names(
    files: Mapping[str, str | bytes],
    frontmatter: Mapping[str, Any],
) -> tuple[str, ...]:
    names: list[str] = []
    raw = frontmatter.get("hooks")
    if isinstance(raw, str) and raw.strip():
        names.append(raw.strip())
    elif isinstance(raw, Mapping):
        names.extend(str(key).strip() for key in raw if str(key).strip())
    elif isinstance(raw, (list, tuple)):
        names.extend(str(item).strip() for item in raw if str(item).strip())
    for rel in files:
        cleaned = rel.replace("\\", "/")
        if cleaned == "hooks" or cleaned.startswith("hooks/"):
            names.append(cleaned)
    return tuple(dict.fromkeys(name for name in names if name))


def _permission_modes(frontmatter: Mapping[str, Any]) -> tuple[str, ...]:
    modes: list[str] = []
    for key in ("permission_mode", "permission-mode", "permissions"):
        raw = frontmatter.get(key)
        if raw is None:
            continue
        if isinstance(raw, str) and raw.strip():
            modes.append(raw.strip())
        elif isinstance(raw, (list, tuple)):
            modes.extend(str(item).strip() for item in raw if str(item).strip())
        elif isinstance(raw, Mapping):
            modes.extend(str(key).strip() for key in raw if str(key).strip())
    return tuple(dict.fromkeys(modes))


def _has_shell_files(files: Mapping[str, str | bytes]) -> bool:
    for rel, content in files.items():
        cleaned = rel.replace("\\", "/")
        if cleaned.endswith(".sh") or "/shell/" in f"/{cleaned}":
            return True
        text = _text_or_empty(content)
        if text and text.startswith(("#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash")):
            return True
    return False


def _bare_tool(token: str) -> str:
    cleaned = (token or "").strip()
    if "(" in cleaned:
        return cleaned.split("(", 1)[0].strip()
    return cleaned


def _specific_approval_set(raw: Sequence[str]) -> frozenset[str]:
    found: set[str] = set()
    for item in raw:
        token = str(item or "").strip()
        if not token or token.lower() in GENERIC_APPROVALS:
            continue
        found.add(token)
    return frozenset(found)


def _privilege_code(grant: str) -> str:
    axis = grant.split(":", 1)[0]
    if axis in PRIVILEGE_AXES:
        return f"privilege_{axis.replace('-', '_')}"
    return "privilege_unapproved"


def _finalize(
    findings: Sequence[ValidationFinding],
    extensions: Sequence[str],
) -> ValidationResult:
    checks: dict[str, str] = {}
    worst = APPROVE
    for finding in findings:
        current = checks.get(finding.check)
        if current != REJECT:
            checks[finding.check] = finding.result
        if finding.result == REJECT:
            worst = REJECT
        elif finding.result == NEEDS_HUMAN_REVIEW and worst != REJECT:
            worst = NEEDS_HUMAN_REVIEW
    reason = ""
    if findings:
        first = next((item for item in findings if item.result == worst), findings[0])
        reason = first.code
    return ValidationResult(
        result=worst,
        mutated=False,
        reason=reason,
        findings=tuple(findings),
        privilege_extensions=tuple(extensions),
        checks=checks,
    )
