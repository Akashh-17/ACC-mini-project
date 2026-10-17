"""Load the shared catalog into the managed services.

    python -m services.seed s3-file [--out recommendations.json]
        Write the recommendations JSON to a local file (upload it with the console
        or `aws s3 cp`; the EC2 instance role is read-only on purpose).
    python -m services.seed s3 --bucket BUCKET [--key recommendations.json] [--region ap-south-1]
        Upload it directly with whatever AWS credentials are active (needs s3:PutObject).
    python -m services.seed azuretable --account-url https://ACCOUNT.table.core.windows.net [--table inventory]
        Create the table if needed and upsert one row per product (managed identity
        on the VM, or `az login` on a laptop; needs Storage Table Data Contributor).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backends import catalog_seed_rows
from .catalog import RECOMMENDATIONS


def recommendations_json() -> str:
    return json.dumps(RECOMMENDATIONS, indent=2)


def seed_s3(bucket: str, key: str, region: str, client=None) -> None:
    if client is None:
        import boto3
        client = boto3.client("s3", region_name=region)
    client.put_object(Bucket=bucket, Key=key, Body=recommendations_json().encode(),
                      ContentType="application/json")


def seed_azure_table(account_url: str, table: str, service_client=None) -> int:
    if service_client is None:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential
        service_client = TableServiceClient(endpoint=account_url, credential=DefaultAzureCredential())
    table_client = service_client.create_table_if_not_exists(table)
    rows = catalog_seed_rows()
    for row in rows:
        table_client.upsert_entity(row)
    return len(rows)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("s3-file")
    f.add_argument("--out", default="recommendations.json")
    s = sub.add_parser("s3")
    s.add_argument("--bucket", required=True)
    s.add_argument("--key", default="recommendations.json")
    s.add_argument("--region", default="ap-south-1")
    a = sub.add_parser("azuretable")
    a.add_argument("--account-url", required=True)
    a.add_argument("--table", default="inventory")
    args = parser.parse_args(argv)

    if args.cmd == "s3-file":
        Path(args.out).write_text(recommendations_json(), encoding="utf-8")
        print(f"wrote {args.out}")
    elif args.cmd == "s3":
        seed_s3(args.bucket, args.key, args.region)
        print(f"uploaded s3://{args.bucket}/{args.key}")
    else:
        n = seed_azure_table(args.account_url, args.table)
        print(f"upserted {n} rows into table {args.table}")


if __name__ == "__main__":
    main()
