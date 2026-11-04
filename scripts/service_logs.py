"""Readable live view of one stub service's access log, for demos.

Prints every real request (e.g. POST /authorize, /simulate/fail) and shows the
registry's 5-second health checks only when the result changes.

    sudo journalctl -u registry-service@payments-api -f -n 20 -o short-iso | python3 -u scripts/service_logs.py
"""
import re
import sys

ACCESS = re.compile(r'^(\S+)\s.*?INFO:\s+([\d.]+):\d+ - "(\w+) (\S+) HTTP/[\d.]+" (\d{3})')
GREEN, RED, BOLD, DIM, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[2m", "\033[0m"

last_health = None
print(f"{BOLD}watching service access log{RESET} (health checks shown only when they change)", flush=True)
for line in sys.stdin:
    m = ACCESS.search(line)
    if not m:
        continue
    ts, client, method, path, code = m.groups()
    ts = ts[11:19] if "T" in ts else ts
    color = GREEN if code.startswith("2") else RED
    if path == "/health":
        state = "UP" if code == "200" else "DOWN"
        if state != last_health:
            print(f"{DIM}{ts}{RESET}  health check from registry -> {color}{BOLD}{code} {state}{RESET}", flush=True)
            last_health = state
        continue
    label = {"/authorize": "payment request", "/simulate/fail": "SIMULATED FAILURE",
             "/simulate/recover": "RECOVERED"}.get(path, path)
    print(f"{DIM}{ts}{RESET}  {method} {path:<18} -> {color}{BOLD}{code}{RESET}  {label}", flush=True)
