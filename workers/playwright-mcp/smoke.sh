#!/usr/bin/env bash
# Smoke test of the playwright-mcp image (#7027): run it as its pod runs it (read-only
# root, /tmp writable, no capabilities) with the Deployment's args, then check over MCP
# that each session's context takes the X-ScadBuddy-Browser header's options, that a
# session without it, or with an invalid one, gets the server's --user-agent, that a
# refused header is logged without its value, that a wrong Host is 403, that a
# client that goes away without a DELETE loses its session, and that a connected client
# that calls nothing for --idle-timeout loses only its context.
#
# Usage: workers/playwright-mcp/smoke.sh <image>
# Env: SMOKE_CONTAINER (default scadbuddy-playwright-mcp-smoke), SMOKE_PORT (default 18931).
set -euo pipefail

image="${1:?usage: smoke.sh <image>}"
name="${SMOKE_CONTAINER:-scadbuddy-playwright-mcp-smoke}"
idle="${name}-idle"
port="${SMOKE_PORT:-18931}"
base="http://127.0.0.1:${port}"
tmp="$(mktemp -d)"

cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "container logs:"
    docker logs "$name" 2>&1 || true
    echo "idle container logs:"
    docker logs "$idle" 2>&1 || true
  fi
  docker rm -f "$name" "$idle" >/dev/null 2>&1 || true
  rm -rf "$tmp"
  exit "$rc"
}
trap cleanup EXIT

fail() {
  echo "::error::playwright-mcp smoke: $*"
  exit 1
}

docker rm -f "$name" >/dev/null 2>&1 || true
docker run -d --name "$name" --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges -e HOME=/tmp -p "127.0.0.1:${port}:8931" "$image" \
  --port 8931 --host 0.0.0.0 --isolated --allowed-hosts "localhost:${port},127.0.0.1:${port}" \
  --output-dir /tmp/output --user-agent DEFAULT-UA >/dev/null

up=no
for _ in $(seq 1 30); do
  if curl --silent --output /dev/null "$base/mcp"; then up=yes; break; fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != true ]; then break; fi
  sleep 1
done
[ "$up" = yes ] || fail "the server did not listen within 30 s"

# post <session id or ""> <JSON body> [curl args...]: the response body; headers in $tmp/headers.
post() {
  local sid="$1" body="$2"
  shift 2
  local headers=(-H 'content-type: application/json' -H 'accept: application/json, text/event-stream')
  if [ -n "$sid" ]; then headers+=(-H "mcp-session-id: $sid"); fi
  curl --silent --show-error --max-time 120 -D "$tmp/headers" "${headers[@]}" "$@" \
    --data "$body" "$base/mcp"
}

# open_session [curl args...]: initialize, then notifications/initialized; the session id.
open_session() {
  post "" '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}' "$@" >/dev/null
  local sid
  sid="$(grep -i '^mcp-session-id:' "$tmp/headers" | cut -d' ' -f2 | tr -d '\r')"
  [ -n "$sid" ] || fail "initialize returned no mcp-session-id"
  post "$sid" '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
  echo "$sid"
}

# call <session id> <tool> <arguments JSON>: the tool result's text.
call() {
  local body out
  body="$(jq -nc --arg n "$2" --argjson a "$3" '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$n,arguments:$a}}')"
  out="$(post "$1" "$body")"
  # Streamable HTTP answers as an event stream (data: lines) or as plain JSON.
  if grep -q '^data: ' <<<"$out"; then out="$(sed -n 's/^data: //p' <<<"$out")"; fi
  jq -r '.result.content[0].text // .error.message // empty' <<<"$out"
}

# probe <session id>: what the session's page reports as "<userAgent> <innerWidth> <language>".
probe() {
  call "$1" browser_navigate '{"url":"about:blank"}' >/dev/null
  call "$1" browser_evaluate '{"function":"() => navigator.userAgent + \" \" + innerWidth + \" \" + navigator.language"}'
}

# A Host the server does not allow is 403, as the CLI does.
status="$(curl --silent --output /dev/null --write-out '%{http_code}' -H 'Host: evil.example:8931' \
  -H 'content-type: application/json' --data '{}' "$base/mcp")"
echo "wrong Host: $status"
[ "$status" = 403 ] || fail "a wrong Host got $status, not 403"

mobile="$(open_session -H 'X-ScadBuddy-Browser: {"user_agent":"SB-TEST-UA","viewport":{"width":390,"height":844},"locale":"de-DE"}')"
got="$(probe "$mobile")"
echo "with the header: $got"
grep -qF '"SB-TEST-UA 390 de-DE"' <<<"$got" || fail "the header's options did not reach the context"

plain="$(open_session)"
got="$(probe "$plain")"
echo "without the header: $got"
grep -qF '"DEFAULT-UA ' <<<"$got" || fail "a session without the header did not get --user-agent"
if grep -qF ' 390 ' <<<"$got"; then fail "a session without the header got another session's viewport"; fi

bad="$(open_session -H 'X-ScadBuddy-Browser: {"user_agent":"SB-BAD-UA","proxy":"x"}')"
got="$(probe "$bad")"
echo "with an invalid header: $got"
grep -qF '"DEFAULT-UA ' <<<"$got" || fail "a session with an invalid header did not get --user-agent"
logs="$(docker logs "$name" 2>&1)"
grep -qF 'ignored the x-scadbuddy-browser header' <<<"$logs" || fail "the invalid header was not logged"
if grep -qF 'SB-BAD-UA' <<<"$logs"; then fail "the log holds the invalid header's value"; fi

# The first session still has its own context after the others opened.
got="$(probe "$mobile")"
echo "the first session again: $got"
grep -qF '"SB-TEST-UA 390 de-DE"' <<<"$got" || fail "the first session lost its options"

# A closed session is gone.
status="$(curl --silent --output /dev/null --write-out '%{http_code}' -X DELETE -H "mcp-session-id: $mobile" "$base/mcp")"
[ "$status" = 200 ] || fail "DELETE of a session got $status"
status="$(curl --silent --output /dev/null --write-out '%{http_code}' -H "mcp-session-id: $mobile" \
  -H 'content-type: application/json' --data '{}' "$base/mcp")"
[ "$status" = 404 ] || fail "a closed session got $status, not 404"

# A client that goes away without a DELETE is collected: once its GET stream is open, a
# ping it does not answer closes its session, as the CLI's heartbeat does.
gone="$(open_session)"
curl --silent --output /dev/null --max-time 2 -H 'accept: text/event-stream' \
  -H "mcp-session-id: $gone" "$base/mcp" || true
closed=no
for _ in $(seq 1 30); do
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' -H "mcp-session-id: $gone" \
    -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
    --data '{"jsonrpc":"2.0","id":9,"method":"ping"}' "$base/mcp")"
  if [ "$status" = 404 ]; then closed=yes; break; fi
  sleep 1
done
echo "a gone client's session closed: $closed"
[ "$closed" = yes ] || fail "a client that went away kept its session"
grep -qF 'did not answer a ping' <<<"$(docker logs "$name" 2>&1)" || fail "the closed session was not logged"

# Idle: a connected client answers every 3 s ping, and that is not activity. With a short
# --idle-timeout, a client that stays connected but calls nothing loses its context (a
# new page, so window.name is empty again) and keeps its session and its options. The
# client is the MCP SDK's, from the image's own playwright-core, run in a second
# container of the image so it reaches the server at the Host it allows.
docker rm -f "$idle" >/dev/null 2>&1 || true
docker run -d --name "$idle" --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges -e HOME=/tmp "$image" \
  --port 8931 --host 127.0.0.1 --isolated --output-dir /tmp/output \
  --user-agent DEFAULT-UA --idle-timeout 4000 >/dev/null
idle_out="$(docker exec "$idle" node -e '
const { Client, StreamableHTTPClientTransport } = require("/opt/scadbuddy-playwright-mcp/node_modules/playwright-core/lib/utilsBundle");
const text = (r) => r.content.map((c) => c.text).join("");
const evaluate = (client, fn) => client.callTool({ name: "browser_evaluate", arguments: { function: fn } }).then(text);
(async () => {
  let client;
  for (let i = 0; i < 30; i++) {
    client = new Client({ name: "smoke", version: "1" });
    const transport = new StreamableHTTPClientTransport(new URL("http://localhost:8931/mcp"), {
      requestInit: { headers: { "X-ScadBuddy-Browser": "{\"user_agent\":\"SB-IDLE-UA\"}" } },
    });
    try { await client.connect(transport); break; } catch { client = undefined; await new Promise((f) => setTimeout(f, 1000)); }
  }
  if (!client) throw new Error("no connection");
  await client.callTool({ name: "browser_navigate", arguments: { url: "about:blank" } });
  console.log("before: " + await evaluate(client, "() => { window.name = \"kept\"; return window.name + \" \" + navigator.userAgent; }"));
  await new Promise((f) => setTimeout(f, 10000));
  await client.callTool({ name: "browser_navigate", arguments: { url: "about:blank" } });
  console.log("after: " + await evaluate(client, "() => \"[\" + window.name + \"] \" + navigator.userAgent"));
  await client.close();
})().catch((e) => { console.log("error: " + e.message); process.exit(1); });
' 2>&1)" || fail "the idle client failed: $idle_out"
echo "$idle_out"
grep -qF 'kept SB-IDLE-UA' <<<"$idle_out" || fail "the idle session did not start with its options"
grep -qF '[] SB-IDLE-UA' <<<"$idle_out" || fail "a connected client that called nothing kept its context past --idle-timeout, or lost its options"
idle_logs="$(docker logs "$idle" 2>&1)"
grep -qF 'a session was idle: closing its browser context' <<<"$idle_logs" || fail "the idle close was not logged"
if grep -qF 'did not answer a ping' <<<"$idle_logs"; then fail "the idle client's session was closed, not only its context"; fi
docker rm -f "$idle" >/dev/null 2>&1 || true

echo "playwright-mcp smoke: ok"
