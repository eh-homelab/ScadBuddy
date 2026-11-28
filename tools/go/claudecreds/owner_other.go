//go:build !unix

package claudecreds

import (
	"io/fs"
	"os"
)

// keepOwner does nothing where files have no Unix owner.
func keepOwner(*os.File, fs.FileInfo) {}
