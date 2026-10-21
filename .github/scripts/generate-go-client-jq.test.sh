#!/usr/bin/env bash
# Tests for scripts/generate-go-client.jq, the spec generate-go-client.sh feeds Kiota
# (#4464): a description's newlines fold to one space where it is documentation, and
# a payload value (a default, an example) that happens to hold one is left as it is.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
fail=0
# shellcheck disable=SC2016  # "$ref" is the spec's key, not an expansion.
spec='{
  "paths": {"/a": {"post": {
    "summary": "one\r\n  two",
    "description": "Bambuddy (#755, #760);\nits slots\n\n#2788: more",
    "responses": {"default": {"description": "a problem\ndocument"}}
  }}},
  "components": {"schemas": {"S": {
    "properties": {
      "default": {"type": "string", "description": "a property\nnamed default"},
      "kept": {"type": "object", "default": {"description": "a\nvalue"},
               "example": {"summary": "x\ny"}, "examples": [{"description": "p\nq"}]},
      "ref": {"$ref": "#/components/schemas/T", "description": "dropped"},
      "tier": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": "standard"}
    }
  }}}
}'
got=$(jq -f "$here/../../scripts/generate-go-client.jq" <<<"$spec")
expect() {
  local filter="$1" want="$2" value
  value=$(jq -c "$filter" <<<"$got")
  [[ "$value" == "$want" ]] || { echo "FAIL: $filter -> $value, want $want"; fail=1; }
}
expect '.paths["/a"].post.summary' '"one two"'
expect '.paths["/a"].post.description' '"Bambuddy (#755, #760); its slots #2788: more"'
expect '.paths["/a"].post.responses.default.description' '"a problem document"'
expect '.components.schemas.S.properties.default.description' '"a property named default"'
expect '.components.schemas.S.properties.kept.default.description' '"a\nvalue"'
expect '.components.schemas.S.properties.kept.example.summary' '"x\ny"'
expect '.components.schemas.S.properties.kept.examples[0].description' '"p\nq"'
expect '.components.schemas.S.properties.ref | has("description")' 'false'
expect '.components.schemas.S.properties.tier | has("default")' 'false'
exit "$fail"
