"""
tests/test_recursion_error_handling.py

Regression tests for issue #163 — "RecursionError 'maximum recursion depth
exceeded' stops the operation".

Two complementary guards are exercised:

  1. ``scanner`` raises Python's default recursion limit at import time so
     defensive code paths inside dependencies don't hit the cap on large
     image collections.

  2. ``error_handler.format_scan_error`` recognises ``RecursionError`` —
     both directly and when it's been re-wrapped by the worker thread as
     ``Exception(str(original_exc))`` — and produces an actionable
     user-facing message instead of the generic "an unexpected error
     stopped the operation" text.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import error_handler
import scanner


class TestScannerRecursionLimit(unittest.TestCase):
    """Importing scanner must guarantee a generous recursion limit."""

    def test_recursion_limit_is_raised_to_safe_floor(self):
        # The scanner module bumps the limit to at least 5 000 frames so
        # large duplicate collections don't trip Python's default of 1 000.
        self.assertGreaterEqual(
            sys.getrecursionlimit(), 5000,
            "scanner must raise sys.recursionlimit to >= 5000",
        )


class TestRecursionErrorClassification(unittest.TestCase):
    """``format_scan_error`` must produce a clear, actionable message for
    RecursionError, not the generic fallback."""

    GENERIC_PHRASE = "an unexpected error stopped the operation"
    RECURSION_PHRASE = "call-stack limit"

    def test_direct_recursion_error_gets_specific_message(self):
        exc = RecursionError("maximum recursion depth exceeded")
        user_msg, _detail = error_handler.format_scan_error(exc, tb="")
        self.assertIn(
            self.RECURSION_PHRASE, user_msg.lower(),
            "RecursionError must surface the dedicated user message, "
            f"got: {user_msg!r}",
        )
        self.assertNotIn(
            self.GENERIC_PHRASE, user_msg.lower(),
            "RecursionError must NOT fall back to the generic message",
        )

    def test_wrapped_recursion_error_in_message_string(self):
        """The worker thread wraps exceptions as ``Exception(str(exc))`` —
        the classifier must still detect the original type from the message."""
        exc = Exception("maximum recursion depth exceeded while calling a Python object")
        user_msg, _detail = error_handler.format_scan_error(exc, tb="")
        self.assertIn(
            self.RECURSION_PHRASE, user_msg.lower(),
            "Wrapped RecursionError (by message) must still get the "
            f"specific user message, got: {user_msg!r}",
        )

    def test_wrapped_recursion_error_in_traceback(self):
        """Some worker paths only have the original error in the traceback
        text (the message string was scrubbed).  The classifier must scan
        the traceback too."""
        exc = Exception("scan failed")
        tb = (
            "Traceback (most recent call last):\n"
            "  File \"scanner.py\", line 999, in find_groups\n"
            "    ...\n"
            "RecursionError: maximum recursion depth exceeded\n"
        )
        user_msg, detail = error_handler.format_scan_error(exc, tb)
        self.assertIn(
            self.RECURSION_PHRASE, user_msg.lower(),
            "RecursionError in traceback must trigger the specific message, "
            f"got: {user_msg!r}",
        )
        # Traceback content should still appear in the developer detail
        self.assertIn("RecursionError", detail)

    def test_unrelated_exception_still_uses_generic_message(self):
        """The new RecursionError detection must not swallow other errors."""
        exc = Exception("Some unrelated failure")
        user_msg, _detail = error_handler.format_scan_error(exc, tb="")
        self.assertNotIn(
            self.RECURSION_PHRASE, user_msg.lower(),
            "Unrelated Exception must NOT be misclassified as RecursionError",
        )
        self.assertIn(
            self.GENERIC_PHRASE, user_msg.lower(),
            "Unrelated Exception should fall back to the generic message",
        )


if __name__ == "__main__":
    unittest.main()
