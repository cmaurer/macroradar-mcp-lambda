"""MacroRadar MCP server for AWS Lambda (function URL).

A Python port of the official, MIT-licensed @macroradar/mcp stdio server
(https://github.com/axio-nyx/macroradar, packages/mcp) to MCP's stateless
streamable-HTTP transport, so claude.ai can reach it from the cloud.
Tool names, descriptions and schemas are taken verbatim from that package
(src/tools.json, extracted by build_template.py's companion step).

MacroRadar's API is free and keyless; the only secret is the bearer token
that guards this endpoint: Secrets Manager JSON {"bearer_token": "..."}.
Standard library + boto3 (bundled in the Lambda runtime) only.
"""

import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

API_BASE = "https://www.macroradar.io/api/v1"
PUBLIC_SITE = "https://www.macroradar.io"
SERVER_NAME = "macroradar-lambda"
SERVER_VERSION = "0.6.0-lambda.3"
SUPPORTED_PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SECRET_TTL_SECONDS = 300
CACHE_TTL_SECONDS = 900  # MacroRadar asks callers to cache; its edge caches for an hour
INSTRUCTIONS = ("Free US macro data from MacroRadar (macroradar.io). Values are historical indicators, "
                "not investment advice. Start with get_catalog to discover series ids; cite macroradar.io "
                "when using values.")

TOOLS = json.loads(TOOLS_JSON)["tools"]  # noqa: F821 - TOOLS_JSON is prepended by build_template.py
TOOL_NAMES = {t["name"] for t in TOOLS}

_secret_cache = {"value": None, "loaded_at": 0.0}
_get_cache = {}


# --------------------------------------------------------------------------
# Secret
# --------------------------------------------------------------------------
def _get_secret():
    now = time.time()
    if _secret_cache["value"] and now - _secret_cache["loaded_at"] < SECRET_TTL_SECONDS:
        return _secret_cache["value"]
    import boto3  # bundled in the Lambda runtime

    raw = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SECRET_ARN"])["SecretString"]
    _secret_cache.update(value=json.loads(raw), loaded_at=now)
    return _secret_cache["value"]


# --------------------------------------------------------------------------
# MacroRadar API
# --------------------------------------------------------------------------
class ToolError(Exception):
    pass


def _attach_claim(body, path):
    """Mirror of the package's attachClaim: forward the ledger id the API asserts."""
    meta = body.get("meta") if isinstance(body, dict) else None
    method_url = meta.get("method_url") if isinstance(meta, dict) else None
    if not isinstance(method_url, str) or "#" not in method_url:
        return
    claim_id = method_url.split("#")[1]
    if not claim_id:
        return
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    meta["claim_id"] = claim_id
    if isinstance(data.get("url"), str):
        meta["recompute_url"] = data["url"]
    elif isinstance(data.get("slug"), str) and path.startswith("/studies"):
        meta["recompute_url"] = f"{PUBLIC_SITE}/studies/{data['slug']}"
    else:
        meta["recompute_url"] = f"{PUBLIC_SITE}/api/v1{path}"


def _fetch(path, payload=None):
    cacheable = payload is None
    now = time.time()
    if cacheable and path in _get_cache and now - _get_cache[path][0] < CACHE_TTL_SECONDS:
        return _get_cache[path][1]

    headers = {"User-Agent": f"macroradar-mcp/{SERVER_VERSION}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_BASE + path, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            _attach_claim(body, path)
            if cacheable:
                _get_cache[path] = (now, body)
            return body
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            msg = (body or {}).get("error") or f"MacroRadar API returned {e.code}"
            # 451 = licensed series: not an error for the model, it is the answer.
            if e.code == 451 and isinstance(body, dict) and body.get("meta"):
                return {"licensed": True, "message": msg, **body["meta"]}
            # 4xx (incl. 422 refusals) are the product: surface verbatim, never retry.
            if e.code < 500 and e.code != 429:
                raise ToolError(msg)
            last_err = ToolError(msg)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = ToolError(f"MacroRadar request failed: {e}")
        time.sleep(1.5 * (attempt + 1))
    raise last_err


def _q(params):
    clean = {k: str(v).lower() if isinstance(v, bool) else str(v)
             for k, v in params.items() if v is not None and v != ""}
    return ("?" + urllib.parse.urlencode(clean)) if clean else ""


def _seg(value):
    return urllib.parse.quote(str(value), safe="")


def _need(args, *keys):
    for k in keys:
        if args.get(k) in (None, "", []):
            raise ToolError(f"`{k}` is required")


def _call_tool(name, a):
    if name == "get_catalog":
        return _fetch("/catalog")
    if name == "get_latest":
        _need(a, "series")
        return _fetch(f"/series/{_seg(a['series'])}/latest")
    if name == "get_series":
        _need(a, "series")
        try:
            limit = int(a.get("limit") or 120)
        except (TypeError, ValueError):
            limit = 120
        limit = max(1, min(limit, 50000))
        return _fetch(f"/series/{_seg(a['series'])}" + _q({"limit": limit, "derived": True if a.get("derived") else None}))
    if name == "get_series_as_of":
        _need(a, "series", "as_of")
        return _fetch(f"/series/{_seg(a['series'])}/as-of" + _q({"as_of": a["as_of"], "period": a.get("period")}))
    if name == "run_study":
        _need(a, "conditions", "measure", "horizon_periods", "range", "vintage")
        return _fetch("/studies", {
            "conditions": a["conditions"], "measure": a["measure"], "horizonPeriods": a["horizon_periods"],
            "range": a["range"], "dedupeWithin": a.get("dedupe_within"), "vintage": a["vintage"]})
    if name == "run_exposure":
        _need(a, "basket", "conditions", "horizon_periods", "range", "vintage")
        return _fetch("/exposure", {
            "basket": a["basket"], "conditions": a["conditions"], "horizonPeriods": a["horizon_periods"],
            "range": a["range"], "dedupeWithin": a.get("dedupe_within"), "vintage": a["vintage"]})
    if name == "get_studies":
        return _fetch("/studies" + _q({"study": a.get("study")}))
    if name == "compute":
        if not a.get("expr"):
            raise ToolError("`expr` is required, e.g. FEDFUNDS~minus~CPIAUCSL.yoy")
        return _fetch("/compute" + _q({"expr": a["expr"], "from": a.get("from"), "to": a.get("to"),
                                       "as_of": a.get("as_of")}))
    if name == "get_regime":
        return _fetch("/regime")
    if name == "get_regime_history":
        return _fetch("/regime/history")
    if name == "get_recession_probability":
        return _fetch("/recession" + _q({"history": True if a.get("history") else None}))
    if name == "get_sentiment":
        return _fetch("/sentiment" + _q({"history": True if a.get("history") else None}))
    if name == "get_whats_moving":
        return _fetch("/whats-moving")
    if name == "get_ranked":
        return _fetch("/ranked")
    if name == "get_breadth":
        return _fetch("/breadth")
    if name == "get_episodes":
        return _fetch("/episodes" + _q({"series": a.get("series")}))
    if name == "get_event_windows":
        return _fetch("/event-windows" + _q({"series": a.get("series")}))
    if name == "get_calendar":
        # from/to is a different mode from the rolling window; the range wins outright.
        if a.get("from") or a.get("to"):
            if not (a.get("from") and a.get("to")):
                raise ToolError("`from` and `to` must be passed together")
            return _fetch("/calendar" + _q({"from": a["from"], "to": a["to"]}))
        return _fetch("/calendar" + _q({"days_ahead": a.get("days_ahead"), "days_back": a.get("days_back")}))
    if name == "get_daily_log":
        if not a.get("date"):
            raise ToolError("`date` is required (YYYY-MM-DD)")
        return _fetch("/log" + _q({"date": a["date"]}))
    if name == "get_policy_regimes":
        return _fetch("/policy-regimes")
    if name == "get_claims":
        return _fetch("/claims")
    raise ToolError(f"Unknown tool: {name}")


# --------------------------------------------------------------------------
# JSON-RPC / MCP
# --------------------------------------------------------------------------
def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _source_sha256():
    import hashlib
    with open(__file__, encoding="utf-8") as f:
        return hashlib.sha256(f.read().strip().encode("utf-8")).hexdigest()


def _handle_message(msg):
    """Returns a response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            return None
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")

    method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        return _result(mid, {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [dict(t, annotations={"readOnlyHint": True, "openWorldHint": True})
                                       for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_NAMES:
            return _error(mid, -32602, f"Unknown tool: {name}")
        try:
            payload = _call_tool(name, params.get("arguments") or {})
            return _result(mid, {"content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
                                 "isError": False})
        except ToolError as e:
            return _result(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception:  # noqa: BLE001
            log.exception("tool %s failed", name)
            return _result(mid, {"content": [{"type": "text", "text": "Internal error calling MacroRadar."}],
                                 "isError": True})
    if method == "server/source_sha256":
        return _result(mid, {"sha256": _source_sha256()})
    if method in ("resources/list", "prompts/list"):
        return _result(mid, {method.split("/")[0]: []})
    if "id" not in msg:
        return None
    return _error(mid, -32601, f"Method not found: {method}")


def _http(status, body=None, extra_headers=None):
    headers = {"Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    resp = {"statusCode": status, "headers": headers}
    if body is not None:
        headers["Content-Type"] = "application/json"
        resp["body"] = json.dumps(body, separators=(",", ":"))
    return resp


def _health():
    """Unauthenticated readiness probe, also the target of the weekday warm-up schedule.

    Does the same cold-start work as a real MCP request -- module imports, importing boto3,
    creating the Secrets Manager client and loading the secret -- so the execution environment
    it runs in is ready for the scheduled task's first call. Returns no data beyond ok/not ok.
    """
    try:
        _get_secret()
    except Exception:  # noqa: BLE001
        log.exception("health: secret load failed")
        return _http(503, {"ok": False})
    return _http(200, {"ok": True})


def _authorized(headers):
    # Accept "Bearer <token>" and the common paste slip "Bearer<token>".
    supplied = headers.get("authorization", "").strip()
    if not supplied.lower().startswith("bearer"):
        return False
    expected = _get_secret().get("bearer_token", "")
    return bool(expected) and hmac.compare_digest(supplied[6:].strip().encode(), expected.encode())


def _auth_format(auth):
    """Describes the Authorization header's SHAPE only. Never returns any part of its value."""
    if not auth:
        return "missing"
    a = auth.strip()
    if a[:7].lower() == "bearer ":
        return "bearer-with-space"
    if a[:6].lower() == "bearer":
        return "bearer-no-space"
    return "other-scheme"


def _rpc_summary(raw):
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return "unparseable"
    msgs = payload if isinstance(payload, list) else [payload]
    out = []
    for m in msgs:
        if isinstance(m, dict):
            name = (m.get("params") or {}).get("name") if m.get("method") == "tools/call" else None
            out.append(f"{m.get('method')}:{name}" if name else str(m.get("method")))
    return ",".join(out)


def handler(event, context):
    """Logs one access line per request (never the token), then dispatches."""
    resp = _dispatch(event)
    try:
        http = event.get("requestContext", {}).get("http", {})
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        auth = headers.get("authorization", "")
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        log.info(json.dumps({
            "access": True, "method": http.get("method"), "path": event.get("rawPath"),
            "status": resp.get("statusCode"), "auth_format": _auth_format(auth),
            "accept": headers.get("accept"), "ua": headers.get("user-agent"),
            "rpc": _rpc_summary(raw) if raw else None, "resp_bytes": len(resp.get("body") or ""),
        }))
    except Exception:  # noqa: BLE001 - logging must never break a request
        log.exception("access log failed")
    return resp


def _dispatch(event):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path = event.get("rawPath", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if path.startswith("/.well-known/"):
        return _http(404, {"error": "not_found"})
    if path.rstrip("/") == "/health":
        return _health()
    if not _authorized(headers):
        return _http(401, {"error": "unauthorized"})
    if method == "DELETE":
        return _http(200)
    if method != "POST":
        return _http(405, None, {"Allow": "POST"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        payload = json.loads(raw)
    except ValueError:
        return _http(400, _error(None, -32700, "Parse error"))

    if isinstance(payload, list):
        responses = [r for r in (_handle_message(m) for m in payload) if r is not None]
        return _http(200, responses) if responses else _http(202)
    response = _handle_message(payload)
    return _http(200, response) if response is not None else _http(202)
