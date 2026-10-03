#!/usr/bin/env bash
# Phase 1 GCP setup (tasks 1.4–1.8). Run in Google Cloud Shell, where gcloud is
# already installed and logged in. Do the console steps in infra/gcp/README.md first.
#
#   PROJECT_ID=my-project MY_IP=203.0.113.7 bash infra/gcp/setup.sh
#
# MY_IP = your own public IP (https://ifconfig.me) — SSH is allowed only from it.
# Safe to re-run: existing resources are skipped.
set -euo pipefail

PROJECT_ID=${PROJECT_ID:?set PROJECT_ID}
MY_IP=${MY_IP:?set MY_IP to your public IP}
REGION=asia-south1
ZONE=asia-south1-a
NETWORK=registry-net
SUBNET=registry-subnet
SUBNET_RANGE=10.2.0.0/24
VM=registry-gcp
TAG=registry-agent
IP_NAME=registry-gcp-ip
SSH_USER=${SSH_USER:-registry}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/gcp-registry}

exists() { "$@" >/dev/null 2>&1; }

gcloud config set project "$PROJECT_ID"
# The non-root user can't enable APIs; only try if Compute isn't on yet (needs the owner).
gcloud compute regions describe "$REGION" >/dev/null 2>&1 || gcloud services enable compute.googleapis.com

# Dedicated VPC instead of "default": the default network ships with
# default-allow-ssh open to 0.0.0.0/0, which breaks "SSH from own IP only".
exists gcloud compute networks describe "$NETWORK" ||
  gcloud compute networks create "$NETWORK" --subnet-mode=custom
exists gcloud compute networks subnets describe "$SUBNET" --region="$REGION" ||
  gcloud compute networks subnets create "$SUBNET" --network="$NETWORK" --region="$REGION" --range="$SUBNET_RANGE"

# 1.6 — firewall scoped to the VM's network tag, SSH from your IP only
exists gcloud compute firewall-rules describe registry-allow-ssh ||
  gcloud compute firewall-rules create registry-allow-ssh --network="$NETWORK" \
    --direction=INGRESS --action=ALLOW --rules=tcp:22 \
    --source-ranges="$MY_IP/32" --target-tags="$TAG"

# 1.7 — SSH key pair
[ -f "$SSH_KEY" ] || ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "$SSH_USER"
echo "$SSH_USER:$(cat "$SSH_KEY.pub")" > /tmp/registry-ssh-keys

# 1.5 — reserve the static IP first so the VM gets it at creation
exists gcloud compute addresses describe "$IP_NAME" --region="$REGION" ||
  gcloud compute addresses create "$IP_NAME" --region="$REGION"
STATIC_IP=$(gcloud compute addresses describe "$IP_NAME" --region="$REGION" --format='value(address)')

# 1.4 — e2-micro in asia-south1 (NOT always-free here: billed to the $300 trial credit)
exists gcloud compute instances describe "$VM" --zone="$ZONE" ||
  gcloud compute instances create "$VM" --zone="$ZONE" \
    --machine-type=e2-micro \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=10GB --boot-disk-type=pd-standard \
    --subnet="$SUBNET" --address="$STATIC_IP" \
    --tags="$TAG" \
    --no-service-account --no-scopes \
    --metadata-from-file=ssh-keys=/tmp/registry-ssh-keys \
    --metadata=enable-oslogin=FALSE

INSTANCE_ID=$(gcloud compute instances describe "$VM" --zone="$ZONE" --format='value(id)')
PRIVATE_IP=$(gcloud compute instances describe "$VM" --zone="$ZONE" --format='value(networkInterfaces[0].networkIP)')

# 1.8 — post these to the team sheet
cat <<EOF

==== post to the team sheet ====
Cloud:        GCP
Region:       $REGION ($ZONE)
Region ID:    gcp-asia-south1
Static IP:    $STATIC_IP
Private IP:   $PRIVATE_IP
Instance ID:  $INSTANCE_ID
Instance:     $VM (e2-micro, Debian 12)
================================
SSH:  ssh -i $SSH_KEY $SSH_USER@$STATIC_IP
EOF
