package claudecreds

import (
	"bytes"
	"errors"
	"io"
	"io/fs"
	"os"
)

// Tail follows one transcript file and returns its credential events. It starts no
// goroutine: the caller drives it with Poll and ends it with Close. It is not safe for
// concurrent use.
//
// The first Poll that finds the file starts at its end. A file that is missing at the
// first Poll is new when it appears, so Tail reads it from 0. Tail keeps the file open
// by inode: when the path names a different file (a rename or a new file), Tail reads
// what is left of the old one, then reads the new one from 0. When the file gets
// shorter than the offset (a truncate), Tail reads it again from 0.
type Tail struct {
	path    string
	f       *os.File
	off     int64
	partial []byte
	polled  bool
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
		t.f.Close()
		t.f = nil
	}
	f, err := os.Open(t.path)
	if errors.Is(err, fs.ErrNotExist) {
		return events, nil
	}
	if err != nil {
		return events, err
	}
	t.f, t.off, t.partial = f, 0, nil
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

// read reads the open file from the offset to its end.
func (t *Tail) read() ([]Event, error) {
	fi, err := t.f.Stat()
	if err != nil {
		return nil, err
	}
	if fi.Size() < t.off {
		t.off, t.partial = 0, nil
	}
	if fi.Size() == t.off {
		return nil, nil
	}
	buf, err := io.ReadAll(io.NewSectionReader(t.f, t.off, fi.Size()-t.off))
	if err != nil {
		return nil, err
	}
	t.off += int64(len(buf))
	data := append(t.partial, buf...)
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
	t.partial = append([]byte(nil), data...)
	return events, nil
}
