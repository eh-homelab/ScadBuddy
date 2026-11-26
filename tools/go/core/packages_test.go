package main

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

// The agent's plugin packages, feeds and hosted reads (internal/packages), and the plugin
// routes that need no KEK (internal/plugins), as backend/tests/api/test_crud_agent_packages.py,
// test_crud_agent_plugin_feeds.py, test_crud_agent_hosted_packages.py and
// test_crud_agent_plugins.py check them.

const packageAreas = "plugin-packages, plugin-feeds, hosted-plugins:read, plugins"

var (
	c1 = strings.Repeat("a", 40)
	c2 = strings.Repeat("b", 40)
	h1 = "sha256:" + strings.Repeat("1", 64)
	h2 = "sha256:" + strings.Repeat("2", 64)
)

func review(name string, extra ...any) map[string]any {
	r := map[string]any{
		"name": name, "description": nil, "version": nil,
		"skills": []string{name + ":hello"}, "commands": []string{}, "agents": []string{},
		"hooks":         []any{map[string]any{"event": "Stop", "type": "prompt", "id": "0123456789abcdef"}},
		"mcp_servers":   []any{map[string]any{"name": "mem", "type": "http", "url": "https://mcp.example/mcp/"}},
		"local_servers": []string{}, "files": []string{}, "refused": []string{}, "builtin_tools": []string{},
		"described": map[string]any{"skill:hello": nil},
	}
	for i := 0; i < len(extra); i += 2 {
		r[extra[i].(string)] = extra[i+1]
	}
	return r
}

func install(name string, extra ...any) string {
	return object("name", name, "source_kind", "git", "source_url", "https://git.test/"+name+".git",
		"source_ref", "HEAD", "marketplace_entry", nil, "fetch_url", "https://git.test/"+name+".git",
		"fetch_path", "", "commit_sha", c1, "content_hash", h1,
		"files", map[string]any{"README.md": map[string]any{"sha256": strings.Repeat("0", 64), "size": 1}},
		"review", review(name, extra...))
}

func pendingPin(name string) string {
	return object("ref", "main", "fetch_url", "https://git.test/"+name+".git", "fetch_path", "",
		"commit_sha", c2, "content_hash", h2, "files", map[string]any{}, "review", review(name))
}

func (a *api) access() string {
	return a.text(`SELECT coalesce(string_agg(source || ' ' || item || '=' || access, ',' ORDER BY source COLLATE "C",
		item COLLATE "C"), '') FROM ai_access`)
}

func (a *api) seed(source string, items ...string) {
	for _, item := range items {
		a.sql(`INSERT INTO ai_access (source, item, access, updated_by) VALUES ($1, $2, 'off', 'test')`, source, item)
	}
}

func TestAnInstallIsStoredUnapprovedAndASecondIsRefused(t *testing.T) {
	a := newAPI(t, packageAreas)
	a.seed("package:greeter", "*", "skill:hello")
	a.sql(`INSERT INTO ai_tool_listings (source, tools) VALUES ('package:greeter/mcp:mem', '[]')`)
	made := a.do("POST", base+"/plugin-packages", install("greeter"))
	expect(t, made.status, 201)
	expect(t, made.body["approved_at"], nil)
	expect(t, made.body["enabled"], false)
	expect(t, made.body["hosted_latest_version"], nil)
	expect(t, made.body["files"], map[string]any{"README.md": map[string]any{"sha256": strings.Repeat("0", 64),
		"size": float64(1)}})
	expect(t, a.access(), "")
	expect(t, a.count(`SELECT count(*) FROM ai_tool_listings`), 0)

	again := a.do("POST", base+"/plugin-packages", install("greeter"))
	expect(t, again.status, 409)
	expect(t, again.body["reason"], "package-exists")
	differs := a.do("POST", base+"/plugin-packages", strings.Replace(install("other"), `"name":"other"`, `"name":"x"`, 1))
	expect(t, differs.status, 422)
	expect(t, differs.body["reason"], "package-name-differs")
	wrongPin := a.do("POST", base+"/plugin-packages", strings.Replace(install("p"), `"source_kind":"git"`,
		`"source_kind":"archive"`, 1))
	expect(t, wrongPin.status, 422)
	expect(t, wrongPin.body["errors"].([]any)[0].(map[string]any)["loc"], []any{"body"})

	listed := a.ok("GET", "/plugin-packages", "").body["packages"].([]any)
	expect(t, len(listed), 1)
	expect(t, a.ok("GET", "/plugin-packages?enabled=true", "").raw, `{"packages":[]}`)
	expect(t, a.ok("GET", "/plugin-packages/nope", "").raw, `{"package":null}`)
}

func TestARepinIsReviewedApprovedAndPrunesWhatItDropped(t *testing.T) {
	a := newAPI(t, packageAreas)
	a.do("POST", base+"/plugin-packages", install("greeter"))
	same := a.do("POST", base+"/plugin-packages/greeter/pending",
		strings.Replace(strings.Replace(pendingPin("greeter"), c2, c1, 1), h2, h1, 1))
	expect(t, same.status, 409)
	expect(t, same.body["reason"], "package-pinned")
	a.ok("POST", "/plugin-packages/greeter/approve", object("commit_sha", c1, "content_hash", h1))
	a.ok("POST", "/plugin-packages/greeter/enabled", `{"enabled":true}`)

	a.seed("package:greeter", "skill:hello", "skill:gone", "hook:Stop:0123456789abcdef", "hook:Stop:old",
		"mcp__x__tool")
	a.seed("package:greeter/mcp:mem", "*")
	a.seed("package:greeter/mcp:old", "*")
	repin := a.ok("POST", "/plugin-packages/greeter/pending", pendingPin("greeter")).body
	expect(t, repin["pending_commit_sha"], c2)
	expect(t, a.access(), "package:greeter hook:Stop:0123456789abcdef=off,package:greeter mcp__x__tool=off,"+
		"package:greeter skill:hello=off,package:greeter/mcp:mem *=off")

	mismatch := a.do("POST", base+"/plugin-packages/greeter/approve", object("commit_sha", c2, "content_hash", h1))
	expect(t, mismatch.body["reason"], "pin-mismatch")
	approved := a.ok("POST", "/plugin-packages/greeter/approve", object("commit_sha", c2, "content_hash", h2)).body
	expect(t, approved["commit_sha"], c2)
	expect(t, approved["pending_commit_sha"], nil)
	// The same place keeps enabled.
	expect(t, approved["enabled"], true)

	// A re-pin from another path is a new source: its approval switches the package off.
	moved := strings.Replace(strings.Replace(pendingPin("greeter"), `"fetch_path":""`, `"fetch_path":"sub"`, 1),
		c2, c1, 1)
	a.ok("POST", "/plugin-packages/greeter/pending", moved)
	discarded := a.ok("POST", "/plugin-packages/greeter/discard-pending", "").body
	expect(t, discarded["pending_review"], nil)
	expect(t, discarded["enabled"], true)
	a.ok("POST", "/plugin-packages/greeter/pending", moved)
	expect(t, a.ok("POST", "/plugin-packages/greeter/approve", object("commit_sha", c1, "content_hash", h2)).
		body["enabled"], false)
	expect(t, a.do("POST", base+"/plugin-packages/nope/discard-pending", "").body["reason"], "package-not-found")
}

func TestARepinWhoseReviewNamesNullSaysNoneAsPythonDoes(t *testing.T) {
	a := newAPI(t, packageAreas)
	a.do("POST", base+"/plugin-packages", install("greeter"))
	pin := object("ref", "main", "fetch_url", "https://git.test/greeter.git", "fetch_path", "",
		"commit_sha", c2, "content_hash", h2, "files", map[string]any{}, "review", review("greeter", "name", nil))
	differs := a.do("POST", base+"/plugin-packages/greeter/pending", pin)
	expect(t, differs.status, 409)
	expect(t, differs.body["detail"], `the new commit names the plugin "None", not "greeter"`)
}

func TestARefusedReviewNeedsAllowAndEnableNeedsApproval(t *testing.T) {
	a := newAPI(t, packageAreas)
	a.do("POST", base+"/plugin-packages", install("greeter", "refused", []string{"a command hook"}))
	off := a.do("POST", base+"/plugin-packages/greeter/enabled", `{"enabled":true}`)
	expect(t, off.body["reason"], "package-unapproved")
	pin := object("commit_sha", c1, "content_hash", h1)
	expect(t, a.do("POST", base+"/plugin-packages/greeter/approve", pin).body["reason"], "refused-needs-allow")
	allowed := a.ok("POST", "/plugin-packages/greeter/approve",
		object("commit_sha", c1, "content_hash", h1, "allow_refused", true)).body
	expect(t, allowed["allow_refused"], true)
	two := a.do("POST", base+"/plugin-packages/greeter/approve",
		object("commit_sha", c1, "version", 1, "content_hash", h1))
	expect(t, two.status, 422)
}

func TestEveryPackageWriteIsAnEventOnceCommitted(t *testing.T) {
	a := newAPI(t, packageAreas)
	conn, err := pgx.Connect(context.Background(), a.pool.Config().ConnString())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close(context.Background())
	if _, err := conn.Exec(context.Background(), `LISTEN scadbuddy_events`); err != nil {
		t.Fatal(err)
	}
	a.do("POST", base+"/plugin-packages", install("greeter"))
	a.do("POST", base+"/plugin-packages", install("greeter"))
	a.ok("POST", "/plugin-packages/greeter/delete", "")
	expect(t, a.ok("POST", "/plugin-packages/greeter/delete", "").raw, `{"done":false}`)
	expect(t, a.count(`SELECT count(*) FROM events WHERE kind = 'plugin_packages.changed'
		AND payload->>'name' = 'greeter'`), 2)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	heard, err := conn.WaitForNotification(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var event map[string]any
	if err := json.Unmarshal([]byte(heard.Payload), &event); err != nil {
		t.Fatal(err)
	}
	expect(t, event["kind"], "plugin_packages.changed")
	expect(t, len(event["id"].(string)), 32)
	if !strings.HasPrefix(heard.Payload, `{"id":`) {
		t.Fatalf("the payload's members are not in pydantic's order: %s", heard.Payload)
	}
}

func TestABytesPinNeedsItsMirrorAndAHostedNameIsTaken(t *testing.T) {
	a := newAPI(t, packageAreas)
	sum := strings.Repeat("c", 64)
	archive := object("name", "arch", "source_kind", "archive", "source_url", "https://x.test/a.zip",
		"source_ref", "", "fetch_url", "https://x.test/a.zip", "fetch_path", "", "pin_kind", "bytes",
		"bytes_sha256", sum, "content_hash", h1, "files", map[string]any{}, "review", review("arch"))
	expect(t, a.do("POST", base+"/plugin-packages", archive).body["reason"], "mirror-missing")
	a.sql(`INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, 'plugin_package_mirror', $2)`,
		"plugin-mirror-"+sum, "arch/"+sum)
	expect(t, a.do("POST", base+"/plugin-packages", archive).status, 201)

	a.sql(`INSERT INTO ai_hosted_plugins (name, description, tags) VALUES ('hosted', 'Hi', '{a}')`)
	expect(t, a.do("POST", base+"/plugin-packages", install("hosted")).body["reason"], "hosted-name-taken")
	hosted := object("name", "hosted", "source_kind", "hosted", "source_url", "hosted:hosted", "source_ref", "",
		"fetch_url", "hosted:hosted", "fetch_path", "", "pin_kind", "version", "version", 1, "content_hash", h1,
		"files", map[string]any{}, "review", review("hosted"))
	expect(t, a.do("POST", base+"/plugin-packages", hosted).body["reason"], "hosted-version-mismatch")
	a.sql(`INSERT INTO ai_hosted_plugin_versions (plugin_id, version, zip_sha256, content_hash, files, published_by)
		SELECT id, 1, $1, $2, '{}', 'b' FROM ai_hosted_plugins`, sum, h1)
	a.sql(`UPDATE ai_hosted_plugins SET latest_version = 1`)
	made := a.do("POST", base+"/plugin-packages", hosted)
	expect(t, made.status, 201)
	expect(t, made.body["hosted_latest_version"], float64(1))
	plugin := a.ok("GET", "/hosted-plugins/hosted", "").body["plugin"].(map[string]any)
	expect(t, plugin["tags"], []any{"a"})
	expect(t, plugin["latest"].(map[string]any)["content_hash"], h1)
	expect(t, a.ok("GET", "/hosted-plugins/nope", "").raw, `{"plugin":null}`)
}

func TestACommitPinsMirrorIsRecordedMovedAndKeepsItsHold(t *testing.T) {
	a := newAPI(t, packageAreas)
	z1, z2 := strings.Repeat("d", 64), strings.Repeat("e", 64)
	hold := func(sum string) {
		a.sql(`INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, 'plugin_package_mirror', $2)`,
			"plugin-mirror-"+sum, "greeter/"+sum)
	}
	record := func(commit, hash, sum string) string {
		return object("commit_sha", commit, "content_hash", hash, "bytes_sha256", sum)
	}
	a.do("POST", base+"/plugin-packages", install("greeter"))
	expect(t, a.do("POST", base+"/plugin-packages/greeter/mirror", record(c1, h1, z1)).body["reason"], "mirror-missing")
	hold(z1)
	expect(t, a.do("POST", base+"/plugin-packages/greeter/mirror", record(c2, h1, z1)).body["reason"], "pin-mismatch")
	expect(t, a.do("POST", base+"/plugin-packages/greeter/mirror", record(c1, h2, z1)).body["reason"], "pin-mismatch")
	expect(t, a.do("POST", base+"/plugin-packages/nope/mirror", record(c1, h1, z1)).body["reason"], "package-not-found")
	expect(t, a.do("POST", base+"/plugin-packages/greeter/mirror", record(c1, h1, "x")).status, 422)
	recorded := a.ok("POST", "/plugin-packages/greeter/mirror", record(c1, h1, z1)).body
	expect(t, recorded["mirror_sha256"], z1)
	expect(t, recorded["pending_mirror_sha256"], nil)
	expect(t, recorded["approved_at"], nil)
	expect(t, a.count(`SELECT count(*) FROM events WHERE kind = 'plugin_packages.changed'`), 1)

	a.ok("POST", "/plugin-packages/greeter/pending", pendingPin("greeter"))
	hold(z2)
	staged := a.ok("POST", "/plugin-packages/greeter/mirror", record(c2, h2, z2)).body
	expect(t, staged["mirror_sha256"], z1)
	expect(t, staged["pending_mirror_sha256"], z2)
	discarded := a.ok("POST", "/plugin-packages/greeter/discard-pending", "").body
	expect(t, discarded["pending_mirror_sha256"], nil)
	a.ok("POST", "/plugin-packages/greeter/pending", pendingPin("greeter"))
	expect(t, a.ok("GET", "/plugin-packages/greeter", "").body["package"].(map[string]any)["pending_mirror_sha256"], nil)
	a.ok("POST", "/plugin-packages/greeter/mirror", record(c2, h2, z2))

	// Past their grace, the holds a row names stay: the pin's and the re-pin's.
	a.sql(`UPDATE blob_refs SET created_at = now() - interval '2 days'`)
	a.do("POST", base+"/plugin-packages", install("other"))
	expect(t, a.count(`SELECT count(*) FROM blob_refs WHERE holder_kind = 'plugin_package_mirror'`), 2)
	approved := a.ok("POST", "/plugin-packages/greeter/approve", object("commit_sha", c2, "content_hash", h2)).body
	expect(t, approved["mirror_sha256"], z2)
	expect(t, approved["pending_mirror_sha256"], nil)
	expect(t, a.text(`SELECT string_agg(holder_id, ',') FROM blob_refs WHERE holder_kind = 'plugin_package_mirror'`),
		"greeter/"+z2)
}

func TestAFeedListsItsPackagesAtTheirPins(t *testing.T) {
	a := newAPI(t, packageAreas)
	a.do("POST", base+"/plugin-packages", install("one"))
	a.do("POST", base+"/plugin-packages", install("two", "description", "Two"))
	a.ok("POST", "/plugin-packages/two/approve", object("commit_sha", c1, "content_hash", h1))
	a.sql(`INSERT INTO ai_plugin_feeds (slug, name, owner) VALUES ('mine', 'mine', 'Me')`)
	expect(t, a.ok("PUT", "/plugin-feeds/mine/packages/one", "").raw, `{"done":true}`)
	expect(t, a.ok("PUT", "/plugin-feeds/mine/packages/one", "").raw, `{"done":false}`)
	a.ok("PUT", "/plugin-feeds/mine/packages/two", "")
	expect(t, a.do("PUT", base+"/plugin-feeds/mine/packages/nope", "").body["reason"], "package-not-found")
	expect(t, a.do("PUT", base+"/plugin-feeds/nofeed/packages/one", "").body["reason"], "feed-not-found")

	feed := a.ok("GET", "/plugin-feeds/mine", "").body
	expect(t, feed["feed"], map[string]any{"slug": "mine", "name": "mine", "owner": "Me"})
	listed := feed["packages"].([]any)
	expect(t, len(listed), 2)
	two := listed[1].(map[string]any)
	expect(t, two["approved"], true)
	expect(t, two["description"], "Two")
	expect(t, two["hosted"], nil)
	expect(t, len(a.ok("GET", "/plugin-feeds/mine?package=two", "").body["packages"].([]any)), 1)
	expect(t, a.ok("GET", "/plugin-feeds/nofeed", "").raw, `{"feed":null,"packages":[]}`)
	expect(t, a.ok("DELETE", "/plugin-feeds/mine/packages/one", "").raw, `{"done":true}`)
	expect(t, a.ok("DELETE", "/plugin-feeds/mine/packages/one", "").raw, `{"done":false}`)
}

func TestAPluginsDeleteAndSignOutNeedNoKey(t *testing.T) {
	a := newAPI(t, packageAreas)
	a.sql(`INSERT INTO ai_plugins (name, kind, url) VALUES ('docs', 'remote_mcp', 'https://docs.example/mcp')`)
	grant := uuid()
	a.sql(`INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource, issuer, token_endpoint, client_id,
		auth_method, registration, secret_sealed, dek_sealed, kek_id, has_refresh, grant_id, connected_by)
		VALUES ('docs', 'https://docs.example/mcp', 'https://docs.example/mcp', 'https://auth.example',
		'https://auth.example/token', 'c', 'none', 'dynamic', 'x', 'y', 'k', true, $1::text::uuid, 'b')`, grant)
	a.seed("remote:docs", "*")

	expect(t, a.ok("POST", "/plugin-oauth/grants/docs/mark", object("reason", "r", "grant", uuid())).raw,
		`{"done":false}`)
	expect(t, a.ok("POST", "/plugin-oauth/grants/docs/mark", object("reason", "expired", "grant", grant)).raw,
		`{"done":true}`)
	expect(t, a.text(`SELECT status || ':' || last_error FROM ai_plugin_oauth`), "error:expired")
	expect(t, a.ok("POST", "/plugin-oauth/grants/docs/mark", object("reason", "again")).raw, `{"done":false}`)
	expect(t, a.ok("POST", "/plugin-oauth/grants/docs/forget", "").raw, `{"done":true}`)
	expect(t, a.ok("POST", "/plugin-oauth/grants/docs/forget", "").raw, `{"done":false}`)
	expect(t, a.ok("POST", "/plugins/docs/delete", "").raw, `{"done":true}`)
	expect(t, a.access(), "")
	expect(t, a.ok("POST", "/plugins/docs/delete", "").raw, `{"done":false}`)
	expect(t, a.do("POST", base+"/plugins/Docs/delete", "").status, 422)
	// A route that opens the KEK stays Python's.
	expect(t, a.do("GET", base+"/plugins", "").status, 418)
}
