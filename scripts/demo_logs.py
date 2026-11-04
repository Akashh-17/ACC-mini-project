"""Readable live view of a registry agent's log for demos.

Reads the agent's journal on stdin and prints only events worth showing:
health of a local service changing, a status change replicated in from another
cloud (with its lag), and peers becoming unreachable / reachable again.

    sudo journalctl -u registry-agent -f -n 0 -o cat | python3 -u scripts/demo_logs.py
"""
import json
import re
import sys
import time

COLORS = {"UP": "\033[32m", "DOWN": "\033[31m", "STALE": "\033[33m", "DELETED": "\033[35m"}
RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
PORTS = {"8081": "payments-api", "8082": "fraud-check", "8083": "inventory-check", "8084": "standard service"}
SHORT = {"gcp-asia-south1": "GCP", "aws-ap-south-1": "AWS", "azure-indiasouthcentral": "Azure"}
HEALTH = re.compile(r'GET http://[\d.]+:(\d+)/health "HTTP/1.1 (\d+)')


def paint(status):
    return f"{COLORS.get(status, '')}{BOLD}{status}{RESET}"


def say(text):
    print(f"{DIM}{time.strftime('%H:%M:%S')}{RESET}  {text}", flush=True)


health, remote = {}, {}
say(f"{BOLD}watching registry agent log{RESET} (status changes only)")
for line in sys.stdin:
    m = HEALTH.search(line)
    if m:
        port, code = m.groups()
        status = "UP" if code == "200" else "DOWN"
        if health.get(port) != status:
            verb = "health check " if port in health else f"{DIM}current     {RESET}"
            say(f"{verb} {PORTS.get(port, port):<16} HTTP {code} -> local status {paint(status)}")
            health[port] = status
        continue
    if "replication_applied" in line:
        d = json.loads(line[line.index("{"):])
        key = (d["service_name"], d["region_id"])
        if remote.get(key) != d["status"]:
            if key in remote:
                say(f"replicated in {d['service_name']:<16} @{SHORT.get(d['region_id'], d['region_id']):<5} "
                    f"-> {paint(d['status'])}   (lag {d['lag_ms'] / 1000:.1f}s, mTLS from {SHORT.get(d['region_id'])})")
            remote[key] = d["status"]
        continue
    if "replication to" in line and "failed" in line:
        say(f"\033[31mpeer unreachable\033[0m {line.split('replication to', 1)[1].split('failed')[0].strip()}")
    elif "reachable again" in line:
        say(f"\033[32mpeer reachable again\033[0m {line.split('peer', 1)[1].split('reachable')[0].strip()}")
    elif "restarted; resending snapshot" in line:
        say(f"peer restarted, sending snapshot: {line.split('peer', 1)[1].split('restarted')[0].strip()}")
