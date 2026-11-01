#!/usr/bin/env bash
# Usage: lint-base64-images.sh [repo root, default this checkout]
#
# No image travels as base64 inside JSON or text (#2479, epic #2486): an image is a
# blob, and what moves is a pointer, `{name, mediaType}`; the consumer opens the file
# or fetches the pointer. The one place an image may be base64 is where the Messages
# API request is built from it, by the SDK or Claude Code: an SDK user message's
# image block, or an MCP tool result's image content the model is shown.
#
# This fails on any line of shipped source (agent/, agent-durable/, frontend/,
# backend/; tests and msw mocks aside) that builds base64 or a data: URL, or takes a
# `*_base64` field, unless the allowlist below names that line with its reason. An
# allowlist entry that no longer matches fails too, so the list cannot go stale.
set -euo pipefail
root="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"

# What builds base64 or carries it: a Messages API base64 source block, a data: URL,
# Node's, the browser's and Python's encoders, and a field named for base64.
pattern="type: ?['\"]base64['\"]|\"type\": ?\"base64\"|;base64,|toString\\(['\"]base64['\"]\\)|btoa\\(|b64encode\\(|readAsDataURL|toDataURL|_base64['\"]?[[:space:]]*:"

# path<TAB>a regex the line must match<TAB>why it may
allow=$(cat <<'EOF'
agent/src/sessions/images.ts	type: 'base64' as const	the SDK boundary: an SDK user message's image block, which Claude Code sends to the Messages API
agent/src/tools/registry.ts	type: 'image', data: Buffer	the model boundary: MCP image content a tool returns, which Claude Code turns into the Messages API request
agent/src/tools/registry.ts	resource: \{ uri, mimeType, blob	an MCP embedded resource (GLB, 3MF), not an image; binary.ts links instead past its cap
agent/src/tools/workspace.ts	const base64 = file.bytes.toString	the model boundary: workspace_read shows the model an image file as MCP image content
agent/src/builtins/files.ts	image: \{ data: file.bytes.toString\(.base64.\)	the model boundary: a durable turn's Read shows the model an image file as MCP image content, which the turn tool activity stores as a session blob
agent/src/tools/activityProjection.ts	data: stored.bytes.toString\('base64'\), mimeType: stored.mediaType	the model boundary: a ToolCall's result image, read back from its session blob, as the MCP image content Claude Code shows the model
agent/src/temporal/toolActivities.ts	source: \{ type: 'base64', media_type: block.mimeType	decodes an MCP result's image into a session blob (inbound); the result names it
agent/src/temporal/payloadCodec.ts	const b64 = 	the Temporal payload codec: sealed ciphertext, not images
agent-durable/src/scadbuddy_durable/codec.py	return base64.b64encode\(data\)	the Temporal payload codec: sealed ciphertext, not images
backend/scadbuddy/workflows/payload_codec.py	return base64.b64encode\(data\)	the Temporal payload codec: sealed ciphertext, not images
backend/scadbuddy/flows/operations.py	return base64.b64encode\(seal_bytes	a sealed answer, not an image
backend/scadbuddy/library/settings_store.py	"(secret|dek)": base64.b64encode\(envelope	a sealed secret, not an image
frontend/src/agent/tabId.ts	return btoa\(binary\)	the bridge tab id: random bytes as base64url, not an image
backend/scadbuddy/activity/index.py	return base64.urlsafe_b64encode\(raw.encode\(\)\)	the activity list's opaque cursor (created_at|id), not an image
backend/scadbuddy/orders/store.py	return base64.urlsafe_b64encode\(f"\{at.isoformat\(\)\}\|\{order_id\}".encode\(\)\)	the orders list's opaque cursor (created_at|id), not an image
backend/scadbuddy/slicing/transfer_token.py	return base64.urlsafe_b64encode\(data\)	the slicer's transfer token (method, key, expiry and its HMAC) as base64url, not an image
agent/src/plugins/oauth.ts	headers.authorization = .Basic 	RFC 6749 §2.3.1 client_secret_basic: an OAuth client id and secret, not an image
EOF
)

fail=0
declare -A used=()
while IFS=: read -r file line text; do
  ok=""
  while IFS=$'\t' read -r path regex _why; do
    if [[ "$file" == "$path" && "$text" =~ $regex ]]; then
      ok=1
      used["$path	$regex"]=1
    fi
  done <<<"$allow"
  if [[ -z "$ok" ]]; then
    echo "FAIL: $file:$line builds or carries base64: ${text#"${text%%[![:space:]]*}"}"
    fail=1
  fi
done < <(
  cd "$root" || exit 1
  git ls-files -- agent/src agent-durable/src frontend/src backend/scadbuddy |
    grep -vE '\.test\.tsx?$|^frontend/src/mocks/|/tests?/' |
    while IFS= read -r f; do if [[ -f "$f" ]]; then printf '%s\0' "$f"; fi; done |
    { xargs -0 -r grep -nE -- "$pattern" || true; }
)

while IFS=$'\t' read -r path regex _why; do
  if [[ -z "${used["$path	$regex"]:-}" ]]; then
    echo "FAIL: the allowlist entry for $path ($regex) matches nothing; remove it"
    fail=1
  fi
done <<<"$allow"

if [[ $fail -ne 0 ]]; then
  echo
  echo "An image moves as a blob and a pointer, {name, mediaType}, never as base64 in JSON or text (#2479)."
  echo "Store the bytes (agent: sessions/blobs.ts; uploads: multipart) and pass the name. If this is not an"
  echo "image, or it is the Messages API boundary, add the line to the allowlist in $0 with the reason."
  exit 1
fi
echo "ok: no image is built as base64 outside the allowlisted boundaries"
