# Business-service stubs

These stubs provide the four HTTP services owned by a region. The same code is
used on AWS, Azure and GCP; only the region and standard service differ.

| Region | Critical services | Standard service |
| --- | --- | --- |
| `aws-ap-south-1` | `payments-api:8081`, `fraud-check:8082`, `inventory-check:8083` | `recommend-api:8084` |
| `azure-indiasouthcentral` | `payments-api:8081`, `fraud-check:8082`, `inventory-check:8083` | `admin-api:8084` |
| `gcp-asia-south1` | `payments-api:8081`, `fraud-check:8082`, `inventory-check:8083` | `notify-api:8084` |

## Local setup

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start a region's four services:

```powershell
.\services\run_region.ps1 -Cloud aws
```

Alternatively, start one service in a terminal:

```powershell
$env:SERVICE_NAME = "payments-api"
$env:SERVICE_REGION = "aws-ap-south-1"
$env:SERVICE_PORT = "8081"
python -m services.app
```

Check health and simulate a failure:

```powershell
Invoke-RestMethod http://127.0.0.1:8081/health
Invoke-RestMethod -Method Post http://127.0.0.1:8081/simulate/fail
Invoke-RestMethod http://127.0.0.1:8081/health
Invoke-RestMethod -Method Post http://127.0.0.1:8081/simulate/recover
```

The failed health check returns HTTP `503`; the recovered check returns `200`.

## Register with the local agent

Start the agent separately. For loopback service endpoints, use:

```powershell
$env:REGION_ID = "aws-ap-south-1"
$env:REGISTRY_SECRET = "choose-a-private-shared-secret"
$env:REGISTRY_ALLOW_LOOPBACK = "true"
python -m agent.main
```

In another terminal, register the region's four services:

```powershell
$env:REGION_ID = "aws-ap-south-1"
$env:REGISTRY_SECRET = "choose-a-private-shared-secret"
$env:SERVICE_HOST = "127.0.0.1"
python -m services.register_services
```

After the health scheduler runs, inspect the records:

```powershell
Invoke-RestMethod "http://127.0.0.1:9000/lookup?service_name=payments-api"
```

On a cloud VM, set `SERVICE_HOST` to the VM's private address and leave
`REGISTRY_ALLOW_LOOPBACK=false`.

Never commit real registry secrets, public IPs, private keys or certificates.