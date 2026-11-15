# Smart Content Delivery System

SDN-Socket Programming, Jackfruit Mini Project.

A TCP file-delivery service running on Mininet. An OS-Ken (OpenFlow 1.3) controller chooses the backend server and the network path for every client connection, and reacts to link failures, server failures and load.

## What is in this repository

| Item | What it is |
|---|---|
| [`smart-cdn-d2-final/`](smart-cdn-d2-final/) | **The final project (D2).** Source code, tests, experiment runner, measured results and full instructions. Start here. |
| [`smart-cdn-d1/`](smart-cdn-d1/) | The D1 submission, kept for comparison. |
| `Smart CDN D2 Project Report.pdf` | Project report: architecture, D1-to-D2 improvements, test evidence with screenshots, and performance results. |
| `Smart CDN Problem Statement and Architecture.pdf` | Problem statement, topology, and the D1 and D2 architectures. |

## Quick start

Requirements: an Ubuntu VM with Mininet, Open vSwitch and OS-Ken 3.1.1. Full setup steps are in [`smart-cdn-d2-final/README.md`](smart-cdn-d2-final/README.md).

```bash
cd smart-cdn-d2-final

# Terminal 1: controller
source ~/osken-venv/bin/activate
osken-manager controller.py

# Terminal 2: topology and the three content servers
sudo mn -c
sudo python3 cdn_topology.py --start-servers

# At the Mininet prompt: download a file through the virtual address
c1 python3 client.py sample.txt --download-dir /tmp/c1
```

## Results at a glance

Measured on 15 November 2026 (5 MB file, 10 Mbit/s links, 3 runs per scenario per mode).

| Scenario | Static baseline | Adaptive controller |
|---|---|---|
| Normal (one download) | 4.59 s | 4.55 s |
| Concurrent (four downloads) | 17.66 s | 7.00 s |
| Path failure during a transfer | 0 of 3 succeed | 3 of 3 succeed, first attempt |
| Server crash during a transfer | 0 of 3 succeed | 3 of 3 succeed, second attempt |

The full table, method and analysis are in the project report and in `smart-cdn-d2-final/README.md`.

## Team

| Member | SRN |
|---|---|
| Vendhan Bollada | PES1UG25AM078 |
| (name to be added) | PES1UG25AM092 |
| (name to be added) | PES1UG25AM074 |
