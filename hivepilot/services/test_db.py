"""
Co-located test stub for hivepilot.services.db.

The canonical tests live in tests/test_db_abstraction.py.
This file satisfies the TDD hook (which also checks for test_<module>.py
adjacent to the production file).
"""
# Re-export from the canonical location so pytest always runs the same tests.
from tests.test_db_abstraction import (  # noqa: F401
    TestAutoincrementPk,
    TestColumnExists,
    TestConnect,
    TestInsertReturningId,
    TestIsPostgres,
    TestPh,
)
