package fsys

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/telemetry"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// cache keeps file contents on disk as <dir>/<id>. A file's bytes never change for
// its ID, so an entry of the right size is always current. Past max bytes of them
// (0: no bound), the least recently used are removed (trim).
type cache struct {
	dir string
	max int64

	mu sync.Mutex
	// busy holds a channel per ID being fetched, closed when the fetch ends.
	busy map[int]chan struct{}
}

func newCache(dir string, max int64) *cache {
	return &cache{dir: dir, max: max, busy: map[int]chan struct{}{}}
}

func (c *cache) path(id int) string { return filepath.Join(c.dir, strconv.Itoa(id)) }

// open returns the file's cached contents, downloading them first into <id>.part and
// renaming that to <id> once its size matches. One fetch per ID runs at a time. A
// download is traced as the FUSE operation op, if one is named.
func (c *cache) open(ctx context.Context, lib Library, f api.File, op string) (*os.File, error) {
	if err := c.claim(ctx, f.ID); err != nil {
		return nil, err
	}
	defer c.release(f.ID)

	path := c.path(f.ID)
	if fh, err := os.Open(path); err == nil {
		if st, err := fh.Stat(); err == nil && st.Size() == f.Size {
			// Its time is when it was last used, for trim.
			now := time.Now()
			_ = os.Chtimes(path, now, now)
			return fh, nil
		}
		_ = fh.Close()
	}
	if err := os.MkdirAll(c.dir, 0o700); err != nil {
		return nil, err
	}
	var span *telemetry.Op
	if op != "" {
		ctx, span = telemetry.StartOp(ctx, op, telemetry.FileID(f.ID), telemetry.Bytes(f.Size))
	}
	part := path + ".part"
	err := c.download(ctx, lib, f, part)
	if err == nil {
		err = os.Rename(part, path)
	}
	span.End(api.Errno(err))
	if err != nil {
		_ = os.Remove(part)
		return nil, err
	}
	fh, err := os.Open(path)
	c.trim(f.ID)
	return fh, err
}

// trim removes the least recently used contents until those left fit in max, never
// keep's: what was just added. Only contents (files named by an ID) count and go,
// never a file being written (write-*) or fetched (.part); writes not sent are under
// the state directory, not here. Removing contents an open handle reads is safe: the
// handle keeps them until it closes.
func (c *cache) trim(keep int) {
	if c.max <= 0 {
		return
	}
	entries, err := os.ReadDir(c.dir)
	if err != nil {
		return
	}
	type content struct {
		path string
		size int64
		used time.Time
	}
	var all []content
	var total int64
	for _, e := range entries {
		id, err := strconv.Atoi(e.Name())
		if err != nil || id == keep {
			continue
		}
		info, err := e.Info()
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		all = append(all, content{filepath.Join(c.dir, e.Name()), info.Size(), info.ModTime()})
		total += info.Size()
	}
	if st, err := os.Stat(c.path(keep)); err == nil {
		total += st.Size()
	}
	slices.SortFunc(all, func(a, b content) int { return a.used.Compare(b.used) })
	for _, old := range all {
		if total <= c.max {
			return
		}
		if err := os.Remove(old.path); err != nil && !errors.Is(err, os.ErrNotExist) {
			slog.Warn("cache trim failed", "path", old.path, "err", err)
			continue
		}
		total -= old.size
	}
}

func (c *cache) download(ctx context.Context, lib Library, f api.File, part string) error {
	out, err := os.OpenFile(part, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	err = lib.Download(ctx, f.ID, out)
	if closeErr := out.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	st, err := os.Stat(part)
	if err != nil {
		return err
	}
	if st.Size() != f.Size {
		return fmt.Errorf("download %d: got %d bytes, the library says %d", f.ID, st.Size(), f.Size)
	}
	return nil
}

func (c *cache) claim(ctx context.Context, id int) error {
	for {
		c.mu.Lock()
		wait, taken := c.busy[id]
		if !taken {
			c.busy[id] = make(chan struct{})
			c.mu.Unlock()
			return nil
		}
		c.mu.Unlock()
		select {
		case <-wait:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (c *cache) release(id int) {
	c.mu.Lock()
	close(c.busy[id])
	delete(c.busy, id)
	c.mu.Unlock()
}

// has is whether a file's contents are cached.
func (c *cache) has(f api.File) bool {
	st, err := os.Stat(c.path(f.ID))
	return err == nil && st.Size() == f.Size
}

// size is the bytes in the cache directory, for the cache gauge. The directory is
// flat, so this is a listing and a stat per file at each collection (every minute).
func (c *cache) size() int64 {
	entries, err := os.ReadDir(c.dir)
	if err != nil {
		return 0
	}
	var total int64
	for _, e := range entries {
		if info, err := e.Info(); err == nil && info.Mode().IsRegular() {
			total += info.Size()
		}
	}
	return total
}

// drop removes a file's cached contents.
func (c *cache) drop(id int) {
	for _, p := range []string{c.path(id), c.path(id) + ".part"} {
		if err := os.Remove(p); err != nil && !errors.Is(err, os.ErrNotExist) {
			slog.Warn("cache drop failed", "file", id, "err", err)
		}
	}
}

func (n *file) Open(ctx context.Context, flags uint32) (fs.FileHandle, uint32, syscall.Errno) {
	write := flags&(syscall.O_WRONLY|syscall.O_RDWR) != 0
	if write && n.tree.opts.ReadOnly {
		return nil, 0, syscall.EROFS
	}
	// While a write is pending, every handle reads and writes its contents.
	if h, errno := n.attach(write, false); errno != 0 {
		return nil, 0, errno
	} else if h != nil {
		return h, 0, 0
	}
	// A queued write is read from its local copy, unless it was sent meanwhile.
	if e := n.entry(); e != nil {
		fh, err := os.Open(e.contents())
		if err == nil {
			return &handle{f: fh}, 0, 0
		}
		if n.entry() == e {
			return nil, 0, failed("open", err, "path", e.dir)
		}
	}
	f := n.current()
	if f.ID == 0 {
		// Created here, never uploaded, and no longer being written.
		return nil, 0, syscall.EIO
	}
	// The contents are fetched by the first read, so an open that only looks (a file
	// manager sniffing types, an fstat) downloads nothing. The bytes never change for
	// this inode, so the kernel may keep its pages.
	return &handle{node: n, file: f}, fuse.FOPEN_KEEP_CACHE, 0
}

// gone forgets a file the library no longer has: its cached contents, its folder's
// listing, and the parent's entry, here and in the kernel.
func (n *file) gone() {
	n.tree.cache.drop(n.current().ID)
	n.tree.unremember(n.current().ID)
	name, parent := n.Parent()
	if parent == nil {
		return
	}
	if d, ok := parent.Operations().(*dir); ok {
		n.tree.forget(d.id)
	}
	parent.RmChild(name)
	if errno := parent.NotifyEntry(name); errno != 0 {
		slog.Warn("entry invalidation failed", "name", name, "err", errno)
	}
}

// handle reads an open file's contents: a queued write's local copy, open from the
// start, or a stored file's cached contents, which the first read fetches.
type handle struct {
	node *file
	file api.File

	mu sync.Mutex
	f  *os.File
}

var (
	_ fs.FileReader   = (*handle)(nil)
	_ fs.FileReleaser = (*handle)(nil)
)

func (h *handle) Read(ctx context.Context, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.f == nil {
		fh, err := h.node.tree.cache.open(ctx, h.node.tree.lib, h.file, "read")
		if err != nil {
			slog.Warn("read failed", "file", h.file.ID, "err", err)
			errno := api.Errno(err)
			if errno == syscall.ENOENT {
				h.node.gone()
			}
			return nil, errno
		}
		h.f = fh
	}
	n, err := h.f.ReadAt(dest, off)
	if err != nil && !errors.Is(err, io.EOF) {
		slog.Warn("read failed", "path", h.f.Name(), "err", err)
		return nil, api.Errno(err)
	}
	return fuse.ReadResultData(dest[:n]), 0
}

func (h *handle) Release(context.Context) syscall.Errno {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.f != nil {
		_ = h.f.Close()
	}
	return 0
}
