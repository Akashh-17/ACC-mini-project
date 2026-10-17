# Azure: back admin-api with Azure Table Storage

admin-api keeps its stock records in an Azure Table, so updates survive restarts. The VM
gets access through its **managed identity** with the *Storage Table Data Contributor*
role on one storage account. No keys or connection strings are stored on the VM.
About 30 minutes.

Use **Azure Cloud Shell (Bash)** in the portal for steps 1–3. Set these first (choose a
storage account name: 3–24 lowercase letters/digits, globally unique):
```bash
RG=<your resource group>              # the one azure-registry-vm is in
VM=azure-registry-vm
SA=accregistryinv<initials>           # storage account name
LOC=$(az vm show -g $RG -n $VM --query location -o tsv); echo $LOC
```

## 1. Storage account + table (~3 min)
```bash
az storage account create -g $RG -n $SA -l $LOC --sku Standard_LRS --kind StorageV2 \
  --min-tls-version TLS1_2 --allow-blob-public-access false
az storage table create --name inventory --account-name $SA --auth-mode login
```
If the second command says you lack permission, skip it: step 4's seed creates the table.

## 2. Turn on the VM's managed identity (~1 min)
```bash
PRINCIPAL=$(az vm identity assign -g $RG -n $VM --query systemAssignedIdentity -o tsv); echo $PRINCIPAL
```

## 3. Grant it table access, on this storage account only (~1 min, then wait ~5 min)
```bash
SCOPE=$(az storage account show -g $RG -n $SA --query id -o tsv)
az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Storage Table Data Contributor" --scope $SCOPE
echo "AZURE_TABLE_ACCOUNT_URL=https://$SA.table.core.windows.net"
```
Role assignments take **up to ~5 minutes** to take effect. Copy the printed URL.

## 4. Install, seed and switch admin-api (on the VM, ~5 min)
```bash
cd <repo folder> && git pull
.venv/bin/pip install -r services/requirements-azure.txt
.venv/bin/python -m services.seed azuretable --account-url https://SA.table.core.windows.net
#   -> "upserted 5 rows into table inventory"
sudo tee -a /etc/registry/services/admin-api.env >/dev/null <<EOF
ADMIN_BACKEND=azuretable
AZURE_TABLE_ACCOUNT_URL=https://SA.table.core.windows.net
AZURE_TABLE_NAME=inventory
EOF
sudo systemctl restart registry-service@admin-api
```
The seed runs as the VM's managed identity. If it fails with `AuthorizationPermissionMismatch`,
the role hasn't propagated yet: wait a few minutes and retry.

## 5. Verify
```bash
curl -s localhost:8084/health        # ... "backend":"azuretable"
curl -s localhost:8084/inventory     # {"items":[...],"source":"azuretable"}
curl -s -X POST localhost:8084/inventory/update -H 'Content-Type: application/json' -d '{"product_id":"p1","stock":24}'
sudo systemctl restart registry-service@admin-api && sleep 3 && curl -s localhost:8084/inventory   # p1 still 24
```
The last line shows the point of the change: the data survives a restart. Then tell Ishan.

## Troubleshooting
| Symptom | Cause |
|---|---|
| `/health` 503, `AuthorizationPermissionMismatch` / `403` | Role not propagated yet (wait), or assigned on the wrong scope |
| `ManagedIdentityCredential ... unavailable` | Step 2 not done on *this* VM |
| `ResourceNotFound` (table) | Seed (step 4) not run, or `AZURE_TABLE_NAME` differs |
| `ModuleNotFoundError: azure` | pip ran in a different venv than the service uses |

**Undo:** remove the three lines from the env file and restart admin-api (it falls back to
in-memory stock).
