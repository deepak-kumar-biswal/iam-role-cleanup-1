import os, json, boto3, datetime as dt
from boto3.dynamodb.conditions import Key

TABLE_NAME  = os.environ["TABLE_NAME"]
FEED_BUCKET = os.environ["FEED_BUCKET"]
FEED_PREFIX = os.environ["FEED_PREFIX"].rstrip("/")

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")
table = ddb.Table(TABLE_NAME)

def s3_get_json(key):
    obj = s3.get_object(Bucket=FEED_BUCKET, Key=key)
    return json.loads(obj["Body"].read())

def load_unused_map_for_accounts(account_ids):
    unused_by_acct = {acct: set() for acct in account_ids}
    found_any = False

    # per-account files
    for acct in account_ids:
        key = f"{FEED_PREFIX}/{acct}/unused-roles.json"
        try:
            data = s3_get_json(key)
            for r in data.get("roles", []):
                arn = r.get("roleArn")
                if arn:
                    unused_by_acct[acct].add(arn)
            found_any = True
        except s3.exceptions.NoSuchKey:
            pass
        except Exception:
            pass

    if found_any:
        return unused_by_acct

    # consolidated
    key = f"{FEED_PREFIX}/unused-roles.json"
    data = s3_get_json(key)
    for acct_block in data.get("accounts", []):
        acct = acct_block.get("accountId")
        if acct in unused_by_acct:
            for r in acct_block.get("roles", []):
                arn = r.get("roleArn")
                if arn:
                    unused_by_acct[acct].add(arn)
    return unused_by_acct

def query_stack_roles(account_id, stack_name):
    pk = f"{account_id}#global#{stack_name}"
    resp = table.query(KeyConditionExpression=Key("Pk").eq(pk) & Key("Sk").begins_with("role#"))
    return resp.get("Items", [])

def update_role_usage(item, is_unused: bool, source="AccessAnalyzer"):
    used_value = "unused" if is_unused else "used"
    table.update_item(
        Key={"Pk": item["Pk"], "Sk": item["Sk"]},
        UpdateExpression="SET Used=:u, AssumeCountRecent=:c, Source=:s, UpdatedAt=:t",
        ExpressionAttributeValues={
            ":u": used_value,
            ":c": 0 if is_unused else 1,
            ":s": source,
            ":t": dt.datetime.utcnow().isoformat()
        }
    )

def summarize_stack(account_id, stack_name):
    items = query_stack_roles(account_id, stack_name)
    used   = sum(1 for i in items if i.get("Used") == "used")
    unused = sum(1 for i in items if i.get("Used") == "unused")
    unknown= sum(1 for i in items if i.get("Used") == "unknown")
    if unused > 0 and used == 0 and unknown == 0:
        state = "all-unused"
    elif used > 0 and unused > 0:
        state = "mixed"
    elif used > 0 and unused == 0 and unknown == 0:
        state = "all-used"
    else:
        state = "pending"
    table.put_item(Item={
        "Pk": f"{account_id}#global#{stack_name}",
        "Sk": "summary#stack",
        "Gsi1Pk": "summary#stack",
        "Gsi1Sk": f"{account_id}#{stack_name}",
        "AccountId": account_id,
        "StackName": stack_name,
        "Summary": { "Used": used, "Unused": unused, "Unknown": unknown, "State": state },
        "UpdatedAt": dt.datetime.utcnow().isoformat()
    })
    return {"AccountId": account_id, "StackName": stack_name, "State": state, "Used": used, "Unused": unused, "Unknown": unknown}

def lambda_handler(event, _context):
    accounts = event.get("accounts") or []

    # Build a map of unused role ARNs per account from S3 feed
    feed_map = load_unused_map_for_accounts(accounts)

    # Minimal scan – acceptable for first pass. You can optimize with Export/Streams later.
    scan = table.scan(ProjectionExpression="Pk, Sk, AccountId, StackName, RoleArn")
    items = scan.get("Items", [])

    # Group by (acct, stack)
    stacks = {}
    for it in items:
        acct = it.get("AccountId")
        if acct not in accounts: 
            continue
        if it["Sk"].startswith("role#"):
            stacks.setdefault((acct, it["StackName"]), []).append(it)

    summaries = []
    for (acct, stack), role_rows in stacks.items():
        unused_set = feed_map.get(acct, set())
        for role_row in role_rows:
            is_unused = role_row.get("RoleArn") in unused_set
            update_role_usage(role_row, is_unused)
        summaries.append(summarize_stack(acct, stack))

    return {"ok": True, "summaries": summaries}
