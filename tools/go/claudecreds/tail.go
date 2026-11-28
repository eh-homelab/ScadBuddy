package claudecreds

import (
	"bytes"
	"errors"
	"io"
	"io/fs"
	"log/slog"
	"os"
)

// MaxLineBytes is the longest partial line Tail keeps while it waits for the line's
// newline. A longer line is dropped up to its newline, and Tail logs that once. No
// credential event is that long.
const MaxLineBytes = 4 << 20

// headBytes is how much of the file's start Tail keeps, to see a rewrite.
const headBytes = 4096

// Tail follows one transcript file and returns its credential events. It starts no
// goroutine: the caller drives it with Poll and ends it with Close. It is not safe for
// concurrent use.
//
// The first Poll that finds the file starts at its end. A file that is missing at the
// first Poll is new when it appears, so Tail reads it from 0. Tail keeps the file open
// by inode: when the path names a different file (a rename or a new file), Tail reads
// what is left of the old one, then reads the new one from 0. While the path names no
// file, Tail keeps the old one open and reads what its writer still appends. When the
// file gets shorter than the offset (a truncate), or its first bytes change (a truncate
// and a rewrite past the offset between two Polls), Tail reads it again from 0. A
// rewrite that keeps the first 4 KiB is not seen.
type Tail struct {
	path     string
	f        *os.File
	off      int64
	partial  []byte
	head     []byte
	skipping bool
	warned   bool
	polled   bool

	// afterRead, if set, runs after each read of the open file (tests only).
	afterRead func()
}

// NewTail returns a Tail of path. It opens nothing until the first Poll.
func NewTail(path string) *Tail {
	return &Tail{path: path}
}

// Poll reads the complete lines written since the last Poll and returns their events in
// file order. It keeps a partial last line for the next Poll. A missing file is not an
// error: Poll returns no events.
func (t *Tail) Poll() ([]Event, error) {
	first := !t.polled
	t.polled = true
	var events []Event
	if t.f != nil {
		ev, err := t.read()
		events = append(events, ev...)
		if err != nil {
			return events, err
		}
		same, err := t.samePath()
		if err != nil || same {
			return events, err
		}
		// The path names another file or none. Read the old file once more: a line
		// written between the read and the rename is in it.
		ev, err = t.read()
		events = append(events, ev...)
		if err != nil {
			return events, err
		}
	}
	f, err := os.Open(t.path)
	if errors.Is(err, fs.ErrNotExist) {
		// Keep the old file, if any, open until a new file takes the path.
		return events, nil
	}
	if err != nil {
		return events, err
	}
	if t.f != nil {
		// The old file may have been renamed back between the check and the open.
		if same, err := sameFile(f, t.f); err != nil || same {
			f.Close()
			return events, err
		}
		t.f.Close()
	}
	t.f, t.off = f, 0
	t.reset()
	if first {
		fi, err := f.Stat()
		if err != nil {
			return events, err
		}
		t.off = fi.Size()
	}
	ev, err := t.read()
	return append(events, ev...), err
}

// Close closes the file that Tail holds open.
func (t *Tail) Close() error {
	if t.f == nil {
		return nil
	}
	err := t.f.Close()
	t.f = nil
	return err
}

// reset forgets what Tail kept of the file before the offset.
func (t *Tail) reset() {
	t.partial, t.head, t.skipping = nil, nil, false
}

// samePath tells whether the path still names the open file. A missing path is not the
// same file.
func (t *Tail) samePath() (bool, error) {
	pfi, err := os.Stat(t.path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	ffi, err := t.f.Stat()
	if err != nil {
		return false, err
	}
	return os.SameFile(pfi, ffi), nil
}

// sameFile tells whether a and b are open on one file.
func sameFile(a, b *os.File) (bool, error) {
	afi, err := a.Stat()
	if err != nil {
		return false, err
	}
	bfi, err := b.Stat()
	if err != nil {
		return false, err
	}
	return os.SameFile(afi, bfi), nil
}

// rewritten tells whether the file's first bytes differ from the ones Tail kept.
func (t *Tail) rewritten(size int64) (bool, error) {
	if len(t.head) == 0 {
		return false, nil
	}
	if size < int64(len(t.head)) {
		return true, nil
	}
	b := make([]byte, len(t.head))
	if _, err := t.f.ReadAt(b, 0); err != nil && !errors.Is(err, io.EOF) {
		return false, err
	}
	return !bytes.Equal(b, t.head), nil
}

// keepHead keeps up to headBytes of the file's start.
func (t *Tail) keepHead(size int64) error {
	n := min(int64(headBytes), size)
	if int64(len(t.head)) >= n {
		return nil
	}
	b := make([]byte, n)
	got, err := t.f.ReadAt(b, 0)
	if err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	t.head = b[:got]
	return nil
}

// read reads the open file from the offset to its end.
func (t *Tail) read() ([]Event, error) {
	if t.afterRead != nil {
		defer t.afterRead()
	}
	fi, err := t.f.Stat()
	if err != nil {
		return nil, err
	}
	size := fi.Size()
	rewritten, err := t.rewritten(size)
	if err != nil {
		return nil, err
	}
	if size < t.off || rewritten {
		t.off = 0
		t.reset()
	}
	if err := t.keepHead(size); err != nil {
		return nil, err
	}
	if size == t.off {
		return nil, nil
	}
	buf, err := io.ReadAll(io.NewSectionReader(t.f, t.off, size-t.off))
	if err != nil {
		return nil, err
	}
	t.off += int64(len(buf))
	data := append(t.partial, buf...)
	if t.skipping {
		i := bytes.IndexByte(data, '\n')
		if i < 0 {
			t.partial = nil
			return nil, nil
		}
		data, t.skipping = data[i+1:], false
	}
	var events []Event
	for {
		i := bytes.IndexByte(data, '\n')
		if i < 0 {
			break
		}
		if ev, ok := ParseEntry(data[:i]); ok {
			events = append(events, ev)
		}
		data = data[i+1:]
	}
	if len(data) > MaxLineBytes {
		if !t.warned {
			slog.Warn("claudecreds: a transcript line is too long; it is dropped", "path", t.path, "max_bytes", MaxLineBytes)
			t.warned = true
		}
		t.partial, t.skipping = nil, true
		return events, nil
	}
	t.partial = append([]byte(nil), data...)
	return events, nil
}
