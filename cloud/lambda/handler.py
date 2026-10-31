"""Керування хмарними прогонами co2-vent (API Gateway HTTP API -> Lambda).

GET  /        сторінка керування (без секретів; вхід через Cognito Hosted UI, PKCE)
POST /run     {"scale": 10|1, "faults": bool} - запуск ECS Fargate задачі   } лише з JWT
GET  /status  поточний прогін, адреса панелі, останні результати в S3       } Cognito
POST /stop    зупинити поточний прогін                                      }
Захист кредитів: одночасно лише один прогін; порт панелі 8000 відкривається тільки
для IP того, хто запустив; контейнер має власний ліміт часу (MAX_RUN_S).
"""
import base64
import json
import os
import time
from pathlib import Path

import boto3

ecs = boto3.client("ecs")
ec2 = boto3.client("ec2")
s3 = boto3.client("s3")
ENV = os.environ
PAGE = (Path(__file__).with_name("page.html").read_text(encoding="utf-8")
        .replace("__COGNITO_DOMAIN__", ENV.get("COGNITO_DOMAIN", ""))
        .replace("__CLIENT_ID__", ENV.get("CLIENT_ID", "")))


def reply(status, body, ctype="application/json"):
    data = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
    return {"statusCode": status, "headers": {"Content-Type": f"{ctype}; charset=utf-8",
                                              "Cache-Control": "no-store"}, "body": data}


def active_tasks():
    arns = ecs.list_tasks(cluster=ENV["CLUSTER"], family=ENV["TASK_FAMILY"], desiredStatus="RUNNING")["taskArns"]
    return ecs.describe_tasks(cluster=ENV["CLUSTER"], tasks=arns)["tasks"] if arns else []


def open_panel_for(ip):
    """Порт 8000 лише для однієї адреси: попередні правила знімаються."""
    sg = ec2.describe_security_groups(GroupIds=[ENV["SECURITY_GROUP"]])["SecurityGroups"][0]
    if sg["IpPermissions"]:
        ec2.revoke_security_group_ingress(GroupId=sg["GroupId"], IpPermissions=sg["IpPermissions"])
    rng = {"Ipv6Ranges": [{"CidrIpv6": f"{ip}/128"}]} if ":" in ip else {"IpRanges": [{"CidrIp": f"{ip}/32"}]}
    ec2.authorize_security_group_ingress(GroupId=sg["GroupId"], IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, **rng}])


def task_info(task):
    env = {e["name"]: e["value"] for o in task.get("overrides", {}).get("containerOverrides", [])
           for e in o.get("environment", [])}
    ip = None
    for att in task.get("attachments", []):
        eni = next((d["value"] for d in att.get("details", []) if d["name"] == "networkInterfaceId"), None)
        if eni:
            nic = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])["NetworkInterfaces"]
            ip = nic[0].get("Association", {}).get("PublicIp") if nic else None
    return {"run_id": env.get("RUN_ID"), "scale": env.get("PZIR_TIME_SCALE"), "faults": bool(env.get("FAULTS")),
            "status": task["lastStatus"], "started_at": task.get("startedAt") or task.get("createdAt"),
            "panel": f"http://{ip}:8000/" if ip and task["lastStatus"] == "RUNNING" else None}


def recent_results(limit=5):
    out = s3.list_objects_v2(Bucket=ENV["BUCKET"], Prefix="runs/", Delimiter="/")
    runs = sorted((p["Prefix"] for p in out.get("CommonPrefixes", [])), reverse=True)[:limit]
    result = []
    for prefix in runs:
        objs = s3.list_objects_v2(Bucket=ENV["BUCKET"], Prefix=prefix).get("Contents", [])
        links = {}
        for o in objs:
            name = o["Key"].rsplit("/", 1)[-1]
            if name in ("SUMMARY.md", "processor.log", "esp.log", "uno_sim.log"):
                links[name] = s3.generate_presigned_url(
                    "get_object", Params={"Bucket": ENV["BUCKET"], "Key": o["Key"]}, ExpiresIn=3600)
        result.append({"run": prefix.split("/")[1], "files": len(objs), "links": links})
    return result


def handler(event, context):
    route = event.get("routeKey", "")
    if route == "GET /":
        return reply(200, PAGE, "text/html")
    ip = event["requestContext"]["http"]["sourceIp"]
    user = (event["requestContext"].get("authorizer", {}).get("jwt", {}).get("claims", {}) or {}).get("username", "?")

    if route == "GET /status":
        return reply(200, {"active": [task_info(t) for t in active_tasks()], "results": recent_results(),
                           "your_ip": ip})

    if route == "POST /run":
        raw = event.get("body") or "{}"
        body = json.loads(base64.b64decode(raw) if event.get("isBase64Encoded") else raw)
        if active_tasks():
            return reply(409, {"error": "прогін уже йде - дочекайтеся завершення або зупиніть його"})
        scale = "1" if str(body.get("scale")) == "1" else "10"
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        env = [{"name": "PZIR_TIME_SCALE", "value": scale}, {"name": "RUN_ID", "value": run_id},
               {"name": "STARTED_BY", "value": user}]
        if body.get("faults"):
            env.append({"name": "FAULTS", "value": "1"})
        open_panel_for(ip)
        resp = ecs.run_task(
            cluster=ENV["CLUSTER"], taskDefinition=ENV["TASK_FAMILY"], launchType="FARGATE", count=1,
            startedBy="co2vent-web",
            networkConfiguration={"awsvpcConfiguration": {
                "subnets": ENV["SUBNETS"].split(","), "securityGroups": [ENV["SECURITY_GROUP"]],
                "assignPublicIp": "ENABLED"}},
            overrides={"containerOverrides": [{"name": "app", "environment": env}]})
        if resp.get("failures"):
            return reply(500, {"error": resp["failures"]})
        return reply(202, {"run_id": run_id, "scale": scale, "faults": bool(body.get("faults")),
                           "panel_allowed_ip": ip})

    if route == "POST /stop":
        stopped = []
        for t in active_tasks():
            ecs.stop_task(cluster=ENV["CLUSTER"], task=t["taskArn"], reason="stopped from web")
            stopped.append(t["taskArn"].rsplit("/", 1)[-1])
        return reply(200, {"stopped": stopped})

    return reply(404, {"error": "route not found"})
