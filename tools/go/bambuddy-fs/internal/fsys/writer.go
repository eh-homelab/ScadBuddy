package fsys

import (
	"context"
	"errors"
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
// name uploads a new file and then deletes the old one. A failed upload's file moves
// to the unsent queue (unsent.go, #2865) and is written there from then on.

// uploadTimeout bounds an upload, which runs detached from the request that started
// it, so a signal that interrupts close(2) does not lose the file.
const uploadTimeout = 30 * time.Minute

// pending is a file's unsent write, shared by every handle open on the file while it
// lasts: writers write the one temp file, readers read it, and the file is uploaded
// once for what they wrote. The temp file is made at the first read, write or
// truncate (or upload), and seeded with base's contents unless the file is new or
// was truncated to nothing first. The upload goes under the node's name and folder,
// which a rename changes. Once queued, the temp file is the queue entry's contents,
// which the pending write holds (the retrier leaves it alone) until it ends.
type pending struct {
	node *file
	size atomic.Int64
	// handles counts the open handles; the node's mu guards it.
	handles int

	mu sync.Mutex
	// base is the stored file this one replaces: the opened file, then each upload.
	// ID 0 is none.
	base  api.File
	tmp   *os.File
	path  string
	seed  bool
	dirty bool
	q     *entry
}

// pendingHandle is one open handle on a pending write.
type pendingHandle struct {
	p     *pending
	write bool
}

var (
	_ fs.FileReader   = (*pendingHandle)(nil)
	_ fs.FileWriter   = (*pendingHandle)(nil)
	_ fs.FileFlusher  = (*pendingHandle)(nil)
	_ fs.FileFsyncer  = (*pendingHandle)(nil)
	_ fs.FileReleaser = (*pendingHandle)(nil)
)

// attach returns a handle on the file's pending write. A writer starts one if there is
// none, and a created file's is uploaded even if nothing is written to it; a reader
// only joins one, and gets nil without. A writer of a queued write holds its entry and
// writes its contents in place.
func (n *file) attach(write, created bool) (*pendingHandle, syscall.Errno) {
	var held *entry
	if e := n.entry(); write && e != nil && n.tree.unsent.hold(e) {
		held = e
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	p := n.pending
	if p == nil {
		if !write {
			return nil, 0
		}
		p = &pending{node: n, base: n.file, seed: !created, dirty: created}
		p.size.Store(n.file.Size)
		if held != nil && n.queued == held {
			rec, _ := n.tree.unsent.record(held)
			tmp, err := os.OpenFile(held.contents(), os.O_RDWR, 0)
			if err != nil {
				n.tree.unsent.unhold(held)
				return nil, failed("open", err, "path", held.dir)
			}
			p.base, p.seed, p.q = api.File{ID: rec.Base.id()}, false, held
			p.tmp, p.path = tmp, held.contents()
			p.size.Store(rec.Size)
			if st, err := tmp.Stat(); err == nil {
				p.size.Store(st.Size())
			}
			held = nil
		}
		n.pending = p
	}
	if held != nil {
		// The pending write already holds it, or the node is no longer its.
		n.tree.unsent.unhold(held)
	}
	p.handles++
	return &pendingHandle{p: p, write: write}, 0
}

// Setattr truncates a file open for writing and accepts times (timesOnly); anything
// else is refused (spec §9). open(O_TRUNC) opens the file and then truncates it
// without naming the handle, so a truncate without one goes to the pending write.
func (n *file) Setattr(ctx context.Context, fh fs.FileHandle, in *fuse.SetAttrIn, out *fuse.AttrOut) syscall.Errno {
	if n.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	if in.Valid&^timesOnly == 0 {
		n.attr(&out.Attr)
		return 0
	}
	var p *pending
	if h, ok := fh.(*pendingHandle); ok && h.write {
		p = h.p
	} else if fh == nil {
		n.mu.Lock()
		p = n.pending
		n.mu.Unlock()
	}
	size, hasSize := in.GetSize()
	if p == nil || !hasSize || in.Valid&^(timesOnly|fuse.FATTR_SIZE|fuse.FATTR_KILL_SUIDGID) != 0 {
		return syscall.ENOTSUP
	}
	if errno := p.truncate(ctx, int64(size)); errno != 0 {
		return errno
	}
	n.attr(&out.Attr)
	return 0
}

// ensure makes the temp file. The caller holds p.mu.
func (p *pending) ensure(ctx context.Context) syscall.Errno {
	if p.tmp != nil {
		return 0
	}
	t := p.node.tree
	if err := os.MkdirAll(t.cache.dir, 0o700); err != nil {
		return failed("write", err)
	}
	tmp, err := os.CreateTemp(t.cache.dir, "write-*")
	if err != nil {
		return failed("write", err)
	}
	if p.seed && p.base.ID != 0 {
		if err := seed(ctx, t, p.base, tmp); err != nil {
			_ = tmp.Close()
			_ = os.Remove(tmp.Name())
			return failed("write", err, "file", p.base.ID)
		}
	}
	st, err := tmp.Stat()
	if err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmp.Name())
		return failed("write", err)
	}
	p.tmp, p.path, p.seed = tmp, tmp.Name(), false
	p.size.Store(st.Size())
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

func (h *pendingHandle) Read(ctx context.Context, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	p := h.p
	p.mu.Lock()
	defer p.mu.Unlock()
	if errno := p.ensure(ctx); errno != 0 {
		return nil, errno
	}
	n, err := p.tmp.ReadAt(dest, off)
	if err != nil && err != io.EOF {
		return nil, failed("read", err)
	}
	return fuse.ReadResultData(dest[:n]), 0
}

func (h *pendingHandle) Write(ctx context.Context, data []byte, off int64) (uint32, syscall.Errno) {
	p := h.p
	p.mu.Lock()
	defer p.mu.Unlock()
	if errno := p.ensure(ctx); errno != 0 {
		return 0, errno
	}
	n, err := p.tmp.WriteAt(data, off)
	p.dirty = true
	if end := off + int64(n); end > p.size.Load() {
		p.size.Store(end)
	}
	if err != nil {
		return uint32(n), failed("write", err)
	}
	return uint32(n), 0
}

func (p *pending) truncate(ctx context.Context, size int64) syscall.Errno {
	p.mu.Lock()
	defer p.mu.Unlock()
	if size == 0 && p.tmp == nil {
		p.seed = false
	}
	if errno := p.ensure(ctx); errno != 0 {
		return errno
	}
	if err := p.tmp.Truncate(size); err != nil {
		return failed("truncate", err)
	}
	p.dirty = true
	p.size.Store(size)
	return 0
}

// Fsync does nothing: the contents are uploaded when the file is closed, or queued
// on disk, fsync'd, if that upload fails.
func (h *pendingHandle) Fsync(context.Context, uint32) syscall.Errno { return 0 }

// Flush uploads what was written since the last upload, by any handle. close(2) waits
// for flush and returns its error, and not for release, which the kernel sends
// without waiting, so the upload is here for the caller to learn its outcome. A flush
// with nothing new (closing a duplicate descriptor, or a second handle's close) sends
// nothing, and a reader's flush never uploads. An upload that fails but is queued
// (pending.queue) is a success.
func (h *pendingHandle) Flush(ctx context.Context) syscall.Errno {
	if !h.write {
		return 0
	}
	p := h.p
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.upload(ctx)
}

// Release ends the handle. The last one uploads anything still unsent: what a failed
// flush could neither send nor queue (one more try), or what a mapping wrote after the
// last close. If that fails too, unqueued, and no handle has opened meanwhile, the
// contents are lost and a file never uploaded is dropped. A queued write goes to the
// retrier.
func (h *pendingHandle) Release(ctx context.Context) syscall.Errno {
	p, n, t := h.p, h.p.node, h.p.node.tree
	if t.released != nil {
		defer t.released()
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	n.mu.Lock()
	p.handles--
	last := p.handles == 0
	n.mu.Unlock()
	if !last {
		return 0
	}
	errno := p.upload(ctx)
	n.mu.Lock()
	// A handle opened during the upload keeps the pending write, unsent contents and
	// all: its own flush tries again.
	ended := p.handles == 0
	if ended {
		n.pending = nil
	}
	n.mu.Unlock()
	if !ended {
		return errno
	}
	if p.dirty && p.q == nil {
		cur := n.current()
		slog.Error("upload failed again; the written contents are lost", "folder", cur.FolderID, "name", cur.Name)
		if cur.ID == 0 {
			n.drop()
		}
	}
	if p.tmp != nil {
		_ = p.tmp.Close()
		if p.q == nil {
			_ = os.Remove(p.path)
		}
		p.tmp = nil
	}
	if p.q != nil {
		t.unsent.unhold(p.q)
	}
	return errno
}

// drop removes a file that was created here and never uploaded from its folder, here
// and in the kernel. Called from release, which holds no directory lock.
func (n *file) drop() {
	name, parent := n.Parent()
	if parent == nil {
		return
	}
	parent.RmChild(name)
	if errno := parent.NotifyEntry(name); errno != 0 && errno != syscall.ENOENT {
		slog.Warn("entry invalidation failed", "name", name, "err", errno)
	}
}

// upload sends the contents as a new file under the node's name and folder, makes
// them the new ID's cached contents, and only then deletes the file they replace.
// If that delete fails the new file stays, and the error says the old one remains.
// The contents stay unsent until an upload succeeds. The caller holds p.mu.
//
// The node takes the new file's ID unless the old file remains: a stored file's
// inode number names its ID, so the parent's entry is invalidated and the name is
// looked up again.
func (p *pending) upload(ctx context.Context) syscall.Errno {
	if !p.dirty {
		return 0
	}
	n, t := p.node, p.node.tree
	name, parent := n.Parent()
	if parent == nil {
		slog.Info("not uploaded: the file was removed while open", "name", name)
		p.dirty = false
		return 0
	}
	if errno := p.ensure(ctx); errno != 0 {
		return errno
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), uploadTimeout)
	defer cancel()
	cur, old := n.current(), p.base
	// A queued write is sent as the retrier sends it (tree.target).
	sendAs, conflict := cur.Name, false
	var err error
	if p.q != nil {
		rec, ok := t.unsent.record(p.q)
		if !ok {
			slog.Info("not uploaded: a later save superseded the queued one", "name", cur.Name)
			p.dirty = false
			return 0
		}
		sendAs, conflict, err = t.target(ctx, cur.FolderID, cur.Name, rec.Base, t.unsent.now())
	}
	var got api.File
	if err == nil {
		got, err = t.lib.Upload(ctx, cur.FolderID, sendAs, io.NewSectionReader(p.tmp, 0, p.size.Load()))
	}
	if err != nil {
		errno := failed("upload", err, "folder", cur.FolderID, "name", sendAs)
		if !queueable(err) {
			return errno
		}
		return p.queue(cur, err)
	}
	p.dirty = false
	p.base = got
	t.forget(cur.FolderID)
	// The temp file becomes the new ID's cached contents; a later write seeds a new one.
	if err := os.Rename(p.path, t.cache.path(got.ID)); err != nil {
		if !errors.Is(err, syscall.EXDEV) {
			slog.Warn("caching an upload failed", "file", got.ID, "err", err)
		}
		_ = os.Remove(p.path)
	}
	_ = p.tmp.Close()
	p.tmp, p.seed = nil, true
	if p.q != nil {
		t.unsent.drop(p.q)
		n.bind(nil)
		p.q = nil
		slog.Info("unsent write sent", "folder", cur.FolderID, "name", cur.Name, "file", got.ID)
	}

	var errno syscall.Errno
	if old.ID != 0 && !conflict {
		if err := t.lib.DeleteFiles(ctx, old.ID); err != nil {
			errno = failed("replace: the old file remains beside the new one", err, "old", old.ID, "new", got.ID)
		} else {
			t.cache.drop(old.ID)
		}
	}
	if errno == 0 {
		n.set(got)
	}
	if e := parent.NotifyEntry(name); e != 0 && e != syscall.ENOENT {
		slog.Warn("entry invalidation failed", "name", name, "err", e)
	}
	return errno
}

// queue keeps contents whose upload failed for the retrier: the first failure moves
// the temp file into the unsent queue, under the state directory, and a later one
// updates its record. The caller holds p.mu.
//
// Once the contents are fsync'd there, the failure is answered with success: the bytes
// are safe, and the retrier sends them under whatever name and folder they then have.
// Answering EIO would have the app report a failed save, and perhaps delete what it
// wrote, though nothing is lost. Only a failure to queue them is an error (#2865).
func (p *pending) queue(cur api.File, cause error) syscall.Errno {
	q := p.node.tree.unsent
	now := q.now().UTC()
	size := p.size.Load()
	if p.q != nil {
		if err := p.tmp.Sync(); err != nil {
			return errQueue(err, "path", p.q.dir)
		}
		err := q.update(p.q, func(r *record) {
			r.FolderID, r.Name, r.Size, r.SavedAt = cur.FolderID, cur.Name, size, q.now()
			r.Attempts++
			r.LastError, r.LastAttempt = cause.Error(), now
		})
		if err != nil {
			return errQueue(err, "path", p.q.dir)
		}
		p.dirty = false
		return 0
	}
	// The base is the stored file the write was opened on, with the created_at its
	// listing gave, if that listing is still cached.
	var b *base
	if p.base.ID != 0 {
		b = &base{ID: p.base.ID, Created: p.node.tree.listedCreated(p.base)}
	}
	rec := record{
		FolderID: cur.FolderID, Name: cur.Name, Size: size, Created: now,
		Attempts: 1, LastError: cause.Error(), LastAttempt: now, Base: b, SavedAt: q.now(),
	}
	e, f, err := q.add(p.tmp, p.path, size, rec)
	if err != nil {
		return errQueue(err, "folder", cur.FolderID, "name", cur.Name)
	}
	_ = p.tmp.Close()
	p.tmp, p.path, p.q = f, e.contents(), e
	p.node.bind(e)
	p.dirty = false
	slog.Warn("upload failed; the contents are kept and will be sent again", "path", e.dir, "folder", cur.FolderID, "name", cur.Name, "err", cause)
	return 0
}
