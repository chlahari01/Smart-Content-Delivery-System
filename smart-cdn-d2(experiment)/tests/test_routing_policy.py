import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routing_policy import choose_backend, choose_path, heartbeat_fresh, parse_heartbeat


class RoutingPolicyTests(unittest.TestCase):
    def test_lower_utilization_path_is_selected(self):
        selected, cursor = choose_path(
            {"A": True, "B": True}, {"A": 0.72, "B": 0.11}
        )
        self.assertEqual(selected, "B")
        self.assertEqual(cursor, 1)

    def test_equal_paths_rotate(self):
        up = {"A": True, "B": True}
        loads = {"A": 0.0, "B": 0.0}
        first, cursor = choose_path(up, loads, 0)
        second, cursor = choose_path(up, loads, cursor)
        self.assertEqual((first, second), ("A", "B"))

    def test_down_path_is_excluded(self):
        selected, _ = choose_path(
            {"A": False, "B": True}, {"A": 0.0, "B": 0.9}
        )
        self.assertEqual(selected, "B")

    def test_no_path_returns_none(self):
        selected, cursor = choose_path({"A": False, "B": False}, {}, 3)
        self.assertIsNone(selected)
        self.assertEqual(cursor, 3)

    def test_backend_round_robin_skips_down_server(self):
        order = ("srv1", "srv2", "srv3")
        status = {"srv1": False, "srv2": True, "srv3": True}
        first, cursor = choose_backend(order, status, 0)
        second, _ = choose_backend(order, status, cursor)
        self.assertEqual((first, second), ("srv2", "srv3"))

    def test_no_backend_returns_none(self):
        selected, cursor = choose_backend(("srv1",), {"srv1": False}, 2)
        self.assertIsNone(selected)
        self.assertEqual(cursor, 2)

    def test_least_loaded_backend_is_preferred(self):
        order = ("srv1", "srv2", "srv3")
        status = dict.fromkeys(order, True)
        selected, _ = choose_backend(order, status, 0, {"srv1": 2, "srv2": 0, "srv3": 1})
        self.assertEqual(selected, "srv2")

    def test_equally_loaded_backends_rotate(self):
        order = ("srv1", "srv2", "srv3")
        status = dict.fromkeys(order, True)
        load = {"srv1": 1, "srv2": 1, "srv3": 4}
        first, cursor = choose_backend(order, status, 0, load)
        second, _ = choose_backend(order, status, cursor, load)
        self.assertEqual((first, second), ("srv1", "srv2"))

    def test_idle_but_down_backend_is_never_chosen(self):
        order = ("srv1", "srv2")
        selected, _ = choose_backend(
            order, {"srv1": False, "srv2": True}, 0, {"srv1": 0, "srv2": 9})
        self.assertEqual(selected, "srv2")

    def test_heartbeat_parsing(self):
        self.assertEqual(parse_heartbeat(b"HB srv2 3"), ("srv2", 3, True))
        self.assertEqual(parse_heartbeat(b"BYE srv1"), ("srv1", 0, False))
        for bad in (b"", b"HB srv1", b"HB srv1 -1", b"HELLO srv1 1", b"\xff\xfe"):
            self.assertIsNone(parse_heartbeat(bad))

    def test_heartbeat_freshness(self):
        self.assertFalse(heartbeat_fresh(None, 100.0, 3.0))
        self.assertTrue(heartbeat_fresh(98.0, 100.0, 3.0))
        self.assertFalse(heartbeat_fresh(96.0, 100.0, 3.0))


if __name__ == "__main__":
    unittest.main()
