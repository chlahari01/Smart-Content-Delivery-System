# Smart CDN reworked demo

This is a clean, consistent replacement for the earlier mixed project folders. It contains a TCP file service, a two-path Mininet topology, and an OS-Ken/OpenFlow 1.3 controller.

## What is implemented

- Two Mininet clients and three backend hosts.
- A stable client-facing service address: `10.0.0.100:5000`.
- Three identical sample files, replicated in each backend directory.
- A controller that chooses a live backend round-robin and a least-utilized live path using OpenFlow port statistics.
- Path and backend-link state handling from OpenFlow port-status events.
- Forward and return flow rules for VIP TCP transfers, including rewriting backend replies to the VIP address.
- IPv4 forwarding for client/backend reachability checks; Mininet installs static ARP entries to avoid controller-dependent ARP flooding.
- A fixed-backend/fixed-path mode for the static baseline comparison.
- A safer client that validates filenames, checks the declared transfer size, and atomically replaces the destination only after a complete download.

The controller detects backend link state, not a server process crash while its switch port remains up. To demonstrate backend unavailability, use Mininet to bring down the backend link. Existing TCP sessions cannot move to a different backend because TCP session state is not replicated; reconnect and restart the download after a backend failure.

## Architecture

```text
c1, c2 -- s1 -- s2 -- s4 -- srv1, srv2, srv3
              \ s3 /
```

Path A is `s1-s2-s4`; path B is `s1-s3-s4`. The controller binds each VIP TCP flow to a backend and path. Port counters affect new path choices, and port-status events remove stale application flows so a retransmitted packet can use a live path.

## Local TCP tests

The transfer code uses only the Python standard library. From this directory:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover small, empty, and 5 MiB files; missing files; path traversal; interrupted transfers; concurrent clients; health checks; and path/backend selection policies. They do not emulate Open vSwitch or Mininet.

## Ubuntu VM setup

Use the Ubuntu VM from the course installation guide. Install Mininet and Open vSwitch with the system package manager, then create a separate Python environment for OS-Ken:

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-venv python3-pip
python3 -m venv ~/osken-venv
source ~/osken-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The controller dependency is pinned to OS-Ken 3.1.1. It supports Python 3.8+, which covers Ubuntu 20.04, and retains the `osken-manager` command used below. OS-Ken 4.x requires Python 3.10+ and its 4.0 release removed that CLI, so do not upgrade this project to 4.x without changing the startup command.

Copy this folder into the VM at `/tmp/smart-cdn-reworked`. Check the source before starting the demo:

```bash
cd /tmp/smart-cdn-reworked
python3 -m py_compile client.py content_server.py cdn_topology.py routing_policy.py controller.py
```

The controller requires OS-Ken; the topology script requires Mininet and root privileges.

## Demo startup

### Terminal 1: adaptive controller

```bash
cd /tmp/smart-cdn-reworked
source ~/osken-venv/bin/activate
osken-manager controller.py --verbose
```

Controller logs show backend/path choices, path utilization, and link-status changes.

### Terminal 2: Mininet

```bash
cd /tmp/smart-cdn-reworked
sudo mn -c
sudo python3 cdn_topology.py --controller-ip 127.0.0.1
```

At the Mininet prompt, first run:

```text
mininet> nodes
mininet> net
mininet> pingall
```

`pingall` should report 0% packet loss. If it does not, stop and fix connectivity before presenting file delivery.

### Start one server in each backend host

At the Mininet prompt, run:

```text
mininet> srv1 sh -c 'python3 /tmp/smart-cdn-reworked/content_server.py --host 0.0.0.0 --port 5000 --directory /tmp/smart-cdn-reworked/content/server1 --id srv1 >/tmp/srv1.log 2>&1 & echo $! >/tmp/srv1.pid'
mininet> srv2 sh -c 'python3 /tmp/smart-cdn-reworked/content_server.py --host 0.0.0.0 --port 5000 --directory /tmp/smart-cdn-reworked/content/server2 --id srv2 >/tmp/srv2.log 2>&1 & echo $! >/tmp/srv2.pid'
mininet> srv3 sh -c 'python3 /tmp/smart-cdn-reworked/content_server.py --host 0.0.0.0 --port 5000 --directory /tmp/smart-cdn-reworked/content/server3 --id srv3 >/tmp/srv3.log 2>&1 & echo $! >/tmp/srv3.pid'
```

### Request through the VIP

```text
mininet> c1 python3 /tmp/smart-cdn-reworked/client.py sample.txt --host 10.0.0.100 --port 5000 --download-dir /tmp/c1-downloads
mininet> c2 python3 /tmp/smart-cdn-reworked/client.py sample.txt --host 10.0.0.100 --port 5000 --download-dir /tmp/c2-downloads
mininet> c1 sha256sum /tmp/c1-downloads/sample.txt
mininet> srv1 tail -n 10 /tmp/srv1.log
```

Show the controller's backend/path decision and inspect installed rules:

```text
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s4
```

## Static baseline and adaptive comparison

Stop the controller, then restart Terminal 1 in fixed mode:

```bash
CDN_FIXED_PATH=A CDN_FIXED_BACKEND=srv1 osken-manager controller.py --verbose
```

Run the same client requests and workload. Stop the controller and restart without those environment variables for adaptive selection. Keep the file, topology, number of clients, and duration the same. Repeat each run and record elapsed time/throughput from the client plus packet loss and recovery time from the failure scenario. Use `results/comparison-template.csv` to record results; do not fill in measurements until they have been run in the VM.

## Controlled failure demonstrations

- **Path A failure:** at the Mininet prompt run `link s1 s2 down`, then request a new file through the VIP. The controller should report path A down and use path B. Restore with `link s1 s2 up`.
- **Backend link failure:** run `link s4 srv1 down`, then request a new file. The controller should mark `srv1` unavailable and choose another live backend. Restore with `link s4 srv1 up`.
- **Congestion:** create a large, identical file in each content directory, start a transfer from one client, then start a second client transfer while the first is active. The controller samples path counters every two seconds and logs its next-flow path choice. If you change `cdn_topology.py --bandwidth`, set the matching `CDN_LINK_BANDWIDTH_MBPS` value in the controller terminal so utilization is calculated against the configured rate.

For server-process failure while the link stays up, add an application-level health-report mechanism before claiming server health monitoring. This version only sees backend port link state.

## Troubleshooting

- `simple_switch.py` from the earlier package is obsolete; run `controller.py` from this folder.
- Use only the `client.py` and `content_server.py` from this folder together.
- If the controller reports no path or no backend, inspect Mininet link state and the `s4` backend ports.
- If `pingall` fails, check that the controller is running, switches use OpenFlow 1.3, and the topology DPID/port mapping matches `controller.py`.
- A file transfer interrupted by a backend failure is removed from the client download folder; TCP state does not migrate between backends.
