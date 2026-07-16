#!/usr/bin/env python3
"""
reset-student-passwords.py

(Re)generates console login passwords for students in a roster CSV and writes
a fresh credentials handout CSV. IAM never exposes a login profile's password
after creation, so this is also the recovery path if a credentials file gets
lost, corrupted, or was never captured.

Usage:
    python reset-student-passwords.py --roster students.csv --region us-east-1 \\
        --profile admin_cli_us_west_2 --created-date 16-Jul-2026

Roster CSV columns: username,full_name,cohort,active
"""

import argparse
import csv
import os
import secrets
import string

import boto3
from botocore.exceptions import ClientError

parser = argparse.ArgumentParser(description="Reset/regenerate IAM console passwords for a student roster")
parser.add_argument("--roster", required=True)
parser.add_argument("--region", default="us-east-1")
parser.add_argument("--profile", default=None)
parser.add_argument("--lambda-role-name", default="quicklabs-fullstack-shared-lambda-exec")
parser.add_argument("--created-date", required=True, help="dd-mmm-yyyy, used only in the output filename/label")
parser.add_argument("--credentials-out", default=None)
args = parser.parse_args()

session = boto3.Session(profile_name=args.profile, region_name=args.region)
iam = session.client("iam")
sts = session.client("sts")
account_id = sts.get_caller_identity()["Account"]
lambda_role_arn = f"arn:aws:iam::{account_id}:role/{args.lambda_role_name}"

ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"

def generate_password(length=20):
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*()-_=+" for c in pw)):
            return pw

with open(args.roster, newline="") as f:
    rows = [r for r in csv.DictReader(f) if r.get("username", "").strip()]

credentials = []
reset_count, skipped, errors = 0, 0, 0

for row in rows:
    username = row["username"].strip()
    full_name = row.get("full_name", "").strip()
    cohort = row.get("cohort", "").strip()
    active = row.get("active", "true").strip().lower()
    if active == "false":
        print(f"  [SKIP] {username} (active=false)")
        skipped += 1
        continue

    password = generate_password()
    try:
        try:
            iam.update_login_profile(UserName=username, Password=password, PasswordResetRequired=True)
            print(f"  [RESET] {username}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
            iam.create_login_profile(UserName=username, Password=password, PasswordResetRequired=True)
            print(f"  [CREATED LOGIN PROFILE] {username}")

        slug = username.split("@")[0]
        credentials.append({
            "username": username,
            "full_name": full_name,
            "cohort": cohort,
            "console_url": f"https://{account_id}.signin.aws.amazon.com/console",
            "console_password": password,
            "region": args.region,
            "slug": slug,
            "lambda_role_arn": lambda_role_arn,
        })
        reset_count += 1
    except ClientError as e:
        print(f"  [ERROR] {username}: {e}")
        errors += 1

out_path = args.credentials_out or os.path.join(os.path.dirname(args.roster), f"students-credentials-{args.created_date}.csv")
with open(out_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["username", "full_name", "cohort", "console_url", "console_password", "region", "slug", "lambda_role_arn"])
    w.writeheader()
    for c in credentials:
        w.writerow(c)
    f.flush()
    os.fsync(f.fileno())
os.chmod(out_path, 0o600)

print(f"\n{'='*60}")
print(f"Reset: {reset_count}  Skipped: {skipped}  Errors: {errors}")
print(f"Credentials written to: {out_path} (chmod 600)")

with open(out_path) as f:
    line_count = sum(1 for _ in f)
print(f"Verification: file now has {line_count} lines (expected {reset_count + 1} incl. header)")
