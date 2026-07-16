#!/usr/bin/env python3
"""
create-batch-users.py

Creates AWS IAM users for a cohort from a roster CSV, adds them to an existing
IAM group, and writes a credentials handout CSV. Also (idempotently) makes sure
the group has a working sandbox policy attached and a shared Lambda execution
role exists, since student-iam-policy.json's PassRole statement expects a role
named quicklabs-*-lambda-exec to exist.

This intentionally does NOT use Terraform / terraform-iam's per-cohort
workspace pattern. It targets one flat, pre-existing IAM group
(fullstack_student_group by default) shared across batches, per the process
this was asked to replicate.

Usage:
    python create-batch-users.py --roster students.csv --region us-east-1 \\
        --group fullstack_student_group --profile admin_cli_us_west_2 \\
        --created-date 16-Jul-2026

Roster CSV columns: username,full_name,cohort,active
"""

import argparse
import csv
import json
import os
import secrets
import string
import sys

import boto3
from botocore.exceptions import ClientError

parser = argparse.ArgumentParser(description="Create IAM users for a batch and add them to the student group")
parser.add_argument("--roster",  required=True, help="Path to the roster CSV (username,full_name,cohort,active)")
parser.add_argument("--region",  default="us-east-1")
parser.add_argument("--group",   default="fullstack_student_group")
parser.add_argument("--policy-name", default="FullStackStudentSandboxPolicy")
parser.add_argument("--policy-file", default=os.path.join(os.path.dirname(__file__), "..", "student-iam-policy.json"))
parser.add_argument("--lambda-role-name", default="quicklabs-fullstack-shared-lambda-exec")
parser.add_argument("--created-date", required=True, help="dd-mmm-yyyy, e.g. 16-Jul-2026")
parser.add_argument("--profile", default=None)
parser.add_argument("--credentials-out", default=None, help="Where to write the credentials CSV (default: alongside the roster)")
args = parser.parse_args()

session = boto3.Session(profile_name=args.profile, region_name=args.region)
iam = session.client("iam")
sts = session.client("sts")

account_id = sts.get_caller_identity()["Account"]
print(f"Account: {account_id}  Region: {args.region}")

# --- Step 1: ensure the sandbox policy exists and is attached to the group ---

with open(args.policy_file) as f:
    policy_doc = f.read().replace("{ACCOUNT_ID}", account_id)

policy_arn = f"arn:aws:iam::{account_id}:policy/{args.policy_name}"
try:
    iam.get_policy(PolicyArn=policy_arn)
    print(f"Policy {args.policy_name} already exists, leaving its content as-is (edit in console/CLI if it needs updating).")
except ClientError as e:
    if e.response["Error"]["Code"] != "NoSuchEntity":
        raise
    iam.create_policy(
        PolicyName=args.policy_name,
        PolicyDocument=policy_doc,
        Description="Region-locked (us-east-1) full access to Lambda/EC2/S3/CloudWatch/CloudFront/DynamoDB, PassRole scoped to the shared Lambda role only.",
        Tags=[
            {"Key": "workshop", "Value": "full-stack"},
            {"Key": "autodelete", "Value": "false"},
        ],
    )
    print(f"Created policy {args.policy_name}.")

attached = iam.list_attached_group_policies(GroupName=args.group)["AttachedPolicies"]
attached_names = {p["PolicyName"] for p in attached}

STALE_POLICIES = {
    "StudentCloudWatchPolicy",
    "StudentCloudFrontPolicy",
    "StudentAPIGatewayPolicy",
    "Restricted_EC2_student_policy",
    "StudentS3Policy",
    "StudentLambdaPolicy",
    "StudentIAMPolicy",
    "StudentTaggingPolicy",
    "StudentSelfServicePolicy",
}
for p in attached:
    if p["PolicyName"] in STALE_POLICIES:
        print(f"Detaching stale policy from {args.group}: {p['PolicyName']}")
        iam.detach_group_policy(GroupName=args.group, PolicyArn=p["PolicyArn"])

if args.policy_name not in attached_names:
    print(f"Attaching {args.policy_name} to {args.group}")
    iam.attach_group_policy(GroupName=args.group, PolicyArn=policy_arn)
else:
    print(f"{args.policy_name} already attached to {args.group}")

# --- Step 2: ensure the shared Lambda execution role exists ---

try:
    role = iam.get_role(RoleName=args.lambda_role_name)["Role"]
    print(f"Shared Lambda role already exists: {role['Arn']}")
except ClientError as e:
    if e.response["Error"]["Code"] != "NoSuchEntity":
        raise
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    role = iam.create_role(
        RoleName=args.lambda_role_name,
        AssumeRolePolicyDocument=json.dumps(trust_policy),
        Tags=[
            {"Key": "workshop", "Value": "full-stack"},
            {"Key": "autodelete", "Value": "false"},
            {"Key": "date", "Value": args.created_date},
        ],
    )["Role"]
    iam.attach_role_policy(
        RoleName=args.lambda_role_name,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=args.lambda_role_name,
        PolicyName=f"{args.lambda_role_name}-dynamodb",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "dynamodb:*", "Resource": "*"}],
        }),
    )
    print(f"Created shared Lambda role: {role['Arn']}")

lambda_role_arn = role["Arn"]

# --- Step 3: read roster, create users ---

with open(args.roster, newline="") as f:
    rows = [r for r in csv.DictReader(f) if r.get("username", "").strip()]

print(f"\n{len(rows)} student(s) in roster.")

ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"

def generate_password(length=20):
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*()-_=+" for c in pw)):
            return pw

credentials = []
created, skipped, errors = 0, 0, 0

for row in rows:
    username = row["username"].strip()
    full_name = row.get("full_name", "").strip()
    cohort = row.get("cohort", "").strip()
    active = row.get("active", "true").strip().lower()
    if active == "false":
        print(f"  [SKIP] {username} (active=false)")
        skipped += 1
        continue

    try:
        iam.get_user(UserName=username)
        print(f"  [SKIP] {username} already exists")
        skipped += 1
        continue
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    try:
        iam.create_user(
            UserName=username,
            Tags=[
                {"Key": "full_name", "Value": full_name},
                {"Key": "cohort", "Value": cohort},
                {"Key": "workshop", "Value": "full-stack"},
                {"Key": "autodelete", "Value": "true"},
                {"Key": "date", "Value": args.created_date},
            ],
        )
        password = generate_password()
        iam.create_login_profile(UserName=username, Password=password, PasswordResetRequired=True)
        iam.add_user_to_group(GroupName=args.group, UserName=username)

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
        print(f"  [CREATED] {username}")
        created += 1
    except ClientError as e:
        print(f"  [ERROR] {username}: {e}")
        errors += 1

# --- Step 4: write credentials CSV ---

out_path = args.credentials_out or os.path.join(os.path.dirname(args.roster), f"students-credentials-{args.created_date}.csv")
with open(out_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["username", "full_name", "cohort", "console_url", "console_password", "region", "slug", "lambda_role_arn"])
    w.writeheader()
    for c in credentials:
        w.writerow(c)
os.chmod(out_path, 0o600)

print(f"\n{'='*60}")
print(f"Created: {created}  Skipped: {skipped}  Errors: {errors}")
print(f"Credentials written to: {out_path} (chmod 600)")
print(f"Shared Lambda role ARN (give to students as lambda_role_arn=): {lambda_role_arn}")
