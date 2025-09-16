import os, boto3, datetime as dt

EXECUTION_ROLE_NAME = os.environ["EXECUTION_ROLE_NAME"]
TABLE_NAME = os.environ["TABLE_NAME"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
sts = boto3.client("sts")

def assume(account_id, session_name="iam-ident"):
    resp = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{EXECUTION_ROLE_NAME}",
        RoleSessionName=session_name,
        DurationSeconds=3600
    )
    creds = resp["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"]
    )

def list_stack_roles(session):
    cfn = session.client("cloudformation")
    iam = session.client("iam")
    roles_by_stack = []
    paginator = cfn.get_paginator("list_stacks")
    for page in paginator.paginate(StackStatusFilter=[
        "CREATE_COMPLETE","UPDATE_COMPLETE","UPDATE_ROLLBACK_COMPLETE","IMPORT_COMPLETE","IMPORT_ROLLBACK_COMPLETE"
    ]):
        for s in page.get("StackSummaries", []):
            stack_name = s["StackName"]
            try:
                r = cfn.list_stack_resources(StackName=stack_name)
            except Exception:
                continue
            role_phys_ids = [x["PhysicalResourceId"] for x in r.get("StackResourceSummaries", [])
                             if x["ResourceType"] == "AWS::IAM::Role" and "PhysicalResourceId" in x]
            if not role_phys_ids:
                continue
            roles = []
            for pid in role_phys_ids:
                role_name = pid.split("/")[-1]
                try:
                    rd = iam.get_role(RoleName=role_name)["Role"]
                except Exception:
                    continue
                # Filter service-linked roles if ever present
                if rd.get("Path") == "/aws-service-role/":
                    continue
                roles.append({
                    "RoleName": role_name,
                    "RoleArn": rd["Arn"],
                    "CreateDate": rd["CreateDate"].isoformat(),
                    "Path": rd.get("Path"),
                    "Tags": rd.get("Tags", [])
                })
            if roles:
                roles_by_stack.append({
                    "StackName": stack_name,
                    "Roles": roles
                })
    return roles_by_stack

def put_role_items(account_id, items):
    with table.batch_writer() as bw:
        for stack in items:
            stack_name = stack["StackName"]
            for r in stack["Roles"]:
                pk = f"{account_id}#global#{stack_name}"
                sk = f"role#{r['RoleName']}"
                bw.put_item(Item={
                    "Pk": pk,
                    "Sk": sk,
                    "Gsi1Pk": f"stack#{stack_name}",
                    "Gsi1Sk": f"{account_id}#role#{r['RoleName']}",

                    "AccountId": account_id,
                    "Region": "global",
                    "StackName": stack_name,
                    "RoleName": r["RoleName"],
                    "RoleArn": r["RoleArn"],
                    "CreateDate": r["CreateDate"],
                    "Tags": r.get("Tags", []),
                    "Used": "unknown",
                    "AssumeCountRecent": 0,
                    "Source": "AccessAnalyzer",
                    "UpdatedAt": dt.datetime.utcnow().isoformat()
                })

def lambda_handler(event, _context):
    accounts = event.get("accounts") or []
    results = []
    for acct in accounts:
        sess = assume(acct)
        stacks = list_stack_roles(sess)
        put_role_items(acct, stacks)
        results.append({"AccountId": acct, "StacksWithRoles": len(stacks), "RolesSeen": sum(len(s["Roles"]) for s in stacks)})
    return {"ok": True, "results": results}
