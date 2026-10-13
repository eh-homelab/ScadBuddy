package fsys

import (
	"context"
	"io"
	"net/http"
	"slices"
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
	// failUploads is how many uploads fail, with a 502, before they succeed.
	failUploads int
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
		deleted: map[int]bool{}, folderDeleted: map[int]bool{}, busy: map[int]bool{},
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

func (l *fakeLibrary) Upload(_ context.Context, folderID int, name string, r io.Reader) (api.File, error) {
	data, err := io.ReadAll(r)
	if err != nil {
		return api.File{}, err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.failUploads > 0 {
		l.failUploads--
		return api.File{}, &api.Error{Op: "upload", Status: http.StatusBadGateway}
	}
	l.uploads++
	l.nextID++
	f := api.File{ID: l.nextID, Name: name, FolderID: folderID, Size: int64(len(data)), Created: time.Now().UTC()}
	l.files[f.ID] = f
	l.data[f.ID] = data
	return f, nil
}

func (l *fakeLibrary) UpdateFile(_ context.Context, fileID int, name *string, folderID *int) (api.File, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.updates = append(l.updates, update{id: fileID, name: name, parent: folderID})
	f, ok := l.files[fileID]
	if !ok {
		return api.File{}, &api.Error{Op: "update file", Status: http.StatusNotFound}
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
