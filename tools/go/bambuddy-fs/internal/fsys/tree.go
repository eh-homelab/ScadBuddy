// Package fsys is the library as a FUSE tree on go-fuse's node API: a directory per
// Bambuddy folder, a file per library file (spec §3), listings cached for the TTL and
// contents cached on disk (spec §8).
package fsys

import (
	"context"
	"io"
	"log/slog"
	"maps"
	"os"
	"slices"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/telemetry"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
	"go.opentelemetry.io/otel/trace"
)

// Library is what the tree reads and writes the library through; *api.Client is one.
type Library interface {
	List(ctx context.Context, folderID int, all bool) (api.Listing, error)
	Download(ctx context.Context, fileID int, w io.Writer) error
	Upload(ctx context.Context, folderID int, name, key string, r io.Reader) (api.File, error)
	UpdateFile(ctx context.Context, fileID int, name *string, folderID *int) (api.File, error)
	DeleteFiles(ctx context.Context, ids ...int) error
	Mkdir(ctx context.Context, parentID int, name string) (api.Folder, error)
	UpdateFolder(ctx context.Context, folderID int, name *string, parentID *int) (api.Folder, error)
	DeleteFolder(ctx context.Context, folderID int) error
}

var _ Library = (*api.Client)(nil)

// Options are the mount's settings. All lists every file type, ReadOnly refuses every
// write with EROFS, TTL is how long a folder's listing is kept, CacheDir holds
// downloaded contents (at most CacheMax bytes of them, 0 no bound) and files being
// written, and StateDir the writes not yet sent, under unsent/ (#2865): those whose
// upload failed and, with WriteBack, every write until the retrier sends it.
// WriteBack makes a close queue what was written there instead of uploading it (#7286).
type Options struct {
	All, ReadOnly      bool
	TTL                time.Duration
	CacheDir, StateDir string
	CacheMax           int64
	WriteBack          bool
}

// NewRoot returns the root directory, the library's top level, with the unsent writes
// under opts.StateDir queued (RetryUnsent sends them).
func NewRoot(lib Library, opts Options) fs.InodeEmbedder {
	t := &tree{
		lib: lib, opts: opts, cache: newCache(opts.CacheDir, opts.CacheMax), listings: map[int]listing{}, fetches: map[int]*fetch{}, uploaded: map[int]api.File{},
		owner:    fuse.Owner{Uid: uint32(os.Getuid()), Gid: uint32(os.Getgid())},
		pendings: map[*pending]bool{},
		unsent:   newQueue(opts.StateDir),
	}
	t.unsent.discard = t.discardSent
	t.unsent.load()
	telemetry.Observe(telemetry.Gauges{
		CacheBytes:    t.cache.size,
		UnsentEntries: func() int64 { n, _ := t.unsent.totals(); return n },
		UnsentBytes:   func() int64 { _, b := t.unsent.totals(); return b },
	})
	return &dir{tree: t}
}

// tree is the state every node shares.
type tree struct {
	lib   Library
	opts  Options
	cache *cache
	// owner is the mounting process's user and group, which own every entry: with
	// root's, the mounting user's access(W_OK) would be refused.
	owner fuse.Owner
	// unsent is the writes not yet sent (#2865): those whose upload failed and, with
	// WriteBack, every write until the retrier sends it.
	unsent *queue

	mu       sync.Mutex
	listings map[int]listing
	// fetches are the listing requests in flight, by folder.
	fetches map[int]*fetch
	// uploaded holds, by ID, the files this mount uploaded that a listing without
	// --all may not show (not printable, such as an app's "x.3mf.tmp" before it renames
	// it), so they can still be renamed and removed. One goes once a listing shows it,
	// or it is removed.
	uploaded map[int]api.File
	// pendings are the writes open now.
	pendings map[*pending]bool
	// down is set while listings are served stale because ScadBuddy is not answering.
	down bool
	// failedAt is when a listing last failed because ScadBuddy was not answering.
	failedAt time.Time

	// released, if set, is called when a write handle's release has finished (tests).
	released func()
}

// listing is one folder's entries by the names they show as.
type listing struct {
	at      time.Time
	folders map[string]api.Folder
	files   map[string]api.File
}

// listTimeout bounds one listing request, which no single caller can cancel: others
// may share it.
const listTimeout = time.Minute

// downBackoff is how long after a failed listing, while ScadBuddy is down, an expired
// folder starts no new background refresh.
const downBackoff = 5 * time.Second

// fetch is one folder's listing request in flight. Every caller that needs the folder
// meanwhile shares it and its answer.
type fetch struct {
	done chan struct{}
	l    listing
	err  error
	// serveLast is whether the request failed because ScadBuddy is not answering and
	// the folder has a last listing to serve.
	serveLast bool
}

// staleness is how old a listing list may answer with.
type staleness bool

const (
	// anyAge serves an expired listing at once, and refreshes it in the background:
	// for lookups and readdirs.
	anyAge staleness = true
	// current waits for a listing younger than the TTL, which may be the request in
	// flight: for the checks a write makes before it changes the library, which must
	// not act on a listing from long ago.
	current staleness = false
)

// list returns a folder's entries, from the cache while they are younger than the TTL.
// One request at a time lists a folder; concurrent callers share it (#7285). With a
// TTL above 0 and anyAge, an expired listing is served at once and refreshed in the
// background; only a folder never listed, or one a write forgot, waits for its
// listing. With current, an expired listing waits for the request in flight, or a new
// one.
//
// While ScadBuddy cannot answer (unreachable, a timeout, a 5xx: a deploy takes
// minutes), the folder's last listing is served whatever its age, so lookups,
// listings, creates and renames go on and a save reaches the unsent queue; the first
// such answer is logged, and the first listing after it. A folder never listed has
// none to serve and answers the error.
func (t *tree) list(ctx context.Context, folderID int, a staleness) (listing, syscall.Errno) {
	t.mu.Lock()
	l, ok := t.listings[folderID]
	if ok && (t.fresh(l) || a == anyAge && t.servable(l)) {
		if !t.fresh(l) && t.fetches[folderID] == nil && !(t.down && time.Since(t.failedAt) < downBackoff) {
			t.startFetch(ctx, folderID, true)
		}
		t.mu.Unlock()
		return l, 0
	}
	f := t.fetches[folderID]
	if f == nil {
		f = t.startFetch(ctx, folderID, false)
	}
	t.mu.Unlock()
	select {
	case <-f.done:
	case <-ctx.Done():
		return listing{}, syscall.EINTR
	}
	if f.serveLast {
		t.mu.Lock()
		l = t.listings[folderID]
		t.mu.Unlock()
		return l, 0
	}
	if f.err != nil {
		return listing{}, api.Errno(f.err)
	}
	return f.l, 0
}

// startFetch starts listing a folder; t.mu is held. A background fetch is a refresh
// of a listing already served, and has a span of its own.
func (t *tree) startFetch(ctx context.Context, folderID int, background bool) *fetch {
	f := &fetch{done: make(chan struct{})}
	t.fetches[folderID] = f
	var r *telemetry.Refresh
	if background {
		ctx, r = telemetry.StartRefresh(ctx, folderID)
	} else {
		// The caller's request ends when it answers, and others may share this one.
		ctx = trace.ContextWithSpanContext(context.Background(), trace.SpanContextFromContext(ctx))
	}
	go t.fetch(ctx, folderID, f, r)
	return f
}

// fetch runs f. Its listing is kept only if f is still the folder's request: a write
// that forgot the folder meanwhile has the next caller list it again, and an answer
// from before the write must not replace that one.
func (t *tree) fetch(ctx context.Context, folderID int, f *fetch, r *telemetry.Refresh) {
	defer close(f.done)
	ctx, cancel := context.WithTimeout(ctx, listTimeout)
	defer cancel()
	got, err := t.lib.List(ctx, folderID, t.opts.All)
	t.mu.Lock()
	isCurrent := t.fetches[folderID] == f
	if isCurrent {
		delete(t.fetches, folderID)
	}
	if err != nil {
		f.err = err
		last, had := t.listings[folderID]
		first := false
		if had && unreachable(err) {
			f.serveLast = true
			first = !t.down
			t.down = true
			t.failedAt = time.Now()
		} else if had && isCurrent && r != nil {
			// A refresh refused (the folder is gone, say): the next use lists it again
			// and answers the error.
			last.at = time.Time{}
			t.listings[folderID] = last
		}
		t.mu.Unlock()
		switch {
		case first:
			slog.Warn("ScadBuddy is not answering; serving the last listings, and queueing writes, until it does", "folder", folderID, "err", err)
		case !f.serveLast:
			slog.Warn("list failed", "folder", folderID, "err", err)
		}
		if r != nil {
			r.End(telemetry.RefreshFailed)
		}
		return
	}
	folders, files := shownNames(got.Folders, got.Files, folderID)
	f.l = listing{at: time.Now(), folders: folders, files: files}
	// An answer no longer current may be from before ScadBuddy went down, and says
	// nothing of now.
	back := false
	if isCurrent {
		t.listings[folderID] = f.l
		back = t.down
		t.down = false
	}
	t.mu.Unlock()
	if back {
		slog.Info("ScadBuddy answers again; listings are fresh")
	}
	if r != nil {
		r.End(telemetry.RefreshOK)
	}
}

// unreachable is whether a library error is ScadBuddy not answering, not an answer:
// no response, or a 5xx.
func unreachable(err error) bool {
	s := status(err)
	return s == 0 || s >= 500
}

// fresh is whether a listing is younger than the TTL.
func (t *tree) fresh(l listing) bool {
	return !l.at.IsZero() && time.Since(l.at) < t.opts.TTL
}

// servable is whether a listing is answered at once: fresh, or, with a TTL above 0,
// expired but not forgotten. With a TTL of 0 every use lists the folder.
func (t *tree) servable(l listing) bool {
	return t.fresh(l) || (t.opts.TTL > 0 && !l.at.IsZero())
}

// isDown is whether listings are served stale because ScadBuddy is not answering.
func (t *tree) isDown() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.down
}

// answersNow is whether list answers a folder from its listing without waiting.
func (t *tree) answersNow(folderID int) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	l, ok := t.listings[folderID]
	return ok && t.servable(l)
}

// listing starts the span of a lookup or readdir that waits for its folder's listing;
// one answered from the cached listing, fresh or refreshed in the background, has
// none, as they are too many to trace.
func (d *dir) listing(ctx context.Context, op string) (context.Context, *telemetry.Op) {
	if d.tree.answersNow(d.id) {
		return ctx, nil
	}
	return telemetry.StartOp(ctx, op, telemetry.FolderID(d.id))
}

// view is a folder as the mount shows it: its listing without the stored files that
// queued writes replace, and the queued writes whose names no listed entry has, by
// name. A queued write that names a listed entry stays queued but is not shown, so a
// name never shows twice.
type view struct {
	listing
	queued map[string]*entry
}

func (t *tree) view(ctx context.Context, folderID int, a staleness) (view, syscall.Errno) {
	l, errno := t.list(ctx, folderID, a)
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

// discardSent deletes files that queued writes, since removed or superseded, had
// already sent.
func (t *tree) discardSent(ids ...int) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	if err := t.lib.DeleteFiles(ctx, ids...); err != nil {
		slog.Warn("a removed unsent write's sent file remains in the library", "files", ids, "err", err)
	}
	for _, id := range ids {
		t.unremember(id)
	}
}

// rebase follows a stored file renamed through the mount in every write that replaces
// it, open or queued: a base is the file wherever it now lives. to is the file now; a
// retype gives it a new ID, whose created_at the base then leaves unknown.
//
// Open writes go first: one that queues meanwhile then records the moved base, or is
// queued before the records are rebased.
func (t *tree) rebase(id int, to api.File) {
	defer t.unsent.rebase(id, to)
	t.mu.Lock()
	open := slices.Collect(maps.Keys(t.pendings))
	t.mu.Unlock()
	for _, p := range open {
		n := p.node
		n.mu.Lock()
		for i := range p.bases {
			if p.bases[i].ID == id {
				p.bases[i] = p.bases[i].movedTo(to)
			}
		}
		n.mu.Unlock()
	}
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

// forget has a folder's listing fetched again at its next use, which waits for it: a
// request already in flight may have been answered before the write that forgets it.
// The listing is kept, expired, for list to serve while ScadBuddy is not answering.
func (t *tree) forget(folderID int) {
	t.mu.Lock()
	if l, ok := t.listings[folderID]; ok {
		l.at = time.Time{}
		t.listings[folderID] = l
	}
	delete(t.fetches, folderID)
	t.mu.Unlock()
}

// forgetAll expires every listing, as forget does.
func (t *tree) forgetAll() {
	t.mu.Lock()
	for id, l := range t.listings {
		l.at = time.Time{}
		t.listings[id] = l
	}
	clear(t.fetches)
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
	_ fs.NodeStatfser  = (*dir)(nil)
	_ fs.NodeStatfser  = (*file)(nil)
)

func (d *dir) Statfs(_ context.Context, out *fuse.StatfsOut) syscall.Errno {
	d.tree.statfs(out)
	return 0
}

func (n *file) Statfs(_ context.Context, out *fuse.StatfsOut) syscall.Errno {
	n.tree.statfs(out)
	return 0
}

// statfs answers with the cache directory's filesystem, where every write is staged
// before its upload, so its free space is what a copy in can use; a zeroed answer
// would have Explorer and file managers refuse the copy as out of space. Without it
// (the directory is gone), 1 TiB free. Names are up to 255 bytes, as Bambuddy's are.
func (t *tree) statfs(out *fuse.StatfsOut) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(t.cache.dir, &st); err == nil {
		out.FromStatfsT(&st)
	} else {
		const blocks = 1 << 40 / 4096
		*out = fuse.StatfsOut{Bsize: 4096, Frsize: 4096, Blocks: blocks, Bfree: blocks, Bavail: blocks, Files: 1 << 20, Ffree: 1 << 20}
	}
	out.NameLen = 255
}

func (d *dir) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (_ *fs.Inode, errno syscall.Errno) {
	ctx, op := d.listing(ctx, "lookup")
	defer func() { op.End(errno) }()
	l, errno := d.tree.view(ctx, d.id, anyAge)
	if errno != 0 {
		return nil, errno
	}
	if child := d.find(ctx, l, name, out); child != nil {
		return child, 0
	}
	// With a TTL of 0 the first view was listed just now, so it is current.
	if d.tree.fresh(l.listing) || d.tree.opts.TTL == 0 || d.tree.isDown() {
		return nil, syscall.ENOENT
	}
	if op == nil {
		ctx, op = telemetry.StartOp(ctx, "lookup", telemetry.FolderID(d.id))
	}
	// A miss in an expired listing waits for a current one: the kernel keeps an
	// ENOENT as a negative entry, which would hide a file added since. While
	// ScadBuddy is not answering, the last listing is the answer.
	if l, errno = d.tree.view(ctx, d.id, current); errno != 0 {
		return nil, errno
	}
	if child := d.find(ctx, l, name, out); child != nil {
		return child, 0
	}
	return nil, syscall.ENOENT
}

// find returns the child called name in l, or nil.
func (d *dir) find(ctx context.Context, l view, name string, out *fuse.EntryOut) *fs.Inode {
	if f, ok := l.folders[name]; ok {
		child := &dir{tree: d.tree, id: f.ID}
		child.attr(&out.Attr)
		return d.NewInode(ctx, child, fs.StableAttr{Mode: syscall.S_IFDIR, Ino: dirIno(f.ID)})
	}
	if f, ok := l.files[name]; ok {
		child := &file{tree: d.tree, file: f}
		child.attr(&out.Attr)
		return d.NewInode(ctx, child, fs.StableAttr{Mode: syscall.S_IFREG, Ino: fileIno(f.ID)})
	}
	if child, n := d.unsent(name); child != nil {
		n.attr(&out.Attr)
		return child
	}
	if e := l.queued[name]; e != nil {
		if rec, ok := d.tree.unsent.record(e); ok {
			n := &file{tree: d.tree, file: api.File{Name: rec.Name, FolderID: rec.FolderID, Size: rec.Size, Created: rec.Created}, queued: e}
			child := d.NewInode(ctx, n, fs.StableAttr{Mode: syscall.S_IFREG, Ino: queueIno(e.seq)})
			if n, ok := child.Operations().(*file); ok {
				n.bind(e)
				n.attr(&out.Attr)
			}
			return child
		}
	}
	return nil
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

func (d *dir) Readdir(ctx context.Context) (_ fs.DirStream, errno syscall.Errno) {
	ctx, op := d.listing(ctx, "readdir")
	defer func() { op.End(errno) }()
	l, errno := d.tree.view(ctx, d.id, anyAge)
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
	out.Owner = d.tree.owner
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
	// queued is the file's unsent write, if its upload failed or WriteBack queued it.
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
	out.Owner = n.tree.owner
	// A link count of 0 means unlinked: link(2) would answer ENOENT.
	out.Nlink = 1
	out.Size = uint64(f.Size)
	if p != nil {
		out.Size = uint64(p.size.Load())
	} else if e != nil {
		// Also after the entry leaves the queue: until the node becomes the sent file,
		// n.file of a file created here has size 0 (#7563).
		rec, _ := n.tree.unsent.record(e)
		out.Size = uint64(rec.Size)
	}
	out.SetTimes(&f.Created, &f.Created, &f.Created)
}
