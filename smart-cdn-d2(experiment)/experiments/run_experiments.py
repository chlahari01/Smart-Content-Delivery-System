#!/usr/bin/env python3
"""Run the D2 experiments in Mininet: static baseline vs adaptive controller.

Scenarios (each repeated --repeats times per mode):
  normal           one client downloads the benchmark file
  concurrent       four simultaneous downloads (two per client)
  path_failure     the path carrying the transfer is cut one second in
  backend_failure  the server process sending the file is killed one second in

Two ways to run it (as root, from the project folder):

  # 1. Fully automatic: the script starts the controller in each mode itself.
  sudo python3 experiments/run_experiments.py \
       --osken-manager /home/<user>/osken-venv/bin/osken-manager

  # 2. One mode at a time against a controller you started yourself.
  sudo python3 experiments/run_experiments.py --mode static
  sudo python3 experiments/run_experiments.py --mode adaptive

Rows are appended to results/results.csv. A per-scenario comparison is written
to results/summary.md; rebuild it at any time with --summarize-only.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS_DIR = ROOT / "results"
CSV_PATH = RESULTS_DIR / "results.csv"
SUMMARY_PATH = RESULTS_DIR / "summary.md"
FIELDS = ["mode", "scenario", "run", "client", "ok", "bytes", "elapsed_s",
          "throughput_mbps", "attempts", "note"]
SCENARIOS = ("normal", "concurrent", "path_failure", "backend_failure")
BENCH_FILE = "bench.bin"
APP_COOKIE = "cookie=0xcd000001/-1"
STATIC_ENV = {"CDN_FIXED_PATH": "A", "CDN_FIXED_BACKEND": "srv1"}


# --------------------------------------------------------------------------
# Summary (standard library only; works without Mininet)
# --------------------------------------------------------------------------

def summarize(rows: list[dict]) -> str:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["mode"])].append(row)

    def mean(values):
        values = list(values)
        return sum(values) / len(values) if values else None

    def fmt(value, digits=2):
        return "-" if value is None else f"{value:.{digits}f}"

    normal_time = {}
    for mode in ("static", "adaptive"):
        good = [r for r in groups.get(("normal", mode), []) if r["ok"] == "1"]
        normal_time[mode] = mean(float(r["elapsed_s"]) for r in good)

    lines = [
        "# Static baseline vs adaptive controller",
        "",
        "| Scenario | Mode | Transfers | Success | Mean time (s) | Mean throughput (Mbit/s) "
        "| Mean attempts | Extra time vs normal (s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for scenario in SCENARIOS:
        for mode in ("static", "adaptive"):
            items = groups.get((scenario, mode))
            if not items:
                continue
            good = [r for r in items if r["ok"] == "1"]
            elapsed = mean(float(r["elapsed_s"]) for r in good)
            extra = None
            if scenario.endswith("failure") and elapsed is not None and normal_time[mode]:
                extra = elapsed - normal_time[mode]
            lines.append(
                f"| {scenario} | {mode} | {len(items)} | {100 * len(good) / len(items):.0f}% "
                f"| {fmt(elapsed)} | {fmt(mean(float(r['throughput_mbps']) for r in good))} "
                f"| {fmt(mean(float(r['attempts']) for r in good), 1)} | {fmt(extra)} |"
            )
    lines += [
        "",
        "Time, throughput and attempts are averaged over successful transfers only; "
        "failed transfers show up in the Success column. "
        "\"Extra time vs normal\" is the recovery cost of a failure: mean completion "
        "time in the failure scenario minus the same mode's mean time in `normal`.",
        "",
    ]
    return "\n".join(lines)


def write_summary() -> None:
    if not CSV_PATH.exists():
        print(f"No results yet: {CSV_PATH} does not exist")
        return
    with CSV_PATH.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    text = summarize(rows)
    SUMMARY_PATH.write_text(text)
    print(text)
    print(f"Summary written to {SUMMARY_PATH}")


# --------------------------------------------------------------------------
# Experiment helpers
# --------------------------------------------------------------------------

def ensure_bench_file(size_mb: int) -> None:
    """Create the same benchmark file in every backend's content directory."""
    block = bytes(range(256)) * 4096  # 1 MiB
    for number in (1, 2, 3):
        directory = ROOT / "content" / f"server{number}"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / BENCH_FILE
        if not target.exists() or target.stat().st_size != size_mb * len(block):
            with target.open("wb") as handle:
                for _ in range(size_mb):
                    handle.write(block)


class Runner:
    def __init__(self, net, servers, mode, args, writer):
        self.net = net
        self.servers = servers
        self.mode = mode
        self.args = args
        self.writer = writer
        self.counter = 0

    def start_client(self, host_name: str):
        self.counter += 1
        out_dir = f"/tmp/cdn-exp/{self.mode}-{host_name}-{self.counter}"
        return self.net.get(host_name).popen(
            [sys.executable, str(ROOT / "client.py"), BENCH_FILE,
             "--download-dir", out_dir, "--json",
             "--timeout", str(self.args.timeout), "--retries", str(self.args.retries)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def finish_client(self, process) -> dict:
        limit = (self.args.timeout + 2) * (self.args.retries + 1) + 60
        try:
            stdout, stderr = process.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            process.kill()
            return {"ok": False, "error": "client did not finish"}
        for line in stdout.splitlines():
            if line.startswith("{"):
                return json.loads(line)
        return {"ok": False, "error": (stderr.strip().splitlines() or ["no output"])[-1]}

    def record(self, scenario, run, client, result, note=""):
        if not result.get("ok") and not note:
            note = result.get("error", "")
        elif not result.get("ok"):
            note = f"{note}; {result.get('error', '')}"
        self.writer.writerow({
            "mode": self.mode, "scenario": scenario, "run": run, "client": client,
            "ok": int(bool(result.get("ok"))), "bytes": result.get("bytes", 0),
            "elapsed_s": result.get("elapsed_s", ""),
            "throughput_mbps": result.get("throughput_mbps", ""),
            "attempts": result.get("attempts", ""), "note": note,
        })
        status = "ok" if result.get("ok") else "FAILED"
        print(f"  [{self.mode}] {scenario} run {run} {client}: {status} "
              f"{result.get('elapsed_s', '?')} s {note}", flush=True)

    def flows(self, switch: str) -> str:
        return self.net.get(switch).cmd(f"ovs-ofctl -O OpenFlow13 dump-flows {switch}")

    def clear_app_flows(self):
        """Remove old application flows so the next transfer's flows stand out."""
        for switch in ("s1", "s2", "s3", "s4"):
            self.net.get(switch).cmd(
                f"ovs-ofctl -O OpenFlow13 del-flows {switch} '{APP_COOKIE}'")

    def active_path(self) -> str:
        service = re.compile(r"t(?:c)?p_(?:src|dst)=5000\b")
        if service.search(self.flows("s2")):
            return "A"
        if service.search(self.flows("s3")):
            return "B"
        return "A"

    def active_backend(self) -> str:
        found = re.search(r"nw_dst=10\.0\.0\.1([123])\b[^\n]*t(?:c)?p_dst=5000\b", self.flows("s4"))
        return f"srv{found.group(1)}" if found else "srv1"

    # -- scenarios ---------------------------------------------------------

    def normal(self, run):
        self.record("normal", run, "c1", self.finish_client(self.start_client("c1")))

    def concurrent(self, run):
        clients = ["c1", "c2", "c1", "c2"]
        processes = [self.start_client(name) for name in clients]
        for index, (name, process) in enumerate(zip(clients, processes), 1):
            self.record("concurrent", run, f"{name}#{index}", self.finish_client(process))

    def path_failure(self, run):
        self.clear_app_flows()
        process = self.start_client("c1")
        time.sleep(self.args.fail_after)
        path = self.active_path()
        middle = "s2" if path == "A" else "s3"
        self.net.configLinkStatus("s1", middle, "down")
        result = self.finish_client(process)
        self.net.configLinkStatus("s1", middle, "up")
        self.record("path_failure", run, "c1", result, f"cut path {path} (s1-{middle})")
        time.sleep(3)

    def backend_failure(self, run):
        from cdn_topology import start_servers

        self.clear_app_flows()
        process = self.start_client("c1")
        time.sleep(self.args.fail_after)
        name = self.active_backend()
        self.servers[name].kill()  # SIGKILL: a crash, no goodbye heartbeat
        self.servers[name].wait()
        result = self.finish_client(process)
        self.servers.update(start_servers(self.net, (name,), str(RESULTS_DIR / "logs")))
        self.record("backend_failure", run, "c1", result, f"killed {name} process")
        time.sleep(4)  # let the restarted server's heartbeat bring it back


def run_mode(mode: str, args, writer) -> bool:
    from cdn_topology import build_net, configure_vip, start_servers, stop_servers
    from mininet.clean import cleanup

    controller = None
    if args.osken_manager:
        env = dict(os.environ)
        env.pop("CDN_FIXED_PATH", None)
        env.pop("CDN_FIXED_BACKEND", None)
        env["CDN_LINK_BANDWIDTH_MBPS"] = str(args.bandwidth)
        if mode == "static":
            env.update(STATIC_ENV)
        log = open(RESULTS_DIR / "logs" / f"controller-{mode}.log", "wb")
        controller = subprocess.Popen(
            [args.osken_manager, "controller.py"], cwd=ROOT, env=env,
            stdout=log, stderr=subprocess.STDOUT)
        log.close()
        time.sleep(4)
        if controller.poll() is not None:
            print(f"Controller failed to start; see results/logs/controller-{mode}.log")
            return False

    print(f"=== mode: {mode} ===", flush=True)
    cleanup()
    net = build_net(args.controller_ip, args.controller_port, args.bandwidth)
    servers = {}
    try:
        net.start()
        configure_vip(net)
        servers = start_servers(net, log_dir=str(RESULTS_DIR / "logs"))
        time.sleep(4)  # first heartbeats reach the controller
        runner = Runner(net, servers, mode, args, writer)

        check = runner.finish_client(runner.start_client("c1"))
        if not check.get("ok"):
            print(f"Warm-up download failed ({check.get('error')}). Is the controller "
                  f"running in {mode} mode? Skipping this mode.")
            return False

        for scenario in args.scenarios:
            for run in range(1, args.repeats + 1):
                getattr(runner, scenario)(run)
        return True
    finally:
        stop_servers(servers)
        net.stop()
        if controller:
            controller.terminate()
            try:
                controller.wait(timeout=5)
            except subprocess.TimeoutExpired:
                controller.kill()
            time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("static", "adaptive"),
                        help="label for a controller you started yourself")
    parser.add_argument("--osken-manager", metavar="PATH",
                        help="path to osken-manager; the script then runs both modes itself")
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--bandwidth", type=float, default=10.0)
    parser.add_argument("--size-mb", type=int, default=5, help="benchmark file size")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--fail-after", type=float, default=1.0,
                        help="seconds into a transfer before the failure is injected")
    parser.add_argument("--timeout", type=float, default=8.0, help="client socket timeout")
    parser.add_argument("--retries", type=int, default=2, help="client reconnect attempts")
    parser.add_argument("--fresh", action="store_true", help="delete earlier results first")
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(exist_ok=True)
    if args.summarize_only:
        write_summary()
        return 0
    if not args.mode and not args.osken_manager:
        parser.error("give --mode (controller already running) or --osken-manager PATH")
    if os.geteuid() != 0:
        parser.error("Mininet needs root: run with sudo")

    (RESULTS_DIR / "logs").mkdir(exist_ok=True)
    if args.fresh and CSV_PATH.exists():
        CSV_PATH.unlink()
    ensure_bench_file(args.size_mb)

    modes = [args.mode] if args.mode else ["static", "adaptive"]
    new_file = not CSV_PATH.exists()
    completed = True
    with CSV_PATH.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        for mode in modes:
            completed = run_mode(mode, args, writer) and completed
            handle.flush()
    write_summary()
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
