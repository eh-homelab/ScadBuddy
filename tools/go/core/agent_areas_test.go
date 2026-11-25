package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"mime/multipart"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"testing"
)

// The tests of backend/tests/crud/test_crud_agent_blobs.py, test_crud_agent_tasks.py and
// test_crud_agent_workspace.py that cover what Go answers.

const blobAreas = "session-blobs, attachments, http-bodies, background-tasks, operations:read, workspace:sql"

var (
	png  = append([]byte("\x89PNG\r\n\x1a\n"), make([]byte, 24)...)
	jpeg = append([]byte("\xff\xd8\xff\xe0"), bytes.Repeat([]byte{1}, 24)...)
	pdf  = append([]byte("%PDF-1.7\n"), bytes.Repeat([]byte{3}, 24)...)
	webp = append([]byte("RIFF\x00\x00\x00\x00WEBPVP8 "), bytes.Repeat([]byte{2}, 16)...)
)

func blobName(data []byte, ext string) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]) + "." + ext
}

func with(b []byte, tail string) []byte { return append(append([]byte{}, b...), tail...) }

// multipartBody is a meta part and file parts, in order.
func multipartBody(meta any, files ...any) (string, string) {
	var b bytes.Buffer
	w := multipart.NewWriter(&b)
	raw, _ := json.Marshal(meta)
	_ = w.WriteField("meta", string(raw))
	for i := 0; i < len(files); i += 2 {
		part, _ := w.CreateFormFile(files[i].(string), files[i].(string))
		_, _ = part.Write(files[i+1].([]byte))
	}
	_ = w.Close()
	return b.String(), w.FormDataContentType()
}

func (a *api) form(path string, meta any, files ...any) answer {
	a.t.Helper()
	body, kind := multipartBody(meta, files...)
	return a.do("POST", base+path, body, func(r *http.Request) { r.Header.Set("Content-Type", kind) })
}

func (a *api) blobs(session string, images [][2]any, extra ...any) answer {
	a.t.Helper()
	ext := map[string]string{"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/pdf": "pdf"}
	metas := []map[string]string{}
	var files []any
	for i, im := range images {
		media, data := im[0].(string), im[1].([]byte)
		metas = append(metas, map[string]string{"name": blobName(data, ext[media]), "media_type": media})
		files = append(files, fmt.Sprintf("image%d", i), data)
	}
	meta := map[string]any{"session_id": session, "images": metas}
	for i := 0; i < len(extra); i += 2 {
		meta[extra[i].(string)] = extra[i+1]
	}
	return a.form("/session-blobs", meta, files...)
}

func (a *api) rows(query string, args ...any) []string {
	a.t.Helper()
	rows, err := a.pool.Query(context.Background(), query, args...)
	if err != nil {
		a.t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		values, err := rows.Values()
		if err != nil {
			a.t.Fatal(err)
		}
		out = append(out, fmt.Sprint(values...))
	}
	return out
}

func query(pairs ...string) string {
	v := url.Values{}
	for i := 0; i < len(pairs); i += 2 {
		v.Add(pairs[i], pairs[i+1])
	}
	return "?" + v.Encode()
}

func TestTheBlobRoutesTakeOnlyTheAgentsCredential(t *testing.T) {
	a := newAPI(t, blobAreas)
	path := base + "/session-blobs" + query("session_id", uuid(), "name", blobName(png, "png"))
	none := a.do("GET", path, "", func(r *http.Request) { r.Header.Del("Authorization") })
	expect(t, none.status, 401)
	render := a.credential("render", "crud.render")
	expect(t, a.do("GET", path, "", func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+render) }).status, 403)
	for _, p := range []string{"/attachments/sweeps", "/background-tasks", "/workspace/sweeps"} {
		got := a.do("POST", base+p, "{}", func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+render) })
		expect(t, got.status, 403)
	}
}

func TestANameOrIDThatClimbsIsRefused(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	for _, name := range []string{"../x.png", strings.Repeat("a", 64) + ".png/../x", "x.png", strings.Repeat("A", 64) + ".png"} {
		expect(t, a.do("GET", base+"/session-blobs"+query("session_id", sid, "name", name), "").status, 422)
	}
	expect(t, a.do("GET", base+"/session-blobs"+query("session_id", "../x", "name", blobName(png, "png")), "").status, 422)
	owner := query("owner_kind", "browser", "owner_id", "browser")
	expect(t, a.do("GET", base+"/attachments/not-a-uuid/image"+owner, "").status, 422)
	expect(t, a.do("GET", base+"/http-bodies/bytes"+query("session_id", sid, "id", "../x"), "").status, 422)
}

func TestImagesAreStoredOnceByNameAndReadBack(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	first := a.blobs(sid, [][2]any{{"image/png", png}, {"image/jpeg", jpeg}})
	expect(t, first.body["stored"], true)
	expect(t, first.body["created"], []any{blobName(png, "png"), blobName(jpeg, "jpg")})
	again := a.blobs(sid, [][2]any{{"image/png", png}})
	expect(t, again.body["created"], []any{})
	if again.body["at"].(string) <= first.body["at"].(string) {
		t.Fatalf("at did not move: %s %s", first.body["at"], again.body["at"])
	}
	read := a.do("GET", base+"/session-blobs"+query("session_id", sid, "name", blobName(jpeg, "jpg")), "")
	expect(t, read.status, 200)
	expect(t, read.raw, string(jpeg))
	expect(t, read.header.Get("Content-Type"), "image/jpeg")
	expect(t, read.header.Get("X-ScadBuddy-Crud"), "1")
	expect(t, a.do("GET", base+"/session-blobs"+query("session_id", sid, "name", blobName([]byte("other"), "png")), "").status, 404)
	expect(t, a.blobs(uuid(), [][2]any{{"image/png", png}}).status, 404)
	// pydantic reads an upper-case UUID too.
	expect(t, a.do("GET", base+"/session-blobs"+query("session_id", strings.ToUpper(sid), "name", blobName(png, "png")), "").status, 200)
}

func TestAnImageIsCheckedAgainstItsNameTypeAndCaps(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	meta := func(name, media string) map[string]any {
		return map[string]any{"session_id": sid, "images": []map[string]string{{"name": name, "media_type": media}}}
	}
	expect(t, a.form("/session-blobs", meta(blobName(jpeg, "png"), "image/png"), "image0", png).status, 422)
	expect(t, a.form("/session-blobs", meta(blobName(jpeg, "png"), "image/png"), "image0", jpeg).status, 422)
	expect(t, a.form("/session-blobs", meta(blobName(png, "png"), "image/png"), "image1", png).status, 422)
	many := [][2]any{}
	for i := range 9 {
		many = append(many, [2]any{"image/png", with(png, string(rune('a'+i)))})
	}
	expect(t, a.blobs(sid, many).status, 422)
	big := append(append([]byte{}, png...), make([]byte, 5*1024*1024/4*3)...)
	expect(t, a.blobs(sid, [][2]any{{"image/png", big}}).status, 413)
	stored := a.blobs(sid, [][2]any{{"application/pdf", pdf}})
	expect(t, stored.body["created"], []any{blobName(pdf, "pdf")})
	expect(t, a.do("GET", base+"/session-blobs"+query("session_id", sid, "name", blobName(pdf, "pdf")), "").header.Get("Content-Type"), "application/pdf")
	bigPDF := append(append([]byte{}, pdf...), make([]byte, 8*1024*1024/4*3)...)
	expect(t, a.blobs(sid, [][2]any{{"application/pdf", bigPDF}}).status, 413)
	expect(t, a.rows(`SELECT name FROM ai_session_blobs`), []string{blobName(pdf, "pdf")})
	notForm := a.do("POST", base+"/session-blobs", `{"session_id":"x"}`)
	expect(t, notForm.status, 422)
}

func TestAToolCallsImagesReplaceItsEarlierAttempts(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	expect(t, a.blobs(sid, [][2]any{{"image/png", with(png, "1")}}, "tool_use_id", "toolu_1").status, 200)
	expect(t, a.blobs(sid, [][2]any{{"image/png", with(png, "2")}}, "tool_use_id", "toolu_1").status, 200)
	expect(t, a.rows(`SELECT ord, name FROM ai_session_tool_images ORDER BY ord`),
		[]string{"0" + blobName(with(png, "2"), "png")})
}

func TestAFencedWriteAndDeleteDoNothingOnceTheTurnMoved(t *testing.T) {
	a := newAPI(t, blobAreas)
	turn := uuid()
	sid := a.session(map[string]any{"attempt": "run:1", "turn": turn})
	moved := a.blobs(sid, [][2]any{{"image/png", png}}, "fence", map[string]string{"attempt": "run:2", "turn_id": turn})
	expect(t, moved.raw, `{"stored":false,"created":[],"at":null}`)
	expect(t, len(a.rows(`SELECT 1 FROM ai_session_blobs`)), 0)
	made := a.blobs(sid, [][2]any{{"image/png", png}}, "fence", map[string]string{"attempt": "run:1", "turn_id": turn})
	expect(t, made.body["stored"], true)
	both := a.blobs(sid, [][2]any{{"image/png", png}}, "tool_use_id", "t", "fence", map[string]string{"attempt": "run:1", "turn_id": turn})
	expect(t, both.status, 422)
	name, at := blobName(png, "png"), made.body["at"]
	del := func(extra ...any) answer {
		body := map[string]any{"session_id": sid, "names": []string{name}, "before": at}
		for i := 0; i < len(extra); i += 2 {
			body[extra[i].(string)] = extra[i+1]
		}
		raw, _ := json.Marshal(body)
		return a.do("POST", base+"/session-blobs/deletes", string(raw))
	}
	expect(t, del("fence", map[string]string{"attempt": "run:2", "turn_id": turn}).raw, `{"ran":false,"deleted":[]}`)
	for _, names := range [][]string{{}, {"../x.png"}, {strings.Repeat("A", 64) + ".png"}} {
		expect(t, del("names", names).status, 422)
	}
	expect(t, del("before", nil).status, 422)
	expect(t, del("fence", map[string]string{"attempt": "run:1", "turn_id": turn}).raw, `{"ran":true,"deleted":["`+name+`"]}`)
}

func TestADeleteTakesOnlyWhatItsWriteMadeAndNoWriteStoredSince(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	mine, shared, tool := with(png, "mine"), with(png, "shared"), with(png, "tool")
	names := []string{blobName(mine, "png"), blobName(shared, "png"), blobName(tool, "png")}
	a.blobs(sid, [][2]any{{"image/png", tool}}, "tool_use_id", "toolu_1")
	made := a.blobs(sid, [][2]any{{"image/png", mine}, {"image/png", shared}, {"image/png", tool}})
	expect(t, made.body["created"], []any{names[0], names[1]})
	a.blobs(sid, [][2]any{{"image/png", shared}})
	body := object("session_id", sid, "names", names, "before", made.body["at"])
	expect(t, a.ok("POST", "/session-blobs/deletes", body).raw, `{"ran":true,"deleted":["`+names[0]+`"]}`)
	expect(t, a.ok("POST", "/session-blobs/deletes", body).raw, `{"ran":true,"deleted":[]}`)
}

func TestAForkReadsOnlyTheAncestorsImagesItsEventsName(t *testing.T) {
	a := newAPI(t, blobAreas)
	parent := a.session(nil)
	named, later := with(png, "named"), with(png, "later")
	a.blobs(parent, [][2]any{{"image/png", named}, {"image/png", later}})
	a.sql(`INSERT INTO ai_session_events (session_id, seq, event) VALUES ($1::text::uuid, 1, $2), ($1::text::uuid, 2, $3)`,
		parent, object("type", "user.turn", "images", []any{map[string]string{"name": blobName(named, "png")}}),
		object("type", "user.turn", "images", []any{map[string]string{"name": blobName(later, "png")}}))
	fork := a.session(map[string]any{"parent": parent})
	a.sql(`UPDATE ai_sessions SET fork_event_cut = 1 WHERE id = $1::text::uuid`, fork)
	expect(t, a.do("GET", base+"/session-blobs"+query("session_id", fork, "name", blobName(named, "png")), "").raw, string(named))
	expect(t, a.do("GET", base+"/session-blobs"+query("session_id", fork, "name", blobName(later, "png")), "").status, 404)
}

var browser = map[string]string{"kind": "browser", "id": "browser"}

func (a *api) attach(owner map[string]string, image, preview []byte) answer {
	a.t.Helper()
	meta := map[string]any{"owner": owner, "media_type": "image/png", "preview_media_type": "image/webp"}
	return a.form("/attachments", meta, "image", image, "preview", preview)
}

func TestAnAttachmentIsStagedResolvedReadAndDeletedByItsOwner(t *testing.T) {
	a := newAPI(t, blobAreas)
	made := a.attach(browser, png, webp)
	expect(t, made.status, 201)
	id := made.body["id"].(string)
	expect(t, made.body["name"], blobName(png, "png"))
	expect(t, made.body["preview"], map[string]any{"name": blobName(webp, "webp"), "media_type": "image/webp"})
	ids := object("owner", browser, "ids", []string{id})
	resolved := a.ok("POST", "/attachments/resolve", ids).body["attachments"].([]any)
	expect(t, resolved[0].(map[string]any)["id"], id)
	owner := query("owner_kind", "browser", "owner_id", "browser")
	expect(t, a.do("GET", base+"/attachments/"+id+"/image"+owner, "").raw, string(png))
	preview := a.do("GET", base+"/attachments/"+id+"/preview"+owner, "")
	expect(t, preview.raw, string(webp))
	expect(t, preview.header.Get("Content-Type"), "image/webp")
	other := map[string]string{"kind": "bearer", "id": "token:other"}
	expect(t, a.do("GET", base+"/attachments/"+id+"/image"+query("owner_kind", "bearer", "owner_id", "token:other"), "").status, 404)
	expect(t, a.ok("POST", "/attachments/resolve", object("owner", other, "ids", []string{id})).raw, `{"attachments":[]}`)
	expect(t, a.ok("POST", "/attachments/deletes", object("owner", other, "ids", []string{id})).raw, `{"deleted":0}`)
	expect(t, a.ok("POST", "/attachments/deletes", ids).raw, `{"deleted":1}`)
	expect(t, a.do("GET", base+"/attachments/"+id+"/image"+owner, "").status, 404)
	expect(t, a.do("POST", base+"/attachments/resolve", object("owner", browser, "ids", []string{})).status, 422)
	expect(t, a.do("POST", base+"/attachments/resolve", object("owner", map[string]string{"kind": "root", "id": "x"}, "ids", []string{id})).status, 422)
}

func TestAnAttachmentIsCheckedAndRefusedPastTheOwnersCaps(t *testing.T) {
	a := newAPI(t, blobAreas)
	expect(t, a.attach(browser, jpeg, webp).status, 422)
	expect(t, a.attach(browser, png, png).status, 422)
	expect(t, a.attach(browser, png, append(append([]byte{}, webp...), make([]byte, 48*1024)...)).status, 413)
	for range 24 {
		expect(t, a.attach(browser, png, webp).status, 201)
	}
	full := a.attach(browser, png, webp)
	expect(t, full.status, 409)
	expect(t, full.body["reason"], "quota")
	expect(t, a.attach(map[string]string{"kind": "bearer", "id": "token:other"}, png, webp).status, 201)
}

func TestTheSweepDeletesExpiredAttachments(t *testing.T) {
	a := newAPI(t, blobAreas)
	kept := a.attach(browser, png, webp).body["id"].(string)
	gone := a.attach(browser, png, webp).body["id"].(string)
	a.sql(`UPDATE ai_attachments SET expires_at = now() - interval '1 s' WHERE id = $1::text::uuid`, gone)
	expect(t, a.ok("POST", "/attachments/sweeps", "").raw, `{"deleted":1}`)
	expect(t, a.rows(`SELECT id::text FROM ai_attachments`), []string{kept})
}

func (a *api) saveBody(session, id string, data []byte) answer {
	a.t.Helper()
	meta := map[string]any{"session_id": session, "id": id, "url": "https://example.com/x",
		"content_type": "text/plain", "size_bytes": len(data), "complete": true}
	return a.form("/http-bodies", meta, "body", data)
}

func TestASavedBodyIsReadByRangeAndTheNewestTenAreKept(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid, first := a.session(nil), uuid()
	expect(t, a.saveBody(sid, first, []byte("0123456789")).raw, `{"stored":true}`)
	meta := a.ok("GET", "/http-bodies/meta"+query("session_id", sid, "id", first), "").body
	expect(t, []any{meta["url"], meta["content_type"], meta["size_bytes"], meta["complete"]},
		[]any{"https://example.com/x", "text/plain", 10.0, true})
	read := func(pairs ...string) answer {
		return a.do("GET", base+"/http-bodies/bytes"+query(append([]string{"session_id", sid, "id", first}, pairs...)...), "")
	}
	expect(t, read("offset", "2", "length", "3").raw, "234")
	expect(t, read("offset", "8", "length", "9").raw, "89")
	expect(t, read("offset", "20", "length", "1").raw, "")
	far := read("offset", fmt.Sprint(1<<31-2), "length", "4")
	expect(t, []any{far.status, far.raw}, []any{200, ""})
	all := read()
	expect(t, all.raw, "0123456789")
	expect(t, all.header.Get("Content-Type"), "application/octet-stream")
	expect(t, read("offset", fmt.Sprint(1<<31-1)).status, 422)
	expect(t, a.saveBody(sid, first, []byte("again")).status, 409)
	for range 10 {
		a.saveBody(sid, uuid(), []byte("x"))
	}
	expect(t, a.do("GET", base+"/http-bodies/meta"+query("session_id", sid, "id", first), "").status, 404)
	expect(t, read().status, 404)
	expect(t, len(a.rows(`SELECT 1 FROM ai_session_http_bodies`)), 10)
	expect(t, a.saveBody(uuid(), uuid(), []byte("x")).status, 404)
	expect(t, a.saveBody(sid, uuid(), make([]byte, 20*1024*1024+1)).status, 413)
}

// --- background tasks and operations ---

var taskOwner = map[string]any{"kind": "browser", "id": "browser", "label": "You"}

func newTask(session string, extra ...any) string {
	pairs := append([]any{"session_id", session, "kind", "cron", "description", "every minute",
		"spec", map[string]any{"cron": "* * * * *", "prompt": "hi"}, "created_by", taskOwner}, extra...)
	return object(pairs...)
}

func (a *api) task(session string, extra ...any) map[string]any {
	a.t.Helper()
	task, _ := a.ok("POST", "/background-tasks", newTask(session, extra...)).body["task"].(map[string]any)
	return task
}

func TestATaskIsMadeReadListedAndEndedOnce(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	task := a.task(sid, "next_at", "2026-11-15T08:00:00Z")
	id := task["id"].(string)
	expect(t, []any{task["session_id"], task["kind"], task["state"], task["fired"], task["created_by"], task["next_at"]},
		[]any{sid, "cron", "active", 0.0, taskOwner, "2026-11-15T08:00:00Z"})
	expect(t, a.ok("GET", "/background-tasks/"+id, "").body["task"].(map[string]any)["spec"],
		map[string]any{"cron": "* * * * *", "prompt": "hi"})
	expect(t, a.ok("GET", "/background-tasks/0000abcd", "").raw, `{"task":null}`)
	a.ok("POST", "/background-tasks/"+id+"/scheduled", `{"at":"2026-11-15T09:00:00.5+01:00"}`)
	a.ok("POST", "/background-tasks/"+id+"/fired", `{"at":"2026-11-15T09:00:01Z"}`)
	a.ok("POST", "/background-tasks/"+id+"/progress", `{"detail":"phase 2"}`)
	a.ok("POST", "/background-tasks/"+id+"/spec", `{"spec":{"scriptPath":"a.js"}}`)
	got := a.ok("GET", "/background-tasks/"+id, "").body["task"].(map[string]any)
	expect(t, []any{got["fired"], got["next_at"], got["last_fired_at"], got["detail"], got["spec"]},
		[]any{1.0, nil, "2026-11-15T09:00:01Z", "phase 2", map[string]any{"scriptPath": "a.js"}})
	end := `{"state":"cancelled","detail":"cancelled by You"}`
	expect(t, a.ok("POST", "/background-tasks/"+id+"/finish", end).raw, `{"finished":true}`)
	expect(t, a.ok("POST", "/background-tasks/"+id+"/finish", end).raw, `{"finished":false}`)
	a.ok("POST", "/background-tasks/"+id+"/progress", `{"detail":"late"}`)
	ended := a.ok("GET", "/background-tasks/"+id, "").body["task"].(map[string]any)
	expect(t, []any{ended["state"], ended["detail"]}, []any{"cancelled", "cancelled by You"})
	expect(t, a.ok("GET", "/background-tasks"+query("session_id", sid), "").raw, `{"tasks":[]}`)
	expect(t, len(a.ok("GET", "/background-tasks"+query("session_id", sid, "all", "true"), "").body["tasks"].([]any)), 1)
}

func TestConcurrentCreatesNeverPassTheCap(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	var wg sync.WaitGroup
	made := make(chan bool, 15)
	for range 15 {
		wg.Go(func() {
			made <- a.do("POST", base+"/background-tasks", newTask(sid)).body["task"] != nil
		})
	}
	wg.Wait()
	close(made)
	n := 0
	for ok := range made {
		if ok {
			n++
		}
	}
	expect(t, n, 10)
	expect(t, a.rows(`SELECT count(*) FROM ai_background_tasks`), []string{"10"})
}

func TestTheRecurringCronsAreListed(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	recurring := a.task(sid)["id"]
	a.task(sid, "spec", map[string]any{"cron": "0 9 * * *", "recurring": false})
	a.task(sid, "kind", "wakeup", "spec", map[string]any{"prompt": "x"})
	listed := a.ok("GET", "/background-tasks/recurring-crons", "").body["tasks"].([]any)
	expect(t, len(listed), 1)
	expect(t, listed[0].(map[string]any)["id"], recurring)
}

func TestAMonitorsEventsAreCappedPagedAndDelivered(t *testing.T) {
	a := newAPI(t, blobAreas)
	path := "/background-tasks/" + a.task(a.session(nil), "kind", "monitor")["id"].(string)
	for n := range 200 {
		expect(t, a.ok("POST", path+"/events", object("text", fmt.Sprintf("line %d", n))).raw, `{"appended":true}`)
	}
	expect(t, a.ok("POST", path+"/events", `{"text":"over"}`).raw, `{"appended":false}`)
	expect(t, a.ok("POST", path+"/events", `{"text":"the end","notice":true}`).raw, `{"appended":true}`)
	expect(t, a.ok("GET", path+"/events/count", "").raw, `{"count":201}`)
	first := a.ok("GET", path+"/events/pending?max_chars=20", "").body["events"].([]any)
	texts := func(events []any) (out []string) {
		for _, e := range events {
			out = append(out, e.(map[string]any)["text"].(string))
		}
		return out
	}
	expect(t, texts(first), []string{"line 0", "line 1", "line 2"})
	expect(t, texts(a.ok("GET", path+"/events/pending?max_chars=1", "").body["events"].([]any)), []string{"line 0"})
	a.ok("POST", path+"/events/delivered", object("up_to", first[2].(map[string]any)["seq"]))
	expect(t, a.ok("GET", path+"/events/count?pending=1", "").raw, `{"count":198}`)
	rest := texts(a.ok("GET", path+"/events/pending", "").body["events"].([]any))
	expect(t, []string{rest[0], rest[len(rest)-1]}, []string{"line 3", "the end"})
}

func TestAWorkflowsJournalAndOpenChildren(t *testing.T) {
	a := newAPI(t, blobAreas)
	key := strings.Repeat("ab", 32)
	path := "/background-tasks/" + a.task(a.session(nil), "kind", "workflow")["id"].(string)
	expect(t, a.ok("GET", path+"/calls"+query("key", key, "occurrence", "0"), "").raw, `{"call":null}`)
	journalled, done, open := a.session(nil), a.session(nil), a.session(nil)
	a.sql(`UPDATE ai_sessions SET status = 'done' WHERE id = $1::text::uuid`, done)
	a.ok("POST", path+"/calls", object("key", key, "occurrence", 0, "result", nil, "ok", true))
	a.ok("POST", path+"/calls", object("key", key, "occurrence", 1, "result", map[string]any{"a": 1}, "ok", false, "child_id", journalled))
	a.ok("POST", path+"/calls", object("key", key, "occurrence", 1, "result", "second", "ok", true))
	expect(t, a.ok("GET", path+"/calls"+query("key", key, "occurrence", "0"), "").raw, `{"call":{"result":null,"ok":true}}`)
	expect(t, a.ok("GET", path+"/calls"+query("key", key, "occurrence", "1"), "").raw, `{"call":{"result":{"a":1},"ok":false}}`)
	a.sql(`INSERT INTO ai_workflow_calls (task_id, call_key, occurrence, result, ok) VALUES ($1, $2, 2, '{}', true)`,
		strings.TrimPrefix(path, "/background-tasks/"), key)
	expect(t, a.ok("GET", path+"/calls"+query("key", key, "occurrence", "2"), "").raw, `{"call":{"result":null,"ok":true}}`)
	for _, child := range []string{journalled, done, open, open} {
		a.ok("POST", path+"/children", object("child_id", child))
	}
	expect(t, a.ok("GET", path+"/children/open", "").raw, `{"children":["`+open+`"]}`)
}

func TestBadTaskBodiesAreRefused(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	for _, body := range []string{
		newTask("not-a-uuid"), newTask(sid, "kind", "daemon"), newTask(sid, "description", ""),
		newTask(sid, "created_by", map[string]any{"kind": "root", "id": "x", "label": "y"}),
		newTask(sid, "spec", []int{1}), newTask(sid, "next_at", "soon"),
	} {
		expect(t, a.do("POST", base+"/background-tasks", body).status, 422)
	}
	expect(t, a.do("GET", base+"/background-tasks/NOT-HEX", "").status, 422)
	expect(t, a.do("POST", base+"/background-tasks/0000abcd/events", object("text", strings.Repeat("x", 16385))).status, 422)
	expect(t, a.do("POST", base+"/background-tasks/0000abcd/finish", `{"state":"active"}`).status, 422)
	expect(t, a.do("POST", base+"/background-tasks/0000abcd/calls", `{"key":"short","occurrence":0,"ok":true}`).status, 422)
	expect(t, a.do("POST", base+"/background-tasks/0000abcd/events/delivered", `{"up_to":"x"}`).status, 422)
	expect(t, a.do("GET", base+"/background-tasks/0000abcd/calls?key="+strings.Repeat("ab", 32), "").status, 422)
	expect(t, a.do("GET", base+"/background-tasks"+query("session_id", sid, "all", "maybe"), "").status, 422)
	expect(t, a.rows(`SELECT id FROM ai_background_tasks`), []string(nil))
}

func TestAnOperationIsReadByIDAndByKey(t *testing.T) {
	a := newAPI(t, blobAreas)
	key := strings.Repeat("ab", 32)
	a.sql(`INSERT INTO ai_operations (id, kind, subject, operation_key, status, request, workflow_id,
		workflow_run_id, result, finished_at) VALUES ('o1', 'plugin_package_install', 'hindsight', $1,
		'succeeded', '{"source":"x"}', 'agent-op-o1', 'run1', '{"ok": true}', now())`, key)
	got := a.ok("GET", "/operations/o1", "").body["operation"].(map[string]any)
	expect(t, []any{got["id"], got["status"], got["result"], got["error"], got["workflow_run_id"]},
		[]any{"o1", "succeeded", map[string]any{"ok": true}, nil, "run1"})
	expect(t, a.ok("GET", "/operations/by-key/"+key, "").body["operation"].(map[string]any)["id"], "o1")
	expect(t, a.ok("GET", "/operations/none", "").raw, `{"operation":null}`)
	expect(t, a.ok("GET", "/operations/by-key/"+strings.Repeat("cd", 32), "").raw, `{"operation":null}`)
	expect(t, a.do("GET", base+"/operations/by-key/short", "").status, 422)
	// The insert and the end write an activity row: still Python's.
	expect(t, a.do("POST", base+"/operations", "{}").status, http.StatusTeapot)
}

// --- workspace ---

// file puts a file kept in Postgres, its bytes in a row of their own.
func (a *api) file(workspace, path string, data []byte) {
	a.t.Helper()
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	a.sql(`INSERT INTO ai_session_file_bytes (sha256, data) VALUES ($1, $2) ON CONFLICT DO NOTHING`, sha, data)
	var session any
	if id, ok := strings.CutPrefix(workspace, "session:"); ok {
		session = id
	}
	a.sql(`INSERT INTO ai_session_files (workspace, session_id, path, media_type, bytes_sha256, sha256, size, source)
		VALUES ($1, $2::text::uuid, $3, 'text/plain', $4, $4, $5, '{"tool": "test"}')`,
		workspace, session, path, sha, len(data))
}

func TestTheWorkspaceIsListedAndRead(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	ws := "session:" + sid
	a.file(ws, "a/b.txt", []byte("hello"))
	a.file(ws, "c.txt", []byte("hi"))
	listed := a.ok("GET", "/workspace/files"+query("workspace", ws), "").body["files"].([]any)
	expect(t, len(listed), 2)
	first := listed[0].(map[string]any)
	expect(t, []any{first["path"], first["media_type"], first["size"], first["source"]},
		[]any{"a/b.txt", "text/plain", 5.0, map[string]any{"tool": "test"}})
	expect(t, len(a.ok("GET", "/workspace/files"+query("workspace", ws, "prefix", "a//"), "").body["files"].([]any)), 1)
	expect(t, a.ok("GET", "/workspace/file"+query("workspace", ws, "path", "./c.txt"), "").body["file"].(map[string]any)["size"], 2.0)
	expect(t, a.ok("GET", "/workspace/file"+query("workspace", ws, "path", "none"), "").raw, `{"file":null}`)
	expect(t, a.ok("GET", "/workspace/usage"+query("workspace", ws), "").raw, `{"files":2,"bytes":7}`)
	sessions := a.ok("POST", "/workspace/session-files", object("session_ids", []string{sid, uuid()})).body["sessions"].(map[string]any)
	expect(t, len(sessions), 1)
	expect(t, sessions[sid].([]any)[0].(map[string]any)["sealed"], false)
	expect(t, a.ok("POST", "/workspace/session-files", `{"session_ids":[]}`).raw, `{"sessions":{}}`)

	for _, q := range []string{
		query("workspace", "session:../x"), query("workspace", "principal:root:x"), query("workspace", ""),
	} {
		got := a.do("GET", base+"/workspace/files"+q, "")
		expect(t, got.status, 422)
	}
	bad := a.do("GET", base+"/workspace/file"+query("workspace", ws, "path", "../é"), "")
	expect(t, bad.body["reason"], "invalid_path")
	expect(t, strings.SplitAfter(bad.body["detail"].(string), " is not")[0], "\"../\\u00e9\" is not")
	expect(t, a.do("POST", base+"/workspace/session-files", `{"session_ids":["X"]}`).status, 422)
}

func TestAWorkspaceDeleteChecksItsShaAndLogsItsEvents(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	ws := "session:" + sid
	a.file(ws, "a.txt", []byte("one"))
	a.file(ws, "b.txt", []byte("one"))
	event := object("type", "workspace.file", "sessionId", sid, "path", "a.txt")
	conflict := a.do("POST", base+"/workspace/file-deletes", object("workspace", ws, "path", "a.txt", "expected_sha256", strings.Repeat("0", 64)))
	expect(t, []any{conflict.status, conflict.body["reason"]}, []any{409, "conflict"})
	other := object("type", "workspace.file", "sessionId", uuid())
	expect(t, a.do("POST", base+"/workspace/file-deletes", object("workspace", ws, "path", "a.txt", "events", []string{other})).status, 422)
	expect(t, a.do("POST", base+"/workspace/file-deletes", object("workspace", "principal:flow:x", "path", "a.txt", "events", []string{event})).status, 422)
	expect(t, a.ok("POST", "/workspace/file-deletes", object("workspace", ws, "path", "a.txt", "events", []string{event})).raw,
		`{"deleted":true,"seqs":[1]}`)
	expect(t, len(a.rows(`SELECT 1 FROM ai_session_file_bytes`)), 1)
	expect(t, a.ok("POST", "/workspace/file-deletes", object("workspace", ws, "path", "b.txt")).raw, `{"deleted":true,"seqs":[]}`)
	expect(t, len(a.rows(`SELECT 1 FROM ai_session_file_bytes`)), 0)
	expect(t, a.ok("POST", "/workspace/file-deletes", object("workspace", ws, "path", "b.txt")).raw, `{"deleted":false,"seqs":[]}`)
}

func TestTheWorkspaceSweepAndReconcile(t *testing.T) {
	a := newAPI(t, blobAreas)
	sid := a.session(nil)
	a.file("principal:bearer:token:1", "old", []byte("x"))
	a.file("session:"+sid, "kept", []byte("y"))
	a.sql(`UPDATE ai_session_files SET updated_at = now() - interval '2 days'`)
	expect(t, a.do("POST", base+"/workspace/sweeps", "").status, 422)
	expect(t, a.ok("POST", "/workspace/sweeps", "{}").raw, `{"swept":1}`)
	expect(t, a.rows(`SELECT path FROM ai_session_files`), []string{"kept"})
	expect(t, a.rows(`SELECT count(*) FROM ai_session_file_bytes`), []string{"1"})

	blob := "workspace-" + strings.Repeat("2", 64)
	a.sql(`INSERT INTO ai_session_files (workspace, session_id, path, media_type, blob_key, key_sealed, sha256, size)
		VALUES ($1, $2::text::uuid, 'a', 'text/plain', $3, '\x00', $4, 1)`, "session:"+sid, sid, blob, strings.Repeat("3", 64))
	orphan := "workspace-" + strings.Repeat("1", 64)
	a.sql(`INSERT INTO blob_refs (key, holder_kind, holder_id, created_at) VALUES
		($1, 'ai_session_file', 'session:gone/x', now() - interval '2 hours'),
		($1, 'ai_session_file', 'session:fresh/x', now())`, orphan)
	expect(t, a.ok("POST", "/workspace/reconciles", `{"grace_seconds":3600}`).raw, `{"released":1,"held":1,"skipped":null}`)
	expect(t, a.rows(`SELECT key, holder_id FROM blob_refs ORDER BY key`),
		[]string{orphan + "session:fresh/x", blob + "session:" + sid + "/a"})
	tx, err := a.pool.Begin(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(context.Background())
	if _, err := tx.Exec(context.Background(), `SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:ai_session_files:reconcile', 0))`); err != nil {
		t.Fatal(err)
	}
	expect(t, a.ok("POST", "/workspace/reconciles", "{}").raw, `{"released":0,"held":0,"skipped":"held"}`)
}
