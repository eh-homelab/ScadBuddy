// Package fsys is the library as a FUSE tree on go-fuse's node API: a directory per
// Bambuddy folder, a file per library file (spec §3), listings cached for the TTL and
// contents cached on disk (spec §8).
package fsys

import (
	"context"
	"io"
	"log/slog"
	"maps"
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
// write with EROFS, TTL is how long a folder's listing is kept, CacheDir holds
// downloaded contents and files being written, and StateDir the writes whose upload
// failed, under unsent/ (#2865).
type Options struct {
	All, ReadOnly      bool
	TTL                time.Duration
	CacheDir, StateDir string
}

// NewRoot returns the root directory, the library's top level, with the unsent writes
// under opts.StateDir queued (RetryUnsent sends them).
func NewRoot(lib Library, opts Options) fs.InodeEmbedder {
	t := &tree{
		lib: lib, opts: opts, cache: newCache(opts.CacheDir), listings: map[int]listing{}, uploaded: map[int]api.File{},
		unsent: newQueue(opts.StateDir),
	}
	t.unsent.load()
	return &dir{tree: t}
}

// tree is the state every node shares.
type tree struct {
	lib   Library
	opts  Options
	cache *cache
	// unsent is the writes whose upload failed (#2865).
	unsent *queue

	mu       sync.Mutex
	listings map[int]listing
	// uploaded holds, by ID, the files this mount uploaded that a listing without
	// --all may not show (not printable, such as an app's "x.3mf.tmp" before it renames
	// it), so they can still be renamed and removed. One goes once a listing shows it,
	// or it is removed.
	uploaded map[int]api.File

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

// view is a folder as the mount shows it: its listing without the stored files that
// queued writes replace, and the queued writes whose names no listed entry has, by
// name. A queued write that names a listed entry stays queued but is not shown, so a
// name never shows twice.
type view struct {
	listing
	queued map[string]*entry
}

func (t *tree) view(ctx context.Context, folderID int) (view, syscall.Errno) {
	l, errno := t.list(ctx, folderID)
	if errno != 0 {
		return view{}, errno
	}
	queued, replaced := t.unsent.in(folderID)
	if extra := t.unlisted(l, folderID); len(replaced) > 0 || len(extra) > 0 {
		l.files = maps.Clone(l.files)
		maps.DeleteFunc(l.files, func(_ string, f api.File) bool { return replaced[f.ID] })
		for _, f := range extra {
			if _, isDir := l.folders[f.Name]; !isDir && !replaced[f.ID] {
				if _, taken := l.files[f.Name]; !taken {
					l.files[f.Name] = f
				}
			}
		}
	}
	for name := range queued {
		_, isDir := l.folders[name]
		if _, isFile := l.files[name]; isDir || isFile {
			delete(queued, name)
		}
	}
	return view{listing: l, queued: queued}, 0
}

// remember keeps a file this mount uploaded, for a listing without --all.
func (t *tree) remember(f api.File) {
	if t.opts.All {
		return
	}
	t.mu.Lock()
	t.uploaded[f.ID] = f
	t.mu.Unlock()
}

func (t *tree) unremember(id int) {
	t.mu.Lock()
	delete(t.uploaded, id)
	t.mu.Unlock()
}

// moved follows a rename of a file this mount uploaded.
func (t *tree) moved(id int, name string, folderID int) {
	t.mu.Lock()
	if f, ok := t.uploaded[id]; ok {
		f.Name, f.FolderID = name, folderID
		t.uploaded[id] = f
	}
	t.mu.Unlock()
}

// unlisted returns the files this mount uploaded into folderID that l does not show,
// and forgets those it shows.
func (t *tree) unlisted(l listing, folderID int) []api.File {
	t.mu.Lock()
	defer t.mu.Unlock()
	if len(t.uploaded) == 0 {
		return nil
	}
	listed := map[int]bool{}
	for _, f := range l.files {
		listed[f.ID] = true
	}
	var out []api.File
	for id, f := range t.uploaded {
		switch {
		case listed[id]:
			delete(t.uploaded, id)
		case f.FolderID == folderID:
			out = append(out, f)
		}
	}
	return out
}

// listedCreated is a file's created_at in its folder's cached listing, or zero.
func (t *tree) listedCreated(f api.File) time.Time {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, lf := range t.listings[f.FolderID].files {
		if lf.ID == f.ID {
			return lf.Created
		}
	}
	return time.Time{}
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
	l, errno := d.tree.view(ctx, d.id)
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
	if e := l.queued[name]; e != nil {
		if rec, ok := d.tree.unsent.record(e); ok {
			n := &file{tree: d.tree, file: api.File{Name: rec.Name, FolderID: rec.FolderID, Size: rec.Size, Created: rec.Created}, queued: e}
			child := d.NewInode(ctx, n, fs.StableAttr{Mode: syscall.S_IFREG, Ino: queueIno(e.seq)})
			if n, ok := child.Operations().(*file); ok {
				n.bind(e)
				n.attr(&out.Attr)
			}
			return child, 0
		}
	}
	return nil, syscall.ENOENT
}

// unsent returns the child called name if it is a file created here and not uploaded
// yet, which no listing has, or a queued write.
func (d *dir) unsent(name string) (*fs.Inode, *file) {
	child := d.GetChild(name)
	if child == nil {
		return nil, nil
	}
	if n, ok := child.Operations().(*file); ok && (n.current().ID == 0 || n.entry() != nil) {
		return child, n
	}
	return nil, nil
}

func (d *dir) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	l, errno := d.tree.view(ctx, d.id)
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
	for name, e := range l.queued {
		ino := queueIno(e.seq)
		if child, _ := d.unsent(name); child != nil {
			ino = child.StableAttr().Ino
		}
		entries = append(entries, fuse.DirEntry{Name: name, Mode: syscall.S_IFREG, Ino: ino})
	}
	for name, child := range d.Children() {
		_, isListed := l.files[name]
		if _, isQueued := l.queued[name]; !isListed && !isQueued {
			if n, ok := child.Operations().(*file); ok && n.current().ID == 0 && n.entry() == nil {
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
// has ID 0 until its first upload gives it one. A queued write (#2865) is a file whose
// entry is set, read from its local copy until it is sent.
type file struct {
	fs.Inode
	tree *tree

	mu   sync.Mutex
	file api.File
	// pending is the file's write while handles are open on it; its size is the file's.
	pending *pending
	// queued is the file's unsent write, if its upload failed.
	queued *entry
}

func (n *file) entry() *entry {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.queued
}

// bind makes the node the queued write e's, or no queued write's if e is nil.
func (n *file) bind(e *entry) {
	n.mu.Lock()
	n.queued = e
	n.mu.Unlock()
	if e != nil {
		n.tree.unsent.bind(e, n)
	}
}

// notify has the kernel look the node's name up again, as a sent queued write's
// inode number changes.
func (n *file) notify() {
	name, parent := n.Parent()
	if parent == nil {
		return
	}
	if errno := parent.NotifyEntry(name); errno != 0 && errno != syscall.ENOENT {
		slog.Warn("entry invalidation failed", "name", name, "err", errno)
	}
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
	f, p, e := n.file, n.pending, n.queued
	n.mu.Unlock()
	out.Mode = syscall.S_IFREG | n.tree.mode(0o644)
	// A link count of 0 means unlinked: link(2) would answer ENOENT.
	out.Nlink = 1
	out.Size = uint64(f.Size)
	if p != nil {
		out.Size = uint64(p.size.Load())
	} else if e != nil {
		if rec, ok := n.tree.unsent.record(e); ok {
			out.Size = uint64(rec.Size)
		}
	}
	out.SetTimes(&f.Created, &f.Created, &f.Created)
}
