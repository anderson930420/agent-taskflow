from __future__ import annotations

import unittest

from agent_taskflow._helpers import (
    dedupe_non_empty_preserve_order,
    dedupe_preserve_order,
    require_non_empty,
)


class HelpersTests(unittest.TestCase):
    def test_dedupe_preserve_order_keeps_first_empty_string(self) -> None:
        self.assertEqual(
            dedupe_preserve_order(["a", "", "b", "a", "", "c"]),
            ["a", "", "b", "c"],
        )

    def test_dedupe_non_empty_preserve_order_drops_empty_strings(self) -> None:
        self.assertEqual(
            dedupe_non_empty_preserve_order(["a", "", "b", "a", "", "c"]),
            ["a", "b", "c"],
        )

    def test_require_non_empty_strips_and_preserves_error_message(self) -> None:
        self.assertEqual(require_non_empty(" value ", "field"), "value")
        with self.assertRaisesRegex(ValueError, "field must not be empty"):
            require_non_empty("   ", "field")


if __name__ == "__main__":
    unittest.main()
