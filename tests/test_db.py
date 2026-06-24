"""
Tests for hivepilot.services.db — re-exports from the full abstraction test suite.

The canonical test file is tests/test_db_abstraction.py.
This stub satisfies the TDD hook which expects tests/test_db.py for db.py.
"""

# Re-export all tests so pytest discovers them from this module too.
from tests.test_db_abstraction import (  # noqa: F401
    TestAutoincrementPk,
    TestColumnExists,
    TestConnect,
    TestInsertReturningId,
    TestIsPostgres,
    TestPh,
)
