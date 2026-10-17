# AWS: back recommend-api with Amazon S3

recommend-api reads "customers also bought" data from one JSON file in a private S3
bucket. The EC2 instance gets **read-only** access to that one file through an IAM
role. No access keys are stored on the VM. About 30 minutes in total.

Pick a globally unique bucket name, e.g. `acc-registry-recs-<your-initials>-2026`, and
use it everywhere `BUCKET` appears below. Region: `ap-south-1` (same as the VM).

## 1. Create the bucket (console, ~3 min)
S3 → **Create bucket**
- Name: `BUCKET`, Region: **Asia Pacific (Mumbai) ap-south-1**
- **Block all public access: ON** (default). The file is private.
- Everything else default → **Create bucket**

## 2. Upload the data file (~3 min)
On your laptop or the VM, from the repo root (after `git pull`):
```bash
python -m services.seed s3-file --out recommendations.json
```
Upload `recommendations.json` to the bucket root (S3 console → bucket → **Upload**).
Or with AWS CloudShell / an AWS CLI that has write access:
`aws s3 cp recommendations.json s3://BUCKET/recommendations.json`

## 3. IAM role for the EC2 instance (console, ~10 min)
IAM → **Roles** → **Create role**
- Trusted entity: **AWS service** → **EC2** → Next
- Skip the managed policies → Next → Name: `registry-recommend-s3-read` → **Create role**
- Open the role → **Add permissions → Create inline policy** → JSON:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::BUCKET/recommendations.json"
  }]
}
```
  Name it `read-recommendations-json` → **Create policy**.

Attach it: EC2 → Instances → select the registry VM → **Actions → Security → Modify IAM
role** → `registry-recommend-s3-read` → **Update IAM role**. No reboot is needed.

## 4. Switch recommend-api to S3 (on the VM, ~5 min)
```bash
cd <repo folder> && git pull
.venv/bin/pip install -r services/requirements-aws.txt
sudo tee -a /etc/registry/services/recommend-api.env >/dev/null <<EOF
RECOMMEND_BACKEND=s3
S3_BUCKET=BUCKET
S3_REGION=ap-south-1
EOF
sudo systemctl restart registry-service@recommend-api
```
If your venv or env file lives elsewhere, adjust the paths; the three variables are what
matter.

## 5. Verify
```bash
curl -s localhost:8084/health                               # ... "backend":"s3"
curl -s "localhost:8084/recommendations?product_id=p1"      # {"recommended_ids":[...],"source":"s3"}
sudo journalctl -u registry-service@recommend-api -n 5      # "recommend-api using s3 backend"
```
Then tell Ishan, who verifies it from GCP and the storefront.

## Troubleshooting
| Symptom | Cause |
|---|---|
| `/health` 503, log says `AccessDenied` / `403` | Role not attached, or the policy `Resource` doesn't match the bucket/key exactly |
| `NoCredentialsError` | Role not attached to *this* instance |
| `NoSuchBucket` / `404` | `S3_BUCKET` typo, or the file isn't at the bucket root |
| `ModuleNotFoundError: boto3` | Step 4 `pip install` ran in a different venv than the service uses |

**Undo** (falls back to the built-in data): remove the three lines from the env file and
restart the service.
