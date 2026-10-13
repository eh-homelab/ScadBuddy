// Package fsys is the library as a FUSE tree on go-fuse's node API: a directory per
// Bambuddy folder, a file per library file (spec §3), listings cached for the TTL and
// contents cached on disk (spec §8).
package fsys

import (
	"context"
	"io"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// Library is what the tree reads the library through; *api.Client is one.
type Library interface {
	List(ctx context.Context, folderID int, all bool) (api.Listing, error)
	Download(ctx context.Context, fileID int, w io.Writer) error
}

// Options are the mount's settings. All lists every file type, TTL is how long a
// folder's listing is kept, and CacheDir holds downloaded contents.
type Options struct {
	All, ReadOnly bool
	TTL           time.Duration
	CacheDir      string
}

// NewRoot returns the root directory, the library's top level.
func NewRoot(lib Library, opts Options) fs.InodeEmbedder {
	t := &tree{lib: lib, opts: opts, cache: newCache(opts.CacheDir), listings: map[int]listing{}}
	return &dir{tree: t}
}

// tree is the state every node shares.
type tree struct {
	lib   Library
	opts  Options
	cache *cache

	mu       sync.Mutex
	listings map[int]listing
}

// listing is one folder's entries by the names they show as.
type listing struct {
	at      time.Time
	folders map[string]api.Folder
	files   map[string]api.File
}

// list returns a folder's entries, from the cache while they are younger than the TTL.
func (t *tree) list(ctx context.Context, folderID int) (listing, syscall.Errno) {
	t.mu.Lock()
	l, ok := t.listings[folderID]
	t.mu.Unlock()
	if ok && time.Since(l.at) < t.opts.TTL {
		return l, 0
	}
	got, err := t.lib.List(ctx, folderID, t.opts.All)
	if err != nil {
		slog.Warn("list failed", "folder", folderID, "err", err)
		return listing{}, api.Errno(err)
	}
	l = listing{at: time.Now(), folders: folderNames(got.Folders, folderID), files: Names(got.Files)}
	t.mu.Lock()
	t.listings[folderID] = l
	t.mu.Unlock()
	return l, 0
}

// forget drops a folder's cached listing.
func (t *tree) forget(folderID int) {
	t.mu.Lock()
	delete(t.listings, folderID)
	t.mu.Unlock()
}

// Inode numbers: folders are even and files odd, so the two ID spaces never meet.
func dirIno(folderID int) uint64 { return uint64(folderID) << 1 }
func fileIno(fileID int) uint64  { return uint64(fileID)<<1 | 1 }

// dir is a folder; id 0 is the top level.
type dir struct {
	fs.Inode
	tree *tree
	id   int
}

var (
	_ fs.NodeLookuper  = (*dir)(nil)
	_ fs.NodeReaddirer = (*dir)(nil)
	_ fs.NodeGetattrer = (*dir)(nil)
)

func (d *dir) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return nil, errno
	}
	if f, ok := l.folders[name]; ok {
		child := &dir{tree: d.tree, id: f.ID}
		child.attr(&out.Attr)
		return d.NewInode(ctx, child, fs.StableAttr{Mode: syscall.S_IFDIR, Ino: dirIno(f.ID)}), 0
	}
	if f, ok := l.files[name]; ok {
		child := &file{tree: d.tree, file: f}
		child.attr(&out.Attr)
		return d.NewInode(ctx, child, fs.StableAttr{Mode: syscall.S_IFREG, Ino: fileIno(f.ID)}), 0
	}
	return nil, syscall.ENOENT
}

func (d *dir) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return nil, errno
	}
	entries := make([]fuse.DirEntry, 0, len(l.folders)+len(l.files))
	for name, f := range l.folders {
		entries = append(entries, fuse.DirEntry{Name: name, Mode: syscall.S_IFDIR, Ino: dirIno(f.ID)})
	}
	for name, f := range l.files {
		entries = append(entries, fuse.DirEntry{Name: name, Mode: syscall.S_IFREG, Ino: fileIno(f.ID)})
	}
	slices.SortFunc(entries, func(a, b fuse.DirEntry) int { return strings.Compare(a.Name, b.Name) })
	return fs.NewListDirStream(entries), 0
}

func (d *dir) Getattr(_ context.Context, _ fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	d.attr(&out.Attr)
	return 0
}

// attr: writes arrive in Task 8, so until then everything is read-only.
func (d *dir) attr(out *fuse.Attr) {
	out.Mode = syscall.S_IFDIR | 0o555
}

// file is a library file. Its bytes, size and creation time never change for its ID.
type file struct {
	fs.Inode
	tree *tree
	file api.File
}

var (
	_ fs.NodeGetattrer = (*file)(nil)
	_ fs.NodeOpener    = (*file)(nil)
)

func (n *file) Getattr(_ context.Context, _ fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	n.attr(&out.Attr)
	return 0
}

func (n *file) attr(out *fuse.Attr) {
	out.Mode = syscall.S_IFREG | 0o444
	out.Size = uint64(n.file.Size)
	out.SetTimes(&n.file.Created, &n.file.Created, &n.file.Created)
}
