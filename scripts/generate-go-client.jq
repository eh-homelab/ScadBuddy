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
def name_maps: ["responses", "properties", "patternProperties", "headers", "content",
  "examples", "schemas", "parameters", "requestBodies", "securitySchemes", "links",
  "callbacks", "pathItems", "encoding", "$defs", "definitions"];
def payload: . as $q | any(range(1; length);
  ($q[.] | IN("default", "example", "examples"))
  and ($q[. - 1] | IN(name_maps[]) | not)
  and ($q[.] != "examples" or ($q[. + 1] | type) == "number" or $q[. + 2] == "value"));
walk(if type == "object" and has("$ref") and (has("description") or has("summary")) then del(.description, .summary) else . end)
| walk(if type == "object" and (has("anyOf") or has("oneOf")) then del(.default) else . end)
| reduce (paths(strings) | select(.[-1] == "description" or .[-1] == "summary")
          | select(payload | not)) as $p
    (.; setpath($p; getpath($p) | gsub("\\s*\n\\s*"; " ")))
