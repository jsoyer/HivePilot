"""Tests for `hivepilot.services.partition_service` -- the durable partition
journal (propose -> ratify -> dispatch PRD, Sprint 2, spec section 8).

The ratification GATE's fail-closed validation order lives in its own
sibling module, `tests/test_partition_ratify_validation.py`; this file
covers persistence, the conditional-UPDATE state machine, idempotency and
audit.
"""

from __future__ import annotations
