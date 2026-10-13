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

// Library is what the tree reads and writes the library through; *api.Client is one.
type Library interface {
	List(ctx context.Context, folderID int, all bool) (api.Listing, error)
	Download(ctx context.Context, fileID int, w io.Writer) error
	Upload(ctx context.Context, folderID int, name string, r io.Reader) (api.File, error)
	UpdateFile(ctx context.Context, fileID int, name *string, folderID *int) (api.File, error)
	DeleteFiles(ctx context.Context, ids ...int) error
	Mkdir(ctx context.Context, parentID int, name string) (api.Folder, error)
	UpdateFolder(ctx context.Context, folderID int, name *string, parentID *int) (api.Folder, error)
	DeleteFolder(ctx context.Context, folderID int) error
}

var _ Library = (*api.Client)(nil)

// Options are the mount's settings. All lists every file type, ReadOnly refuses every
// write with EROFS, TTL is how long a folder's listing is kept, and CacheDir holds
// downloaded contents and files being written.
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

	// released, if set, is called when a write handle's release has finished (tests).
	released func()
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
	folders, files := shownNames(got.Folders, got.Files, folderID)
	l = listing{at: time.Now(), folders: folders, files: files}
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

// forgetAll drops every cached listing.
func (t *tree) forgetAll() {
	t.mu.Lock()
	clear(t.listings)
	t.mu.Unlock()
}

// mode is perm without its write bits on a read-only mount.
func (t *tree) mode(perm uint32) uint32 {
	if t.opts.ReadOnly {
		return perm &^ 0o222
	}
	return perm
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
	if child, n := d.unsent(name); child != nil {
		n.attr(&out.Attr)
		return child, 0
	}
	return nil, syscall.ENOENT
}

// unsent returns the child called name if it is a file created here and not uploaded
// yet, which no listing has.
func (d *dir) unsent(name string) (*fs.Inode, *file) {
	child := d.GetChild(name)
	if child == nil {
		return nil, nil
	}
	if n, ok := child.Operations().(*file); ok && n.current().ID == 0 {
		return child, n
	}
	return nil, nil
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
	for name, child := range d.Children() {
		if _, isListed := l.files[name]; !isListed {
			if n, ok := child.Operations().(*file); ok && n.current().ID == 0 {
				entries = append(entries, fuse.DirEntry{Name: name, Mode: syscall.S_IFREG, Ino: child.StableAttr().Ino})
			}
		}
	}
	slices.SortFunc(entries, func(a, b fuse.DirEntry) int { return strings.Compare(a.Name, b.Name) })
	return fs.NewListDirStream(entries), 0
}

func (d *dir) Getattr(_ context.Context, _ fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	d.attr(&out.Attr)
	return 0
}

func (d *dir) attr(out *fuse.Attr) {
	out.Mode = syscall.S_IFDIR | d.tree.mode(0o755)
	out.Nlink = 1
}

// file is a library file. Its bytes, size and creation time never change for its ID:
// a write uploads a new file under the same name (pending.upload). A file created here
// has ID 0 until its first upload gives it one.
type file struct {
	fs.Inode
	tree *tree

	mu   sync.Mutex
	file api.File
	// pending is the file's write while handles are open on it; its size is the file's.
	pending *pending
}

func (n *file) current() api.File {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.file
}

func (n *file) set(f api.File) {
	n.mu.Lock()
	n.file = f
	n.mu.Unlock()
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
	n.mu.Lock()
	f, p := n.file, n.pending
	n.mu.Unlock()
	out.Mode = syscall.S_IFREG | n.tree.mode(0o644)
	// A link count of 0 means unlinked: link(2) would answer ENOENT.
	out.Nlink = 1
	out.Size = uint64(f.Size)
	if p != nil {
		out.Size = uint64(p.size.Load())
	}
	out.SetTimes(&f.Created, &f.Created, &f.Created)
}
