package fsys

import (
	"context"
	"io"
	"net/http"
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
}

func newFakeLibrary() *fakeLibrary {
	return &fakeLibrary{files: map[int]api.File{}, data: map[int][]byte{}}
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
