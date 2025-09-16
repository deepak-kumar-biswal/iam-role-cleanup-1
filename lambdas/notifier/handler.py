import os, json, boto3, urllib.request
param = os.environ.get("SLACK_WEBHOOK_PARAM")
ssm = boto3.client("ssm")

def send(msg):
    if not param: return
    url = ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
    data = json.dumps({"text": msg}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
    urllib.request.urlopen(req).read()

def lambda_handler(event, _context):
    txt = json.dumps(event, default=str)
    send(f":mag: IAM Role Identification – run summary\n```{txt[:3900]}```")
    return {"ok": True}
