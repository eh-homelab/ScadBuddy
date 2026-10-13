package fsys

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/hanwen/go-fuse/v2/fs"
)

// Unsent writes (#2865). A write whose upload fails is the user's only copy, so its
// bytes move to <state dir>/unsent/<seq>/ (contents, and record.json saying where it
// goes), not to the cache directory, which cache cleaners may empty. The entry stays
// in its folder, read from that copy, and can be renamed or removed like any file
// being written; a retrier sends it with backoff, from mount start until unmount. A
// queued copy is never written in place: a writer works on a copy of it, which, once
// it is durable itself (queued or sent), supersedes it. An upload the library refuses
// outright (400, 413, 415, 422) is kept under <state dir>/refused/<seq>/ and not retried.

const (
	retryFirst = 15 * time.Second
	retryMax   = 10 * time.Minute
)

// queueIno is a queued write's inode number. Folders are id<<1 and files id<<1|1 for
// IDs far below 2^61, and go-fuse numbers the nodes it is given no number for from
// 1<<63 up, so bit 62 is free for the queue's own numbers.
func queueIno(seq uint64) uint64 { return 1<<62 | seq }

// record is a queued write's record.json.
type record struct {
	FolderID    int       `json:"folder_id"`
	Name        string    `json:"name"`
	Size        int64     `json:"size"`
	Created     time.Time `json:"created"`
	Attempts    int       `json:"attempts"`
	LastError   string    `json:"last_error"`
	LastAttempt time.Time `json:"last_attempt"`
	// Bases are the stored files the write replaces: the one that had its name when it
	// was saved, and those of what it was renamed over. SavedAt is when it was saved,
	// local time.
	Bases   []base    `json:"bases"`
	SavedAt time.Time `json:"saved_at"`
	// Base is the single base of a record written before Bases; it is read into Bases.
	Base *base `json:"base,omitempty"`
	// Uploaded is the file the write was sent as when a rename made during the upload
	// could not be applied to it: the next pass renames that file instead of sending
	// the write again.
	Uploaded *uploadedAs `json:"uploaded,omitempty"`
}

// uploadedAs is a sent write's file, where it was sent.
type uploadedAs struct {
	ID       int    `json:"id"`
	FolderID int    `json:"folder_id"`
	Name     string `json:"name"`
}

// base is a stored file a write replaces, and where it was: it is deleted once the
// write is sent only if it is still that file under that name. Created is the
// library's created_at, zero when it is not known (the file was uploaded here): then
// the ID alone decides.
type base struct {
	ID       int       `json:"id"`
	Created  time.Time `json:"created_at"`
	FolderID int       `json:"folder_id"`
	Name     string    `json:"name"`
}

// is whether f is the base: the same ID and, if known, the same created_at.
func (b base) is(f api.File) bool {
	return b.ID == f.ID && (b.Created.IsZero() || b.Created.Equal(f.Created))
}

// baseOf is the base for a stored file, with its created_at if known.
func baseOf(f api.File, created time.Time) base {
	return base{ID: f.ID, Created: created, FolderID: f.FolderID, Name: f.Name}
}

func ours(bases []base, f api.File) bool {
	return slices.ContainsFunc(bases, func(b base) bool { return b.is(f) })
}

// union adds to bases those of more it lacks, by ID.
func union(bases []base, more ...base) []base {
	for _, b := range more {
		if !slices.ContainsFunc(bases, func(o base) bool { return o.ID == b.ID }) {
			bases = append(bases, b)
		}
	}
	return bases
}

func ids(bases []base) []int {
	out := make([]int, len(bases))
	for i, b := range bases {
		out[i] = b.ID
	}
	return out
}

// conflictName is what a write saved at savedAt is uploaded as when another file took
// its name first: "<stem> (conflict 2026-10-13 141502)<ext>".
func conflictName(name string, savedAt time.Time) string {
	stem, ext := splitExt(name)
	return fmt.Sprintf("%s (conflict %s)%s", stem, savedAt.Local().Format("2006-01-02 150405"), ext)
}

// entry is one queued write.
type entry struct {
	seq uint64
	dir string
	// sending is held while the retrier uploads the entry; a writer opening it waits.
	sending sync.Mutex

	// The queue's mu guards the rest. held counts the pending writes using the entry,
	// which the retrier leaves alone; gone is set once it is sent, superseded, refused
	// or removed.
	rec  record
	held int
	gone bool
	due  time.Time
	// node is the mount's node for the entry, if it has one.
	node *file
}

func (e *entry) contents() string { return filepath.Join(e.dir, "contents") }

// queue is the unsent writes under <state dir>/unsent, and the refused ones under
// <state dir>/refused.
type queue struct {
	dir, refusedDir string
	// now, jitter (in [0, 1)) and the backoff's first and max delays; tests set them.
	now        func() time.Time
	jitter     func() float64
	first, max time.Duration
	wake       chan struct{}

	mu      sync.Mutex
	entries map[uint64]*entry
	nextSeq uint64
	// shadowed counts, by ID, the stored files that writes still being written were
	// renamed over; they are hidden as queued writes' bases are.
	shadowed map[int]int
	// discard deletes files that dropped entries had already sent (record.Uploaded).
	discard func(ids ...int)
}

func newQueue(stateDir string) *queue {
	return &queue{
		dir: filepath.Join(stateDir, "unsent"), refusedDir: filepath.Join(stateDir, "refused"),
		now: time.Now, jitter: rand.Float64, first: retryFirst, max: retryMax,
		wake: make(chan struct{}, 1), entries: map[uint64]*entry{}, nextSeq: 1, shadowed: map[int]int{},
		discard: func(...int) {},
	}
}

// uploadedIDs are the files the entries had already sent.
func uploadedIDs(es ...*entry) []int {
	var out []int
	for _, e := range es {
		if e.rec.Uploaded != nil {
			out = append(out, e.rec.Uploaded.ID)
		}
	}
	return out
}

// stored is an entry as read from disk; err says why its record could not be used.
type stored struct {
	seq uint64
	dir string
	rec record
	err error
}

// readQueue reads every entry under dir, in sequence order. Directories not named by
// a number are not the queue's and are passed over.
func readQueue(dir string) ([]stored, error) {
	des, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []stored
	for _, de := range des {
		seq, err := strconv.ParseUint(de.Name(), 10, 62)
		if err != nil || !de.IsDir() {
			continue
		}
		s := stored{seq: seq, dir: filepath.Join(dir, de.Name())}
		s.rec, s.err = readRecord(s.dir)
		out = append(out, s)
	}
	slices.SortFunc(out, func(a, b stored) int { return int(a.seq) - int(b.seq) })
	return out, nil
}

func readRecord(dir string) (record, error) {
	var rec record
	data, err := os.ReadFile(filepath.Join(dir, "record.json"))
	if err != nil {
		return rec, err
	}
	if err := json.Unmarshal(data, &rec); err != nil {
		return rec, err
	}
	if rec.Name == "" {
		return rec, errors.New("record.json names no file")
	}
	if rec.Base != nil {
		rec.Bases = union(rec.Bases, *rec.Base)
		rec.Base = nil
	}
	if _, err := os.Stat(filepath.Join(dir, "contents")); err != nil {
		return rec, err
	}
	return rec, nil
}

// load queues every entry on disk, due at once. One whose record cannot be read is
// logged and left where it is. Of two entries with one folder and name (a crash between
// a rename and its supersede), the later save supersedes the earlier.
func (q *queue) load() {
	all, err := readQueue(q.dir)
	if err != nil {
		slog.Error("reading the unsent writes failed", "path", q.dir, "err", err)
		return
	}
	q.mu.Lock()
	for _, s := range all {
		q.nextSeq = max(q.nextSeq, s.seq+1)
		if s.err != nil {
			slog.Warn("unsent write skipped: its record cannot be read; it is left in place", "path", s.dir, "err", s.err)
			continue
		}
		q.entries[s.seq] = &entry{seq: s.seq, dir: s.dir, rec: s.rec, due: q.now()}
		slog.Warn("unsent write queued from an earlier mount", "path", s.dir, "folder", s.rec.FolderID, "name", s.rec.Name)
	}
	latest := map[[2]string]*entry{}
	for _, e := range q.entries {
		k := [2]string{strconv.Itoa(e.rec.FolderID), e.rec.Name}
		if o := latest[k]; o == nil || e.seq > o.seq {
			latest[k] = e
		}
	}
	q.mu.Unlock()
	for _, e := range latest {
		if err := q.supersede(e); err != nil {
			slog.Error("superseding an earlier unsent save failed", "path", e.dir, "err", err)
		}
	}
}

// makeDir creates dir if it is missing, durably: its entry in its parent is fsync'd.
func makeDir(dir string) error {
	if _, err := os.Stat(dir); err == nil {
		return nil
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	return syncDir(filepath.Dir(dir))
}

// store moves the file at path, size bytes of src, into a new numbered directory under
// area with rec, and returns that directory and its contents open for reading and
// writing. Contents and record are fsync'd before it returns. On an error the file is
// left at path.
func (q *queue) store(area string, src *os.File, path string, size int64, rec record) (uint64, string, *os.File, error) {
	if err := makeDir(area); err != nil {
		return 0, "", nil, err
	}
	seq, dir, err := q.newDir(area)
	if err != nil {
		return 0, "", nil, err
	}
	contents := filepath.Join(dir, "contents")
	f, renamed, err := moveFile(src, path, contents, size)
	if err == nil {
		err = writeRecord(dir, rec)
	}
	if err == nil {
		err = syncDir(area)
	}
	if err != nil {
		if f != nil {
			_ = f.Close()
		}
		if renamed {
			_ = os.Rename(contents, path)
		}
		_ = os.RemoveAll(dir)
		return 0, "", nil, err
	}
	if !renamed {
		_ = os.Remove(path)
	}
	return seq, dir, f, nil
}

// newDir makes the next free numbered directory under area.
func (q *queue) newDir(area string) (uint64, string, error) {
	for {
		q.mu.Lock()
		seq := q.nextSeq
		q.nextSeq++
		q.mu.Unlock()
		dir := filepath.Join(area, strconv.FormatUint(seq, 10))
		err := os.Mkdir(dir, 0o700)
		if errors.Is(err, os.ErrExist) {
			continue
		}
		return seq, dir, err
	}
}

// add queues the file at path (see store) as an entry held by its writer, superseding
// the entries over and any other with its folder and name, and returns the entry and
// its contents, open.
func (q *queue) add(src *os.File, path string, size int64, rec record, over ...*entry) (*entry, *os.File, error) {
	seq, dir, f, err := q.store(q.dir, src, path, size, rec)
	if err != nil {
		return nil, nil, err
	}
	e := &entry{seq: seq, dir: dir, rec: rec, held: 1}
	q.mu.Lock()
	q.entries[e.seq] = e
	q.mu.Unlock()
	if err := q.supersede(e, over...); err != nil {
		slog.Error("superseding an earlier unsent save failed", "path", e.dir, "err", err)
	}
	return e, f, nil
}

// refuse keeps the file at path (see store) under refused/, never to be retried, and
// returns where.
func (q *queue) refuse(src *os.File, path string, size int64, rec record) (string, error) {
	_, dir, f, err := q.store(q.refusedDir, src, path, size, rec)
	if err != nil {
		return "", err
	}
	_ = f.Close()
	return dir, nil
}

// moveRefused moves a queued entry the library refused under refused/, with cause in
// its record, and returns its node, if any.
func (q *queue) moveRefused(e *entry, cause error) (*file, error) {
	if err := makeDir(q.refusedDir); err != nil {
		return nil, err
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	if e.gone {
		return nil, nil
	}
	rec := e.rec
	rec.Attempts++
	rec.LastError, rec.LastAttempt = cause.Error(), q.now().UTC()
	if err := writeRecord(e.dir, rec); err != nil {
		return nil, err
	}
	for {
		to := filepath.Join(q.refusedDir, strconv.FormatUint(q.nextSeq, 10))
		q.nextSeq++
		if _, err := os.Stat(to); err == nil {
			continue
		}
		if err := os.Rename(e.dir, to); err != nil {
			return nil, err
		}
		_ = syncDir(q.refusedDir)
		_ = syncDir(q.dir)
		slog.Warn("the library refused an unsent write; it is kept and not sent again", "path", to, "folder", rec.FolderID, "name", rec.Name, "err", cause)
		break
	}
	e.gone = true
	delete(q.entries, e.seq)
	return e.node, nil
}

// moveFile renames path to dst, or copies size bytes of src there when they are on
// different filesystems, and returns dst open and fsync'd.
func moveFile(src *os.File, path, dst string, size int64) (*os.File, bool, error) {
	renamed := os.Rename(path, dst) == nil
	var f *os.File
	var err error
	if renamed {
		f, err = os.OpenFile(dst, os.O_RDWR, 0)
	} else {
		f, err = os.OpenFile(dst, os.O_RDWR|os.O_CREATE|os.O_EXCL, 0o600)
		if err == nil {
			_, err = io.Copy(f, io.NewSectionReader(src, 0, size))
		}
	}
	if err == nil {
		err = f.Sync()
	}
	if err != nil && f != nil {
		_ = f.Close()
		f = nil
	}
	return f, renamed, err
}

// writeRecord replaces dir's record.json atomically and durably.
func writeRecord(dir string, rec record) error {
	data, err := json.MarshalIndent(rec, "", "  ")
	if err != nil {
		return err
	}
	tmp := filepath.Join(dir, "record.json.tmp")
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	_, err = f.Write(append(data, '\n'))
	if err == nil {
		err = f.Sync()
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(tmp, filepath.Join(dir, "record.json"))
	}
	if err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return syncDir(dir)
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	err = d.Sync()
	if closeErr := d.Close(); err == nil {
		err = closeErr
	}
	return err
}

// update changes an entry's record, on disk too. A sent or removed entry is left alone.
func (q *queue) update(e *entry, change func(*record)) error {
	q.mu.Lock()
	defer q.mu.Unlock()
	if e.gone {
		return nil
	}
	rec := e.rec
	change(&rec)
	if err := writeRecord(e.dir, rec); err != nil {
		return err
	}
	e.rec = rec
	return nil
}

// supersede drops the entries over and the others with e's folder and name: earlier
// saves e replaces, which must not land after it. e is durable when it is called. It
// takes over their bases, the stored files those saves replaced.
func (q *queue) supersede(e *entry, over ...*entry) error {
	q.mu.Lock()
	if e.gone {
		q.mu.Unlock()
		return nil
	}
	earlier := slices.Clone(over)
	for _, o := range q.entries {
		if o != e && o.rec.FolderID == e.rec.FolderID && o.rec.Name == e.rec.Name && !slices.Contains(earlier, o) {
			earlier = append(earlier, o)
		}
	}
	earlier = slices.DeleteFunc(earlier, func(o *entry) bool { return o == e || o.gone })
	rec := e.rec
	for _, o := range earlier {
		rec.Bases = union(rec.Bases, o.rec.Bases...)
	}
	if len(rec.Bases) != len(e.rec.Bases) {
		if err := writeRecord(e.dir, rec); err != nil {
			q.mu.Unlock()
			return err
		}
		e.rec = rec
	}
	for _, o := range earlier {
		o.gone = true
		delete(q.entries, o.seq)
	}
	sent := uploadedIDs(earlier...)
	q.mu.Unlock()
	if len(sent) > 0 {
		q.discard(sent...)
	}
	for _, o := range earlier {
		slog.Info("an earlier unsent save is superseded", "path", o.dir, "by", e.dir, "folder", rec.FolderID, "name", rec.Name)
		if err := os.RemoveAll(o.dir); err != nil {
			slog.Warn("removing an unsent write failed", "path", o.dir, "err", err)
		}
	}
	return nil
}

// reschedule sets an entry's next attempt a backoff from now. Schedules run on the
// monotonic clock, never the record's wall-clock times, which may jump.
func (q *queue) reschedule(e *entry) {
	now := q.now()
	q.mu.Lock()
	e.due = now.Add(q.backoff(e.rec.Attempts))
	q.mu.Unlock()
}

// backoff is the wait after attempts failed attempts: retryFirst doubling up to
// retryMax, each within 20% either way, and never past retryMax.
func (q *queue) backoff(attempts int) time.Duration {
	d := q.first
	for i := 1; i < attempts && d < q.max; i++ {
		d *= 2
	}
	d = time.Duration(float64(d) * (0.8 + 0.4*q.jitter()))
	return min(d, q.max)
}

// record returns an entry's record and whether it is still queued.
func (q *queue) record(e *entry) (record, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	return e.rec, !e.gone
}

// hold keeps the retrier off an entry while a writer uses it, after any upload of it
// has finished. It is false if the entry was sent or removed meanwhile.
func (q *queue) hold(e *entry) bool {
	e.sending.Lock()
	defer e.sending.Unlock()
	q.mu.Lock()
	defer q.mu.Unlock()
	if e.gone {
		return false
	}
	e.held++
	return true
}

// unhold gives an entry back to the retrier, due a backoff from now.
func (q *queue) unhold(e *entry) {
	q.reschedule(e)
	q.mu.Lock()
	e.held--
	q.mu.Unlock()
	q.poke()
}

func (q *queue) poke() {
	select {
	case q.wake <- struct{}{}:
	default:
	}
}

// drop removes an entry, record and contents.
func (q *queue) drop(e *entry) {
	q.mu.Lock()
	if e.gone {
		q.mu.Unlock()
		return
	}
	e.gone = true
	delete(q.entries, e.seq)
	sent := uploadedIDs(e)
	q.mu.Unlock()
	if len(sent) > 0 {
		q.discard(sent...)
	}
	if err := os.RemoveAll(e.dir); err != nil {
		slog.Warn("removing an unsent write failed", "path", e.dir, "err", err)
	}
}

// rebase follows a stored file renamed through the mount in every record that
// replaces it: a base is the file wherever it now lives.
func (q *queue) rebase(id int, name string, folderID int) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for _, e := range q.entries {
		i := slices.IndexFunc(e.rec.Bases, func(b base) bool { return b.ID == id })
		if i < 0 {
			continue
		}
		rec := e.rec
		rec.Bases = slices.Clone(rec.Bases)
		rec.Bases[i].Name, rec.Bases[i].FolderID = name, folderID
		if err := writeRecord(e.dir, rec); err != nil {
			slog.Error("updating an unsent write's record failed", "path", e.dir, "err", err)
			continue
		}
		e.rec = rec
	}
}

// bind records the mount's node for an entry.
func (q *queue) bind(e *entry, n *file) {
	q.mu.Lock()
	e.node = n
	q.mu.Unlock()
}

// shadow hides stored files a write still being written was renamed over, or shows
// them again (by -1).
func (q *queue) shadow(bases []base, by int) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for _, b := range bases {
		if q.shadowed[b.ID] += by; q.shadowed[b.ID] <= 0 {
			delete(q.shadowed, b.ID)
		}
	}
}

// in returns the entries in a folder by name (the lowest sequence number, should a
// name be taken twice), and every stored file a write replaces.
func (q *queue) in(folderID int) (map[string]*entry, map[int]bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	var byName map[string]*entry
	var replaced map[int]bool
	add := func(id int) {
		if replaced == nil {
			replaced = map[int]bool{}
		}
		replaced[id] = true
	}
	for id := range q.shadowed {
		add(id)
	}
	for _, e := range q.entries {
		for _, b := range e.rec.Bases {
			add(b.ID)
		}
		if e.rec.FolderID != folderID {
			continue
		}
		if byName == nil {
			byName = map[string]*entry{}
		}
		if other, ok := byName[e.rec.Name]; !ok || e.seq < other.seq {
			byName[e.rec.Name] = e
		}
	}
	return byName, replaced
}

// due returns the entries the retrier should send now, in sequence order.
func (q *queue) due() []*entry {
	now := q.now()
	q.mu.Lock()
	defer q.mu.Unlock()
	var out []*entry
	for _, e := range q.entries {
		if e.held == 0 && !e.due.After(now) {
			out = append(out, e)
		}
	}
	slices.SortFunc(out, func(a, b *entry) int { return int(a.seq) - int(b.seq) })
	return out
}

// untilNext is how long until an entry the retrier may send is due; an hour if none,
// since adding or releasing one wakes the retrier.
func (q *queue) untilNext() time.Duration {
	now := q.now()
	q.mu.Lock()
	defer q.mu.Unlock()
	wait := time.Hour
	for _, e := range q.entries {
		if e.held == 0 {
			wait = min(wait, max(e.due.Sub(now), 0))
		}
	}
	return wait
}

// take returns the record of an entry the retrier may send.
func (q *queue) take(e *entry) (record, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	return e.rec, !e.gone && e.held == 0
}

// finish takes a sent entry out of the queue, returning its record as it now is (a
// rename may have changed it during the upload), its node, and whether it was removed
// or superseded during the upload.
func (q *queue) finish(e *entry) (record, *file, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	removed := e.gone
	e.gone = true
	delete(q.entries, e.seq)
	return e.rec, e.node, removed
}

// RetryUnsent sends the queued writes of the tree under root until ctx ends: each when
// it is due and no handle has it open, with backoff between failed attempts (#2865).
// Run it once the tree is mounted, and stop it before unmounting.
func RetryUnsent(ctx context.Context, root fs.InodeEmbedder) {
	t := root.(*dir).tree
	q := t.unsent
	for ctx.Err() == nil {
		for _, e := range q.due() {
			if ctx.Err() != nil {
				return
			}
			t.send(ctx, e)
		}
		timer := time.NewTimer(q.untilNext())
		select {
		case <-ctx.Done():
		case <-q.wake:
		case <-timer.C:
		}
		timer.Stop()
	}
}

// send uploads one queued entry under its record's current name and folder, or as a
// conflict copy (target).
func (t *tree) send(ctx context.Context, e *entry) {
	q := t.unsent
	e.sending.Lock()
	rec, ok := q.take(e)
	if !ok {
		e.sending.Unlock()
		return
	}
	sentAs, conflict := rec, false
	var got api.File
	var err error
	if u := rec.Uploaded; u != nil {
		// Sent on an earlier pass; only its rename is left.
		got = api.File{ID: u.ID, Name: u.Name, FolderID: u.FolderID}
		sentAs.Name, sentAs.FolderID = u.Name, u.FolderID
	} else {
		var name string
		name, conflict, err = t.target(ctx, rec.FolderID, rec.Name, rec.Bases, rec.SavedAt)
		if err == nil {
			got, err = t.uploadFile(ctx, rec.FolderID, name, e.contents())
		}
	}
	if err != nil {
		e.sending.Unlock()
		if ctx.Err() == nil {
			t.sendFailed(ctx, e, rec, err)
		}
		return
	}
	// The node becomes the stored file before a writer waiting to hold the entry can
	// run, and the kernel is told after, under no lock a lookup might wait on. What
	// follows the upload finishes even if the retrier is being stopped.
	post, cancel := context.WithTimeout(context.WithoutCancel(ctx), time.Minute)
	n := t.sent(post, e, sentAs, got, conflict)
	cancel()
	e.sending.Unlock()
	if n != nil {
		n.notify()
	}
}

func (t *tree) uploadFile(ctx context.Context, folderID int, name, path string) (api.File, error) {
	f, err := os.Open(path)
	if err != nil {
		return api.File{}, err
	}
	defer f.Close()
	ctx, cancel := context.WithTimeout(ctx, uploadTimeout)
	defer cancel()
	return t.lib.Upload(ctx, folderID, name, f)
}

// target is the name a write is sent under, from a fresh listing of its folder, so a
// save never overwrites a newer one: its own name if no file has it or only its bases
// do, else a conflict copy's (conflictName), and then no base is deleted. Library
// times are never compared with local ones.
func (t *tree) target(ctx context.Context, folderID int, name string, bases []base, savedAt time.Time) (string, bool, error) {
	l, err := t.lib.List(ctx, folderID, true)
	if err != nil {
		return "", false, err
	}
	for _, f := range l.Files {
		if f.FolderID == folderID && f.Name == name && !ours(bases, f) {
			copyName := conflictName(name, savedAt)
			slog.Warn("another file took the name of a write; it is sent as a conflict copy",
				"folder", folderID, "name", name, "file", f.ID, "bases", ids(bases), "copy", copyName)
			return copyName, true, nil
		}
	}
	return name, false, nil
}

// deleteBases deletes, once a write that replaces them is sent, the bases that are
// still the same file under the same name, from a fresh listing; a base renamed, moved
// or replaced meanwhile is someone else's now and is left alone. The first failed
// delete is the errno, logged with the new file's ID.
func (t *tree) deleteBases(ctx context.Context, bases []base, newID int) syscall.Errno {
	listed := map[int][]api.File{}
	var errno syscall.Errno
	for _, b := range bases {
		files, ok := listed[b.FolderID]
		if !ok {
			l, err := t.lib.List(ctx, b.FolderID, true)
			if err != nil {
				return failed("replace: the old file remains beside the new one", err, "old", b.ID, "new", newID)
			}
			files = l.Files
			listed[b.FolderID] = files
		}
		if !slices.ContainsFunc(files, func(f api.File) bool { return b.is(f) && f.Name == b.Name && f.FolderID == b.FolderID }) {
			continue
		}
		if err := t.lib.DeleteFiles(ctx, b.ID); err != nil {
			if errno == 0 {
				errno = failed("replace: the old file remains beside the new one", err, "old", b.ID, "new", newID)
			}
			continue
		}
		t.cache.drop(b.ID)
		t.unremember(b.ID)
		t.forget(b.FolderID)
	}
	return errno
}

// sendFailed counts a failed attempt. A refusal moves the entry to refused/. A 404
// whose folder the library no longer has sends the entry to the top level from the
// next attempt on.
func (t *tree) sendFailed(ctx context.Context, e *entry, rec record, cause error) {
	q := t.unsent
	if !queueable(cause) {
		n, err := q.moveRefused(e, cause)
		if err != nil {
			slog.Error("moving a refused unsent write failed; it stays queued", "path", e.dir, "err", err)
			q.reschedule(e)
			return
		}
		t.forget(rec.FolderID)
		if n != nil {
			// It leaves its folder; a stored file it was to replace shows again.
			n.bind(nil)
			if n.current().ID == 0 {
				n.drop()
			} else {
				n.notify()
			}
		}
		return
	}
	toTop := rec.FolderID != 0 && status(cause) == 404 && t.folderGone(ctx, rec.FolderID)
	err := q.update(e, func(r *record) {
		r.Attempts++
		r.LastError = cause.Error()
		r.LastAttempt = q.now().UTC()
		if toTop && r.FolderID == rec.FolderID {
			r.FolderID = 0
		}
	})
	if err != nil {
		slog.Error("updating an unsent write's record failed", "path", e.dir, "err", err)
	}
	q.reschedule(e)
	if toTop {
		t.forget(rec.FolderID)
		t.forget(0)
		slog.Warn("the unsent write's folder is gone; it will be sent to the top level", "path", e.dir, "folder", rec.FolderID, "name", rec.Name)
		if err := q.supersede(e); err != nil {
			slog.Error("superseding an earlier unsent save failed", "path", e.dir, "err", err)
		}
	}
	slog.Warn("sending an unsent write failed", "path", e.dir, "folder", rec.FolderID, "name", rec.Name, "attempts", rec.Attempts+1, "err", cause)
}

// folderGone is whether a listing succeeds without the folder.
func (t *tree) folderGone(ctx context.Context, folderID int) bool {
	l, err := t.lib.List(ctx, 0, false)
	if err != nil {
		return false
	}
	return !slices.ContainsFunc(l.Folders, func(f api.Folder) bool { return f.ID == folderID })
}

// sent finishes an entry the library now has as got, sent as sentAs says: applies a
// rename or removal made during the upload, deletes the bases still in place unless it
// was sent as a conflict copy, keeps its contents as got's cached ones, removes the
// entry and makes its node, which it returns, got's. If the rename fails, the entry
// stays, pointing at got, its bases untouched, and the next pass renames again.
func (t *tree) sent(ctx context.Context, e *entry, sentAs record, got api.File, conflict bool) *file {
	q := t.unsent
	// Drop the listing first, so the folder shows the new file once the entry is gone.
	t.forget(sentAs.FolderID)
	at := sentAs
	for !conflict {
		rec, ok := q.record(e)
		if !ok || (rec.Name == at.Name && rec.FolderID == at.FolderID) {
			break
		}
		name, folder := changes(at.Name, rec.Name, at.FolderID, rec.FolderID)
		moved, err := t.lib.UpdateFile(ctx, got.ID, name, folder)
		if err != nil {
			gone := status(err) == 404
			uerr := q.update(e, func(r *record) {
				r.Uploaded = &uploadedAs{ID: got.ID, FolderID: at.FolderID, Name: at.Name}
				if gone {
					// The sent file is gone: send the write again.
					r.Uploaded = nil
				}
				r.Attempts++
				r.LastError, r.LastAttempt = err.Error(), q.now().UTC()
			})
			if uerr != nil {
				slog.Error("updating an unsent write's record failed", "path", e.dir, "err", uerr)
			}
			q.reschedule(e)
			slog.Warn("renaming a sent write failed; it is renamed on the next pass", "path", e.dir, "file", got.ID, "name", rec.Name, "folder", rec.FolderID, "err", err)
			return nil
		}
		got.Name, got.FolderID = moved.Name, moved.FolderID
		at.Name, at.FolderID = rec.Name, rec.FolderID
		t.forget(rec.FolderID)
	}
	rec, n, removed := q.finish(e)
	if removed {
		if err := t.lib.DeleteFiles(ctx, got.ID); err != nil {
			slog.Warn("an unsent write removed or superseded while it was sent remains in the library", "file", got.ID, "name", got.Name, "err", err)
		}
		return nil
	}
	if !conflict && (rec.Name != at.Name || rec.FolderID != at.FolderID) {
		// Renamed between the last rename and now.
		name, folder := changes(at.Name, rec.Name, at.FolderID, rec.FolderID)
		if moved, err := t.lib.UpdateFile(ctx, got.ID, name, folder); err != nil {
			slog.Warn("renaming a sent write failed", "file", got.ID, "name", rec.Name, "folder", rec.FolderID, "err", err)
		} else {
			got.Name, got.FolderID = moved.Name, moved.FolderID
		}
		t.forget(got.FolderID)
	}
	if !conflict {
		t.deleteBases(ctx, rec.Bases, got.ID)
	}
	if err := os.MkdirAll(t.cache.dir, 0o700); err == nil {
		_ = os.Rename(e.contents(), t.cache.path(got.ID))
	}
	if err := os.RemoveAll(e.dir); err != nil {
		slog.Warn("removing a sent write's local copy failed", "path", e.dir, "err", err)
	}
	t.remember(got)
	slog.Info("unsent write sent", "path", e.dir, "folder", got.FolderID, "name", got.Name, "file", got.ID)
	if n != nil {
		n.mu.Lock()
		n.file, n.queued = got, nil
		n.mu.Unlock()
	}
	return n
}

// status is a library error's HTTP status, or 0.
func status(err error) int {
	var e *api.Error
	if errors.As(err, &e) {
		return e.Status
	}
	return 0
}

// queueable is whether a failed upload is worth sending again: everything but a
// refusal of the request itself, which would only be refused again.
func queueable(err error) bool {
	switch status(err) {
	case 400, 413, 415, 422:
		return false
	}
	return true
}

// Unsent is one queued or refused write, as the unsent command lists it. Err is why its
// record cannot be read; the other fields but Path and Refused are then empty.
type Unsent struct {
	Path      string
	Refused   bool
	FolderID  int
	Name      string
	Size      int64
	Attempts  int
	LastError string
	Err       error
}

// ListUnsent lists the writes queued, then those refused, under a state directory. It
// takes no lock: a running mount may be changing them.
func ListUnsent(stateDir string) ([]Unsent, error) {
	var out []Unsent
	for _, area := range []string{"unsent", "refused"} {
		all, err := readQueue(filepath.Join(stateDir, area))
		if err != nil {
			return nil, err
		}
		for _, s := range all {
			u := Unsent{Path: s.dir, Refused: area == "refused", Err: s.err}
			if s.err == nil {
				u.FolderID, u.Name, u.Size, u.Attempts, u.LastError = s.rec.FolderID, s.rec.Name, s.rec.Size, s.rec.Attempts, s.rec.LastError
			}
			out = append(out, u)
		}
	}
	return out, nil
}

// LockState takes <state dir>/lock for the mount's life, so two mounts never send one
// queue; the second fails at once. Unlock by closing the returned file.
func LockState(stateDir string) (*os.File, error) {
	f, err := os.OpenFile(filepath.Join(stateDir, "lock"), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, fmt.Errorf("another bambuddy-fs mount is using the state directory %s (its unsent writes); unmount it or pass a different --state-dir", stateDir)
		}
		return nil, err
	}
	return f, nil
}

// errQueue is a queue failure's errno, logged.
func errQueue(err error, args ...any) syscall.Errno {
	slog.Error("keeping an unsent write failed", append(args, "err", err)...)
	return syscall.EIO
}
