# macroradar-mcp-lambda

A remote MCP server for [MacroRadar](https://www.macroradar.io/developers)'s free US macro data API, running on AWS Lambda behind a public function URL so claude.ai — including scheduled tasks in Anthropic's cloud — can use it without your computer being on.

It is a Python port of MacroRadar's official, MIT-licensed [`@macroradar/mcp`](https://www.npmjs.com/package/@macroradar/mcp) stdio server (v0.6.0). The 21 tool names, descriptions and input schemas are taken verbatim from that package (`src/tools.json`); behaviour matches it, including:

- **Licensed series (HTTP 451)** — ten of MacroRadar's series (VIX, S&P 500, HY OAS, CAPE, …) can't have raw values redistributed. Those calls return `{"licensed": true, ...}` with the license note and chart link — an answer, not an error.
- **Refusals (HTTP 422)** — when the compute/study grammar refuses a request, the API's message is returned verbatim, never retried or softened.
- **Claim links** — where the API names a claims-ledger entry, `meta.claim_id` and `meta.recompute_url` are added, as the package does.

Data attribution: MacroRadar (macroradar.io). Model outputs are historical indicators, not investment advice.

## What's deployed

| Item | Value |
|---|---|
| AWS account / region | 381491912865 / us-east-1 |
| CloudFormation stack | `macroradar-mcp` |
| Function | `macroradar-mcp` (Python 3.13, arm64, 512 MB, 90 s) |
| Endpoint | `https://zfm3jdiqusemt3zuwtia2ywh3u0uvxph.lambda-url.us-east-1.on.aws/` (stack output `FunctionUrl`) |
| Secret | `macroradar-mcp/config` — `bearer_token` only (the API itself is keyless) |
| Logs | `/aws/lambda/macroradar-mcp` (30-day retention) |
| Warm-up | EventBridge Scheduler `macroradar-mcp-warmup`: `/health` invoke every 3 min, 6:30–6:45 America/Chicago, Mon–Fri |
| claude.ai connector | `MacroRadar` (custom connector, "No sign in", `Authorization: Bearer <token>` header) |

## How it works

- **Transport:** MCP streamable HTTP, stateless: one POST per JSON-RPC message, plain JSON back.
- **Dependencies:** Python standard library + boto3 (in the Lambda runtime). No build artefacts to upload; the code is embedded in the CloudFormation template.
- **Auth:** `Authorization: Bearer <token>` required on every request (401 otherwise). `Bearer<token>` without the space is also accepted, since that paste slip is easy to make in the claude.ai connector form. The token lives in the secret; rotate it as described below.
- **Access log:** one JSON line per request in CloudWatch — method, path, status, MCP method/tool, response size, user agent, and the Authorization header's *shape* only (`missing`, `bearer-with-space`, `bearer-no-space`, `other-scheme`). No part of the header value is ever logged; `tests/test_server.py` asserts this.
- **Caching:** GET responses are cached in memory for 15 minutes per warm Lambda instance (MacroRadar asks callers to cache; its edge caches for an hour). POSTs (`run_study`, `run_exposure`) are not cached.
- **Retries:** 429 and 5xx are retried twice with backoff; other 4xx are returned immediately.
- **`/health`:** unauthenticated (any method) and returns `{"ok": true}` — or 503 `{"ok": false}` if the secret can't be loaded. It does the same cold-start work as a real request (imports, boto3, Secrets Manager client, secret fetch) and returns nothing else.
- **Warm-up:** an EventBridge Scheduler schedule (`macroradar-mcp-warmup`) invokes the function with a `/health` event every 3 minutes from 6:30 to 6:45 America/Chicago on weekdays, bracketing the 6:35 CT morning-brief scheduled task (which can start a few minutes late), so the task's connector doesn't hit a cold start and report the server unavailable. One 6:30 ping wasn't enough margin: an idle environment can be reclaimed within minutes. The schedule uses the Chicago timezone so it follows DST along with the task (11:30–11:45 UTC in summer, 12:30–12:45 UTC in winter). If the task's time changes, change `ScheduleExpression` in `build_template.py` to match.
- **IAM:** the function role can write its own log group and read its one secret.

## Files

```
src/server.py        MCP server + MacroRadar client
src/tools.json       Tool definitions extracted verbatim from @macroradar/mcp
build_template.py    Bundles tools.json + server.py into build/index.py and writes build/template.json
tests/test_server.py Offline tests (MacroRadar API and secret mocked)
```

`build/` is generated and git-ignored.

## Test

```bash
python3 build_template.py && python3 tests/test_server.py
```

## Deploy changes

```bash
python3 build_template.py
aws cloudformation deploy --region us-east-1 --stack-name macroradar-mcp \
  --template-file build/template.json --capabilities CAPABILITY_IAM
```

The inline template must stay under CloudFormation's limits; `build_template.py` prints the size.

## Refresh tool definitions from a new @macroradar/mcp release

```bash
mkdir -p /tmp/mr && cd /tmp/mr && npm pack @macroradar/mcp && tar -xzf macroradar-mcp-*.tgz
python3 - <<'EOF'
import pathlib
src = pathlib.Path("package/index.mjs").read_text()
body = src[src.index("const EXPR_DEFS"):src.index("async function fetchApi")]
pathlib.Path("extract.mjs").write_text(body + "\nprocess.stdout.write(JSON.stringify({tools: TOOLS}));\n")
EOF
node extract.mjs > ~/Projects/GitHub/macroradar-mcp-lambda/src/tools.json
```

Then check `package/index.mjs`'s `callTool` for new tools or changed routes, mirror them in `_call_tool` in `src/server.py`, run the tests and redeploy.

## Check the live endpoint

```bash
URL=$(aws cloudformation describe-stacks --region us-east-1 --stack-name macroradar-mcp \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue" --output text)
TOKEN=$(aws secretsmanager get-secret-value --region us-east-1 --secret-id macroradar-mcp/config \
  --query SecretString --output text | python3 -c 'import json,sys;print(json.load(sys.stdin)["bearer_token"])')

curl -s -o /dev/null -w '%{http_code}\n' "$URL" -H 'Content-Type: application/json' -d '{}'   # expect 401
curl -s \"${URL%/}/health\"; echo   # expect {\"ok\":true}, no token needed

# compare with the sha256 printed by tests/test_server.py
curl -s "$URL" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"server/source_sha256"}'; echo

curl -s "$URL" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_recession_probability","arguments":{}}}'; echo
```

## Rotate the bearer token

1. Generate, store and copy a new token without displaying it:
   ```bash
   NEW=$(openssl rand -hex 24)
   aws secretsmanager put-secret-value --region us-east-1 --secret-id macroradar-mcp/config \
     --secret-string "{\"bearer_token\":\"$NEW\"}" > /dev/null
   printf 'Bearer %s' "$NEW" | pbcopy; unset NEW
   ```
2. Paste the clipboard as the `Authorization` header value on the claude.ai MacroRadar connector.
3. Allow up to 5 minutes for the function's cached copy to expire.

## Tear down

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name macroradar-mcp
```

Then remove the connector in claude.ai.

## License

The tool definitions in `src/tools.json` come from `@macroradar/mcp`, © its authors, MIT License. See https://github.com/axio-nyx/macroradar.
