package unknowntrays

import (
	"cmp"
	"slices"
	"strings"
	"time"
	"unicode"
)

// CandidateLimit is how many candidates a tray lists.
const CandidateLimit = 10

// Row is one unknown tray as the list reads it.
type Row struct {
	ID         string
	PrinterID  string
	Printer    string
	AmsID      int
	TrayID     int
	Material   string
	ColourRGBA *string
	RemainPct  *int
	FirstSeen  time.Time
	SeenAt     time.Time
	PlaceID    *string
	PlaceName  *string
	PlaceNo    *int
}

// SpoolRow is one spool with its effective values (its own, else its type's).
type SpoolRow struct {
	ID           string
	Number       int
	MaterialID   *string
	Material     string
	ColourName   *string
	RGBA         *string
	LabelWeightG *int
	GramsLeft    *float64
	PlaceID      *string
	PlaceName    *string
	Archived     bool
}

// Candidate is a spool the owner may pick for a tray.
type Candidate struct {
	SpoolID        string   `json:"spool_id"`
	Number         int      `json:"number"`
	Code           string   `json:"code"`
	Material       string   `json:"material"`
	ColourName     *string  `json:"colour_name"`
	RGBA           *string  `json:"rgba"`
	LabelWeightG   *int     `json:"label_weight_g"`
	GramsLeft      *float64 `json:"grams_left"`
	PlaceID        *string  `json:"place_id"`
	PlaceName      *string  `json:"place_name"`
	ColourDistance *float64 `json:"colour_distance"`
}

// catalogKey is backend filament/catalog.py catalog_key: "+" read as "PLUS", then the
// letters and digits, upper case.
func catalogKey(name string) string {
	var b strings.Builder
	for _, r := range strings.ReplaceAll(name, "+", "PLUS") {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(unicode.ToUpper(r))
		}
	}
	return b.String()
}

// Rank is the tray's candidates, at most limit. An archived spool is left out. The order:
// unplaced spools first, then the spools of the tray's material, then the nearest colour
// (a spool with no colour to compare last), then the number. keys holds each material's
// catalog keys by material id.
func Rank(row Row, spools []SpoolRow, keys map[string]map[string]bool, limit int) []Candidate {
	type ranked struct {
		spool    SpoolRow
		distance *float64
		sameKind bool
	}
	reported := catalogKey(row.Material)
	var found []ranked
	for _, s := range spools {
		if s.Archived {
			continue
		}
		same := false
		if s.MaterialID != nil && reported != "" {
			same = keys[*s.MaterialID][reported]
		}
		found = append(found, ranked{s, ColourDistance(row.ColourRGBA, s.RGBA), same})
	}
	slices.SortStableFunc(found, func(a, b ranked) int {
		if c := cmp.Compare(boolInt(a.spool.PlaceID != nil), boolInt(b.spool.PlaceID != nil)); c != 0 {
			return c
		}
		if c := cmp.Compare(boolInt(!a.sameKind), boolInt(!b.sameKind)); c != 0 {
			return c
		}
		if c := cmp.Compare(boolInt(a.distance == nil), boolInt(b.distance == nil)); c != 0 {
			return c
		}
		if a.distance != nil && b.distance != nil {
			if c := cmp.Compare(*a.distance, *b.distance); c != 0 {
				return c
			}
		}
		return cmp.Compare(a.spool.Number, b.spool.Number)
	})
	if len(found) > limit {
		found = found[:limit]
	}
	out := make([]Candidate, 0, len(found))
	for _, f := range found {
		s := f.spool
		out = append(out, Candidate{
			SpoolID: s.ID, Number: s.Number, Code: SpoolCode(s.Number), Material: s.Material,
			ColourName: s.ColourName, RGBA: s.RGBA, LabelWeightG: s.LabelWeightG,
			GramsLeft: s.GramsLeft, PlaceID: s.PlaceID, PlaceName: s.PlaceName,
			ColourDistance: f.distance,
		})
	}
	return out
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
