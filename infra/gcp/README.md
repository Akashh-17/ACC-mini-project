# GCP Owner — Setup Guide

Step numbers match `execution-order.md`.

## A. Console steps (do these yourself, in order)

**1.1 Billing.** Sign in at <https://console.cloud.google.com>, start the free trial ($300 / 90 days), and create a project (e.g. `crosscloud-registry`). Note the **project ID**.

**1.2 Billing alert at $1 — before creating anything.**
Billing → Budgets & alerts → Create budget:
- Scope: your project
- Amount: **$1**
- **Credits: untick "Discounts / Promotions and others"** (Savings / Credits section). Otherwise the trial credit nets your cost to $0 and the alert never fires.
- Thresholds: 50%, 90%, 100% → email to billing admins
- Take the screenshot now (needed for step 4.7).

**1.3 Non-root IAM user.** GCP has no "root" user. The equivalent is to stop using your owner account day to day.
IAM & Admin → IAM → Grant access → a second Google account you control, with roles:
- `Compute Instance Admin (v1)`
- `Compute Network Admin`
- `Compute Security Admin` (firewall rules)
- `Billing Account Viewer` (grant on the billing account, under Billing → Account management)

Use that account for everything below.

## B. Create the VM (Cloud Shell)

Open Cloud Shell (the `>_` icon, top right of the console). gcloud is already installed and logged in there. Upload this repo or `git clone` it, then:

```bash
curl -s https://ifconfig.me    # this is MY_IP — run it on YOUR laptop, not in Cloud Shell
PROJECT_ID=<your-project-id> MY_IP=<your-laptop-ip> bash infra/gcp/setup.sh
```

The script does 1.4–1.7 and prints the block to post to the team sheet (1.8). It:
- creates its own VPC `registry-net`. The `default` network has a built-in rule that opens SSH to the whole internet.
- reserves a static IP, then creates an `e2-micro` in `asia-south1-a` with Debian 12, attached to that IP.
- adds a firewall rule allowing SSH from `MY_IP` only, scoped to the VM by network tag.
- creates the SSH key `~/.ssh/gcp-registry` in Cloud Shell. Download it to your laptop if you want to SSH from there.

If your home IP changes, update the SSH rule:
```bash
gcloud compute firewall-rules update registry-allow-ssh --source-ranges=<new-ip>/32
```

## C. Prepare the VM — DONE 2026-10-04

Done on `registry-gcp` (static IP: see the team sheet). The SSH login user `registry` also runs the agent.
- Installed `python3-venv git nginx gettext-base`. `nginx` lives in `/usr/sbin`, so call it with `sudo`.
- Code copied to `/opt/registry` (no shared repo yet), venv at `/opt/registry/.venv`.
- Verified on Python 3.11.2: 13 unit tests pass, and the local 3-agent mTLS test passes.
- Proxy rendered to `/etc/nginx/conf.d/registry.conf`, default site removed, `nginx -t` passes.
  Port 443 is still closed in the firewall until Phase 4.

When the shared repo exists, replace the copied code:
```bash
cd /opt && sudo mv registry registry.old && sudo git clone <shared-repo-url> registry && sudo chown -R registry: registry
cd /opt/registry && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp deploy/agent.env.example /etc/registry/agent.env && sudo chmod 600 /etc/registry/agent.env
```

## D. Phase 4 (after all three static IPs are posted)

1. **Certs (4.3).** On your laptop, generate them for all three agents with their **static public IPs**:
   ```bash
   bash certs/gen-certs.sh certs/out aws-ap-south-1=<AWS_IP> azure-indiasouthcentral=<AZURE_IP> gcp-asia-south1=<GCP_IP>
   ```
   Send each owner their own `.crt` + `.key` + `ca.crt` privately. **`ca.key` never leaves your machine.**
2. **Firewall (4.1):**
   ```bash
   AWS_IP=... AZURE_IP=... T4_IP=... bash infra/gcp/open-peers.sh
   ```
3. **Proxy:** on the VM. The IP is passed in, not stored, because the repo is public:
   ```bash
   sudo PUBLIC_IP=<GCP_STATIC_IP> bash proxy/render.sh proxy/gcp.env | sudo tee /etc/nginx/conf.d/registry.conf
   sudo rm -f /etc/nginx/sites-enabled/default && sudo nginx -t && sudo systemctl reload nginx
   ```
4. **Agent on boot (4.6):** see the header of `deploy/registry-agent.service`.
5. **Reachability (4.2):**
   ```bash
   curl --cacert ca.crt --cert gcp-asia-south1.crt --key gcp-asia-south1.key https://<PEER_IP>:9443/replicate/ping
   ```

## E. When the project is over (5.9)

```bash
gcloud compute instances delete registry-gcp --zone=asia-south1-a
gcloud compute addresses delete registry-gcp-ip --region=asia-south1   # idle static IPs are billed
```
