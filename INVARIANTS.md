# Architecture Invariant Registry

Cross-cutting properties that must hold across the whole engine, each with a
machine-verifiable check. Run them all with:

```bash
grep -oP '(?<=^- \*\*Verify:\*\* `).*(?=`$)' INVARIANTS.md | while read -r c; do
  eval "$c" >/dev/null 2>&1 || echo "VIOLATED: $c"
done
```

Every `Verify:` command exits 0 when the invariant holds. They are test
invocations rather than greps: a grep asserts that a line of code looks a
certain way, a test asserts that the behaviour is a certain way, and only the
second survives a refactor.

---

## Partition dispatch — human ratification

- **Owner:** `hivepilot/services/partition_service.py`
- **Preconditions:** a partition row exists with `status='proposed'`.
- **Postconditions:** no run is created and no queue row is written until a human has ratified.
- **Invariants:** a partition NEVER dispatches without human ratification. No code path reaches the dispatcher from `status='proposed'`.
- **Verify:** `python3 -m pytest -q tests/test_partition_dispatch.py -k "not_ratified or proposed"`
- **Fix:** gate the dispatcher on `mark_partition_dispatching()`, which returns `False` from `proposed`.

## Partition dispatch — outward consent is separate

- **Owner:** `hivepilot/services/partition_service.py`
- **Preconditions:** the plan's computed outward footprint is non-empty.
- **Postconditions:** ratification is refused unless `outward_consent` is explicitly true.
- **Invariants:** consenting to RUN agents is a distinct decision from consenting to PUBLISH outward. `is_destructive` (could this damage the target) and outward (may this become visible outside this machine) are independent axes; both must be satisfied.
- **Verify:** `python3 -m pytest -q tests/test_partition_ratify_validation.py -k consent`
- **Fix:** keep the consent check in `_check_policy` as its own step; never infer consent from the plan.

## Partition dispatch — outward allowlist fails closed

- **Owner:** `policies.yaml` (`outward_actions`), enforced by `partition_service`
- **Preconditions:** none — this must hold when the key is absent, empty, partial, or typo'd.
- **Postconditions:** an absent or empty allowlist permits NOTHING outward.
- **Invariants:** an empty value means DENY, never "no constraint". This is the repo's most frequently repeated bug class; do not reintroduce it here.
- **Verify:** `python3 -m pytest -q tests/test_partition_ratify_validation.py::TestStep3OutwardAllowlist`
- **Fix:** in `_check_policy`, treat a missing/empty allowlist as the empty set and deny every token against it.

## Partition dispatch — never auto-merges

- **Owner:** `hivepilot/services/partition_service.py`
- **Preconditions:** a task names a pipeline whose `git.merge_pr` is true.
- **Postconditions:** ratification is refused, regardless of consent or allowlist.
- **Invariants:** `merge_pr: true` is refused UNCONDITIONALLY in a partition dispatch — inherited from autopilot's never-auto-merge invariant. Allowlisting `forge_merge` does not override it.
- **Verify:** `python3 -m pytest -q tests/test_partition_ratify_validation.py -k merge_pr`
- **Fix:** keep the unconditional refusal ahead of the allowlist check, not inside it.

## Partition dispatch — edits validate against live config

- **Owner:** `hivepilot/services/partition_service.py::validate_ratification`
- **Preconditions:** the operator submitted an edited plan.
- **Postconditions:** every field is re-checked against `pipelines.yaml` / `projects.yaml` / `policies.yaml` as they are NOW.
- **Invariants:** the editable JSON is NOT a privilege-escalation surface. An edit naming an out-of-policy pipeline is denied even when the original proposal was valid. The proposal is never the authority.
- **Verify:** `python3 -m pytest -q tests/test_partition_ratify_validation.py::TestStep2Referential tests/test_partition_ratify_validation.py::TestPolicyIsReadLiveNotFromTheProposal`
- **Fix:** resolve pipelines/projects/policy from the live loaders inside `validate_ratification`; never trust a value carried in the submitted plan.

## Partition dispatch — a ratified partition is immutable

- **Owner:** `hivepilot/services/partition_service.py`
- **Preconditions:** a partition has reached `status='ratified'`.
- **Postconditions:** a second ratify is a no-op; the approver of record and the ratified plan never change.
- **Invariants:** immutability is what makes the audit trail meaningful. A retry creates a NEW run under the same `(partition_id, task_id)` with `attempt += 1` — never a new plan. Wanting a different plan means a new partition.
- **Verify:** `python3 -m pytest -q tests/test_partition_service.py -k "immutable or noop"`
- **Fix:** keep ratify as `UPDATE ... WHERE id=? AND status='proposed'` and treat `rowcount != 1` as a no-op.

## Partition dispatch — claim before create

- **Owner:** `hivepilot/services/partition_service.py`
- **Preconditions:** a wave is being dispatched.
- **Postconditions:** a crash at any point leaves either nothing, or a visible `claimed` row with `run_id IS NULL` — never two runs for one task.
- **Invariants:** the atomic claim happens BEFORE the run row is created. The reconciler sweeps only `status='claimed' AND run_id IS NULL`, exactly once, so recovery can never double-dispatch. An absent or unparseable `claimed_at` is treated as NOT stale — rewinding a possibly-live claim is worse than a stuck row an operator can see.
- **Verify:** `python3 -m pytest -q tests/test_partition_reconcile.py`
- **Fix:** preserve the claim → create → submit order and the `run_id IS NULL` predicate in the reconciler.

## Partition dispatch — queue isolation from autopilot

- **Owner:** `hivepilot/services/autopilot_queue.py`
- **Preconditions:** partition tasks and autopilot objectives share the `autopilot_queue` table.
- **Postconditions:** `drain_one` never picks a partition task; the partition dispatcher never picks an objective.
- **Invariants:** the two dispatchers never contend. Isolation is enforced on TWO independent axes: `next_dispatchable` filters `kind='objective'`, AND partition rows are enqueued in state `running` rather than `queued`.
- **Verify:** `python3 -m pytest -q tests/test_partition_dispatch.py::TestQueueIsolation tests/test_partition_service.py -k kind`
- **Fix:** keep both the `kind` filter and the `running` initial state; either alone is a single point of failure.
