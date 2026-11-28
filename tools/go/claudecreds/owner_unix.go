//go:build unix

package claudecreds

import (
	"io/fs"
	"os"
	"syscall"
)

// keepOwner gives f the user and group of fi, best effort. A process that is not root
// can set only its own user and a group it is in; then it keeps at least the group.
func keepOwner(f *os.File, fi fs.FileInfo) {
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return
	}
	if f.Chown(int(st.Uid), int(st.Gid)) != nil {
		_ = f.Chown(-1, int(st.Gid))
	}
}
