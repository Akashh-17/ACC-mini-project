# Live, readable access log of one GCP stub service (default payments-api). Ctrl+C to stop.
#   powershell -ExecutionPolicy Bypass -File scripts\show-service-logs.ps1 [-Service fraud-check]
param([string]$Service = "payments-api")
$Host.UI.RawUI.WindowTitle = "GCP $Service - live log"
$key = Join-Path $HOME ".ssh\gcp-registry"
ssh -tt -i $key -o ConnectTimeout=15 registry@100.102.19.73 "sh -c 'sudo journalctl -u registry-service@$Service -f -n 40 -o short-iso | python3 -u /opt/registry/scripts/service_logs.py'"
