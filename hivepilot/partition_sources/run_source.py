"""`RunSource` -- the `run` built-in partition source (propose -> ratify ->
dispatch PRD, Sprint 1, spec section 4): a failed/completed run + its steps,
as proposer-fetchable context. Reads HivePilot's own `runs`/`steps` tables
via the existing `state_service.get_run`/`get_steps_for_run` accessors --
no new persistence, no new query surface, no config, no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hivepilot.services import state_service

if TYPE_CHECKING:
    from hivepilot.partition_sources import SourceDocument


class RunSourceError(RuntimeError):
    """Raised when *ref* is blank, isn't an integer run id, or doesn't
    resolve to an existing `runs` row."""


class RunSource:
    """`PartitionSource` reading a single run + its steps by id."""

    name = "run"

    def fetch(self, ref: str) -> "SourceDocument":
        from hivepilot.partition_sources import SourceDocument, compute_digest

        cleaned = (ref or "").strip()
        if not cleaned:
            raise RunSourceError("run source requires a non-blank run id")
        try:
            run_id = int(cleaned)
        except ValueError as exc:
            raise RunSourceError(f"run source ref must be an integer run id, got {ref!r}") from exc

        run = state_service.get_run(run_id)
        if run is None:
            raise RunSourceError(f"no run found with id {run_id}")
        steps = state_service.get_steps_for_run(run_id)

        lines = [
            f"Run #{run_id} -- project={run.get('project')!r} task={run.get('task')!r} "
            f"status={run.get('status')!r}"
        ]
        if run.get("detail"):
            lines.append(f"detail: {run['detail']}")
        for step in steps:
            lines.append(
                f"  step={step.get('step')!r} status={step.get('status')!r} "
                f"detail={step.get('detail')!r}"
            )
        content = "\n".join(lines)

        return SourceDocument(
            kind="run",
            ref=cleaned,
            digest=compute_digest(content),
            content=content,
            metadata={"run_id": run_id, "status": run.get("status")},
        )
