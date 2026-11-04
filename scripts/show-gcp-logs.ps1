# Opens the readable live log of the GCP registry agent (for demos). Ctrl+C to stop.
# The remote pipeline is wrapped in sh -c '...' so Windows OpenSSH passes it intact.
$Host.UI.RawUI.WindowTitle = "GCP registry agent - live log"
$key = Join-Path $HOME ".ssh\gcp-registry"
ssh -tt -i $key -o ConnectTimeout=15 registry@100.102.19.73 "sh -c 'sudo journalctl -u registry-agent -f -n 30 -o cat | python3 -u /opt/registry/scripts/demo_logs.py'"
