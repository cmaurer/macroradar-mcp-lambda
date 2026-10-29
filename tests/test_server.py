"""Offline tests for the bundled MacroRadar MCP server (MacroRadar API and the secret are mocked).

Run: python3 build_template.py && python3 tests/test_server.py
"""
import importlib.util
import io
import json
import pathlib
import time
import urllib.error

root = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("index", root / "build" / "index.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
app._secret_cache.update(value={"bearer_token": "tok123"}, loaded_at=time.time() + 1e9)
app.time.sleep = lambda s: None

calls = []


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(json.dumps(body).encode()))


def fake_urlopen(req, timeout):
    url, method = req.full_url, req.get_method()
    body = json.loads(req.data) if req.data else None
    calls.append((method, url, body))
    path = url.replace(app.API_BASE, "")
    if path.startswith("/series/VIXCLS/latest"):
        raise http_error(url, 451, {"error": "VIXCLS is licensed", "meta": {"license_note": "not redistributable",
                                                                           "chart_url": "https://x/c"}})
    if path.startswith("/compute?expr=BAD"):
        raise http_error(url, 422, {"error": "refused: unaddressable expression BAD", "meta": {}})
    if path.startswith("/whats-moving") and not getattr(fake_urlopen, "flaky_done", False):
        fake_urlopen.flaky_done = True
        raise http_error(url, 503, {"error": "busy"})
    if path.startswith("/studies") and method == "POST":
        return Resp(json.dumps({"data": {"n": 3}, "meta": {"method_url": "https://m/claims#ad-hoc-study"}}).encode())
    if path.startswith("/compute"):
        return Resp(json.dumps({"data": {"url": "https://www.macroradar.io/compute/X", "points": []},
                                "meta": {"method_url": "https://m/claims#compute"}}).encode())
    return Resp(json.dumps({"data": {"path": path}, "meta": {"source": "MacroRadar"}}).encode())


app.urllib.request.urlopen = fake_urlopen


def ev(body, auth="Bearer tok123", method="POST", path="/"):
    h = {"Content-Type": "application/json"}
    if auth:
        h["Authorization"] = auth
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "headers": h,
            "body": json.dumps(body) if body is not None else None}


def call(name, args=None, mid=1):
    r = app.handler(ev({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                        "params": {"name": name, "arguments": args or {}}}), None)
    res = json.loads(r["body"])["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


# transport and auth
assert app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, auth=None), None)["statusCode"] == 401
assert app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, auth="Bearer nope"), None)["statusCode"] == 401
assert app.handler(ev(None, method="GET", path="/.well-known/oauth-protected-resource"), None)["statusCode"] == 404
assert app.handler(ev(None, method="GET"), None)["statusCode"] == 405
init = json.loads(app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}}), None)["body"])["result"]
assert init["protocolVersion"] == "2025-06-18" and "macroradar.io" in init["instructions"]
assert app.handler(ev({"jsonrpc": "2.0", "method": "notifications/initialized"}), None)["statusCode"] == 202

# tool list is the official package's, verbatim
tools = json.loads(app.handler(ev({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), None)["body"])["result"]["tools"]
official = json.loads((root / "src" / "tools.json").read_text())["tools"]
assert len(tools) == 21 == len(official)
for mine, theirs in zip(tools, official):
    assert {k: v for k, v in mine.items() if k != "annotations"} == theirs, mine["name"]
assert "$defs" in next(t for t in tools if t["name"] == "run_study")["inputSchema"]

# URL building
call("get_series", {"series": "unemployment-rate", "derived": True})
assert calls[-1][1].endswith("/series/unemployment-rate?limit=120&derived=true"), calls[-1]
call("get_series", {"series": "UNRATE", "limit": 999999})
assert "limit=50000" in calls[-1][1]
call("get_series_as_of", {"series": "CPIAUCSL", "as_of": "2008-09-15", "period": "2008-06-01"})
assert calls[-1][1].endswith("/series/CPIAUCSL/as-of?as_of=2008-09-15&period=2008-06-01")
call("get_recession_probability", {"history": True})
assert calls[-1][1].endswith("/recession?history=true")
call("get_sentiment")
assert calls[-1][1].endswith("/sentiment")
call("get_calendar", {"from": "2024-01-01", "to": "2024-03-01", "days_ahead": 5})
assert calls[-1][1].endswith("/calendar?from=2024-01-01&to=2024-03-01")
err, msg = call("get_calendar", {"from": "2024-01-01"})
assert err and "together" in msg
call("get_episodes", {"series": "T10Y2Y"})
assert calls[-1][1].endswith("/episodes?series=T10Y2Y")
call("get_daily_log", {"date": "2020-03-15"})
assert calls[-1][1].endswith("/log?date=2020-03-15")

# compute: expression passed through, claim attached from data.url; 422 verbatim
_, out = call("compute", {"expr": "FEDFUNDS~minus~CPIAUCSL.yoy", "from": "2020-01-01"})
assert "expr=FEDFUNDS~minus~CPIAUCSL.yoy" in calls[-1][1].replace("%7E", "~")
assert out["meta"]["claim_id"] == "compute" and out["meta"]["recompute_url"].endswith("/compute/X")
err, msg = call("compute", {"expr": "BAD"})
assert err and msg == "refused: unaddressable expression BAD"

# run_study: snake_case in, camelCase out, POST, claim fallback URL
study = {"conditions": [{"kind": "threshold", "expr": {"kind": "series", "seriesId": "T10Y2Y"},
                         "comparator": "below", "value": 0}],
         "measure": {"kind": "series", "seriesId": "UNRATE", "transforms": [{"op": "diff", "periods": 12}]},
         "horizon_periods": 12, "range": {"start": "1980-01-01", "end": "2026-01-01"},
         "dedupe_within": 6, "vintage": {"mode": "latest"}}
_, out = call("run_study", study)
method, url, body = calls[-1]
assert method == "POST" and url.endswith("/studies")
assert body["horizonPeriods"] == 12 and body["dedupeWithin"] == 6 and "horizon_periods" not in body
assert out["meta"]["recompute_url"] == "https://www.macroradar.io/api/v1/studies"
err, msg = call("run_study", {k: v for k, v in study.items() if k != "vintage"})
assert err and "vintage" in msg

# licensed series: 451 is an answer, not an error
err, out = call("get_latest", {"series": "VIXCLS"})
assert not err and out["licensed"] is True and out["chart_url"] == "https://x/c"

# retry on 503, then cache hit on the second call
n = len(calls)
assert not call("get_whats_moving")[0]
assert len(calls) == n + 2
call("get_whats_moving")
assert len(calls) == n + 2, "second call should come from cache"

# unknown tool / method; deployment hash method
assert json.loads(app.handler(ev({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                  "params": {"name": "nope"}}), None)["body"])["error"]["code"] == -32602
assert json.loads(app.handler(ev({"jsonrpc": "2.0", "id": 9, "method": "bogus"}), None)["body"])["error"]["code"] == -32601
# "Bearer<token>" (no space) is accepted; the access log never contains any part of the header value
import logging
captured = []
handler_ = logging.Handler()
handler_.emit = lambda record: captured.append(record.getMessage())
app.log.addHandler(handler_)
for hdr, expect in (("Bearertok123", 200), ("Bearer tok123", 200), ("  bearer   tok123 ", 200),
                    ("Bearertok12", 401), ("Basic tok123", 401), ("tok123", 401)):
    r = app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "ping"}, auth=hdr), None)
    assert r["statusCode"] == expect, (hdr, r["statusCode"])
app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "ping"}, auth=None), None)
assert captured and all("tok12" not in line for line in captured), captured
formats = [json.loads(line)["auth_format"] for line in captured if line.startswith("{")]
assert formats == ["bearer-no-space", "bearer-with-space", "bearer-with-space", "bearer-no-space",
                   "other-scheme", "other-scheme", "missing"], formats
app.log.removeHandler(handler_)

sha = json.loads(app.handler(ev({"jsonrpc": "2.0", "id": 9, "method": "server/source_sha256"}), None)["body"])["result"]["sha256"]
print("bundle sha256", sha)
print("ALL OK")
