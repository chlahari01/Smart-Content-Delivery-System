"""Controller decisions exercised with fake switches (needs OS-Ken, not Mininet)."""

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from os_ken.lib.packet import ethernet, ipv4, packet, tcp, udp
    from os_ken.ofproto import ofproto_v1_3, ofproto_v1_3_parser
    import controller
except ImportError:  # OS-Ken lives in the controller virtualenv
    controller = None


class FakeDatapath:
    ofproto = ofproto_v1_3 if controller else None
    ofproto_parser = ofproto_v1_3_parser if controller else None

    def __init__(self, dpid):
        self.id = dpid
        self.sent = []

    def send_msg(self, message):
        self.sent.append(message)

    def flow_mods(self):
        return [m for m in self.sent if isinstance(m, ofproto_v1_3_parser.OFPFlowMod)]


class FakeMsg:
    def __init__(self, datapath, in_port, data):
        self.datapath = datapath
        self.match = {"in_port": in_port}
        self.data = data
        self.buffer_id = ofproto_v1_3.OFP_NO_BUFFER


class FakeEvent:
    def __init__(self, msg):
        self.msg = msg


def frame(src_ip, dst_ip, l4, payload=b""):
    pkt = packet.Packet()
    pkt.add_protocol(ethernet.ethernet(ethertype=0x0800))
    pkt.add_protocol(ipv4.ipv4(src=src_ip, dst=dst_ip, proto=17 if isinstance(l4, udp.udp) else 6))
    pkt.add_protocol(l4)
    if payload:
        pkt.add_protocol(payload)
    pkt.serialize()
    return bytes(pkt.data)


@unittest.skipIf(controller is None, "OS-Ken is not installed in this interpreter")
class ControllerLogicTests(unittest.TestCase):
    def setUp(self):
        self.app = controller.SmartCDNController()
        self.switches = {dpid: FakeDatapath(dpid) for dpid in (1, 2, 3, 4)}
        self.app.datapaths = dict(self.switches)
        self.next_port = 40000

    def tearDown(self):
        self.app.monitor_thread.kill()

    def heartbeat(self, name, active=0, text=None, src_ip=None, in_port=None):
        backend = controller.BACKENDS[name]
        data = frame(src_ip or backend["ip"], controller.VIP,
                     udp.udp(src_port=50000, dst_port=controller.HEARTBEAT_PORT),
                     (text or f"HB {name} {active}").encode())
        self.app._packet_in(FakeEvent(FakeMsg(self.switches[4], in_port or backend["port"], data)))

    def request(self, client_ip="10.0.0.1", in_port=1):
        """Send a new VIP connection; return the backend IP it was bound to."""
        self.next_port += 1
        for switch in self.switches.values():
            switch.sent.clear()
        data = frame(client_ip, controller.VIP,
                     tcp.tcp(src_port=self.next_port, dst_port=controller.SERVICE_PORT))
        self.app._packet_in(FakeEvent(FakeMsg(self.switches[1], in_port, data)))
        binding = self.app.bindings.get((client_ip, self.next_port, controller.SERVICE_PORT))
        return binding["backend"] if binding else None

    def test_backend_without_heartbeat_gets_no_traffic(self):
        self.assertIsNone(self.request())
        self.assertEqual(self.switches[1].flow_mods(), [])
        self.heartbeat("srv2")
        self.assertEqual(self.request(), "srv2")
        self.assertTrue(self.switches[1].flow_mods())
        self.assertTrue(self.switches[4].flow_mods())

    def test_new_flows_go_to_least_loaded_backend(self):
        self.heartbeat("srv1", 3)
        self.heartbeat("srv2", 1)
        self.heartbeat("srv3", 2)
        self.assertEqual(self.request(), "srv2")   # 1 -> 2
        self.assertIn(self.request(), ("srv2", "srv3"))  # tie at 2
        self.assertEqual(self.app.backend_load["srv1"], 3)

    def test_idle_backends_share_flows_round_robin(self):
        for name in controller.BACKEND_ORDER:
            self.heartbeat(name)
        chosen = [self.request() for _ in range(6)]
        self.assertEqual(sorted(chosen), ["srv1", "srv1", "srv2", "srv2", "srv3", "srv3"])

    def test_silent_backend_is_removed_and_recovers(self):
        for name in controller.BACKEND_ORDER:
            self.heartbeat(name)
        self.app.backend_seen["srv1"] = time.time() - controller.HEARTBEAT_TIMEOUT - 1
        self.switches[4].sent.clear()
        self.app._refresh_backends()
        self.assertFalse(self.app.backend_up["srv1"])
        deletes = [m for m in self.switches[4].flow_mods()
                   if m.command == ofproto_v1_3.OFPFC_DELETE]
        self.assertTrue(deletes, "stale flows must be cleared when a backend dies")
        self.assertNotIn("srv1", {self.request() for _ in range(6)})
        self.heartbeat("srv1")
        self.assertTrue(self.app.backend_up["srv1"])

    def test_goodbye_removes_backend_immediately(self):
        self.heartbeat("srv1")
        self.heartbeat("srv1", text="BYE srv1")
        self.assertFalse(self.app.backend_up["srv1"])

    def test_spoofed_heartbeat_is_ignored(self):
        self.heartbeat("srv1", src_ip="10.0.0.12")          # wrong address
        self.heartbeat("srv1", in_port=4)                    # wrong switch port
        self.assertFalse(self.app.backend_up["srv1"])

    def test_backend_link_down_overrides_heartbeat(self):
        self.heartbeat("srv3")
        self.app.backend_link_up["srv3"] = False
        self.app._refresh_backends()
        self.assertFalse(self.app.backend_up["srv3"])

    def test_path_failure_moves_new_flows_to_other_path(self):
        self.heartbeat("srv1")
        self.app.path_up["A"] = False
        self.request()
        self.assertTrue(self.switches[3].flow_mods())
        self.assertEqual(self.switches[2].flow_mods(), [])

    def test_congested_path_is_avoided(self):
        self.heartbeat("srv1")
        self.app.path_load.update(A=0.9, B=0.1)
        for _ in range(3):
            self.request()
            self.assertEqual(self.switches[2].flow_mods(), [])
            self.assertTrue(self.switches[3].flow_mods())

    def test_blocked_client_gets_drop_rule(self):
        self.heartbeat("srv1")
        blocked = controller.BLOCKED_CLIENTS
        controller.BLOCKED_CLIENTS = {"10.0.0.2"}
        try:
            self.assertIsNone(self.request("10.0.0.2", in_port=2))
        finally:
            controller.BLOCKED_CLIENTS = blocked
        (rule,) = self.switches[1].flow_mods()
        self.assertEqual(rule.priority, controller.BLOCK_PRIORITY)
        self.assertEqual(rule.instructions[0].actions, [])
        self.assertEqual(self.switches[4].flow_mods(), [])


if __name__ == "__main__":
    unittest.main()
