package api

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeFolder struct {
	id, parent int
	name       string
}

type fakeFile struct {
	id, folder int
	name       string
	data       []byte
	external   bool
}

type fakeOp struct {
	polls  int
	status int
	body   any
}

// fake is an in-memory ScadBuddy behind the routes the mount calls.
type fake struct {
	t   *testing.T
	srv *httptest.Server

	mu      sync.Mutex
	folders map[int]*fakeFolder
	files   map[int]*fakeFile
	// queued files refuse a delete with 409, as a file waiting to print does.
	queued map[int]bool
	// skip makes a delete report the file in `skipped` with this reason.
	skip map[int]string
	// slow names the writes ("mkdir", "update_file", "update_folder", "delete",
	// "delete_folder", "upload") that answer 202 and finish on the second poll.
	slow map[string]bool
	// keep makes a folder delete keep the folder without naming anything in skipped.
	keep map[int]bool
	// failPolls answers that many operation polls with 503; lostOps answers them 404.
	failPolls      int
	lostOps        bool
	ops            map[string]*fakeOp
	operationPolls int
	nextID         int
	lastKey        string
	lastBody       map[string]any
}

func newFake(t *testing.T) *fake {
	f := &fake{
		t:       t,
		folders: map[int]*fakeFolder{},
		files:   map[int]*fakeFile{},
		queued:  map[int]bool{},
		skip:    map[int]string{},
		slow:    map[string]bool{},
		keep:    map[int]bool{},
		ops:     map[string]*fakeOp{},
		nextID:  100,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/print/library", f.list)
	mux.HandleFunc("GET /api/v1/print/library/{id}/file", f.download)
	mux.HandleFunc("PATCH /api/v1/print/library/{id}", f.write("update_file", f.updateFile))
	mux.HandleFunc("POST /api/v1/print/library/delete", f.write("delete", f.deleteFiles))
	mux.HandleFunc("POST /api/v1/print/library/files", f.write("upload", f.upload))
	mux.HandleFunc("POST /api/v1/print/library/folders", f.write("mkdir", f.mkdir))
	mux.HandleFunc("PATCH /api/v1/print/library/folders/{id}", f.write("update_folder", f.updateFolder))
	mux.HandleFunc("POST /api/v1/print/library/folders/{id}/delete", f.write("delete_folder", f.deleteFolder))
	mux.HandleFunc("GET /api/v1/operations/{id}", f.operation)
	f.srv = httptest.NewServer(mux)
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fake) client(t *testing.T) *Client {
	c, err := New(f.srv.URL+"/api/v1", f.srv.Client())
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func (f *fake) folder(id int, name string, parent int) {
	f.folders[id] = &fakeFolder{id: id, name: name, parent: parent}
}

func (f *fake) file(id int, name string, folder int, data []byte, external bool) {
	f.files[id] = &fakeFile{id: id, name: name, folder: folder, data: data, external: external}
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func problem(status int, detail string) (int, any) {
	return status, map[string]any{"type": "about:blank", "title": http.StatusText(status), "status": status, "detail": detail}
}

func writeProblem(w http.ResponseWriter, status int, detail string) {
	status, body := problem(status, detail)
	writeJSON(w, status, body)
}

func pathID(r *http.Request) int {
	id, _ := strconv.Atoi(r.PathValue("id"))
	return id
}

// write wraps a write route: it requires an Idempotency-Key and, for a slow kind,
// answers 202 with an operation that finishes on its second poll.
func (f *fake) write(kind string, handle func(*http.Request) (int, any)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		key := r.Header.Get("Idempotency-Key")
		if key == "" {
			writeProblem(w, 428, "Idempotency-Key is required")
			return
		}
		f.lastKey = key
		status, body := handle(r)
		if !f.slow[kind] {
			writeJSON(w, status, body)
			return
		}
		id := fmt.Sprintf("op%d", len(f.ops)+1)
		f.ops[id] = &fakeOp{status: status, body: body}
		writeJSON(w, http.StatusAccepted, map[string]any{
			"id": id, "kind": kind, "subject": "library", "status": "running",
			"created_at": time.Now().UTC().Format(time.RFC3339), "finished_at": nil,
			"result": nil, "error": nil,
		})
	}
}

func (f *fake) operation(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failPolls > 0 {
		f.failPolls--
		f.operationPolls++
		writeProblem(w, 503, "the database is restarting")
		return
	}
	op, ok := f.ops[r.PathValue("id")]
	if !ok || f.lostOps {
		writeProblem(w, 404, "no such operation")
		return
	}
	op.polls++
	f.operationPolls++
	out := map[string]any{
		"id": r.PathValue("id"), "kind": "library", "subject": "library", "status": "running",
		"created_at": time.Now().UTC().Format(time.RFC3339), "result": nil, "error": nil,
	}
	switch {
	case op.polls < 2:
	case op.status < 300:
		out["status"], out["result"] = "succeeded", op.body
	default:
		body := op.body.(map[string]any)
		out["status"] = "failed"
		out["error"] = map[string]any{
			"type": "about:blank", "title": body["title"], "status": op.status,
			"detail": body["detail"], "extensions": map[string]any{},
		}
	}
	writeJSON(w, http.StatusOK, out)
}

func (f *fake) list(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	folderID, _ := strconv.Atoi(r.URL.Query().Get("folder_id"))
	all := r.URL.Query().Get("all") == "true"
	folders := []map[string]any{}
	for _, d := range f.sortedFolders() {
		var parent any
		if d.parent != 0 {
			parent = d.parent
		}
		folders = append(folders, map[string]any{"id": d.id, "name": d.name, "parent_id": parent, "depth": 0, "file_count": nil})
	}
	files := []map[string]any{}
	hidden := 0
	for _, fl := range f.sortedFiles() {
		if fl.folder != folderID {
			continue
		}
		if !all && !strings.HasSuffix(fl.name, ".3mf") {
			hidden++
			continue
		}
		var folder any
		if fl.folder != 0 {
			folder = fl.folder
		}
		files = append(files, map[string]any{
			"id": fl.id, "filename": fl.name, "file_type": "3mf", "folder_id": folder,
			"file_size": len(fl.data), "created_at": "2026-10-01T12:00:00Z",
			"is_external": fl.external, "output_id": nil, "has_thumbnail": false,
			"printable": true, "print_count": 0,
		})
	}
	var folderOut any
	if folderID != 0 {
		folderOut = folderID
	}
	writeJSON(w, http.StatusOK, map[string]any{"folder_id": folderOut, "all": all, "hidden": hidden, "folders": folders, "files": files})
}

func (f *fake) sortedFolders() []*fakeFolder {
	out := make([]*fakeFolder, 0, len(f.folders))
	for _, d := range f.folders {
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].id < out[j].id })
	return out
}

func (f *fake) sortedFiles() []*fakeFile {
	out := make([]*fakeFile, 0, len(f.files))
	for _, fl := range f.files {
		out = append(out, fl)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].id < out[j].id })
	return out
}

func (f *fake) download(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	fl, ok := f.files[pathID(r)]
	f.mu.Unlock()
	if !ok {
		writeProblem(w, 404, "no such file")
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	_, _ = w.Write(fl.data)
}

func (f *fake) decode(r *http.Request) map[string]any {
	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		f.t.Errorf("decode %s %s: %v", r.Method, r.URL.Path, err)
	}
	f.lastBody = body
	return body
}

func entryUpdated(fl *fakeFile) map[string]any {
	var folder any
	if fl.folder != 0 {
		folder = fl.folder
	}
	return map[string]any{"id": fl.id, "filename": fl.name, "folder_id": folder}
}

func folderBody(d *fakeFolder) map[string]any {
	var parent any
	if d.parent != 0 {
		parent = d.parent
	}
	return map[string]any{"id": d.id, "name": d.name, "parent_id": parent}
}

func (f *fake) updateFile(r *http.Request) (int, any) {
	body := f.decode(r)
	fl, ok := f.files[pathID(r)]
	if !ok {
		return problem(404, "no such file")
	}
	if name, ok := body["filename"].(string); ok {
		fl.name = name
	}
	if folder, ok := body["folder_id"].(float64); ok {
		fl.folder = int(folder)
	}
	return http.StatusOK, entryUpdated(fl)
}

func (f *fake) deleteIDs(ids []int) (deleted, skipped []map[string]any, refused bool) {
	for _, id := range ids {
		if f.queued[id] {
			return nil, nil, true
		}
	}
	for _, id := range ids {
		fl, ok := f.files[id]
		switch {
		case !ok:
			skipped = append(skipped, map[string]any{"id": id, "filename": nil, "reason": "not found"})
		case f.skip[id] != "":
			skipped = append(skipped, map[string]any{"id": id, "filename": fl.name, "reason": f.skip[id]})
		default:
			delete(f.files, id)
			deleted = append(deleted, map[string]any{"id": id, "filename": fl.name, "trashed": !fl.external})
		}
	}
	return deleted, skipped, false
}

func (f *fake) deleteFiles(r *http.Request) (int, any) {
	body := f.decode(r)
	var ids []int
	for _, v := range body["file_ids"].([]any) {
		ids = append(ids, int(v.(float64)))
	}
	deleted, skipped, refused := f.deleteIDs(ids)
	if refused {
		return problem(409, "a file is waiting to print")
	}
	return http.StatusOK, map[string]any{"deleted": orEmpty(deleted), "skipped": orEmpty(skipped)}
}

func orEmpty(v []map[string]any) []map[string]any {
	if v == nil {
		return []map[string]any{}
	}
	return v
}

func (f *fake) upload(r *http.Request) (int, any) {
	file, header, err := r.FormFile("file")
	if err != nil {
		f.t.Errorf("upload: %v", err)
		return problem(422, "no file part")
	}
	defer file.Close()
	data, err := io.ReadAll(file)
	if err != nil {
		f.t.Errorf("upload: %v", err)
	}
	folder, _ := strconv.Atoi(r.FormValue("folder_id"))
	f.nextID++
	fl := &fakeFile{id: f.nextID, name: header.Filename, folder: folder, data: data}
	f.files[fl.id] = fl
	return http.StatusOK, entryUpdated(fl)
}

func (f *fake) mkdir(r *http.Request) (int, any) {
	body := f.decode(r)
	parent := 0
	if p, ok := body["parent_id"].(float64); ok {
		parent = int(p)
	}
	f.nextID++
	d := &fakeFolder{id: f.nextID, name: body["name"].(string), parent: parent}
	f.folders[d.id] = d
	return http.StatusOK, folderBody(d)
}

func (f *fake) updateFolder(r *http.Request) (int, any) {
	body := f.decode(r)
	d, ok := f.folders[pathID(r)]
	if !ok {
		return problem(404, "no such folder")
	}
	if name, ok := body["name"].(string); ok {
		d.name = name
	}
	if parent, ok := body["parent_id"].(float64); ok {
		d.parent = int(parent)
	}
	return http.StatusOK, folderBody(d)
}

func (f *fake) deleteFolder(r *http.Request) (int, any) {
	id := pathID(r)
	if _, ok := f.folders[id]; !ok {
		return problem(404, "no such folder")
	}
	var ids []int
	for _, fl := range f.sortedFiles() {
		if fl.folder == id {
			ids = append(ids, fl.id)
		}
	}
	deleted, skipped, refused := f.deleteIDs(ids)
	if refused {
		return problem(409, "a file is waiting to print")
	}
	folders := []int{}
	if len(skipped) == 0 && !f.keep[id] {
		delete(f.folders, id)
		folders = append(folders, id)
	}
	return http.StatusOK, map[string]any{"deleted": orEmpty(deleted), "skipped": orEmpty(skipped), "folders": folders}
}
