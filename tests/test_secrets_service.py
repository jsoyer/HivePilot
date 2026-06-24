"""
Tests for hivepilot.services.secrets_service — Vault and SOPS resolvers.

All network/binary calls are mocked — no real Vault or sops required.
Imports the comprehensive test suite from test_vault_resolver so both
file naming conventions are satisfied.
"""

from tests.test_vault_resolver import *  # noqa: F401,F403
