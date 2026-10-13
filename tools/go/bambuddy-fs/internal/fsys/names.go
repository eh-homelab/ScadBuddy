package fsys

import (
	"fmt"
	"slices"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
)

// Names gives each of a folder's files the name it shows as (spec §3). The oldest
// file of a name (Created, then ID) keeps it; later ones become
// "<stem> (id <N>)<ext>", so a path stays the same for as long as its file exists.
func Names(files []api.File) map[string]api.File {
	sorted := slices.Clone(files)
	slices.SortFunc(sorted, func(a, b api.File) int {
		if c := a.Created.Compare(b.Created); c != 0 {
			return c
		}
		return a.ID - b.ID
	})
	out := make(map[string]api.File, len(sorted))
	taken := map[string]bool{}
	for _, f := range sorted {
		name := f.Name
		if taken[name] {
			stem, ext := splitExt(name)
			name = fmt.Sprintf("%s (id %d)%s", stem, f.ID, ext)
		}
		taken[f.Name] = true
		out[name] = f
	}
	return out
}

// splitExt splits a name at the first dot that does not start it, so
// "a.gcode.3mf" is "a" and ".gcode.3mf", and ".hidden" has no extension.
func splitExt(name string) (stem, ext string) {
	if i := strings.Index(name[min(1, len(name)):], "."); i >= 0 {
		return name[:i+1], name[i+1:]
	}
	return name, ""
}

// folderNames names the subfolders of parent. Folders have no creation time, so the
// lowest ID keeps a name and the others become "<name> (id <N>)".
func folderNames(folders []api.Folder, parent int) map[string]api.Folder {
	var children []api.Folder
	for _, f := range folders {
		if f.ParentID == parent {
			children = append(children, f)
		}
	}
	slices.SortFunc(children, func(a, b api.Folder) int { return a.ID - b.ID })
	out := make(map[string]api.Folder, len(children))
	for _, f := range children {
		name := f.Name
		if _, ok := out[name]; ok {
			name = fmt.Sprintf("%s (id %d)", name, f.ID)
		}
		out[name] = f
	}
	return out
}
