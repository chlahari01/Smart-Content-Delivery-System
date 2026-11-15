"""Mininet topology with two clients, three servers, and two SDN paths."""

from __future__ import annotations

import argparse

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.topo import Topo


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


def run(controller_ip: str, controller_port: int, bandwidth: float):
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
    try:
        net.start()
        # Clients use a stable L2 address for the virtual service IP.
        for name in ("c1", "c2"):
            host = net.get(name)
            host.cmd(
                "ip neigh replace 10.0.0.100 lladdr 00:00:00:00:00:aa "
                f"dev {host.defaultIntf().name}"
            )
        print("Topology: c1/c2 -> s1 -> (s2 or s3) -> s4 -> srv1/srv2/srv3")
        print(f"Link bandwidth: {bandwidth} Mbit/s (use Mininet link commands to inject failure)")
        CLI(net)
    finally:
        net.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--bandwidth", type=float, default=10.0)
    args = parser.parse_args()
    run(args.controller_ip, args.controller_port, args.bandwidth)


if __name__ == "__main__":
    setLogLevel("info")
    main()
