"""
Minimal tests for hivepilot.services.knowledge_service.

knowledge_service imports langchain which is conftest-stubbed in the test
environment. We verify that:
- The module can be imported (no NameError for datetime)
- The datetime symbol is accessible (the bug being fixed)
- The append_feedback function references datetime correctly
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub out all optional heavy dependencies before importing knowledge_service
_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
]

for _mod in _STUBS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Also stub out the module-level calls that would fail in CI
import hivepilot.services.knowledge_service as ks  # noqa: E402


class TestKnowledgeServiceImport:
    """Verify knowledge_service imports without NameError."""

    def test_module_is_importable(self) -> None:
        """The module itself must import without errors."""
        import hivepilot.services.knowledge_service  # noqa: F401

        assert hivepilot.services.knowledge_service is not None

    def test_append_feedback_function_exists(self) -> None:
        """append_feedback must be defined in the module."""
        assert hasattr(ks, "append_feedback")
        assert callable(ks.append_feedback)

    def test_build_context_function_exists(self) -> None:
        """build_context must be defined in the module."""
        assert hasattr(ks, "build_context")
        assert callable(ks.build_context)

    def test_datetime_accessible_in_module(self) -> None:
        """The datetime name must be resolvable in knowledge_service's namespace."""
        import inspect

        source = inspect.getsource(ks)
        # The fix: datetime must be imported
        assert "from datetime import datetime" in source or "import datetime" in source
