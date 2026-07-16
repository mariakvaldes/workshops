#!/usr/bin/env python3
"""
cleanup-student-resources.py

Nightly sweep of lab resources (Lambda, S3, EC2, CloudFront, WAFv2 Web ACLs,
DynamoDB, log groups, key pairs, security groups). Deletion is tag-driven, not name-driven:

    A resource is deleted UNLESS it is tagged autodelete=false.

That means a resource with no tags at all, autodelete=true, or any other
value gets swept. Only autodelete=false (case-insensitive) protects it. This
matches the tagging convention in the root README: tag your resources or the
nightly script removes them, no warning.

This script does NOT touch IAM. The shared Lambda execution role, the IAM
group/policy, and student IAM users are admin-provisioned in terraform-iam
and tagged autodelete=true by that module's own default_tags (because they
need to survive nightly sweeps, not because they're supposed to persist
forever): running this script unscoped would otherwise delete every
student's login and the shared execution role on night one. Cohort-level IAM
teardown stays a deliberate step: `terraform destroy` in terraform-iam, per
admin-walkthrough.md.

This AWS account is shared with other projects, confirmed by a real dry run
(unrelated security groups, a Kubernetes ELB group, dozens of untagged key
pairs from unrelated experiments). Two extra safety nets beyond the tag rule:
  1. A hard-coded exclude list for well-known AWS-managed resource name
     prefixes (CloudTrail logs, Config, CDK/CloudFormation bootstrap, etc.)
     that is never deleted regardless of tags.
  2. Security groups and key pairs additionally require a student-/quicklabs-
     style name OR a matching --workshop tag before they're considered: most
     of what's actually in this account for these two resource types was
     created by hand outside Terraform and carries no tags at all, so the tag
     rule alone isn't enough discrimination for them specifically.
Always pass --workshop full-stack for a real cohort unless you deliberately
want to sweep the whole account.

Usage:
    # Dry run: preview everything (safe, no deletions)
    python cleanup-student-resources.py --region us-east-1

    # Actually delete
    python cleanup-student-resources.py --region us-east-1 --delete

    # Extra safety: also require workshop=<value> tag to match (recommended
    # if this AWS account is ever used for anything besides this workshop)
    python cleanup-student-resources.py --region us-east-1 --delete --workshop full-stack

    # Restrict to one student's named resources only (still tag-gated on top)
    python cleanup-student-resources.py --region us-east-1 --delete --student john.doe

    # Restrict to a list of students (one username per line, or CSV with 'username' column)
    python cleanup-student-resources.py --region us-east-1 --delete --users-file students.csv
"""

import boto3
import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from botocore.exceptions import ClientError, WaiterError

# ─── CLI ─────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Tag-driven nightly cleanup of student lab resources")
parser.add_argument("--region",     required=True, help="AWS region (e.g. us-east-1)")
parser.add_argument("--delete",     action="store_true", help="Actually delete resources (default is dry run)")
parser.add_argument("--workshop",   default=None, help="Also require workshop=<value> tag (e.g. full-stack). Omit to sweep the whole account/region.")
parser.add_argument("--student",    default=None, help="Restrict to one student's named resources (student-<name>-* / <name>-prefixed), still tag-gated")
parser.add_argument("--users-file", default=None, help="Path to a CSV or .txt file with usernames to restrict to")
parser.add_argument("--profile",    default=None, help="AWS CLI profile to use")
args = parser.parse_args()

DRY_RUN  = not args.delete
REGION   = args.region
WORKSHOP = args.workshop

# ─── Build optional student-name filter (name-based, on top of the tag gate) ──

TARGET_USERS = None  # None means "no name restriction: tag gate alone decides"

if args.users_file:
    if not os.path.exists(args.users_file):
        print(f"ERROR: --users-file '{args.users_file}' not found.")
        sys.exit(1)

    TARGET_USERS = []
    with open(args.users_file, newline="") as f:
        sample = f.read(1024)
        f.seek(0)
        if "," in sample:
            reader = csv.DictReader(f)
            col = next((c for c in reader.fieldnames if c.lower() in ("username", "user_name")), None)
            if not col:
                print(f"ERROR: CSV must have a 'username' column. Found: {reader.fieldnames}")
                sys.exit(1)
            TARGET_USERS = [row[col].strip() for row in reader if row[col].strip()]
        else:
            KNOWN_HEADERS = {"username", "user_name", "name"}
            lines = [line.strip() for line in f if line.strip()]
            if lines and lines[0].lower() in KNOWN_HEADERS:
                lines = lines[1:]
            TARGET_USERS = list(dict.fromkeys(lines))

    print(f"Loaded {len(TARGET_USERS)} user(s) from {args.users_file}")

elif args.student:
    TARGET_USERS = [args.student]

PREFIX = "student-"
WORKSHOP_NAME_PREFIXES = ("student-", "quicklabs-")

# Never touched, regardless of tags or --workshop. These are well-known
# AWS/account-managed resource name prefixes, not lab resources. Found the
# hard way: a real dry run against this account flagged the CloudTrail log
# bucket for deletion because it (correctly) has no autodelete tag at all.
NEVER_DELETE_NAME_PREFIXES = (
    "aws-cloudtrail-logs-",
    "aws-config-",
    "cf-templates-",
    "elasticbeanstalk-",
    "amplify-",
    "cdk-",
    "codepipeline-",
    "do-not-delete-",
)

def is_protected_by_name(resource_name):
    return any(resource_name.startswith(p) for p in NEVER_DELETE_NAME_PREFIXES)

def name_matches_target(resource_name):
    """True if no student filter is set, or the name looks like it belongs to a targeted student.
    Resources are conventionally named student-<slug>-...: this is a recommendation, not
    IAM-enforced, so treat it as best-effort when a --student/--users-file filter is used."""
    if TARGET_USERS is None:
        return True
    if not resource_name.startswith(PREFIX):
        return False
    suffix = resource_name[len(PREFIX):]
    return any(suffix.startswith(u) for u in TARGET_USERS)

def looks_like_workshop_resource(resource_name, raw_tags):
    """For resource types that are frequently created by hand outside Terraform
    (security groups, key pairs) and therefore often carry no tags at all: require
    EITHER a recognizable student-/quicklabs- style name OR a workshop tag match,
    so an untagged unrelated resource with a generic name doesn't get swept."""
    if resource_name.startswith(WORKSHOP_NAME_PREFIXES):
        return True
    tags = normalize_tags(raw_tags)
    if WORKSHOP is not None:
        return tags.get("workshop", "").strip().lower() == WORKSHOP.strip().lower()
    return "workshop" in tags

# ─── Tag gate: the actual deletion decision ───────────────────────────────────

def normalize_tags(raw):
    """Accepts a dict, a list of {'Key':.., 'Value':..}, or a list of {'key':.., 'value':..}
    and returns a lowercase-keyed dict for case-insensitive lookups."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k).lower(): str(v) for k, v in raw.items()}
    out = {}
    for item in raw:
        k = item.get("Key", item.get("key"))
        v = item.get("Value", item.get("value"))
        if k is not None:
            out[str(k).lower()] = str(v)
    return out

def eligible_for_deletion(raw_tags):
    """The core rule: delete unless autodelete is explicitly false. Also requires the
    workshop tag to match if --workshop was given."""
    tags = normalize_tags(raw_tags)
    if tags.get("autodelete", "").strip().lower() == "false":
        return False
    if WORKSHOP is not None and tags.get("workshop", "").strip().lower() != WORKSHOP.strip().lower():
        return False
    return True

# ─────────────────────────────────────────────────────────────────────────────

session = boto3.Session(profile_name=args.profile, region_name=REGION)

try:
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    ACCOUNT_ID = identity["Account"]
except ClientError as e:
    code = e.response["Error"]["Code"]
    if code in ("InvalidClientTokenId", "ExpiredTokenException"):
        print("\n[FATAL] AWS credentials are invalid or expired.")
        print("  Run: aws sso login  (or re-export your credentials)")
        sys.exit(1)
    raise

scope_bits = []
if TARGET_USERS is not None:
    scope_bits.append(f"{len(TARGET_USERS)} named student(s)")
if WORKSHOP is not None:
    scope_bits.append(f"workshop={WORKSHOP}")
scope_label = " + ".join(scope_bits) if scope_bits else "entire account/region (no name or workshop filter)"

print(f"\n{'[DRY RUN] ' if DRY_RUN else '[DELETING] '}Region: {REGION}")
print(f"Scope: {scope_label}")
print("Rule: delete everything found UNLESS tagged autodelete=false\n")

if not DRY_RUN and WORKSHOP is None and TARGET_USERS is None:
    print("!" * 60)
    print("! No --workshop filter set. This will sweep the ENTIRE account/region,")
    print("! not just this workshop's resources. This account is known to hold")
    print("! resources from other projects. Strongly recommended: re-run with")
    print("! --workshop full-stack. Proceeding in 10 seconds, Ctrl+C to abort.")
    print("!" * 60)
    import time
    time.sleep(10)

deleted = []
errors  = []

def log(resource_type, name):
    print(f"  {'[DRY RUN]' if DRY_RUN else '[DELETED]'} {resource_type}: {name}")

def log_protected(resource_type, name):
    print(f"  [PROTECTED: autodelete=false] {resource_type}: {name}")

EXPIRED_TOKEN_CODES = {"InvalidClientTokenId", "ExpiredTokenException", "RequestExpired"}

def record_error(resource_type, name, e):
    code = e.response["Error"]["Code"] if hasattr(e, "response") else ""
    if code in EXPIRED_TOKEN_CODES:
        print(f"\n[FATAL] AWS credentials expired mid-run.")
        print(f"  Refresh your credentials and re-run.")
        print(f"  Stopped at: {resource_type} '{name}'\n")
        sys.exit(1)
    msg = f"{resource_type} '{name}': {e}"
    errors.append(msg)
    print(f"  [ERROR] {msg}")

# ─── Lambda Functions ─────────────────────────────────────────────────────────

print("── Lambda Functions ─────────────────────────────────────────────")
lam = session.client("lambda")
deleted_function_names = set()

paginator = lam.get_paginator("list_functions")
for page in paginator.paginate():
    for fn in page["Functions"]:
        name = fn["FunctionName"]
        if not name_matches_target(name):
            continue
        try:
            tags = lam.list_tags(Resource=fn["FunctionArn"]).get("Tags", {})
        except ClientError as e:
            record_error("Lambda (tags)", name, e)
            continue
        if not eligible_for_deletion(tags):
            log_protected("Lambda", name)
            continue
        log("Lambda", name)
        deleted.append(("Lambda", name))
        deleted_function_names.add(name)
        if not DRY_RUN:
            try:
                lam.delete_function(FunctionName=name)
            except ClientError as e:
                record_error("Lambda", name, e)

# ─── CloudWatch Log Groups ────────────────────────────────────────────────────
# Checked on their own tags (Terraform-created log groups inherit default_tags).
# Auto-created log groups (Lambda made one on first invoke without Terraform
# managing it) have no tags at all, so they're swept too under the default
# rule unless the associated Lambda's log group was tagged autodelete=false.

print("\n── CloudWatch Log Groups ────────────────────────────────────────")
logs = session.client("logs")
paginator = logs.get_paginator("describe_log_groups")
for prefix in ("/aws/lambda/", "/aws/apigateway/"):
    for page in paginator.paginate(logGroupNamePrefix=prefix):
        for group in page["logGroups"]:
            name = group["logGroupName"]
            fn_or_api_name = name[len(prefix):]
            if not name_matches_target(fn_or_api_name):
                continue
            try:
                tags = logs.list_tags_log_group(logGroupName=name).get("tags", {})
            except ClientError as e:
                record_error("Log Group (tags)", name, e)
                continue
            if not eligible_for_deletion(tags):
                log_protected("Log Group", name)
                continue
            log("Log Group", name)
            deleted.append(("Log Group", name))
            if not DRY_RUN:
                try:
                    logs.delete_log_group(logGroupName=name)
                except ClientError as e:
                    record_error("Log Group", name, e)

# ─── API Gateway HTTP APIs ────────────────────────────────────────────────────

print("\n── API Gateway (HTTP APIs) ───────────────────────────────────────")
apigw = session.client("apigatewayv2")
response = apigw.get_apis()
for api in response.get("Items", []):
    name = api["Name"]
    if not name_matches_target(name):
        continue
    tags = api.get("Tags", {})
    if not eligible_for_deletion(tags):
        log_protected("API Gateway", f"{name} ({api['ApiId']})")
        continue
    log("API Gateway", f"{name} ({api['ApiId']})")
    deleted.append(("API Gateway", name))
    if not DRY_RUN:
        try:
            apigw.delete_api(ApiId=api["ApiId"])
        except ClientError as e:
            record_error("API Gateway", name, e)

# ─── CloudFront Distributions ────────────────────────────────────────────────
# CloudFront distributions must be DISABLED, fully PROPAGATED (~5-15 min), then
# DELETED. Run before S3 so the bucket policy + OAC can come down cleanly.

print("\n── CloudFront Distributions ────────────────────────────────────")
cf = session.client("cloudfront")
paginator = cf.get_paginator("list_distributions")

surviving_dist_ids = set()
to_wait = []

for page in paginator.paginate():
    items = (page.get("DistributionList") or {}).get("Items") or []
    for dist in items:
        dist_id = dist["Id"]
        origins = (dist.get("Origins") or {}).get("Items") or []
        origin_summary = origins[0].get("DomainName") if origins else "<no-origin>"

        # Name-filter (if a student filter is set) by origin bucket name.
        if TARGET_USERS is not None:
            origin_owned = False
            for o in origins:
                dn = o.get("DomainName", "")
                bucket = dn.split(".s3.", 1)[0] if ".s3." in dn else o.get("Id", "")
                if name_matches_target(bucket):
                    origin_owned = True
                    break
            if not origin_owned:
                surviving_dist_ids.add(dist_id)
                continue

        dist_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:distribution/{dist_id}"
        try:
            tags = cf.list_tags_for_resource(Resource=dist_arn).get("Tags", {}).get("Items", [])
        except ClientError as e:
            record_error("CloudFront Distribution (tags)", dist_id, e)
            surviving_dist_ids.add(dist_id)
            continue

        if not eligible_for_deletion(tags):
            log_protected("CloudFront Distribution", f"{dist_id} (origin: {origin_summary})")
            surviving_dist_ids.add(dist_id)
            continue

        log("CloudFront Distribution", f"{dist_id} (origin: {origin_summary})")
        deleted.append(("CloudFront Distribution", dist_id))

        if DRY_RUN:
            continue

        if dist["Enabled"]:
            try:
                cfg = cf.get_distribution_config(Id=dist_id)
                cfg["DistributionConfig"]["Enabled"] = False
                cf.update_distribution(
                    Id=dist_id,
                    IfMatch=cfg["ETag"],
                    DistributionConfig=cfg["DistributionConfig"],
                )
                to_wait.append(dist_id)
            except ClientError as e:
                record_error("CloudFront Distribution (disable)", dist_id, e)
        else:
            to_wait.append(dist_id)

if to_wait and not DRY_RUN:
    print(f"  Waiting for {len(to_wait)} distribution(s) to reach Deployed state (5-15 min, in parallel)...")

    def wait_and_delete(d_id):
        waiter = cf.get_waiter("distribution_deployed")
        try:
            waiter.wait(Id=d_id, WaiterConfig={"Delay": 30, "MaxAttempts": 60})
        except WaiterError as e:
            return (d_id, e)
        try:
            etag = cf.get_distribution(Id=d_id)["ETag"]
            cf.delete_distribution(Id=d_id, IfMatch=etag)
            return (d_id, None)
        except ClientError as e:
            return (d_id, e)

    with ThreadPoolExecutor(max_workers=min(10, len(to_wait))) as pool:
        futures = [pool.submit(wait_and_delete, d) for d in to_wait]
        for future in as_completed(futures):
            d_id, err = future.result()
            if err:
                record_error("CloudFront Distribution (wait/delete)", d_id, err)
            else:
                print(f"  [DELETED] CloudFront Distribution: {d_id}")

# ─── CloudFront Origin Access Controls (OAC) ─────────────────────────────────
# OACs have no tagging API at all. An OAC is only ever a helper object for a
# distribution, so the safe rule is: delete it if no surviving (non-deleted,
# non-protected) distribution references it. Deleted-distribution OACs become
# orphaned; protected-distribution OACs are excluded via surviving_dist_ids.

print("\n── CloudFront Origin Access Controls ───────────────────────────")
referenced_oac_ids = set()
paginator = cf.get_paginator("list_distributions")
for page in paginator.paginate():
    items = (page.get("DistributionList") or {}).get("Items") or []
    for dist in items:
        if dist["Id"] not in surviving_dist_ids:
            continue
        origins = (dist.get("Origins") or {}).get("Items") or []
        for o in origins:
            oac_id = o.get("OriginAccessControlId")
            if oac_id:
                referenced_oac_ids.add(oac_id)

paginator = cf.get_paginator("list_origin_access_controls")
for page in paginator.paginate():
    items = (page.get("OriginAccessControlList") or {}).get("Items") or []
    for oac in items:
        name = oac["Name"]
        oac_id = oac["Id"]
        if oac_id in referenced_oac_ids:
            continue
        if TARGET_USERS is not None and not name_matches_target(name):
            continue
        log("OAC", f"{name} ({oac_id})")
        deleted.append(("OAC", name))
        if not DRY_RUN:
            try:
                etag = cf.get_origin_access_control(Id=oac_id)["ETag"]
                cf.delete_origin_access_control(Id=oac_id, IfMatch=etag)
            except ClientError as e:
                record_error("OAC", name, e)

# ─── WAFv2 Web ACLs (CloudFront scope) ───────────────────────────────────────
# CloudFront-associated Web ACLs only exist in the CLOUDFRONT scope, which is
# only queryable via the us-east-1 endpoint regardless of --region. Run after
# CloudFront distributions are deleted above, since a Web ACL still
# associated with a distribution can't be deleted (WAFAssociatedItemException).

print("\n── WAFv2 Web ACLs (CloudFront scope) ────────────────────────────")
waf = session.client("wafv2", region_name="us-east-1")

marker = None
while True:
    kwargs = {"Scope": "CLOUDFRONT"}
    if marker:
        kwargs["NextMarker"] = marker
    resp = waf.list_web_acls(**kwargs)
    for acl in resp.get("WebACLs", []):
        name = acl["Name"]
        if not name_matches_target(name):
            continue
        try:
            tags = waf.list_tags_for_resource(ResourceARN=acl["ARN"]).get("TagInfoForResource", {}).get("TagList", [])
        except ClientError as e:
            record_error("WAF Web ACL (tags)", name, e)
            continue
        if not eligible_for_deletion(tags):
            log_protected("WAF Web ACL", name)
            continue
        log("WAF Web ACL", name)
        deleted.append(("WAF Web ACL", name))
        if not DRY_RUN:
            try:
                detail = waf.get_web_acl(Name=name, Scope="CLOUDFRONT", Id=acl["Id"])
                waf.delete_web_acl(
                    Name=name,
                    Scope="CLOUDFRONT",
                    Id=acl["Id"],
                    LockToken=detail["LockToken"],
                )
            except ClientError as e:
                record_error("WAF Web ACL", name, e)
    marker = resp.get("NextMarker")
    if not marker:
        break

# ─── S3 Buckets ───────────────────────────────────────────────────────────────

print("\n── S3 Buckets ───────────────────────────────────────────────────")
s3 = session.client("s3")
s3_resource = session.resource("s3")
response = s3.list_buckets()
for bucket in response.get("Buckets", []):
    name = bucket["Name"]
    if is_protected_by_name(name):
        continue
    if not name_matches_target(name):
        continue
    try:
        tags = s3.get_bucket_tagging(Bucket=name).get("TagSet", [])
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "NoSuchTagSet":
            tags = []
        elif code in ("NoSuchBucket", "AccessDenied"):
            # bucket in another region, or we can't read it: skip rather than guess
            continue
        else:
            record_error("S3 Bucket (tags)", name, e)
            continue
    if not eligible_for_deletion(tags):
        log_protected("S3 Bucket", name)
        continue
    log("S3 Bucket", name)
    deleted.append(("S3 Bucket", name))
    if not DRY_RUN:
        try:
            b = s3_resource.Bucket(name)
            b.object_versions.delete()
            b.objects.delete()
            b.delete()
        except ClientError as e:
            record_error("S3 Bucket", name, e)

# ─── DynamoDB Tables ──────────────────────────────────────────────────────────

print("\n── DynamoDB Tables ──────────────────────────────────────────────")
ddb = session.client("dynamodb")
paginator = ddb.get_paginator("list_tables")
for page in paginator.paginate():
    for name in page["TableNames"]:
        if not name_matches_target(name):
            continue
        try:
            table_arn = ddb.describe_table(TableName=name)["Table"]["TableArn"]
            tags = ddb.list_tags_of_resource(ResourceArn=table_arn).get("Tags", [])
        except ClientError as e:
            record_error("DynamoDB Table (tags)", name, e)
            continue
        if not eligible_for_deletion(tags):
            log_protected("DynamoDB Table", name)
            continue
        log("DynamoDB Table", name)
        deleted.append(("DynamoDB Table", name))
        if not DRY_RUN:
            try:
                ddb.delete_table(TableName=name)
            except ClientError as e:
                record_error("DynamoDB Table", name, e)

# ─── EC2 Instances ────────────────────────────────────────────────────────────

print("\n── EC2 Instances ────────────────────────────────────────────────")
ec2 = session.client("ec2")
response = ec2.describe_instances(Filters=[
    {"Name": "instance-state-name", "Values": ["pending", "running", "stopped", "stopping"]},
])
instance_ids = []
for reservation in response["Reservations"]:
    for instance in reservation["Instances"]:
        iid  = instance["InstanceId"]
        tags_list = instance.get("Tags", [])
        name = next((t["Value"] for t in tags_list if t["Key"] == "Name"), iid)
        if not name_matches_target(name):
            continue
        if not eligible_for_deletion(tags_list):
            log_protected("EC2 Instance", f"{name} ({iid})")
            continue
        log("EC2 Instance", f"{name} ({iid})")
        deleted.append(("EC2 Instance", name))
        instance_ids.append(iid)

if instance_ids and not DRY_RUN:
    try:
        ec2.terminate_instances(InstanceIds=instance_ids)
        print(f"  Waiting for {len(instance_ids)} instance(s) to terminate...")
        waiter = ec2.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=instance_ids, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        print(f"  All instances terminated.")
    except ClientError as e:
        record_error("EC2 Instances", str(instance_ids), e)

# ─── Key Pairs ────────────────────────────────────────────────────────────────

print("\n── EC2 Key Pairs ────────────────────────────────────────────────")
response = ec2.describe_key_pairs()
for kp in response["KeyPairs"]:
    name = kp["KeyName"]
    tags = kp.get("Tags", [])
    if not looks_like_workshop_resource(name, tags):
        continue
    if not name_matches_target(name):
        continue
    if not eligible_for_deletion(tags):
        log_protected("Key Pair", name)
        continue
    log("Key Pair", name)
    deleted.append(("Key Pair", name))
    if not DRY_RUN:
        try:
            ec2.delete_key_pair(KeyName=name)
        except ClientError as e:
            record_error("Key Pair", name, e)

# ─── Security Groups ─────────────────────────────────────────────────────────

print("\n── Security Groups ──────────────────────────────────────────────")
response = ec2.describe_security_groups()
for sg in response["SecurityGroups"]:
    name = sg["GroupName"]
    sgid = sg["GroupId"]
    if name == "default":
        continue  # never touch the VPC default security group
    tags = sg.get("Tags", [])
    if not looks_like_workshop_resource(name, tags):
        continue
    if not name_matches_target(name):
        continue
    if not eligible_for_deletion(tags):
        log_protected("Security Group", f"{name} ({sgid})")
        continue
    log("Security Group", f"{name} ({sgid})")
    deleted.append(("Security Group", name))
    if not DRY_RUN:
        try:
            enis = ec2.describe_network_interfaces(Filters=[
                {"Name": "group-id", "Values": [sgid]}
            ])["NetworkInterfaces"]
            for eni in enis:
                eni_id = eni["NetworkInterfaceId"]
                attachment = eni.get("Attachment", {})
                attach_id  = attachment.get("AttachmentId")
                if attach_id and attachment.get("DeviceIndex", 0) != 0:
                    try:
                        ec2.detach_network_interface(AttachmentId=attach_id, Force=True)
                    except ClientError:
                        pass
                if eni.get("Status") != "in-use":
                    try:
                        ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                    except ClientError:
                        pass

            if sg.get("IpPermissions"):
                ec2.revoke_security_group_ingress(GroupId=sgid, IpPermissions=sg["IpPermissions"])
            if sg.get("IpPermissionsEgress"):
                ec2.revoke_security_group_egress(GroupId=sgid, IpPermissions=sg["IpPermissionsEgress"])

            ec2.delete_security_group(GroupId=sgid)

        except ClientError as e:
            record_error("Security Group", name, e)

# ─── Summary ─────────────────────────────────────────────────────────────────

print("\n" + "─" * 60)
if DRY_RUN:
    print(f"DRY RUN complete: {len(deleted)} resource(s) would be deleted.")
    print("Run with --delete to actually remove them.\n")
else:
    print(f"Done: {len(deleted)} resource(s) deleted.")

if errors:
    print(f"\n{len(errors)} error(s):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
