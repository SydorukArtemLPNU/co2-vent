"""Проксі вебпанелі прогону (Lambda у VPC, без доступу до інтернету й AWS API).

Викликає лише co2vent-control: {"ip": <приватна IP задачі>, "path", "query", "key"} ->
GET http://<ip>:8000/<path>?<query> з X-Panel-Key. Порт 8000 задачі відкритий тільки для
групи безпеки цієї функції, тож панель недосяжна з інтернету.
"""
import urllib.error
import urllib.request


def handler(event, context):
    query = event.get("query") or ""
    req = urllib.request.Request(f"http://{event['ip']}:8000/{event.get('path', '')}" + (f"?{query}" if query else ""),
                                 headers={"X-Panel-Key": event.get("key", "")})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status, body, ctype = r.status, r.read(), r.headers.get("Content-Type", "text/plain")
    except urllib.error.HTTPError as exc:
        status, body, ctype = exc.code, exc.read(), exc.headers.get("Content-Type", "text/plain")
    except OSError as exc:
        return {"status": 502, "error": str(exc)}
    return {"status": status, "ctype": ctype, "body": body.decode("utf-8", "replace")}
