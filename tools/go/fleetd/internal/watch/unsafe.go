//go:build linux

package watch

import "unsafe"

// unsafePointer is &buf[off]: inotify's events are read in place, as x/sys lays them out.
func unsafePointer(buf []byte, off int) unsafe.Pointer { return unsafe.Pointer(&buf[off]) }
