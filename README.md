# IAM Cleanup – Part 1 (Identification via Access Analyzer)

This repo deploys a **CloudFormation-only** identification pipeline that:
- discovers IAM roles created by **CloudFormation stacks** across **multiple accounts**,
- determines **used vs unused** **solely from your Access Analyzer feed** in S3,
- writes per-**role** and per-**stack** results to **DynamoDB**, and
- summarizes each stack as **all-unused**, **mixed**, **all-used**, or **pending**.

> This is **Part 1 (Identification)** only. Part 2 (Quarantine/Deletion) can plug into the same data model later.

---

## Contents

```
iam-cleanup-ident-aa-only/
├─ orchestrator.yaml                 # Central CFN stack – DynamoDB, Step Functions, Lambdas, IAM
├─ target-execution-role.yaml        # Cross-account role for each target account
├─ lambdas/
│  ├─ inventory_collector/handler.py # Lists CFN stacks and their IAM::Role resources into DynamoDB
│  ├─ usage_checker/handler.py       # Marks role Used/Unused based on Access Analyzer feed in S3
│  ├─ writer/handler.py              # (No-op placeholder for pipeline structure)
│  └─ notifier/handler.py            # Optional Slack/webhook notifier
└─ README.md
```

---

## Prerequisites

1. **Access Analyzer feed** in S3:
   - **Per-account files** (preferred):
     - `s3://<FEED_BUCKET>/<FEED_PREFIX>/<ACCOUNT_ID>/unused-roles.json`
     - Example payload:
       ```json
       {
         "accountId": "111111111111",
         "generatedAt": "2025-09-10T12:00:00Z",
         "roles": [{ "roleArn": "arn:aws:iam::111111111111:role/AppRole1", "source": "AccessAnalyzer", "reason": "UnusedIAMRole", "lastSeen": null }]
       }
       ```
   - **OR** a single consolidated file:
     - `s3://<FEED_BUCKET>/<FEED_PREFIX>/unused-roles.json`
     - Example payload:
       ```json
       {
         "generatedAt": "2025-09-10T12:00:00Z",
         "accounts": [{"accountId":"111111111111","roles":[{"roleArn":"arn:aws:iam::111111111111:role/AppRole1"}]}]
       }
       ```

2. **CloudTrail/AssumeRole data is NOT required** (we rely only on Access Analyzer).

3. **Central (orchestrator) account** with permissions to deploy the stack and read the S3 feed.

4. **Target accounts** each need a cross-account execution role (template provided below).

---

## Deploy Step-by-Step

### 1) Create the cross-account role in **every target account**
Deploy `target-execution-role.yaml` in each target account (or via StackSets).

Parameters:
- `OrchestratorAccountId` = your **central account ID**
- `RoleName` = (default) `IAMCleanupExecutionRole` (keep default unless you want to rename)

This role allows the central Lambdas to list CFN resources and IAM roles.

### 2) Zip and upload Lambda packages

Create 4 zips and upload to an S3 **code** bucket you control (name of your choice):
- `inventory_collector.zip` – contains `handler.py` from `lambdas/inventory_collector/`
- `usage_checker.zip` – contains `handler.py` from `lambdas/usage_checker/`
- `writer.zip` – contains `handler.py` from `lambdas/writer/`
- `notifier.zip` – contains `handler.py` from `lambdas/notifier/`

> You can simply zip each folder’s contents (no subfolder inside the zip). The handler path is `handler.lambda_handler`.

### 3) Deploy the **orchestrator** stack (central account)

Use the CFN console or CLI to deploy `orchestrator.yaml` with parameters:

- `TargetAccountIds` = comma-separated account IDs, e.g. `111111111111,222222222222`
- `ExecutionRoleName` = `IAMCleanupExecutionRole` (or the value you chose in step 1)
- `FeedBucket` = S3 bucket that contains your Access Analyzer feed
- `FeedPrefix` = S3 prefix (folder) for the feed (no leading slash)
- `LambdaCodeBucket` = S3 bucket that holds the 4 lambda **code zips**
- `InventoryCollectorKey` = `inventory_collector.zip` (or your key/path)
- `UsageCheckerKey` = `usage_checker.zip`
- `WriterKey` = `writer.zip`
- `NotifierKey` = `notifier.zip`
- `SlackWebhookSSMParam` (optional) = SSM SecureString param that stores your webhook URL

### 4) Run the state machine

Start the state machine using the console or CLI with input like:
```json
{
  "run_id": "ident-aa-only",
  "accounts": ["111111111111","222222222222"]
}
```

The pipeline does:
1. **Inventory**: find CFN stacks and their IAM::Role resources; write **per-role rows** to DynamoDB with `Used=unknown`.
2. **UsageCheck**: read Access Analyzer feed from S3; mark each role as `used` or `unused` and write a **per-stack summary** row.
3. **WriteSummaries**: (placeholder – summaries already written by UsageChecker).
4. **Notify**: optional Slack/webhook summary.

---

## DynamoDB Data Model

**Table name**: `IamStackRoleUsage-<orchestrator-stack-name>`

- **PK** (`Pk`) = `<AccountId>#global#<StackName>`
- **SK** (`Sk`):
  - `role#<RoleName>` for per-role rows
  - `summary#stack` for per-stack summary row
- **GSI1** (for listing stack summaries):
  - `Gsi1Pk` = `summary#stack`
  - `Gsi1Sk` = `<AccountId>#<StackName>`

**Per-role row example**
```json
{
  "Pk": "111111111111#global#AppStack",
  "Sk": "role#AppRole",
  "Gsi1Pk": "stack#AppStack",
  "Gsi1Sk": "111111111111#role#AppRole",
  "AccountId": "111111111111",
  "Region": "global",
  "StackName": "AppStack",
  "RoleName": "AppRole",
  "RoleArn": "arn:aws:iam::111111111111:role/AppRole",
  "Used": "unused | used | unknown",
  "AssumeCountRecent": 0,  // Indicator only; not from CloudTrail
  "Source": "AccessAnalyzer",
  "UpdatedAt": "2025-09-16T00:00:00Z"
}
```

**Per-stack summary row**
```json
{
  "Pk": "111111111111#global#AppStack",
  "Sk": "summary#stack",
  "Gsi1Pk": "summary#stack",
  "Gsi1Sk": "111111111111#AppStack",
  "AccountId": "111111111111",
  "StackName": "AppStack",
  "Summary": {"Used": 1, "Unused": 2, "Unknown": 0, "State": "mixed"},
  "UpdatedAt": "2025-09-16T00:00:00Z"
}
```

**Summary.State** values:
- `all-unused` → whole-stack deletion candidate for Part 2
- `mixed`      → surgical removal candidate for Part 2
- `all-used`   → skip
- `pending`    → awaiting first usage pass (or new stacks detected)

---

## Notes & Customization

- To exclude **service-linked roles**, add a filter in `inventory_collector/handler.py` for path `"/aws-service-role/"`.
- If your Access Analyzer feed carries a `lastSeen` field and you want extra safety, modify `usage_checker` to only mark as unused when `lastSeen` is `null` or older than a threshold.
- This pipeline is read-only across target accounts (no mutating IAM/CFN).

---

## License

MIT (do whatever, attribution appreciated).
