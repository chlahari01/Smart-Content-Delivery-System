# Smart CDN reworked demo

This is a clean, consistent replacement for the earlier mixed project folders. It contains a TCP file service, a two-path Mininet topology, and an OS-Ken/OpenFlow 1.3 controller.

## What is implemented

- Two Mininet clients and three backend hosts.
- A stable client-facing service address: `10.0.0.100:5000`.
- Three identical sample files, replicated in each backend directory.
- A controller that chooses the live backend with the fewest active transfers (round-robin on ties) and the least-utilized live path using OpenFlow port statistics.
- Path and backend-link state handling from OpenFlow port-status events.
- Backend process health: each content server sends a UDP heartbeat to the VIP every second; the controller removes a silent backend from rotation and brings it back when it reports again.
- Client-side recovery: the client reconnects through the VIP after a failed transfer and is bound to a healthy backend.
- Optional client blocking (`CDN_BLOCKED_CLIENTS`) with a drop rule on the client-side switch.
- An experiment runner that compares the static baseline with the adaptive controller.
- Forward and return flow rules for VIP TCP transfers, including rewriting backend replies to the VIP address.
- IPv4 forwarding for client/backend reachability checks; Mininet installs static ARP entries to avoid controller-dependent ARP flooding.
- A fixed-backend/fixed-path mode for the static baseline comparison.
- A safer client that validates filenames, checks the declared transfer size, and atomically replaces the destination only after a complete download.

Existing TCP sessions cannot move to a different backend because TCP session state is not replicated. After a backend failure the client reconnects and restarts the download (`--retries`, default 2).

## D2 rubric map

| D2 component | Where it is implemented | How to show it |
|---|---|---|
| Complete application functionality | `client.py`, `content_server.py`: concurrent TCP transfers, validation, retry | "Demo startup" below, two clients at once |
| Dynamic SDN functionality | `controller.py`: path monitoring, least-loaded path, mid-transfer rerouting, heartbeat health tracking, least-connections backend choice, recovery, blocking | Controller log lines `VIP decision`, `backend ... is DOWN/UP`, `port status changed`, `blocked client` |
| Challenging scenario / robustness | Path failure, server process crash, backend link failure, congestion, unauthorized client | "Controlled failure demonstrations" |
| Performance evaluation and comparison | `experiments/run_experiments.py` | "Static baseline and adaptive comparison" |
| Documentation, final demo and viva | This README | "Viva notes" at the end |

## How the socket application and the SDN controller interact

```text
content_server.py --UDP "HB srv2 1"--> 10.0.0.100:5001 --> s4 (no rule) --Packet-In--> controller
```

- **Heartbeat format:** `HB <server-id> <active-transfers>` once per second, `BYE <server-id>` on clean shutdown.
- The switch has no rule for these datagrams, so each one reaches the controller as a Packet-In. No separate management network is needed.
- The controller accepts a heartbeat only if its source IP and switch port match the named backend.
- A backend is **up** when its switch port is up **and** a heartbeat arrived in the last 3 seconds (`CDN_HEARTBEAT_TIMEOUT`). A backend that has never reported gets no traffic, so start the servers before the clients.
- The active-transfer count drives backend choice: new connections go to the least busy backend.
- When a backend's state changes, the controller deletes the application flows so no traffic follows stale rules.

Controller environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CDN_FIXED_PATH`, `CDN_FIXED_BACKEND` | unset | Static baseline: always this path / backend |
| `CDN_LINK_BANDWIDTH_MBPS` | 10 | Link rate used to compute utilization |
| `CDN_HEARTBEAT_TIMEOUT` | 3 | Seconds of silence before a backend is down |
| `CDN_REQUIRE_HEARTBEAT` | 1 | `0` = judge backends by switch-port state only |
| `CDN_BLOCKED_CLIENTS` | unset | Comma-separated client IPs that may not use the VIP |

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

The tests cover small, empty, and 5 MiB files; missing files; path traversal; interrupted transfers; client retry; concurrent clients; heartbeats; and path/backend selection policies. `tests/test_controller_logic.py` drives the real controller class with fake switches (heartbeat timeout, least-loaded backend, spoofed heartbeat, path failure, congestion, blocking); it needs OS-Ken, so run the tests with `~/osken-venv/bin/python` to include it, otherwise it is skipped. The tests do not emulate Open vSwitch or Mininet.

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
python3 -m py_compile client.py content_server.py cdn_topology.py routing_policy.py controller.py experiments/run_experiments.py
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

Shortcut: start Mininet with `sudo python3 cdn_topology.py --start-servers` and skip this step (logs go to `/tmp/srvN.log`).

Otherwise, at the Mininet prompt, run:

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

Run the same client requests and workload. Stop the controller and restart without those environment variables for adaptive selection.

### Automated experiments

`experiments/run_experiments.py` runs the same four scenarios in both modes and records every transfer. Stop any running controller and Mininet first, then:

```bash
cd /tmp/smart-cdn-reworked
sudo mn -c
sudo python3 experiments/run_experiments.py --fresh --osken-manager "$HOME/osken-venv/bin/osken-manager"
```

The script starts the controller itself, first with `CDN_FIXED_PATH=A CDN_FIXED_BACKEND=srv1` (static) and then without (adaptive). To use a controller you started by hand instead, run it once per mode with `--mode static` or `--mode adaptive`.

| Scenario | What happens | What it measures |
|---|---|---|
| `normal` | c1 downloads a 5 MB file | Baseline time and throughput |
| `concurrent` | Four downloads at once (two per client) | Load balancing over two 10 Mbit/s paths and three backends |
| `path_failure` | The path carrying the transfer is cut 1 s in | Rerouting of a transfer in progress |
| `backend_failure` | The server process sending the file is killed 1 s in | Heartbeat detection and client failover |

Metrics per transfer: success, completion time, throughput, connection attempts. Output:

- `results/results.csv`: one row per transfer.
- `results/summary.md`: static vs adaptive table per scenario, including recovery cost ("extra time vs normal").
- `results/logs/`: controller and server logs for each mode.

Useful options: `--repeats 5`, `--size-mb 10`, `--scenarios normal concurrent`, `--summarize-only`.

### Results

Run the experiments in the VM and paste `results/summary.md` here before submission. Do not write numbers that were not measured.

What the design predicts, to check against your measurements:

- `normal`: both modes similar, close to the 10 Mbit/s link rate.
- `concurrent`: static puts all four flows on path A and srv1; adaptive spreads them over both paths, so per-flow throughput should be roughly double.
- `path_failure`: static fails (its only path is down); adaptive completes on the first attempt after a short stall.
- `backend_failure`: static fails (srv1 is its only backend); adaptive completes on the second attempt.

## Controlled failure demonstrations

- **Path A failure:** at the Mininet prompt run `link s1 s2 down`, then request a new file through the VIP. The controller should report path A down and use path B. Restore with `link s1 s2 up`. A transfer already in progress also survives: its flows are cleared and the next retransmitted packet is routed over the live path.
- **Server process crash:** start a download, then run `srv1 sh -c 'kill -9 $(cat /tmp/srv1.pid)'`. Within 3 seconds the controller logs `backend srv1 is DOWN (no heartbeat)`. The client reports `Attempt 1 failed ... reconnecting` and completes from another backend. Restart the server with the command from "Start one server"; the controller logs `backend srv1 is UP`. (With `--start-servers`, use `srv1 pkill -9 -f "id srv1"` instead.)
- **Server overload:** start several downloads of a large file at once and watch `VIP decision` lines: new flows go to the backend reporting the fewest active transfers.
- **Unauthorized client:** start the controller with `CDN_BLOCKED_CLIENTS=10.0.0.2`. A request from c2 times out, the controller logs `blocked client 10.0.0.2`, and `dump-flows s1` shows a priority-200 rule with `actions=drop`. c1 still works.
- **Backend link failure:** run `link s4 srv1 down`, then request a new file. The controller should mark `srv1` unavailable and choose another live backend. Restore with `link s4 srv1 up`.
- **Congestion:** create a large, identical file in each content directory, start a transfer from one client, then start a second client transfer while the first is active. The controller samples path counters every two seconds and logs its next-flow path choice. If you change `cdn_topology.py --bandwidth`, set the matching `CDN_LINK_BANDWIDTH_MBPS` value in the controller terminal so utilization is calculated against the configured rate.

Limits to state honestly in the viva: a heartbeat shows the server process is running, not that every file is intact; detection takes up to `CDN_HEARTBEAT_TIMEOUT` seconds; a failed download restarts from byte 0; the controller does not detect packet loss on a link that stays up.

## Troubleshooting

- `simple_switch.py` from the earlier package is obsolete; run `controller.py` from this folder.
- Use only the `client.py` and `content_server.py` from this folder together.
- If the controller reports no path or no backend, inspect Mininet link state and the `s4` backend ports.
- If downloads hang and the controller logs `no healthy backend`, no heartbeats have arrived: check that the servers are running (`srv1 tail /tmp/srv1.log` should show `heartbeat to 10.0.0.100:5001`) and that you are using this folder's `cdn_topology.py`. `CDN_REQUIRE_HEARTBEAT=0` falls back to link-state only.
- If `pingall` fails, check that the controller is running, switches use OpenFlow 1.3, and the topology DPID/port mapping matches `controller.py`.
- A file transfer interrupted by a backend failure is removed from the client download folder; TCP state does not migrate between backends.

## Viva notes

- **Why a VIP?** Clients know one address. The controller rewrites the destination to a backend on s1 and rewrites the source back to the VIP on s4, so the client never sees which backend served it.
- **Why per-connection rules?** Matching on client IP and TCP source port keeps every packet of a connection on one backend, which TCP requires.
- **How is load measured?** Path load: byte counters of s1 ports 3 and 4, sampled every 2 s, as a fraction of link capacity (the busier direction, since file data flows server to client). Backend load: active transfers reported in heartbeats.
- **What happens on a link failure?** OVS sends a port-status message; the controller marks the path down and deletes its flows; the next packet of each connection causes a Packet-In and gets rules for the live path.
- **What happens on a server crash?** Heartbeats stop; after 3 s the backend leaves rotation; the client's broken connection fails and its retry is a new flow bound to a live backend.
- **Why is the static baseline fair?** Same topology, file, clients and timing; only the controller's decisions are fixed (`CDN_FIXED_PATH`, `CDN_FIXED_BACKEND`).
