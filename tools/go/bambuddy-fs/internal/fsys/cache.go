package fsys

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// cache keeps file contents on disk as <dir>/<id>. A file's bytes never change for
// its ID, so an entry of the right size is always current.
type cache struct {
	dir string

	mu sync.Mutex
	// busy holds a channel per ID being fetched, closed when the fetch ends.
	busy map[int]chan struct{}
}

func newCache(dir string) *cache {
	return &cache{dir: dir, busy: map[int]chan struct{}{}}
}

func (c *cache) path(id int) string { return filepath.Join(c.dir, strconv.Itoa(id)) }

// open returns the file's cached contents, downloading them first into <id>.part and
// renaming that to <id> once its size matches. One fetch per ID runs at a time.
func (c *cache) open(ctx context.Context, lib Library, f api.File) (*os.File, error) {
	if err := c.claim(ctx, f.ID); err != nil {
		return nil, err
	}
	defer c.release(f.ID)

	path := c.path(f.ID)
	if fh, err := os.Open(path); err == nil {
		if st, err := fh.Stat(); err == nil && st.Size() == f.Size {
			return fh, nil
		}
		_ = fh.Close()
	}
	if err := os.MkdirAll(c.dir, 0o700); err != nil {
		return nil, err
	}
	part := path + ".part"
	if err := c.download(ctx, lib, f, part); err != nil {
		_ = os.Remove(part)
		return nil, err
	}
	if err := os.Rename(part, path); err != nil {
		return nil, err
	}
	return os.Open(path)
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

// drop removes a file's cached contents.
func (c *cache) drop(id int) {
	for _, p := range []string{c.path(id), c.path(id) + ".part"} {
		if err := os.Remove(p); err != nil && !errors.Is(err, os.ErrNotExist) {
			slog.Warn("cache drop failed", "file", id, "err", err)
		}
	}
}

func (n *file) Open(ctx context.Context, flags uint32) (fs.FileHandle, uint32, syscall.Errno) {
	if flags&(syscall.O_WRONLY|syscall.O_RDWR) != 0 {
		if n.tree.opts.ReadOnly {
			return nil, 0, syscall.EROFS
		}
		return n.write(false), 0, 0
	}
	f := n.current()
	fh, err := n.tree.cache.open(ctx, n.tree.lib, f)
	if err != nil {
		slog.Warn("open failed", "file", f.ID, "err", err)
		errno := api.Errno(err)
		if errno == syscall.ENOENT {
			n.gone()
		}
		return nil, 0, errno
	}
	// The bytes never change for this inode, so the kernel may keep its pages.
	return &handle{f: fh}, fuse.FOPEN_KEEP_CACHE, 0
}

// gone forgets a file the library no longer has: its cached contents, its folder's
// listing, and the parent's entry, here and in the kernel.
func (n *file) gone() {
	n.tree.cache.drop(n.current().ID)
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

// handle reads an open file's cached contents.
type handle struct {
	f *os.File
}

var (
	_ fs.FileReader   = (*handle)(nil)
	_ fs.FileReleaser = (*handle)(nil)
)

func (h *handle) Read(_ context.Context, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	n, err := h.f.ReadAt(dest, off)
	if err != nil && !errors.Is(err, io.EOF) {
		slog.Warn("read failed", "path", h.f.Name(), "err", err)
		return nil, api.Errno(err)
	}
	return fuse.ReadResultData(dest[:n]), 0
}

func (h *handle) Release(context.Context) syscall.Errno {
	_ = h.f.Close()
	return 0
}
