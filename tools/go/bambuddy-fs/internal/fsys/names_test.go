package fsys

import (
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
)

func TestDuplicateNamesGetTheirID(t *testing.T) {
	t0 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	files := []api.File{
		{ID: 12, Name: "a.3mf", Created: t0.Add(time.Hour)},
		{ID: 11, Name: "a.3mf", Created: t0},
		{ID: 13, Name: "b.stl", Created: t0},
	}
	got := Names(files)
	if got["a.3mf"].ID != 11 {
		t.Fatalf("oldest keeps the plain name: %+v", got)
	}
	if got["a (id 12).3mf"].ID != 12 {
		t.Fatalf("later one is suffixed: %+v", got)
	}
	if got["b.stl"].ID != 13 {
		t.Fatal("unique name kept")
	}
}

func TestANameWithoutExtensionIsSuffixedAtTheEnd(t *testing.T) {
	got := Names([]api.File{{ID: 1, Name: "README"}, {ID: 2, Name: "README"}})
	if _, ok := got["README (id 2)"]; !ok {
		t.Fatalf("%v", got)
	}
}

func TestTheExtensionStartsAtTheFirstDotPastTheStart(t *testing.T) {
	got := Names([]api.File{
		{ID: 1, Name: "a.gcode.3mf"}, {ID: 2, Name: "a.gcode.3mf"},
		{ID: 3, Name: ".hidden"}, {ID: 4, Name: ".hidden"},
	})
	if _, ok := got["a (id 2).gcode.3mf"]; !ok {
		t.Fatalf("%v", got)
	}
	if _, ok := got[".hidden (id 4)"]; !ok {
		t.Fatalf("%v", got)
	}
}

func TestEqualCreationTimesFallBackToTheID(t *testing.T) {
	got := Names([]api.File{{ID: 9, Name: "x.3mf"}, {ID: 3, Name: "x.3mf"}})
	if got["x.3mf"].ID != 3 || got["x (id 9).3mf"].ID != 9 {
		t.Fatalf("%v", got)
	}
}

func TestDuplicateFolderNamesGetTheirID(t *testing.T) {
	got, _ := shownNames([]api.Folder{
		{ID: 7, Name: "Parts", ParentID: 1},
		{ID: 5, Name: "Parts", ParentID: 1},
		{ID: 6, Name: "Parts", ParentID: 2},
	}, nil, 1)
	if len(got) != 2 || got["Parts"].ID != 5 || got["Parts (id 7)"].ID != 7 {
		t.Fatalf("%v", got)
	}
}

func TestAGeneratedNameThatIsSomeonesRealNameIsDisambiguated(t *testing.T) {
	t0 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	// The real "a (id 12).3mf" is newer than file 12, and still keeps its name.
	got := Names([]api.File{
		{ID: 10, Name: "a.3mf", Created: t0},
		{ID: 12, Name: "a.3mf", Created: t0.Add(time.Hour)},
		{ID: 13, Name: "a (id 12).3mf", Created: t0.Add(2 * time.Hour)},
		{ID: 14, Name: "a (id 12) (2).3mf", Created: t0.Add(3 * time.Hour)},
	})
	want := map[string]int{"a.3mf": 10, "a (id 12).3mf": 13, "a (id 12) (2).3mf": 14, "a (id 12) (3).3mf": 12}
	if len(got) != len(want) {
		t.Fatalf("%v", got)
	}
	for name, id := range want {
		if got[name].ID != id {
			t.Fatalf("%s: %v", name, got)
		}
	}
}

func TestAFolderWinsANameOverAFile(t *testing.T) {
	folders, files := shownNames(
		[]api.Folder{{ID: 4, Name: "Parts"}, {ID: 5, Name: "Parts (id 4)"}, {ID: 4, Name: "x", ParentID: 9}},
		[]api.File{{ID: 10, Name: "Parts"}, {ID: 11, Name: "Parts (id 4)"}, {ID: 12, Name: "b.3mf"}},
		0,
	)
	if len(folders) != 2 || folders["Parts"].ID != 4 || folders["Parts (id 4)"].ID != 5 {
		t.Fatalf("folders %v", folders)
	}
	if len(files) != 3 || files["Parts (id 10)"].ID != 10 || files["Parts (id 4) (id 11)"].ID != 11 || files["b.3mf"].ID != 12 {
		t.Fatalf("files %v", files)
	}
}
