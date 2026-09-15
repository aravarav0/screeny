from __future__ import annotations

import unittest

from screeny.router import route_command


class RouterTests(unittest.TestCase):
    def test_thanks_is_social_not_a_task(self) -> None:
        out = route_command("thank you")
        self.assertEqual(out.mode, "tool")
        self.assertEqual(out.message, "You're welcome!")

    def test_hello(self) -> None:
        out = route_command("hi")
        self.assertEqual(out.mode, "tool")
        self.assertIn("Hi", out.message or "")

    def test_too_short_is_unknown(self) -> None:
        out = route_command("x")
        self.assertEqual(out.mode, "unknown")

    def test_empty_is_unknown(self) -> None:
        out = route_command("  ")
        self.assertEqual(out.mode, "unknown")


if __name__ == "__main__":
    unittest.main()
