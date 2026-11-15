"""OS-Ken/OpenFlow 1.3 controller for the Smart CDN Mininet demo.

Topology and port map are defined in cdn_topology.py. The controller chooses a
live backend round-robin and a least-loaded live path for each new VIP flow.
"""

from __future__ import annotations

import time
import os

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib import hub
from os_ken.lib.packet import ipv4, packet, tcp
from os_ken.ofproto import ofproto_v1_3

from routing_policy import choose_backend, choose_path

VIP = "10.0.0.100"
VIP_MAC = "00:00:00:00:00:aa"
SERVICE_PORT = 5000
LINK_CAPACITY_BPS = float(os.environ.get("CDN_LINK_BANDWIDTH_MBPS", "10")) * 1_000_000
APP_COOKIE = 0xCD000001
COOKIE_MASK = 0xFFFFFFFFFFFFFFFF
FLOW_IDLE_TIMEOUT = 180
FIXED_PATH = os.environ.get("CDN_FIXED_PATH")
FIXED_BACKEND = os.environ.get("CDN_FIXED_BACKEND")

if FIXED_PATH not in (None, "A", "B"):
    raise ValueError("CDN_FIXED_PATH must be A or B")

# Host IP: (edge DPID, edge port, MAC, side)
HOSTS = {
    "10.0.0.1": (1, 1, "00:00:00:00:00:01", "client"),
    "10.0.0.2": (1, 2, "00:00:00:00:00:02", "client"),
    "10.0.0.11": (4, 3, "00:00:00:00:00:0b", "backend"),
    "10.0.0.12": (4, 4, "00:00:00:00:00:0c", "backend"),
    "10.0.0.13": (4, 5, "00:00:00:00:00:0d", "backend"),
}

BACKENDS = {
    "srv1": {"ip": "10.0.0.11", "mac": "00:00:00:00:00:0b", "port": 3},
    "srv2": {"ip": "10.0.0.12", "mac": "00:00:00:00:00:0c", "port": 4},
    "srv3": {"ip": "10.0.0.13", "mac": "00:00:00:00:00:0d", "port": 5},
}
BACKEND_ORDER = ("srv1", "srv2", "srv3")

if FIXED_BACKEND not in (None, *BACKEND_ORDER):
    raise ValueError("CDN_FIXED_BACKEND must be srv1, srv2, or srv3")

# For each path: source-edge port, middle switch, middle in/out ports,
# destination-edge ingress/egress ports, and the switches that carry it.
PATHS = {
    "A": {
        "s1_out": 3,
        "mid_dpid": 2,
        "mid_in": 1,
        "mid_out": 2,
        "s4_in": 1,
        "s4_out": 1,
        "switch_dpids": ((1, 3), (2, 2), (4, 1)),
    },
    "B": {
        "s1_out": 4,
        "mid_dpid": 3,
        "mid_in": 1,
        "mid_out": 2,
        "s4_in": 2,
        "s4_out": 2,
        "switch_dpids": ((1, 4), (3, 2), (4, 2)),
    },
}


class SmartCDNController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.path_up = {"A": True, "B": True}
        self.path_load = {"A": 0.0, "B": 0.0}
        self.path_cursor = 0
        self.backend_up = {name: True for name in BACKEND_ORDER}
        self.backend_cursor = 0
        # Key: (client IP, client TCP source port, service port).
        self.bindings = {}
        self.port_samples = {}
        self.monitor_thread = hub.spawn(self._monitor)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
            self.logger.info("switch connected: dpid=%s", datapath.id)
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)
            self.logger.warning("switch disconnected: dpid=%s", datapath.id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofp = datapath.ofproto
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, match, actions, priority=0, cookie=0)
        self.logger.info("OpenFlow 1.3 table-miss installed: dpid=%s", datapath.id)

    def _add_flow(self, datapath, match, actions, priority=100, cookie=APP_COOKIE):
        parser = datapath.ofproto_parser
        ofp = datapath.ofproto
        instructions = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        message = parser.OFPFlowMod(
            datapath=datapath,
            cookie=cookie,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=FLOW_IDLE_TIMEOUT if cookie == APP_COOKIE else 0,
        )
        datapath.send_msg(message)

    def _delete_app_flows(self):
        for datapath in list(self.datapaths.values()):
            parser = datapath.ofproto_parser
            ofp = datapath.ofproto
            datapath.send_msg(
                parser.OFPFlowMod(
                    datapath=datapath,
                    cookie=APP_COOKIE,
                    cookie_mask=COOKIE_MASK,
                    table_id=ofp.OFPTT_ALL,
                    command=ofp.OFPFC_DELETE,
                    out_port=ofp.OFPP_ANY,
                    out_group=ofp.OFPG_ANY,
                    match=parser.OFPMatch(),
                )
            )

    def _packet_out(self, msg, actions):
        datapath = msg.datapath
        ofp = datapath.ofproto
        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
        datapath.send_msg(
            datapath.ofproto_parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=msg.match["in_port"],
                actions=actions,
                data=data,
            )
        )

    def _choose_path(self):
        if FIXED_PATH:
            return FIXED_PATH if self.path_up[FIXED_PATH] else None
        path, self.path_cursor = choose_path(
            self.path_up, self.path_load, self.path_cursor
        )
        return path

    def _choose_backend(self):
        if FIXED_BACKEND:
            return FIXED_BACKEND if self.backend_up[FIXED_BACKEND] else None
        name, self.backend_cursor = choose_backend(
            BACKEND_ORDER, self.backend_up, self.backend_cursor
        )
        return name

    def _path_at_switch(self, dpid, in_port, forward):
        if forward and dpid == 1:
            return self._choose_path()
        if not forward and dpid == 4:
            return self._choose_path()
        for name, path in PATHS.items():
            if forward:
                if dpid == path["mid_dpid"] and in_port == path["mid_in"]:
                    return name
                if dpid == 4 and in_port == path["s4_in"]:
                    return name
            else:
                if dpid == path["mid_dpid"] and in_port == path["mid_out"]:
                    return name
                if dpid == 1 and in_port == path["s1_out"]:
                    return name
        return None

    def _install_host_route(self, msg, ip):
        """Install a route for ordinary IPv4 traffic, including pingall."""
        src_ip, dst_ip = ip.src, ip.dst
        src, dst = HOSTS.get(src_ip), HOSTS.get(dst_ip)
        if not src or not dst:
            return
        datapath = msg.datapath
        dpid = datapath.id
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]
        proto = ip.proto
        dst_mac = dst[2]
        base = dict(in_port=in_port, eth_type=0x0800, ip_proto=proto,
                    ipv4_src=src_ip, ipv4_dst=dst_ip)

        if src[0] == dst[0]:
            if dpid != src[0]:
                return
            actions = [parser.OFPActionSetField(eth_dst=dst_mac),
                       parser.OFPActionOutput(dst[1])]
            self._add_flow(datapath, parser.OFPMatch(**base), actions)
            self._packet_out(msg, actions)
            return

        forward = src[0] == 1 and dst[0] == 4
        reverse = src[0] == 4 and dst[0] == 1
        if not (forward or reverse):
            return
        path = self._path_at_switch(dpid, in_port, forward)
        if path is None or not self.path_up[path]:
            self.logger.warning("no live path for %s -> %s at dpid=%s", src_ip, dst_ip, dpid)
            return
        p = PATHS[path]
        mid = self.datapaths.get(p["mid_dpid"])
        s1 = self.datapaths.get(1)
        s4 = self.datapaths.get(4)
        if not mid or not s1 or not s4:
            self.logger.warning("path %s is not fully connected", path)
            return

        if forward:
            # Install the remaining hops as well as the first one. If a packet
            # races ahead of these rules, the intermediate-switch branch below
            # completes the route when its Packet-In arrives.
            if dpid == 1:
                actions = [parser.OFPActionSetField(eth_dst=dst_mac),
                           parser.OFPActionOutput(p["s1_out"])]
                self._add_flow(datapath, parser.OFPMatch(**base), actions)
            else:
                actions = [parser.OFPActionOutput(p["mid_out"] if dpid == p["mid_dpid"] else dst[1])]
                if dpid == 4:
                    actions.insert(0, parser.OFPActionSetField(eth_dst=dst_mac))

            if dpid in (1, p["mid_dpid"]):
                mid_match = mid.ofproto_parser.OFPMatch(
                    in_port=p["mid_in"], eth_type=0x0800, ip_proto=proto,
                    ipv4_src=src_ip, ipv4_dst=dst_ip)
                self._add_flow(mid, mid_match,
                               [mid.ofproto_parser.OFPActionOutput(p["mid_out"])])
            if dpid in (1, p["mid_dpid"], 4):
                edge_match = s4.ofproto_parser.OFPMatch(
                    in_port=p["s4_in"], eth_type=0x0800, ip_proto=proto,
                    ipv4_src=src_ip, ipv4_dst=dst_ip)
                edge_actions = [s4.ofproto_parser.OFPActionSetField(eth_dst=dst_mac),
                                s4.ofproto_parser.OFPActionOutput(dst[1])]
                self._add_flow(s4, edge_match, edge_actions)
                if dpid == 4:
                    actions = edge_actions
            self._packet_out(msg, actions)
        else:
            if dpid == 4:
                actions = [parser.OFPActionSetField(eth_dst=dst_mac),
                           parser.OFPActionOutput(p["s4_out"])]
                self._add_flow(s4, parser.OFPMatch(**base), actions)
            elif dpid == p["mid_dpid"]:
                actions = [parser.OFPActionOutput(p["mid_in"])]
            else:
                actions = [parser.OFPActionSetField(eth_dst=dst_mac),
                           parser.OFPActionOutput(dst[1])]

            if dpid in (4, p["mid_dpid"]):
                mid_match = mid.ofproto_parser.OFPMatch(
                    in_port=p["mid_out"], eth_type=0x0800, ip_proto=proto,
                    ipv4_src=src_ip, ipv4_dst=dst_ip)
                self._add_flow(mid, mid_match,
                               [mid.ofproto_parser.OFPActionOutput(p["mid_in"])])
            if dpid in (4, p["mid_dpid"], 1):
                edge_match = s1.ofproto_parser.OFPMatch(
                    in_port=p["s1_out"], eth_type=0x0800, ip_proto=proto,
                    ipv4_src=src_ip, ipv4_dst=dst_ip)
                edge_actions = [s1.ofproto_parser.OFPActionSetField(eth_dst=dst_mac),
                                s1.ofproto_parser.OFPActionOutput(dst[1])]
                self._add_flow(s1, edge_match, edge_actions)
                if dpid == 1:
                    actions = edge_actions
            self._packet_out(msg, actions)
        self.logger.info("IPv4 path: %s -> %s uses path %s", src_ip, dst_ip, path)

    def _handle_vip_reply_at_mid(self, msg, ip, tcp_header):
        """Finish a VIP return path if a packet races the first Flow-Mods."""
        key = self._vip_key(ip.dst, tcp_header.dst_port)
        binding = self.bindings.get(key)
        if not binding:
            return
        path_name = next((name for name, path in PATHS.items()
                          if path["mid_dpid"] == msg.datapath.id), None)
        if not path_name or not self.path_up[path_name]:
            return
        client = HOSTS.get(ip.dst)
        if not client:
            return
        path = PATHS[path_name]
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        mid_match = parser.OFPMatch(
            in_port=path["mid_out"], eth_type=0x0800, ip_proto=6,
            ipv4_src=VIP, ipv4_dst=ip.dst,
            tcp_src=SERVICE_PORT, tcp_dst=tcp_header.dst_port)
        mid_actions = [parser.OFPActionOutput(path["mid_in"])]
        self._add_flow(datapath, mid_match, mid_actions)
        s1 = self.datapaths.get(1)
        if s1:
            s1p = s1.ofproto_parser
            match = s1p.OFPMatch(
                in_port=path["s1_out"], eth_type=0x0800, ip_proto=6,
                ipv4_src=VIP, ipv4_dst=ip.dst,
                tcp_src=SERVICE_PORT, tcp_dst=tcp_header.dst_port)
            self._add_flow(s1, match, [s1p.OFPActionOutput(client[1])])
        self._packet_out(msg, mid_actions)

    def _vip_key(self, client_ip, client_port):
        return client_ip, client_port, SERVICE_PORT

    def _install_vip_flow(self, msg, client_ip, client_port, backend_name, path, first_direction):
        client = HOSTS.get(client_ip)
        backend = BACKENDS[backend_name]
        if not client or client[3] != "client":
            return
        p = PATHS[path]
        dps = self.datapaths
        if not all(dpid in dps for dpid in (1, p["mid_dpid"], 4)):
            self.logger.warning("cannot install VIP flow: switch path %s is incomplete", path)
            return

        s1, mid, s4 = dps[1], dps[p["mid_dpid"]], dps[4]
        p1, pm, p4 = s1.ofproto_parser, mid.ofproto_parser, s4.ofproto_parser
        common_fwd = dict(eth_type=0x0800, ip_proto=6, ipv4_src=client_ip,
                          ipv4_dst=backend["ip"], tcp_src=client_port,
                          tcp_dst=SERVICE_PORT)
        request_match = p1.OFPMatch(
            in_port=client[1], eth_type=0x0800, ip_proto=6,
            ipv4_src=client_ip, ipv4_dst=VIP,
            tcp_src=client_port, tcp_dst=SERVICE_PORT,
        )
        request_actions = [p1.OFPActionSetField(ipv4_dst=backend["ip"]),
                           p1.OFPActionSetField(eth_dst=backend["mac"]),
                           p1.OFPActionOutput(p["s1_out"])]
        self._add_flow(s1, request_match, request_actions)

        mid_match = pm.OFPMatch(in_port=p["mid_in"], **common_fwd)
        self._add_flow(mid, mid_match, [pm.OFPActionOutput(p["mid_out"])])
        server_match = p4.OFPMatch(in_port=p["s4_in"], **common_fwd)
        self._add_flow(s4, server_match, [p4.OFPActionOutput(backend["port"])])

        common_rev = dict(eth_type=0x0800, ip_proto=6, ipv4_src=VIP,
                          ipv4_dst=client_ip, tcp_src=SERVICE_PORT,
                          tcp_dst=client_port)
        server_reply_match = p4.OFPMatch(
            in_port=backend["port"], eth_type=0x0800, ip_proto=6,
            ipv4_src=backend["ip"], ipv4_dst=client_ip,
            tcp_src=SERVICE_PORT, tcp_dst=client_port,
        )
        server_reply_actions = [p4.OFPActionSetField(ipv4_src=VIP),
                                p4.OFPActionSetField(eth_src=VIP_MAC),
                                p4.OFPActionOutput(p["s4_out"])]
        self._add_flow(s4, server_reply_match, server_reply_actions)
        mid_reply_match = pm.OFPMatch(in_port=p["mid_out"], **common_rev)
        self._add_flow(mid, mid_reply_match, [pm.OFPActionOutput(p["mid_in"])])
        client_reply_match = p1.OFPMatch(in_port=p["s1_out"], **common_rev)
        self._add_flow(s1, client_reply_match, [p1.OFPActionOutput(client[1])])

        if first_direction == "request":
            self._packet_out(msg, request_actions)
        else:
            self._packet_out(msg, server_reply_actions)

    def _handle_vip_request(self, msg, ip, tcp_header):
        client_ip = ip.src
        key = self._vip_key(client_ip, tcp_header.src_port)
        binding = self.bindings.get(key)
        if binding and not self.backend_up.get(binding["backend"], False):
            self.logger.error("backend for existing TCP flow is down: %s; client must reconnect", key)
            return
        backend_name = binding["backend"] if binding else None
        if not backend_name:
            backend_name = self._choose_backend()
        path = self._choose_path()
        if not backend_name or path is None:
            self.logger.error("no healthy backend or live path for %s", key)
            return
        self.bindings[key] = {"backend": backend_name, "path": path, "updated": time.time()}
        self._install_vip_flow(msg, client_ip, tcp_header.src_port, backend_name, path, "request")
        self.logger.info("VIP decision: client=%s:%s backend=%s path=%s",
                         client_ip, tcp_header.src_port, backend_name, path)

    def _handle_vip_reply(self, msg, ip, tcp_header):
        key = self._vip_key(ip.dst, tcp_header.dst_port)
        binding = self.bindings.get(key)
        if not binding:
            return
        backend_name = binding["backend"]
        if BACKENDS[backend_name]["ip"] != ip.src or not self.backend_up.get(backend_name, False):
            return
        path = self._choose_path()
        if path is None:
            self.logger.error("no live return path for %s", key)
            return
        binding.update(path=path, updated=time.time())
        self._install_vip_flow(msg, ip.dst, tcp_header.dst_port, backend_name, path, "reply")

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        pkt = packet.Packet(msg.data)
        ip = pkt.get_protocol(ipv4.ipv4)
        if not ip:
            return  # Mininet installs static ARP entries for all hosts and the VIP.
        tcp_header = pkt.get_protocol(tcp.tcp)
        dpid = msg.datapath.id
        if dpid == 1 and ip.dst == VIP and tcp_header and tcp_header.dst_port == SERVICE_PORT:
            self._handle_vip_request(msg, ip, tcp_header)
            return
        if (dpid == 4 and ip.src in {item["ip"] for item in BACKENDS.values()}
                and tcp_header and tcp_header.src_port == SERVICE_PORT):
            self._handle_vip_reply(msg, ip, tcp_header)
            return
        if (dpid in (2, 3) and ip.src == VIP and tcp_header
                and tcp_header.src_port == SERVICE_PORT):
            self._handle_vip_reply_at_mid(msg, ip, tcp_header)
            return
        if (dpid == 1 and ip.src == VIP and tcp_header
                and tcp_header.src_port == SERVICE_PORT and ip.dst in HOSTS):
            client = HOSTS[ip.dst]
            actions = [msg.datapath.ofproto_parser.OFPActionOutput(client[1])]
            self._add_flow(msg.datapath, msg.datapath.ofproto_parser.OFPMatch(
                in_port=msg.match["in_port"], eth_type=0x0800, ip_proto=6,
                ipv4_src=VIP, ipv4_dst=ip.dst, tcp_src=SERVICE_PORT,
                tcp_dst=tcp_header.dst_port), actions)
            self._packet_out(msg, actions)
            return
        self._install_host_route(msg, ip)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        msg = ev.msg
        if msg.datapath.id != 1:
            return
        now = time.time()
        for stat in msg.body:
            path = {3: "A", 4: "B"}.get(stat.port_no)
            if path is None:
                continue
            previous = self.port_samples.get(stat.port_no)
            self.port_samples[stat.port_no] = (stat.tx_bytes, now)
            if previous:
                old_bytes, old_time = previous
                elapsed = now - old_time
                if elapsed > 0 and stat.tx_bytes >= old_bytes:
                    rate = (stat.tx_bytes - old_bytes) * 8.0 / elapsed
                    self.path_load[path] = min(1.0, rate / LINK_CAPACITY_BPS)
        self.logger.debug("path utilization: A=%.1f%% B=%.1f%%",
                          100 * self.path_load["A"], 100 * self.path_load["B"])

    def _monitor(self):
        while True:
            datapath = self.datapaths.get(1)
            if datapath:
                parser = datapath.ofproto_parser
                datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0,
                                                              datapath.ofproto.OFPP_ANY))
            hub.sleep(2.0)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        msg = ev.msg
        dpid = msg.datapath.id
        port = msg.desc.port_no
        ofp = msg.datapath.ofproto
        is_up = not (msg.desc.state & ofp.OFPPS_LINK_DOWN) and not (
            msg.desc.config & ofp.OFPPC_PORT_DOWN
        )
        changed = False
        if dpid == 1 and port in (3, 4):
            name = "A" if port == 3 else "B"
            changed = self.path_up[name] != is_up
            self.path_up[name] = is_up
        elif (dpid, port) in {(2, 2), (4, 1)}:
            changed = self.path_up["A"] != is_up
            self.path_up["A"] = is_up
        elif (dpid, port) in {(3, 2), (4, 2)}:
            changed = self.path_up["B"] != is_up
            self.path_up["B"] = is_up
        elif dpid == 4 and port in (3, 4, 5):
            name = {3: "srv1", 4: "srv2", 5: "srv3"}[port]
            changed = self.backend_up[name] != is_up
            self.backend_up[name] = is_up
            self.logger.warning("backend status: %s %s", name, "up" if is_up else "down")
        if changed:
            self._delete_app_flows()
            self.logger.warning("port status changed: dpid=%s port=%s up=%s; app flows cleared",
                                dpid, port, is_up)

