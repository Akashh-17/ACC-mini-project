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
sudo bash scripts/netfault.sh partition PEER_PRIVATE_IP
sudo bash scripts/netfault.sh delay PEER_PRIVATE_IP 200
sudo bash scripts/netfault.sh clear PEER_PRIVATE_IP
```

Collect replication JSONL from the service logs and compare the owner's
`version` with the receiver's `applied_at`. Never commit cloud IPs or secrets.