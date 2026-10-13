package fsys

import (
	"fmt"
	"slices"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
)

// Names gives each of a folder's files the name it shows as (spec §3); see shownNames.
func Names(files []api.File) map[string]api.File {
	_, out := shownNames(nil, files, 0)
	return out
}

// shownNames names parent's subfolders and files so that every name is shown once and
// stays the same for as long as its entry exists (spec §3). A real name goes to the
// folder of that name with the lowest ID (folders have no creation time), else to
// the oldest file of that name (Created, then ID). Every other entry is generated a
// name, "<name> (id <N>)" for a folder and "<stem> (id <N>)<ext>" for a file, and only
// once every real name is placed, so a generated name never hides a real one: should
// it be one, " (2)", " (3)", ... follows the id.
func shownNames(folders []api.Folder, files []api.File, parent int) (map[string]api.Folder, map[string]api.File) {
	var subs []api.Folder
	for _, f := range folders {
		if f.ParentID == parent {
			subs = append(subs, f)
		}
	}
	slices.SortFunc(subs, func(a, b api.Folder) int { return a.ID - b.ID })
	sorted := slices.Clone(files)
	slices.SortFunc(sorted, func(a, b api.File) int {
		if c := a.Created.Compare(b.Created); c != 0 {
			return c
		}
		return a.ID - b.ID
	})

	taken := map[string]bool{}
	outFolders := make(map[string]api.Folder, len(subs))
	outFiles := make(map[string]api.File, len(sorted))
	var laterFolders []api.Folder
	var laterFiles []api.File
	for _, f := range subs {
		if taken[f.Name] {
			laterFolders = append(laterFolders, f)
			continue
		}
		taken[f.Name] = true
		outFolders[f.Name] = f
	}
	for _, f := range sorted {
		if taken[f.Name] {
			laterFiles = append(laterFiles, f)
			continue
		}
		taken[f.Name] = true
		outFiles[f.Name] = f
	}
	for _, f := range laterFolders {
		outFolders[generated(taken, fmt.Sprintf("%s (id %d)", f.Name, f.ID), "")] = f
	}
	for _, f := range laterFiles {
		stem, ext := splitExt(f.Name)
		outFiles[generated(taken, fmt.Sprintf("%s (id %d)", stem, f.ID), ext)] = f
	}
	return outFolders, outFiles
}

// generated takes the first free name of base+ext, base+" (2)"+ext, ...
func generated(taken map[string]bool, base, ext string) string {
	name := base + ext
	for n := 2; taken[name]; n++ {
		name = fmt.Sprintf("%s (%d)%s", base, n, ext)
	}
	taken[name] = true
	return name
}

// splitExt splits a name at the first dot that does not start it, so
// "a.gcode.3mf" is "a" and ".gcode.3mf", and ".hidden" has no extension.
func splitExt(name string) (stem, ext string) {
	if i := strings.Index(name[min(1, len(name)):], "."); i >= 0 {
		return name[:i+1], name[i+1:]
	}
	return name, ""
}
