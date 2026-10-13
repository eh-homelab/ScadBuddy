package fsys

import (
	"context"
	"io"
	"log/slog"
	"os"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// Writes (spec §8). Bambuddy has no in-place write, so a file is written to a temp
// file under the cache directory and uploaded when it is closed, and writing over a
// name uploads a new file and then deletes the old one.
//
// The kernel holds the parent directory's lock while it waits for create, mkdir,
// unlink, rmdir and rename, and an entry notification takes that lock too, so those
// handlers never notify: their answers update the kernel's entries, and they drop
// only this tree's listings. An upload notifies, from flush or release, which run
// under no directory lock.

var (
	_ fs.NodeCreater   = (*dir)(nil)
	_ fs.NodeMkdirer   = (*dir)(nil)
	_ fs.NodeUnlinker  = (*dir)(nil)
	_ fs.NodeRmdirer   = (*dir)(nil)
	_ fs.NodeRenamer   = (*dir)(nil)
	_ fs.NodeSetattrer = (*file)(nil)
)

func (d *dir) Create(ctx context.Context, name string, _, _ uint32, out *fuse.EntryOut) (*fs.Inode, fs.FileHandle, uint32, syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return nil, nil, 0, syscall.EROFS
	}
	if errno := d.free(ctx, d, name); errno != 0 {
		return nil, nil, 0, errno
	}
	n := &file{tree: d.tree, file: api.File{Name: name, FolderID: d.id, Created: time.Now().UTC()}}
	w := n.write(true)
	n.attr(&out.Attr)
	// Ino 0: go-fuse numbers it until the upload gives the file an ID.
	return d.NewInode(ctx, n, fs.StableAttr{Mode: syscall.S_IFREG}), w, 0, 0
}

func (d *dir) Mkdir(ctx context.Context, name string, _ uint32, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return nil, syscall.EROFS
	}
	if errno := d.free(ctx, d, name); errno != 0 {
		return nil, errno
	}
	f, err := d.tree.lib.Mkdir(ctx, d.id, name)
	d.tree.forget(d.id)
	if err != nil {
		return nil, failed("mkdir", err, "parent", d.id, "name", name)
	}
	child := &dir{tree: d.tree, id: f.ID}
	child.attr(&out.Attr)
	return d.NewInode(ctx, child, fs.StableAttr{Mode: syscall.S_IFDIR, Ino: dirIno(f.ID)}), 0
}

func (d *dir) Unlink(ctx context.Context, name string) syscall.Errno {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return errno
	}
	f, ok := l.files[name]
	if !ok {
		if _, isDir := l.folders[name]; isDir {
			return syscall.EISDIR
		}
		if child, _ := d.unsent(name); child != nil {
			// Never uploaded: its writer finds it unlinked and sends nothing.
			return 0
		}
		return syscall.ENOENT
	}
	err := d.tree.lib.DeleteFiles(ctx, f.ID)
	d.tree.forget(d.id)
	if err != nil {
		return failed("unlink", err, "file", f.ID)
	}
	d.tree.cache.drop(f.ID)
	return 0
}

// Rmdir deletes the folder with everything in it (spec §5); the route refuses with
// 409, EBUSY, before deleting anything if a file in it is queued to print.
func (d *dir) Rmdir(ctx context.Context, name string) syscall.Errno {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return errno
	}
	f, ok := l.folders[name]
	if !ok {
		if _, isFile := l.files[name]; isFile {
			return syscall.ENOTDIR
		}
		return syscall.ENOENT
	}
	err := d.tree.lib.DeleteFolder(ctx, f.ID)
	// Even a refused delete may have moved or trashed some of the subtree.
	d.tree.forgetAll()
	if err != nil {
		return failed("rmdir", err, "folder", f.ID)
	}
	return 0
}

// Rename renames and/or moves a file or folder with one update. A name that is taken
// is EEXIST: the library cannot replace an entry atomically.
func (d *dir) Rename(ctx context.Context, name string, newParent fs.InodeEmbedder, newName string, flags uint32) syscall.Errno {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	to, ok := newParent.(*dir)
	if !ok || flags&fs.RENAME_EXCHANGE != 0 {
		return syscall.ENOTSUP
	}
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return errno
	}
	if errno := d.free(ctx, to, newName); errno != 0 {
		return errno
	}
	defer d.tree.forget(to.id)
	defer d.tree.forget(d.id)

	if f, ok := l.folders[name]; ok {
		newName, parent := changes(f.Name, newName, d.id, to.id)
		if _, err := d.tree.lib.UpdateFolder(ctx, f.ID, newName, parent); err != nil {
			return failed("rename folder", err, "folder", f.ID)
		}
		return 0
	}
	if f, ok := l.files[name]; ok {
		newName, folder := changes(f.Name, newName, d.id, to.id)
		got, err := d.tree.lib.UpdateFile(ctx, f.ID, newName, folder)
		if err != nil {
			return failed("rename", err, "file", f.ID)
		}
		if child := d.GetChild(name); child != nil {
			if n, ok := child.Operations().(*file); ok {
				// The answer has no size or times: keep the file's.
				if cur := n.current(); cur.ID == f.ID {
					cur.Name, cur.FolderID = got.Name, got.FolderID
					n.set(cur)
				}
			}
		}
		return 0
	}
	if _, n := d.unsent(name); n != nil {
		// Not uploaded yet: it will be uploaded under its new name.
		f := n.current()
		f.Name, f.FolderID = newName, to.id
		n.set(f)
		return 0
	}
	return syscall.ENOENT
}

// changes is what a rename sends: within a folder always the name, and on a move the
// folder (0 being the top level) and the name only if it changes.
func changes(name, newName string, from, to int) (*string, *int) {
	if from == to {
		return &newName, nil
	}
	if newName == name {
		return nil, &to
	}
	return &newName, &to
}

// free is EEXIST if name is shown in in, or is a file being created there.
func (d *dir) free(ctx context.Context, in *dir, name string) syscall.Errno {
	l, errno := d.tree.list(ctx, in.id)
	if errno != 0 {
		return errno
	}
	_, isDir := l.folders[name]
	_, isFile := l.files[name]
	if child, _ := in.unsent(name); isDir || isFile || child != nil {
		return syscall.EEXIST
	}
	return 0
}

// Setattr can only truncate a file open for writing (spec §9). open(O_TRUNC) opens
// the file and then truncates it without naming the handle, so a truncate without
// one goes to the file's open write handle.
func (n *file) Setattr(ctx context.Context, fh fs.FileHandle, in *fuse.SetAttrIn, out *fuse.AttrOut) syscall.Errno {
	const truncate = fuse.FATTR_SIZE | fuse.FATTR_FH | fuse.FATTR_LOCKOWNER | fuse.FATTR_MTIME | fuse.FATTR_CTIME | fuse.FATTR_MTIME_NOW | fuse.FATTR_KILL_SUIDGID
	size, hasSize := in.GetSize()
	w, writing := fh.(*writer)
	if fh == nil {
		n.mu.Lock()
		w, writing = n.writing, n.writing != nil
		n.mu.Unlock()
	}
	if !writing || !hasSize || in.Valid&^truncate != 0 {
		if n.tree.opts.ReadOnly {
			return syscall.EROFS
		}
		return syscall.ENOTSUP
	}
	if errno := w.truncate(ctx, int64(size)); errno != 0 {
		return errno
	}
	n.attr(&out.Attr)
	return 0
}

// failed logs a library error with the operation and maps it to its errno.
func failed(op string, err error, args ...any) syscall.Errno {
	slog.Warn(op+" failed", append(args, "err", err)...)
	return api.Errno(err)
}

// writer is a file open for writing. Its contents live in a temp file, made at the
// first read, write or truncate (or upload), and seeded with base's contents unless
// the file is new or was truncated to nothing first. It is uploaded under the node's
// name and folder, which a rename changes.
type writer struct {
	node *file
	size atomic.Int64

	mu sync.Mutex
	// base is the stored file this one replaces: the opened file, then each upload.
	// ID 0 is none.
	base  api.File
	tmp   *os.File
	seed  bool
	dirty bool
}

var (
	_ fs.FileReader   = (*writer)(nil)
	_ fs.FileWriter   = (*writer)(nil)
	_ fs.FileFlusher  = (*writer)(nil)
	_ fs.FileFsyncer  = (*writer)(nil)
	_ fs.FileReleaser = (*writer)(nil)
)

// write starts a write handle on the file; a created one is uploaded even if nothing
// is written to it.
func (n *file) write(created bool) *writer {
	n.mu.Lock()
	defer n.mu.Unlock()
	w := &writer{node: n, base: n.file, seed: !created, dirty: created}
	w.size.Store(n.file.Size)
	n.writing = w
	return w
}

// ensure makes the temp file. The caller holds w.mu.
func (w *writer) ensure(ctx context.Context) syscall.Errno {
	if w.tmp != nil {
		return 0
	}
	t := w.node.tree
	if err := os.MkdirAll(t.cache.dir, 0o700); err != nil {
		return failed("write", err)
	}
	tmp, err := os.CreateTemp(t.cache.dir, "write-*")
	if err != nil {
		return failed("write", err)
	}
	if w.seed && w.base.ID != 0 {
		if err := seed(ctx, t, w.base, tmp); err != nil {
			_ = tmp.Close()
			_ = os.Remove(tmp.Name())
			return failed("write", err, "file", w.base.ID)
		}
	}
	st, err := tmp.Stat()
	if err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmp.Name())
		return failed("write", err)
	}
	w.tmp, w.seed = tmp, false
	w.size.Store(st.Size())
	return 0
}

// seed copies a stored file's contents into tmp.
func seed(ctx context.Context, t *tree, f api.File, tmp *os.File) error {
	src, err := t.cache.open(ctx, t.lib, f)
	if err != nil {
		return err
	}
	defer src.Close()
	_, err = io.Copy(tmp, src)
	return err
}

func (w *writer) Read(ctx context.Context, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if errno := w.ensure(ctx); errno != 0 {
		return nil, errno
	}
	n, err := w.tmp.ReadAt(dest, off)
	if err != nil && err != io.EOF {
		return nil, failed("read", err)
	}
	return fuse.ReadResultData(dest[:n]), 0
}

func (w *writer) Write(ctx context.Context, data []byte, off int64) (uint32, syscall.Errno) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if errno := w.ensure(ctx); errno != 0 {
		return 0, errno
	}
	n, err := w.tmp.WriteAt(data, off)
	w.dirty = true
	if end := off + int64(n); end > w.size.Load() {
		w.size.Store(end)
	}
	if err != nil {
		return uint32(n), failed("write", err)
	}
	return uint32(n), 0
}

func (w *writer) truncate(ctx context.Context, size int64) syscall.Errno {
	w.mu.Lock()
	defer w.mu.Unlock()
	if size == 0 && w.tmp == nil {
		w.seed = false
	}
	if errno := w.ensure(ctx); errno != 0 {
		return errno
	}
	if err := w.tmp.Truncate(size); err != nil {
		return failed("truncate", err)
	}
	w.dirty = true
	w.size.Store(size)
	return 0
}

// Fsync does nothing: the contents are uploaded when the file is closed.
func (w *writer) Fsync(context.Context, uint32) syscall.Errno { return 0 }

// Flush uploads what was written since the last upload. close(2) waits for flush and
// returns its error, and not for release, which the kernel sends without waiting, so
// the upload is here for the caller to learn its outcome. A flush with nothing new
// (closing a duplicate descriptor, or a second close) sends nothing.
func (w *writer) Flush(ctx context.Context) syscall.Errno {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.upload(ctx)
}

// Release uploads anything a flush did not (written through a mapping after the last
// close), then drops the temp file.
func (w *writer) Release(ctx context.Context) syscall.Errno {
	w.mu.Lock()
	defer w.mu.Unlock()
	errno := w.upload(ctx)
	if w.tmp != nil {
		_ = w.tmp.Close()
		_ = os.Remove(w.tmp.Name())
		w.tmp = nil
	}
	n := w.node
	n.mu.Lock()
	if n.writing == w {
		n.writing = nil
	}
	n.mu.Unlock()
	return errno
}

// upload sends the contents as a new file under the node's name and folder, makes
// them the new ID's cached contents, and only then deletes the file they replace.
// If that delete fails the new file stays, and the error says the old one remains.
// A failed upload is not retried. The caller holds w.mu.
//
// A stored file's node keeps its ID, which its inode number names: the old file may
// remain. The parent's entry is invalidated instead, so the name is looked up again.
func (w *writer) upload(ctx context.Context) syscall.Errno {
	if !w.dirty {
		return 0
	}
	w.dirty = false
	n, t := w.node, w.node.tree
	name, parent := n.Parent()
	if parent == nil {
		return 0 // unlinked while open
	}
	if errno := w.ensure(ctx); errno != 0 {
		return errno
	}
	cur, old := n.current(), w.base
	got, err := t.lib.Upload(ctx, cur.FolderID, cur.Name, io.NewSectionReader(w.tmp, 0, w.size.Load()))
	if err != nil {
		return failed("upload", err, "folder", cur.FolderID, "name", cur.Name)
	}
	w.base = got
	if cur.ID == 0 {
		n.set(got)
	}
	t.forget(cur.FolderID)
	// The temp file becomes the new ID's cached contents; a later write seeds a new one.
	if err := os.Rename(w.tmp.Name(), t.cache.path(got.ID)); err != nil {
		slog.Warn("caching an upload failed", "file", got.ID, "err", err)
		_ = os.Remove(w.tmp.Name())
	}
	_ = w.tmp.Close()
	w.tmp, w.seed = nil, true

	var errno syscall.Errno
	if old.ID != 0 {
		if err := t.lib.DeleteFiles(ctx, old.ID); err != nil {
			errno = failed("replace: the old file remains beside the new one", err, "old", old.ID, "new", got.ID)
		} else {
			t.cache.drop(old.ID)
		}
	}
	if e := parent.NotifyEntry(name); e != 0 && e != syscall.ENOENT {
		slog.Warn("entry invalidation failed", "name", name, "err", e)
	}
	return errno
}
