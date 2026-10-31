"""Керування хмарними прогонами co2-vent (API Gateway HTTP API -> Lambda).

GET  /        сторінка керування (без секретів; вхід через Cognito Hosted UI, PKCE)
POST /run     {"scale": 10|1, "faults": bool} - запуск ECS Fargate задачі   } лише з JWT
GET  /status  поточний прогін, адреса панелі, останні результати в S3       } Cognito
POST /stop    зупинити поточний прогін                                      }
GET  /panel/dashboard (+ /panel/api/…) вебпанель прогону через цей же HTTPS-домен (через proxy.py у VPC):
              лише з IP, з якого прогін запущено; IP контейнера назовні не видно
Захист кредитів: одночасно лише один прогін; контейнер має власний ліміт часу (MAX_RUN_S).
Панель контейнера відповідає лише з ключем прогону (X-Panel-Key), який знає тільки ця функція.
"""
import base64
import json
import os
import secrets
import time
from pathlib import Path

import boto3
from botocore.config import Config

ENV = os.environ
REGION = ENV.get("AWS_REGION", "eu-north-1")
ecs = boto3.client("ecs")
lam = boto3.client("lambda")
# посилання підписуються для регіонального хоста бакета: глобальний s3.amazonaws.com
# перенаправляє на s3.<region>, і тоді підпис (заголовок host) не збігається - SignatureDoesNotMatch
s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
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


def task_env(task):
    return {e["name"]: e["value"] for o in task.get("overrides", {}).get("containerOverrides", [])
            for e in o.get("environment", [])}


def task_info(task):
    env = task_env(task)
    running = task["lastStatus"] == "RUNNING"
    return {"run_id": env.get("RUN_ID"), "scale": env.get("PZIR_TIME_SCALE"), "faults": bool(env.get("FAULTS")),
            "status": task["lastStatus"], "started_at": task.get("startedAt") or task.get("createdAt"),
            "panel": "panel/dashboard" if running else None}


def proxy_panel(event, ip):
    """GET /panel/<path> -> http://<IP задачі>:8000/<path> з ключем прогону."""
    page = lambda status, text: reply(status, f"<p style='font:15px system-ui;margin:40px'>{text}</p>", "text/html")
    task = next((t for t in active_tasks() if t["lastStatus"] == "RUNNING"), None)
    if not task:
        return page(404, "Зараз немає запущеного прогону. <a href='../'>Назад</a>")
    env = task_env(task)
    if env.get("PANEL_IP") != ip:
        return page(403, "Вебпанель доступна лише з адреси, з якої запущено прогін.")
    target = next((d["value"] for att in task.get("attachments", []) for d in att.get("details", [])
                   if d["name"] == "privateIPv4Address"), None)
    if not target:
        return page(503, "Контейнер ще отримує адресу, спробуйте за кілька секунд.")
    path = event.get("pathParameters", {}).get("proxy", "")
    path = "" if path == "dashboard" else path       # panel/dashboard -> "/": відносні api/... лишаються під panel/
    out = json.loads(lam.invoke(FunctionName=ENV["PROXY_FUNCTION"], Payload=json.dumps(
        {"ip": target, "path": path, "query": event.get("rawQueryString", ""),
         "key": env.get("PZIR_PANEL_KEY", "")}))["Payload"].read())
    if "body" not in out:
        return page(502, "Панель контейнера ще не відповідає, спробуйте за кілька секунд.")
    status, body, ctype = out["status"], out["body"], out["ctype"]
    return {"statusCode": status, "headers": {"Content-Type": ctype, "Cache-Control": "no-store"},
            "body": body}


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
    if route == "GET /panel/{proxy+}":
        return proxy_panel(event, ip)
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
               {"name": "STARTED_BY", "value": user}, {"name": "PANEL_IP", "value": ip},
               {"name": "PZIR_PANEL_KEY", "value": secrets.token_urlsafe(24)}]
        if body.get("faults"):
            env.append({"name": "FAULTS", "value": "1"})
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
