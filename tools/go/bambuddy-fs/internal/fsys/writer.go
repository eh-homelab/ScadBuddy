package fsys

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"slices"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/telemetry"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
	"go.opentelemetry.io/otel/attribute"
)

// Writes (spec §8). Bambuddy has no in-place write, so a file is written to a temp
// file under the cache directory and uploaded when it is closed, and writing over a
// name uploads a new file and then deletes the old one. A failed upload's file moves
// to the unsent queue (unsent.go, #2865), and a later write of it works on a copy.

// uploadTimeout bounds an upload, which runs detached from the request that started
// it, so a signal that interrupts close(2) does not lose the file.
const uploadTimeout = 30 * time.Minute

// pending is a file's unsent write, shared by every handle open on the file while it
// lasts: writers write the one temp file, readers read it, and the file is uploaded
// once for what they wrote. The temp file is made at the first read, write or
// truncate (or upload), and seeded with the contents it starts from (a stored file's,
// or a queued copy's) unless the file is new or was truncated to nothing first. The
// upload goes under the node's name and folder, which a rename changes.
type pending struct {
	node *file
	size atomic.Int64
	// handles counts the open handles; the node's mu guards it.
	handles int

	mu sync.Mutex
	// base is the stored file the contents start from, ID 0 none; seedFile, if set, is
	// a queued copy they start from instead, held open so a removal of the queued save
	// does not take it away.
	base     api.File
	seedFile *os.File
	tmp      *os.File
	path     string
	seed     bool
	dirty    bool
	// refusal is the library's last refusal of the upload, kept by the last release.
	refusal error

	// The node's mu guards these. bases are the stored files the write replaces (see
	// record.Bases); over are the queued entries it holds, the retrier leaving them
	// alone, and supersedes once its own bytes are durable (sent or queued); shadowed
	// are bases a rename gave it, hidden until it ends.
	bases    []base
	over     []*entry
	shadowed []base
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
// works on a copy of it, never the queued copy itself.
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
		if n.file.ID != 0 {
			p.bases = []base{baseOf(n.file, n.tree.listedCreated(n.file))}
		}
		if held != nil && n.queued == held {
			rec, _ := n.tree.unsent.record(held)
			f, err := os.Open(held.contents())
			if err != nil {
				n.tree.unsent.unhold(held)
				return nil, failed("open", err, "path", held.dir)
			}
			p.base, p.seedFile = api.File{}, f
			p.bases, p.over = slices.Clone(rec.Bases), []*entry{held}
			p.size.Store(rec.Size)
			held = nil
		}
		n.pending = p
		n.tree.mu.Lock()
		n.tree.pendings[p] = true
		n.tree.mu.Unlock()
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
	ctx, op := telemetry.StartOp(ctx, "setattr", p.attrs()...)
	errno := p.truncate(ctx, int64(size))
	op.End(errno)
	if errno != 0 {
		return errno
	}
	n.attr(&out.Attr)
	return 0
}

// downloads is whether ensure may download the stored file the contents start from.
// The caller holds p.mu.
func (p *pending) downloads() bool {
	return p.tmp == nil && p.seed && p.seedFile == nil && p.base.ID != 0
}

// traced starts the span of a read or write that may download what it starts from;
// any other has none. The caller holds p.mu.
func (p *pending) traced(ctx context.Context, op string) (context.Context, *telemetry.Op) {
	if !p.downloads() {
		return ctx, nil
	}
	return telemetry.StartOp(ctx, op, telemetry.FileID(p.base.ID), telemetry.Bytes(p.base.Size))
}

// attrs are a write's span attributes: the file (0 if never uploaded), its folder and
// its size.
func (p *pending) attrs() []attribute.KeyValue {
	cur := p.node.current()
	return []attribute.KeyValue{telemetry.FileID(cur.ID), telemetry.FolderID(cur.FolderID), telemetry.Bytes(p.size.Load())}
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
	if p.seed && (p.seedFile != nil || p.base.ID != 0) {
		if err := p.seedInto(ctx, tmp); err != nil {
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

// seedInto copies the contents the write starts from into tmp: a queued copy's, or a
// stored file's.
func (p *pending) seedInto(ctx context.Context, tmp *os.File) error {
	if p.seedFile != nil {
		_, err := io.Copy(tmp, io.NewSectionReader(p.seedFile, 0, 1<<62))
		return err
	}
	t := p.node.tree
	src, err := t.cache.open(ctx, t.lib, p.base)
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
	ctx, op := p.traced(ctx, "read")
	errno := p.ensure(ctx)
	op.End(errno)
	if errno != 0 {
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
	ctx, op := p.traced(ctx, "write")
	errno := p.ensure(ctx)
	op.End(errno)
	if errno != 0 {
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

// Fsync makes what was written durable where it is: the temp file under the cache
// directory, until close uploads or queues it. A write already queued is durable.
func (h *pendingHandle) Fsync(context.Context, uint32) syscall.Errno {
	p := h.p
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.tmp == nil {
		return 0
	}
	if err := p.tmp.Sync(); err != nil {
		return failed("fsync", err, "path", p.path)
	}
	return 0
}

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
	return p.tracedUpload(ctx, "flush")
}

// tracedUpload uploads, in the span of the flush or release that does; one with
// nothing to send has none.
func (p *pending) tracedUpload(ctx context.Context, name string) syscall.Errno {
	if !p.dirty {
		return 0
	}
	ctx, op := telemetry.StartOp(ctx, name, p.attrs()...)
	errno := p.upload(ctx)
	op.End(errno)
	return errno
}

// Release ends the handle. The last one uploads anything still unsent: what a failed
// flush could neither send nor queue (one more try), or what a mapping wrote after the
// last close. If that fails too and no handle has opened meanwhile, the contents are
// kept under refused/ (keepRefused) and a file never uploaded leaves its folder. The
// queued entries it held go back to the retrier.
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
	errno := p.tracedUpload(ctx, "release")
	n.mu.Lock()
	// A handle opened during the upload keeps the pending write, unsent contents and
	// all: its own flush tries again.
	ended := p.handles == 0
	if ended {
		n.pending = nil
		t.mu.Lock()
		delete(t.pendings, p)
		t.mu.Unlock()
	}
	over, shadowed := p.over, p.shadowed
	n.mu.Unlock()
	if !ended {
		return errno
	}
	if p.dirty {
		p.keepRefused()
	}
	if p.tmp != nil {
		_ = p.tmp.Close()
		_ = os.Remove(p.path)
		p.tmp = nil
	}
	p.setSeedFile(nil)
	for _, e := range over {
		t.unsent.unhold(e)
	}
	t.unsent.shadow(shadowed, -1)
	return errno
}

// setSeedFile replaces the queued copy the write starts from, closing the old one.
func (p *pending) setSeedFile(f *os.File) {
	if p.seedFile != nil {
		_ = p.seedFile.Close()
	}
	p.seedFile = f
}

// keepRefused keeps contents that could be neither sent nor queued under refused/,
// never sent again; the log names where. A file never uploaded leaves its folder.
func (p *pending) keepRefused() {
	n, t := p.node, p.node.tree
	cur := n.current()
	n.mu.Lock()
	bases := slices.Clone(p.bases)
	n.mu.Unlock()
	cause := "not queued"
	if p.refusal != nil {
		cause = p.refusal.Error()
	}
	now := t.unsent.now()
	rec := record{
		FolderID: cur.FolderID, Name: cur.Name, Size: p.size.Load(), Created: now.UTC(),
		Attempts: 1, LastError: cause, LastAttempt: now.UTC(), Bases: bases, SavedAt: now,
	}
	if path, err := t.unsent.refuse(p.tmp, p.path, p.size.Load(), rec); err != nil {
		slog.Error("upload failed again and keeping it failed; the written contents are lost", "folder", cur.FolderID, "name", cur.Name, "err", err)
	} else {
		_ = p.tmp.Close()
		p.tmp = nil
		slog.Warn("the library refused a write; it is kept and not sent again", "path", path, "folder", cur.FolderID, "name", cur.Name, "err", cause)
	}
	if cur.ID == 0 && n.entry() == nil {
		n.drop()
	}
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

// upload sends the contents as a new file under the node's name and folder, or as a
// conflict copy if a file it does not replace has taken the name (tree.target), makes
// them the new ID's cached contents, and only then deletes the files they replace that
// are still in place. If a delete fails the new file stays, and the error says the old
// one remains. A failure is queued (pending.queue); a refusal stays unsent. The caller
// holds p.mu.
//
// The node takes the new file's ID unless an old file remains: a stored file's inode
// number names its ID, so the parent's entry is invalidated and the name is looked up
// again.
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
	cur := n.current()
	n.mu.Lock()
	bases, over := slices.Clone(p.bases), slices.Clone(p.over)
	n.mu.Unlock()
	sendAs, conflict, err := t.target(ctx, cur.FolderID, cur.Name, bases, t.unsent.now())
	var got api.File
	if err == nil {
		got, err = t.lib.Upload(ctx, cur.FolderID, sendAs, io.NewSectionReader(p.tmp, 0, p.size.Load()))
	}
	if err != nil {
		errno := failed("upload", err, "folder", cur.FolderID, "name", sendAs)
		if !queueable(err) {
			p.refusal = err
			return errno
		}
		// The bases as they are now: a rename during the upload may have moved one.
		n.mu.Lock()
		bases = slices.Clone(p.bases)
		n.mu.Unlock()
		return p.queue(cur, bases, over, err)
	}
	p.dirty, p.refusal = false, nil
	t.forget(cur.FolderID)
	// The temp file becomes the new ID's cached contents; a later write seeds a new one.
	if err := os.Rename(p.path, t.cache.path(got.ID)); err != nil {
		if !errors.Is(err, syscall.EXDEV) {
			slog.Warn("caching an upload failed", "file", got.ID, "err", err)
		}
		_ = os.Remove(p.path)
	}
	_ = p.tmp.Close()
	p.tmp, p.seed, p.base = nil, true, got
	p.setSeedFile(nil)
	// The queued saves this one supersedes are now on the library.
	for _, e := range over {
		t.unsent.drop(e)
	}
	n.mu.Lock()
	p.over = slices.DeleteFunc(p.over, func(e *entry) bool { return slices.Contains(over, e) })
	if !conflict {
		// The bases as they are now: a rename during the upload may have moved one.
		bases = slices.Clone(p.bases)
		p.bases = []base{baseOf(got, time.Time{})}
	}
	n.mu.Unlock()
	if len(over) > 0 {
		n.bind(nil)
	}

	var errno syscall.Errno
	if !conflict {
		errno = t.deleteBases(ctx, bases, got.ID)
		if errno == 0 {
			n.set(got)
		}
		t.remember(got)
	}
	if e := parent.NotifyEntry(name); e != 0 && e != syscall.ENOENT {
		slog.Warn("entry invalidation failed", "name", name, "err", e)
	}
	return errno
}

// queue keeps contents whose upload failed for the retrier: the temp file moves into
// the unsent queue, under the state directory, superseding the queued saves the write
// held (over), and the write goes on from a copy of it. The caller holds p.mu.
//
// Once the contents are fsync'd there, the failure is answered with success: the bytes
// are safe, and the retrier sends them under whatever name and folder they then have.
// Answering EIO would have the app report a failed save, and perhaps delete what it
// wrote, though nothing is lost. Only a failure to queue them is an error (#2865).
func (p *pending) queue(cur api.File, bases []base, over []*entry, cause error) syscall.Errno {
	q := p.node.tree.unsent
	now := q.now()
	size := p.size.Load()
	rec := record{
		FolderID: cur.FolderID, Name: cur.Name, Size: size, Created: now.UTC(),
		Attempts: 1, LastError: cause.Error(), LastAttempt: now.UTC(), Bases: bases, SavedAt: now,
	}
	e, f, err := q.add(p.tmp, p.path, size, rec, over...)
	if err != nil {
		return errQueue(err, "folder", cur.FolderID, "name", cur.Name)
	}
	_ = p.tmp.Close()
	p.tmp, p.path, p.dirty = nil, "", false
	// The write goes on from the queued copy, held open.
	p.seed, p.base = true, api.File{}
	p.setSeedFile(f)
	n := p.node
	n.mu.Lock()
	// The superseded entries are gone (any add could not supersede stay held); add's
	// hold on e is this write's.
	p.over = append(slices.DeleteFunc(p.over, func(o *entry) bool {
		_, queued := q.record(o)
		return !queued
	}), e)
	n.mu.Unlock()
	n.bind(e)
	slog.Warn("upload failed; the contents are kept and will be sent again", "path", e.dir, "folder", cur.FolderID, "name", cur.Name, "err", cause)
	return 0
}
