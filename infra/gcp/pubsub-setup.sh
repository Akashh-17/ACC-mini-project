#!/usr/bin/env bash
# Back notify-api with Cloud Pub/Sub (GCP owner).
#
#   OWNER=you@gmail.com PROJECT_ID=my-project bash infra/gcp/pubsub-setup.sh
#
# Creates topic `order-events` + pull subscription `order-events-audit`, a service
# account allowed only to publish to (and view) that topic, and attaches it to the
# VM. Attaching requires the VM to be STOPPED: GCP is offline ~1-2 min and peers
# mark its entries STALE until it rejoins. Re-runnable.
# Afterwards, on the VM: set NOTIFY_BACKEND=pubsub (+ PUBSUB_PROJECT/TOPIC) in
# /etc/registry/services/notify-api.env and restart registry-service@notify-api.
set -euo pipefail
OWNER=${OWNER:?owner account (the college account cannot create service accounts)}
PROJECT_ID=${PROJECT_ID:?}
ZONE=${ZONE:-asia-south1-a}
VM=${VM:-registry-gcp}
TOPIC=order-events
SUB=order-events-audit
SA_NAME=notify-publisher
SA="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
g() { gcloud --account="$OWNER" --project="$PROJECT_ID" "$@"; }
exists() { "$@" >/dev/null 2>&1; }

g services enable pubsub.googleapis.com
exists g pubsub topics describe $TOPIC || g pubsub topics create $TOPIC
exists g pubsub subscriptions describe $SUB || g pubsub subscriptions create $SUB --topic=$TOPIC --message-retention-duration=1d
exists g iam service-accounts describe "$SA" ||
  g iam service-accounts create $SA_NAME --display-name="notify-api Pub/Sub publisher"

# Least privilege: publish + view on this one topic only (view = health check).
for role in roles/pubsub.publisher roles/pubsub.viewer; do
  g pubsub topics add-iam-policy-binding $TOPIC --member="serviceAccount:$SA" --role=$role >/dev/null
done

current=$(g compute instances describe $VM --zone=$ZONE --format='value(serviceAccounts[0].email)')
if [ "$current" != "$SA" ]; then
  echo "Stopping $VM to attach $SA (GCP offline ~1-2 min)..."
  g compute instances stop $VM --zone=$ZONE
  g compute instances set-service-account $VM --zone=$ZONE --service-account="$SA" --scopes=cloud-platform
  g compute instances start $VM --zone=$ZONE
else
  echo "$SA already attached to $VM"
fi
echo "Done. Topic projects/$PROJECT_ID/topics/$TOPIC, subscription $SUB, SA $SA attached."
