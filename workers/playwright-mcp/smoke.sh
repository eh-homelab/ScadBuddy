#!/usr/bin/env bash
# Smoke test of the playwright-mcp image (#7027): run it as its pod runs it (read-only
# root, /tmp writable, no capabilities) with the Deployment's args, then check over MCP
# that each session's context takes the X-ScadBuddy-Browser header's options, that a
# session without it, or with an invalid one, gets the server's --user-agent, that a
# refused header is logged without its value, and that a wrong Host is 403.
#
# Usage: workers/playwright-mcp/smoke.sh <image>
# Env: SMOKE_CONTAINER (default scadbuddy-playwright-mcp-smoke), SMOKE_PORT (default 18931).
set -euo pipefail

image="${1:?usage: smoke.sh <image>}"
name="${SMOKE_CONTAINER:-scadbuddy-playwright-mcp-smoke}"
port="${SMOKE_PORT:-18931}"
base="http://127.0.0.1:${port}"
tmp="$(mktemp -d)"

cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "container logs:"
    docker logs "$name" 2>&1 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
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

echo "playwright-mcp smoke: ok"
