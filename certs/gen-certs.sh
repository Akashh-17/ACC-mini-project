#!/usr/bin/env bash
# Generate the shared CA and one mTLS cert per agent.
#
#   ./certs/gen-certs.sh OUT_DIR  NAME=IP[,IP...]  [NAME=IP ...]
#
# Example (real deployment — use each VM's static public IP):
#   ./certs/gen-certs.sh certs/out aws=13.1.2.3 azure=20.1.2.3 gcp=34.1.2.3
# Example (local 3-agent test):
#   ./certs/gen-certs.sh certs/local aws=127.0.0.1 azure=127.0.0.1 gcp=127.0.0.1
#
# Re-running reuses the existing CA, so certs can be (re)issued later.
#
# Distribute to each owner over a PRIVATE channel, never the repo:
#   <name>.crt, <name>.key, ca.crt
# ca.key stays with the GCP owner only.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # stop Git Bash on Windows rewriting "/CN=..." into a path

OUT=${1:?usage: gen-certs.sh OUT_DIR NAME=IP[,IP...] ...}
shift
[ $# -gt 0 ] || { echo "need at least one NAME=IP" >&2; exit 1; }
mkdir -p "$OUT"
cd "$OUT"

if [ ! -f ca.key ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 365 \
    -keyout ca.key -out ca.crt -subj "/CN=crosscloud-registry-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" 2>/dev/null
  echo "created CA: $OUT/ca.crt"
fi

for pair in "$@"; do
  name=${pair%%=*}
  ips=${pair#*=}
  san=$(echo "$ips" | tr ',' '\n' | sed 's/^/IP:/' | paste -sd, -)
  san="$san,DNS:localhost"

  cat > "$name.ext" <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=$san
EOF
  openssl req -newkey rsa:2048 -nodes -keyout "$name.key" -out "$name.csr" -subj "/CN=$name" 2>/dev/null
  openssl x509 -req -sha256 -days 365 -in "$name.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -extfile "$name.ext" -out "$name.crt" 2>/dev/null
  rm -f "$name.csr" "$name.ext"
  chmod 600 "$name.key"
  echo "issued $name.crt  (SAN: $san)"
done
chmod 600 ca.key
