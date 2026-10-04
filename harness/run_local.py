"""Start and smoke-test the four services owned by one region.

This deliberately does not start registry agents. Use scripts/local_three_agents.py
for the existing mTLS replication rehearsal, then register these services with
the assembled agent using services/register_services.py.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


REGIONS = {
    "aws": ("aws-ap-south-1", "recommend-api"),
    "azure": ("azure-indiasouthcentral", "admin-api"),
    "gcp": ("gcp-asia-south1", "notify-api"),
}
CRITICAL = ("payments-api", "fraud-check", "inventory-check")
LOCAL_OPENER = build_opener(ProxyHandler({}))


def request(url: str, method: str = "GET") -> tuple[int, bytes]:
    try:
        with LOCAL_OPENER.open(Request(url, method=method), timeout=2) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except URLError:
        return 0, b""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", choices=REGIONS, default="aws")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-port", type=int, default=8081)
    args = parser.parse_args()

    region, standard = REGIONS[args.cloud]
    services = [*CRITICAL, standard]
    processes: list[subprocess.Popen] = []
    try:
        ports = range(args.base_port, args.base_port + len(services))
        for port, service_name in zip(ports, services):
            env = dict(os.environ, SERVICE_NAME=service_name,
                       SERVICE_REGION=region, SERVICE_PORT=str(port),
                       SERVICE_BIND_HOST=args.host)
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "services.app"], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(request(f"http://{args.host}:{port}/health")[0] == 200
                   for port in ports):
                break
            time.sleep(0.1)
        else:
            print("FAIL: not all services became healthy")
            return 1

        print(f"PASS: {args.cloud} services healthy on ports {args.base_port}-{args.base_port + 3}")
        failure_url = f"http://{args.host}:{args.base_port}/simulate/fail"
        if request(failure_url, "POST")[0] != 200:
            print("FAIL: failure simulation request failed")
            return 1
        if request(f"http://{args.host}:{args.base_port}/health")[0] != 503:
            print("FAIL: failed service did not return HTTP 503")
            return 1
        request(f"http://{args.host}:{args.base_port}/simulate/recover", "POST")
        if request(f"http://{args.host}:{args.base_port}/health")[0] != 200:
            print("FAIL: service did not recover")
            return 1
        print("PASS: failure and recovery behavior")
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in processes:
            process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())