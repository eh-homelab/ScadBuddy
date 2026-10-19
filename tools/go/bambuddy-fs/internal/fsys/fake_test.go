package fsys

import (
	"context"
	"io"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
)

// fakeLibrary is an in-memory Library. Without all, List shows only .3mf files, as
// the library dialog does.
type fakeLibrary struct {
	mu        sync.Mutex
	folders   []api.Folder
	files     map[int]api.File
	data      map[int][]byte
	lists     int
	downloads int

	nextID        int
	uploads       int
	updates       []update
	deleted       map[int]bool
	folderDeleted map[int]bool
	// busy files are refused by a delete, as a queued print's file is.
	busy map[int]bool
	// failUploads is how many uploads fail, with failStatus (else 502), before they
	// succeed; while down is set every upload fails so.
	failUploads int
	failStatus  int
	// failUpstream is the Bambuddy status a failing upload's 502 relays, as ScadBuddy's does (#4142).
	failUpstream int
	down         bool
	// listDown makes every List fail with a 503, as ScadBuddy does during a deploy.
	listDown bool
	// failUpdates is how many UpdateFile calls fail, with a 502; updating, if set, is
	// called at the start of each with its count.
	failUpdates int
	updating    func(n int)
	updateCalls int
	// uploading, if set, is called at the start of each upload with its attempt number.
	uploading func(attempt int)
	attempts  int
	// loseAnswers is how many uploads succeed but answer a 502, as when a deploy cuts
	// the connection after the upload ran. keyed is the file each Idempotency-Key, with
	// its request, made, as answered then: sent again, the route answers it without
	// uploading again, even if that file has since been deleted or renamed.
	loseAnswers int
	keyed       map[string]api.File
	keys        []string
}

// update is one UpdateFile or UpdateFolder call, as sent.
type update struct {
	folder bool
	id     int
	name   *string
	parent *int
}

func newFakeLibrary() *fakeLibrary {
	return &fakeLibrary{
		files: map[int]api.File{}, data: map[int][]byte{}, nextID: 100,
		deleted: map[int]bool{}, folderDeleted: map[int]bool{}, busy: map[int]bool{}, keyed: map[string]api.File{},
	}
}

func (l *fakeLibrary) folder(id int, name string, parent int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.folders = append(l.folders, api.Folder{ID: id, Name: name, ParentID: parent})
}

func (l *fakeLibrary) file(id int, name string, folder int, data []byte) {
	l.mu.Lock()
	defer l.mu.Unlock()
	created := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	l.files[id] = api.File{ID: id, Name: name, FolderID: folder, Size: int64(len(data)), Created: created}
	l.data[id] = data
}

// setDown makes every upload fail, or none.
func (l *fakeLibrary) setDown(down bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.down = down
}

// setAllDown makes every upload and listing fail with a 503, or none.
func (l *fakeLibrary) setAllDown(down bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.down, l.listDown = down, down
	l.failStatus = http.StatusServiceUnavailable
}

func (l *fakeLibrary) remove(id int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.files, id)
	delete(l.data, id)
}

// locked runs f under the fake's lock, so a test reads what the server's goroutines wrote.
func (l *fakeLibrary) locked(f func()) {
	l.mu.Lock()
	defer l.mu.Unlock()
	f()
}

func (l *fakeLibrary) counts() (lists, downloads int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lists, l.downloads
}

func (l *fakeLibrary) List(_ context.Context, folderID int, all bool) (api.Listing, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.lists++
	if l.listDown {
		return api.Listing{}, &api.Error{Op: "list", Status: http.StatusServiceUnavailable}
	}
	out := api.Listing{Folders: append([]api.Folder(nil), l.folders...)}
	for _, f := range l.files {
		if f.FolderID == folderID && (all || strings.HasSuffix(f.Name, ".3mf")) {
			out.Files = append(out.Files, f)
		}
	}
	return out, nil
}

func (l *fakeLibrary) Download(_ context.Context, fileID int, w io.Writer) error {
	l.mu.Lock()
	data, ok := l.data[fileID]
	l.downloads++
	l.mu.Unlock()
	if !ok {
		return &api.Error{Op: "download", Status: http.StatusNotFound}
	}
	_, err := w.Write(data)
	return err
}

func (l *fakeLibrary) Upload(_ context.Context, folderID int, name, key string, r io.Reader) (api.File, error) {
	l.mu.Lock()
	l.attempts++
	hook, attempt := l.uploading, l.attempts
	l.mu.Unlock()
	if hook != nil {
		hook(attempt)
	}
	data, err := io.ReadAll(r)
	if err != nil {
		return api.File{}, err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.keys = append(l.keys, key)
	request := key + "\x00" + strconv.Itoa(folderID) + "\x00" + name + "\x00" + string(data)
	if f, ok := l.keyed[request]; ok && key != "" {
		return f, nil
	}
	if l.down || l.failUploads > 0 {
		if !l.down {
			l.failUploads--
		}
		status := l.failStatus
		if status == 0 {
			status = http.StatusBadGateway
		}
		return api.File{}, &api.Error{Op: "upload", Status: status, Upstream: l.failUpstream}
	}
	if folderID != 0 && !slices.ContainsFunc(l.folders, func(f api.Folder) bool { return f.ID == folderID }) {
		// As ScadBuddy relays it: Bambuddy's 404 named beside its own.
		return api.File{}, &api.Error{Op: "upload", Status: http.StatusNotFound, Detail: "folder not found", Upstream: http.StatusNotFound}
	}
	l.uploads++
	l.nextID++
	f := api.File{ID: l.nextID, Name: name, FolderID: folderID, Size: int64(len(data)), Created: time.Now().UTC()}
	l.files[f.ID] = f
	l.data[f.ID] = data
	l.keyed[request] = f
	if l.loseAnswers > 0 {
		l.loseAnswers--
		return api.File{}, &api.Error{Op: "upload", Status: http.StatusBadGateway}
	}
	return f, nil
}

func (l *fakeLibrary) UpdateFile(_ context.Context, fileID int, name *string, folderID *int) (api.File, error) {
	l.mu.Lock()
	l.updateCalls++
	hook, calls := l.updating, l.updateCalls
	l.mu.Unlock()
	if hook != nil {
		hook(calls)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.updates = append(l.updates, update{id: fileID, name: name, parent: folderID})
	if l.failUpdates > 0 {
		l.failUpdates--
		return api.File{}, &api.Error{Op: "update file", Status: http.StatusBadGateway}
	}
	f, ok := l.files[fileID]
	if !ok {
		return api.File{}, &api.Error{Op: "update file", Status: http.StatusNotFound}
	}
	if folderID != nil && *folderID != 0 && !slices.ContainsFunc(l.folders, func(f api.Folder) bool { return f.ID == *folderID }) {
		return api.File{}, &api.Error{Op: "update file", Status: http.StatusNotFound, Detail: "Folder not found"}
	}
	if name != nil {
		f.Name = *name
	}
	if folderID != nil {
		f.FolderID = *folderID
	}
	l.files[fileID] = f
	// The route answers only the ID, name and folder.
	return api.File{ID: f.ID, Name: f.Name, FolderID: f.FolderID}, nil
}

func (l *fakeLibrary) DeleteFiles(_ context.Context, ids ...int) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, id := range ids {
		if l.busy[id] {
			return &api.Error{Op: "delete", Status: http.StatusConflict, Detail: "queued for printing"}
		}
		if _, ok := l.files[id]; !ok {
			return &api.Error{Op: "delete", Status: http.StatusNotFound}
		}
	}
	for _, id := range ids {
		delete(l.files, id)
		delete(l.data, id)
		l.deleted[id] = true
	}
	return nil
}

func (l *fakeLibrary) Mkdir(_ context.Context, parentID int, name string) (api.Folder, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.nextID++
	f := api.Folder{ID: l.nextID, Name: name, ParentID: parentID}
	l.folders = append(l.folders, f)
	return f, nil
}

func (l *fakeLibrary) UpdateFolder(_ context.Context, folderID int, name *string, parentID *int) (api.Folder, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.updates = append(l.updates, update{folder: true, id: folderID, name: name, parent: parentID})
	i := slices.IndexFunc(l.folders, func(f api.Folder) bool { return f.ID == folderID })
	if i < 0 {
		return api.Folder{}, &api.Error{Op: "update folder", Status: http.StatusNotFound}
	}
	if name != nil {
		l.folders[i].Name = *name
	}
	if parentID != nil {
		l.folders[i].ParentID = *parentID
	}
	return l.folders[i], nil
}

// DeleteFolder deletes a folder with everything under it, or nothing if any file
// under it is busy (spec §5).
func (l *fakeLibrary) DeleteFolder(_ context.Context, folderID int) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	tree := map[int]bool{folderID: true}
	for grew := true; grew; {
		grew = false
		for _, f := range l.folders {
			if tree[f.ParentID] && !tree[f.ID] {
				tree[f.ID], grew = true, true
			}
		}
	}
	for id, f := range l.files {
		if tree[f.FolderID] && l.busy[id] {
			return &api.Error{Op: "rmdir", Status: http.StatusConflict, Detail: f.Name + ": queued for printing"}
		}
	}
	for id, f := range l.files {
		if tree[f.FolderID] {
			delete(l.files, id)
			delete(l.data, id)
			l.deleted[id] = true
		}
	}
	l.folders = slices.DeleteFunc(l.folders, func(f api.Folder) bool { return tree[f.ID] })
	for id := range tree {
		l.folderDeleted[id] = true
	}
	return nil
}
