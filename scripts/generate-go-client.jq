# The spec generate-go-client.sh feeds Kiota, from the backend's export. Three
# generator workarounds, all leaving the wire format alone
# (.github/scripts/generate-go-client-jq.test.sh pins them):
#  - Kiota's doc comments depend on hash order when a $ref has sibling descriptions;
#    dropping them makes generation reproducible (forge #1043).
#  - A default on an anyOf/oneOf property (PrintChoices.tier: Tier | None = "standard")
#    makes Kiota's Go emit `m.SetTier(&"standard")` on a union wrapper, which does not
#    compile; the default is only a hint to a client that never reads it.
#  - Kiota joins a description's lines, and its paragraphs, with no space
#    ("Bambuddy (#755, #760);its slots", "there.#2788", #4464) and writes each as one
#    comment line, so each run of whitespace holding a newline becomes one space first.
#    Only string description/summary keys change, and none inside a payload: a
#    `default` or `example` value, an `examples` array, or an Example Object's `value`.
#    A key of those names in a map of names (a response, a property, a header named
#    `default`) is no keyword, and an Example Object's own summary is documentation.
#    `payload` reads the path from the root, keyword then name in turn: a key in
#    `name_maps` (in keyword position) makes the next key a name, so a property named
#    `content` is a name and a `default` inside it is its keyword, at any depth.
#: OpenAPI 3.1's and JSON Schema's maps of names. A Link Object's `parameters`, a map
#: of values, is not told apart from the operation's; the backend's export has no links.
#: A map keyword missing here makes its `default` entries read as payload, so add any
#: new one the export starts to use.
def name_maps: ["paths", "responses", "properties", "patternProperties", "dependentSchemas",
  "headers", "content", "schemas", "parameters", "requestBodies", "securitySchemes",
  "links", "callbacks", "pathItems", "webhooks", "encoding", "mapping", "$defs",
  "definitions"];
#: Whether a path is inside a payload. State: `kw` (the next key is a keyword),
#: `name` (it is a name in a map), `examples` (an `examples` array index or Example
#: Object name), `exobj` (a keyword of an Example Object, whose `value` is payload).
def payload: reduce .[] as $k ({s: "kw", p: false};
  if .p then .
  elif .s == "name" then .s = "kw"
  elif .s == "examples" then (if ($k | type) == "number" then .p = true else .s = "exobj" end)
  elif .s == "exobj" and $k == "value" then .p = true
  elif ($k | type) != "string" then .s = "kw"
  elif ($k | IN("default", "example")) then .p = true
  elif $k == "examples" then .s = "examples"
  elif ($k | IN(name_maps[])) then .s = "name"
  else .s = "kw" end) | .p;
walk(if type == "object" and has("$ref") and (has("description") or has("summary")) then del(.description, .summary) else . end)
| walk(if type == "object" and (has("anyOf") or has("oneOf")) then del(.default) else . end)
| reduce (paths(strings) | select(.[-1] == "description" or .[-1] == "summary")
          | select(payload | not)) as $p
    (.; setpath($p; getpath($p) | gsub("\\s*\n\\s*"; " ")))
