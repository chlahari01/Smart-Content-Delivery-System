# Test commands

Every command used to test the D2 project, in the order to run them. All commands assume you are in the project folder:

```bash
cd /media/sf_LINUX/CN/smart-cdn-d2-final
```

Two terminals are used throughout:

- **Controller terminal**: runs `osken-manager`.
- **Mininet terminal**: shows the `mininet>` prompt.

Start the controller before Mininet. Start the servers before any download.

## 0. One-time setup: large test file

A 20 MB file, identical on all three servers, so transfers last long enough (about 17 s) to interrupt.

```bash
head -c 20000000 /dev/urandom > content/server1/large.bin
cp content/server1/large.bin content/server2/
cp content/server1/large.bin content/server3/
ls -l content/server*/large.bin
```

## 1. Unit tests

```bash
~/osken-venv/bin/python -m unittest discover -s tests 2>&1 | tail -4
```

Expected: `Ran 37 tests` and `OK`.

## 2. Start the system and check connectivity

Controller terminal:

```bash
source ~/osken-venv/bin/activate
osken-manager controller.py
```

Mininet terminal:

```bash
sudo mn -c
sudo python3 cdn_topology.py --controller-ip 127.0.0.1 --start-servers
```

```text
mininet> pingall
```

Expected: `0% dropped (20/20 received)`. Controller shows four `switch connected` lines and three `backend srvN is UP` lines.

## 3. Normal and concurrent downloads

```text
mininet> c1 python3 client.py sample.txt --download-dir /tmp/c1-download
mininet> c2 python3 client.py sample.txt --download-dir /tmp/c2-download
mininet> c1 sh -c 'python3 client.py large.bin --download-dir /tmp/c1-download > /tmp/c1.log 2>&1 &'
mininet> c2 sh -c 'python3 client.py large.bin --download-dir /tmp/c2-download > /tmp/c2.log 2>&1 &'
```

Wait about 25 seconds, then:

```text
mininet> c1 cat /tmp/c1.log
mininet> c2 cat /tmp/c2.log
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

Expected: both logs show `Downloaded large.bin (20000000 bytes)` at about 9.3 Mbit/s. Controller shows one `VIP decision` line per connection, on different paths and backends.

## 4. Path failure during a transfer

```text
mininet> c1 sh -c 'python3 client.py large.bin --download-dir /tmp/c1-download > /tmp/c1.log 2>&1 &'
```

Read `path=A` or `path=B` on the new `VIP decision` line, then within 5 seconds cut that path:

```text
mininet> link s1 s2 down
```

for path A, or

```text
mininet> link s1 s3 down
```

for path B. Wait about 25 seconds, then:

```text
mininet> c1 cat /tmp/c1.log
mininet> link s1 s2 up
```

(Use `link s1 s3 up` if you cut path B.)

Expected: controller shows `port status changed ... up=False; app flows cleared` then `VIP reroute: ... path A -> B`. The download completes with no `Attempt 1 failed` line.

## 5. Server crash and recovery

```text
mininet> c1 sh -c 'python3 client.py large.bin --download-dir /tmp/c1-download > /tmp/c1.log 2>&1 &'
```

Read `backend=srvN` on the new `VIP decision` line, then within 5 seconds kill that server:

```text
mininet> srv1 pkill -9 -f "content/server1"
mininet> srv2 pkill -9 -f "content/server2"
mininet> srv3 pkill -9 -f "content/server3"
```

(Run only the line for the server named in the decision.) Wait about 30 seconds, then:

```text
mininet> c1 cat /tmp/c1.log
```

Restart the killed server (srv1 shown; change the numbers for another server):

```text
mininet> srv1 sh -c 'python3 content_server.py --directory content/server1 --id=srv1 > /tmp/srv1.log 2>&1 &'
```

Expected: controller shows `backend srv1 is DOWN (no heartbeat); removed from rotation`, a new `VIP decision` on another backend, and `backend srv1 is UP` after the restart. The log shows `Attempt 1 failed ... reconnecting` and `Recovered after 2 attempts`.

Note: write `--id=srv1` with an equals sign. With a space, the Mininet prompt replaces `srv1` with its IP address and the controller rejects the heartbeats.

## 6. Unauthorized client blocked

Controller terminal: press Ctrl+C, then:

```bash
CDN_BLOCKED_CLIENTS=10.0.0.2 osken-manager controller.py
```

Wait for the three `UP` lines. Mininet terminal:

```text
mininet> c2 python3 client.py sample.txt --download-dir /tmp/c2-download --timeout 4 --retries 0
mininet> c1 python3 client.py sample.txt --download-dir /tmp/c1-download
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=200
```

Expected: c2 prints `Download failed: timed out`; c1 downloads normally. Controller shows `blocked client 10.0.0.2: drop rule installed on dpid=1`. The last command shows a rule with `priority=200`, `nw_src=10.0.0.2` and `actions=drop`.

## 7. Static baseline (manual)

Controller terminal: press Ctrl+C, then:

```bash
CDN_FIXED_PATH=A CDN_FIXED_BACKEND=srv1 osken-manager controller.py
```

Repeat sections 3 to 5. Expected: every connection uses path A and srv1; a path A failure or an srv1 crash makes the download fail.

## 8. Automated static-vs-adaptive experiment

Stop the controller (Ctrl+C) and exit Mininet first. The script starts both itself.

```text
mininet> exit
```

```bash
sudo mn -c
sudo pkill -9 -f content_server.py
sudo python3 experiments/run_experiments.py --fresh --osken-manager "$HOME/osken-venv/bin/osken-manager"
```

Takes about 10 to 12 minutes. Expected: a line per transfer, `FAILED` lines for the static failure scenarios, and a final table "Static baseline vs adaptive controller". Output is saved in `results/results.csv` and `results/summary.md`.

If it stops with `Text file busy` (VirtualBox shared folder), run it from a copy:

```bash
cp -r /media/sf_LINUX/CN/smart-cdn-d2-final /tmp/smart-cdn-d2
cd /tmp/smart-cdn-d2
sudo python3 experiments/run_experiments.py --fresh --osken-manager "$HOME/osken-venv/bin/osken-manager"
sudo cp -r /tmp/smart-cdn-d2/results/. /media/sf_LINUX/CN/smart-cdn-d2-final/results/
```

Confirm the rerouting happened in the adaptive path-failure runs (expected output: `3`):

```bash
grep -c "VIP reroute" results/logs/controller-adaptive.log
```

## Reset if something goes wrong

```text
mininet> exit
```

```bash
sudo mn -c
sudo pkill -9 -f content_server.py
```

Then press Ctrl+C in the controller terminal and start again from section 2.

## Useful checks

```text
mininet> srv1 cat /tmp/srv1.log
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s4
mininet> net
mininet> links
```
