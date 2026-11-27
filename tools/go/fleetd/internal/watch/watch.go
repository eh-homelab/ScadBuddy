//go:build linux

// Package watch tells of changes in a set of directories with inotify: a file created,
// written, moved in or out, or removed. It says only that something changed, never what,
// and coalesces a burst into one signal.
package watch

import (
	"os"
	"sync"

	"golang.org/x/sys/unix"
)

const mask = unix.IN_CREATE | unix.IN_CLOSE_WRITE | unix.IN_DELETE | unix.IN_MOVED_TO |
	unix.IN_MOVED_FROM | unix.IN_DELETE_SELF | unix.IN_MOVE_SELF

// Watcher watches directories.
type Watcher struct {
	fd      int // never f.Fd(), which would make the fd blocking
	f       *os.File
	changes chan struct{}

	mu      sync.Mutex
	dirs    []string
	watched map[string]int // dir to its watch descriptor
	byWD    map[int]string
}

// New starts a watcher of dirs. A dir that does not exist yet is watched once Ensure finds
// it.
func New(dirs []string) (*Watcher, error) {
	fd, err := unix.InotifyInit1(unix.IN_CLOEXEC | unix.IN_NONBLOCK)
	if err != nil {
		return nil, err
	}
	// Non-blocking, so the runtime's poller waits for it and Close ends a Read.
	w := &Watcher{fd: fd, f: os.NewFile(uintptr(fd), "inotify"), changes: make(chan struct{}, 1), dirs: dirs,
		watched: map[string]int{}, byWD: map[int]string{}}
	w.Ensure()
	go w.read()
	return w, nil
}

// Changes receives one value after any number of changes.
func (w *Watcher) Changes() <-chan struct{} { return w.changes }

// Ensure watches each dir not watched yet that now exists, and says whether it added one
// (so the caller scans again: a change before the watch was missed).
func (w *Watcher) Ensure() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	added := false
	for _, d := range w.dirs {
		if _, ok := w.watched[d]; ok {
			continue
		}
		wd, err := unix.InotifyAddWatch(w.fd, d, mask|unix.IN_ONLYDIR)
		if err != nil {
			continue
		}
		w.watched[d], w.byWD[wd] = wd, d
		added = true
	}
	return added
}

// Watched is how many dirs are watched now.
func (w *Watcher) Watched() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.watched)
}

func (w *Watcher) read() {
	buf := make([]byte, 64<<10)
	for {
		n, err := w.f.Read(buf)
		if err != nil {
			return
		}
		for off := 0; off+unix.SizeofInotifyEvent <= n; {
			ev := (*unix.InotifyEvent)(unsafePointer(buf, off))
			if ev.Mask&(unix.IN_IGNORED|unix.IN_DELETE_SELF|unix.IN_MOVE_SELF) != 0 {
				// The dir went: forget it, so Ensure watches it again when it is back.
				w.mu.Lock()
				if d, ok := w.byWD[int(ev.Wd)]; ok {
					delete(w.byWD, int(ev.Wd))
					delete(w.watched, d)
				}
				w.mu.Unlock()
			}
			off += unix.SizeofInotifyEvent + int(ev.Len)
		}
		select {
		case w.changes <- struct{}{}:
		default:
		}
	}
}

// Close stops the watcher.
func (w *Watcher) Close() error { return w.f.Close() }
