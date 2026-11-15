"""Mininet topology with two clients, three servers, and two SDN paths."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.topo import Topo

PROJECT_DIR = Path(__file__).resolve().parent


class CDNTopology(Topo):
    """s1 -- (s2 or s3) -- s4, with clients at s1 and servers at s4."""

    def build(self, bandwidth=10.0):
        self.addHost("c1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
        self.addHost("c2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
        self.addHost("srv1", ip="10.0.0.11/24", mac="00:00:00:00:00:0b")
        self.addHost("srv2", ip="10.0.0.12/24", mac="00:00:00:00:00:0c")
        self.addHost("srv3", ip="10.0.0.13/24", mac="00:00:00:00:00:0d")

        for name, dpid in (("s1", "1"), ("s2", "2"), ("s3", "3"), ("s4", "4")):
            self.addSwitch(name, dpid=f"{int(dpid):016x}", protocols="OpenFlow13", failMode="secure")

        # Explicit ports are shared with controller.py.
        self.addLink("c1", "s1", port2=1)
        self.addLink("c2", "s1", port2=2)
        self.addLink("s1", "s2", port1=3, port2=1, bw=bandwidth)
        self.addLink("s1", "s3", port1=4, port2=1, bw=bandwidth)
        self.addLink("s2", "s4", port1=2, port2=1, bw=bandwidth)
        self.addLink("s3", "s4", port1=2, port2=2, bw=bandwidth)
        self.addLink("s4", "srv1", port1=3)
        self.addLink("s4", "srv2", port1=4)
        self.addLink("s4", "srv3", port1=5)


SERVERS = ("srv1", "srv2", "srv3")
CLIENTS = ("c1", "c2")
VIP = "10.0.0.100"
VIP_MAC = "00:00:00:00:00:aa"


def build_net(controller_ip: str, controller_port: int, bandwidth: float) -> Mininet:
    net = Mininet(
        topo=CDNTopology(bandwidth=bandwidth),
        controller=None,
        switch=OVSKernelSwitch,
        link=TCLink,
        autoSetMacs=False,
        autoStaticArp=True,
        waitConnected=True,
    )
    net.addController(
        "c0", controller=RemoteController, ip=controller_ip, port=controller_port
    )
    return net


def configure_vip(net: Mininet) -> None:
    """Give every host a stable L2 address for the virtual service IP.

    Clients need it to reach the service; servers need it so their UDP
    heartbeats to the VIP leave the host and reach the controller.
    """
    for name in CLIENTS + SERVERS:
        host = net.get(name)
        host.cmd(
            f"ip neigh replace {VIP} lladdr {VIP_MAC} "
            f"dev {host.defaultIntf().name}"
        )


def start_servers(net: Mininet, names=SERVERS, log_dir: str = "/tmp") -> dict:
    """Start content_server.py on the given backend hosts; returns the processes."""
    processes = {}
    for name in names:
        number = name[-1]
        content = PROJECT_DIR / "content" / f"server{number}"
        content.mkdir(parents=True, exist_ok=True)
        log = open(Path(log_dir) / f"{name}.log", "ab")
        processes[name] = net.get(name).popen(
            [sys.executable, str(PROJECT_DIR / "content_server.py"),
             "--directory", str(content), "--id", name],
            stdout=log, stderr=subprocess.STDOUT,
        )
        log.close()
    return processes


def stop_servers(processes: dict) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
    for process in processes.values():
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def run(controller_ip: str, controller_port: int, bandwidth: float, with_servers: bool = False):
    net = build_net(controller_ip, controller_port, bandwidth)
    servers = {}
    try:
        net.start()
        configure_vip(net)
        if with_servers:
            servers = start_servers(net)
            print("Content servers started on srv1/srv2/srv3 (logs: /tmp/srvN.log)")
        print("Topology: c1/c2 -> s1 -> (s2 or s3) -> s4 -> srv1/srv2/srv3")
        print(f"Link bandwidth: {bandwidth} Mbit/s (use Mininet link commands to inject failure)")
        CLI(net)
    finally:
        stop_servers(servers)
        net.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--bandwidth", type=float, default=10.0)
    parser.add_argument("--start-servers", action="store_true",
                        help="start content_server.py on srv1-srv3 automatically")
    args = parser.parse_args()
    run(args.controller_ip, args.controller_port, args.bandwidth, args.start_servers)


if __name__ == "__main__":
    setLogLevel("info")
    main()
