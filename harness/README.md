# T4 harness

Run the local service smoke test from the repository root:

```powershell
python -m harness.run_local --cloud aws
python -m harness.run_local --cloud azure
python -m harness.run_local --cloud gcp
```

The default ports are `8081`-`8084`, matching the cloud deployment. If one of
those ports is already occupied locally, use a different base port:

```powershell
python -m harness.run_local --cloud aws --base-port 18081
```

The harness starts the four regional services, checks all four `/health`
endpoints, simulates a failure on port `8081`, verifies HTTP `503`, verifies
recovery, and stops every child process.

The existing `scripts/local_three_agents.py` tests real mTLS replication. Run it
after generating local certificates as documented in `agent/replication/README.md`.
The harness and that script are intentionally separate: service smoke tests are
safe on Windows, while mTLS and network-fault tests use Linux-oriented tooling.

## Cloud experiments

Run `scripts/netfault.sh` as root on a cloud VM. It changes only traffic to the
replication port, leaving SSH and the public proxy available:

```bash
sudo bash scripts/netfault.sh partition PEER_PUBLIC_IP
sudo bash scripts/netfault.sh delay PEER_PUBLIC_IP 200
sudo bash scripts/netfault.sh clear PEER_PUBLIC_IP
```

Use the peer VM's public static IP, not its private address. Apply the command
on the VM whose replication connection should be interrupted. The partition
rules drop both requests and responses on TCP port `9443`; SSH and HTTPS on
ports `22` and `443` remain available.

Collect replication JSONL from the service logs and compare the owner's
`version` with the receiver's `applied_at`. Never commit cloud IPs or secrets.