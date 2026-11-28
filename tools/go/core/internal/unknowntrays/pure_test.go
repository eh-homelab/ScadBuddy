package unknowntrays

import (
	"encoding/json"
	"math"
	"os"
	"testing"
)

// vectorFile is the shared vectors that backend/tests/filament/test_unknown_tray_vectors.py
// writes.
const vectorFile = "../../../../../backend/tests/fixtures/unknown-tray-vectors.json"

type vectors struct {
	ColourDistance []struct {
		Left, Right *string
		Distance    *float64
	} `json:"colour_distance"`
	Labels []struct {
		AmsID      int    `json:"ams_id"`
		TrayID     int    `json:"tray_id"`
		TrayLabel  string `json:"tray_label"`
		PlaceLabel string `json:"place_label_for_printer_H2C"`
	} `json:"labels"`
}

func readVectors(t *testing.T) *vectors {
	t.Helper()
	raw, err := os.ReadFile(vectorFile)
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return &v
}

func TestColourDistanceMatchesPython(t *testing.T) {
	v := readVectors(t)
	if len(v.ColourDistance) < 20 {
		t.Fatalf("the vectors hold %d pairs", len(v.ColourDistance))
	}
	for _, c := range v.ColourDistance {
		got := ColourDistance(c.Left, c.Right)
		switch {
		case c.Distance == nil && got != nil:
			t.Errorf("%v %v: got %v, want nil", c.Left, c.Right, *got)
		case c.Distance != nil && got == nil:
			t.Errorf("%v %v: got nil, want %v", c.Left, c.Right, *c.Distance)
		case c.Distance != nil && math.Abs(*got-*c.Distance) > 1e-9:
			t.Errorf("%v %v: got %v, want %v", *c.Left, *c.Right, *got, *c.Distance)
		}
	}
}

func TestLabelsMatchPython(t *testing.T) {
	v := readVectors(t)
	if len(v.Labels) == 0 {
		t.Fatal("the vectors hold no labels")
	}
	for _, l := range v.Labels {
		if got := TrayLabel(l.AmsID, l.TrayID); got != l.TrayLabel {
			t.Errorf("TrayLabel(%d, %d) = %q, want %q", l.AmsID, l.TrayID, got, l.TrayLabel)
		}
		if got := PlaceLabel("H2C", l.AmsID, l.TrayID); got != l.PlaceLabel {
			t.Errorf("PlaceLabel(%d, %d) = %q, want %q", l.AmsID, l.TrayID, got, l.PlaceLabel)
		}
	}
}

func ptr[T any](v T) *T { return &v }

func spool(number int, material, rgba string, placed bool) SpoolRow {
	s := SpoolRow{ID: "id", Number: number, MaterialID: ptr(material), Material: material, RGBA: ptr(rgba)}
	if placed {
		s.PlaceID = ptr("place")
	}
	return s
}

var materialKeys = map[string]map[string]bool{
	"PLA": {"PLA": true}, "PETG": {"PETG": true},
}

func numbers(c []Candidate) []int {
	out := []int{}
	for _, x := range c {
		out = append(out, x.Number)
	}
	return out
}

func TestRankPutsUnplacedThenMaterialThenColour(t *testing.T) {
	row := Row{Material: "PLA", ColourRGBA: ptr("#FF0000FF")}
	spools := []SpoolRow{
		spool(1, "PLA", "#FF0000FF", true),   // A: placed
		spool(2, "PETG", "#FF0000FF", false), // B: other material
		spool(3, "PLA", "#EE0000FF", false),  // C: near red
		spool(4, "PLA", "#0000FFFF", false),  // D: blue
	}
	got := numbers(Rank(row, spools, materialKeys, 10))
	want := []int{3, 4, 2, 1}
	if len(got) != 4 || got[0] != want[0] || got[1] != want[1] || got[2] != want[2] || got[3] != want[3] {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestRankLastsASpoolWithNoColour(t *testing.T) {
	row := Row{Material: "PLA", ColourRGBA: ptr("#FF0000FF")}
	none := spool(1, "PLA", "", false)
	none.RGBA = nil
	got := numbers(Rank(row, []SpoolRow{none, spool(2, "PLA", "#0000FFFF", false)}, materialKeys, 10))
	if got[0] != 2 || got[1] != 1 {
		t.Fatalf("got %v", got)
	}
}

func TestRankKeepsTenAndSkipsArchived(t *testing.T) {
	row := Row{Material: "PLA", ColourRGBA: ptr("#FF0000FF")}
	var spools []SpoolRow
	for n := 1; n <= 12; n++ {
		spools = append(spools, spool(n, "PLA", "#FF0000FF", false))
	}
	spools[0].Archived = true
	got := numbers(Rank(row, spools, materialKeys, CandidateLimit))
	if len(got) != 10 || got[0] != 2 || got[9] != 11 {
		t.Fatalf("got %v", got)
	}
}

func TestCatalogKeyReadsPlusAsPlus(t *testing.T) {
	for in, want := range map[string]string{"PLA+": "PLAPLUS", "petg cf": "PETGCF", "PETG-CF": "PETGCF", "": ""} {
		if got := catalogKey(in); got != want {
			t.Errorf("catalogKey(%q) = %q, want %q", in, got, want)
		}
	}
}
