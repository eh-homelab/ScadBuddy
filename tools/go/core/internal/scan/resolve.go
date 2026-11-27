package scan

import (
	"context"
	"errors"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// Resolved is the thing a code names, as GET /api/v1/codes/{code} answers it
// (backend/scadbuddy/codes/resolve.py Resolved).
type Resolved struct {
	Kind     string  `json:"kind"`
	ID       string  `json:"id"`
	Code     string  `json:"code"`
	Name     *string `json:"name"`
	Archived bool    `json:"archived"`
}

// lookup is one typed code's prefix: its table and kind (codes/resolve.py PREFIXES). A new
// prefix there is a new entry here too. name is the SQL of the name, "" for none.
type lookup struct{ table, kind, name string }

var prefixes = map[string]lookup{
	"L": {"filament_locations", "location", "name"},
	"S": {"filament_spools", "spool", ""},
	"K": {"item_kinds", "item_kind", "name"},
	"T": {"filament_spool_types", "spool_type", "concat_ws(' ', code, colour_name)"},
	"I": {"items", "item", "name"},
}

// maxNumber is PostgreSQL's integer maximum (codes/parse.py MAX_NUMBER).
const maxNumber = 1<<31 - 1

var (
	// typedCode is codes/parse.py _CODE: a letter, a dash or none, at most 10 digits.
	typedCode = regexp.MustCompile(`^([A-Z])-?([0-9]{1,10})$`)
	// spoolPayload is a spool label's QR code (frontend labelSheet.ts labelPayload).
	spoolPayload = regexp.MustCompile(`^scadbuddy:spool:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$`)
	// placeURL is a place label's QR code, <https origin>/x/<code> (labelSheet.ts
	// locationPayload). Any host: the code is resolved against this ScadBuddy's rows only.
	placeURL = regexp.MustCompile(`^https?://[^/?#\s]+/x/([A-Za-z]-?[0-9]{1,10})/?$`)
)

// parseCode is codes/parse.py parse_code: the prefix and number of a typed code, ok false
// for any other text.
func parseCode(text string) (prefix string, number int, ok bool) {
	m := typedCode.FindStringSubmatch(strings.ToUpper(strings.TrimSpace(text)))
	if m == nil {
		return "", 0, false
	}
	n, err := strconv.Atoi(m[2])
	if err != nil || n < 1 || n > maxNumber {
		return "", 0, false
	}
	return m[1], n, true
}

type querier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// resolve is the thing text names, nil when it names none: a spool label's payload names
// the spool by id; a place label's address, or a typed code, names what its code names.
func resolve(ctx context.Context, q querier, text string) (*Resolved, error) {
	if m := spoolPayload.FindStringSubmatch(text); m != nil {
		var r Resolved
		var number int
		var archived *time.Time
		err := q.QueryRow(ctx, "SELECT id::text, number, archived_at FROM filament_spools WHERE id = $1",
			strings.ToLower(m[1])).Scan(&r.ID, &number, &archived)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}
		r.Kind, r.Code, r.Archived = "spool", "S-"+strconv.Itoa(number), archived != nil
		return &r, nil
	}
	if m := placeURL.FindStringSubmatch(text); m != nil {
		text = m[1]
	}
	prefix, number, ok := parseCode(text)
	if !ok {
		return nil, nil
	}
	l, ok := prefixes[prefix]
	if !ok {
		return nil, nil
	}
	name := "NULL::text"
	if l.name != "" {
		name = l.name
	}
	r := Resolved{Kind: l.kind, Code: prefix + "-" + strconv.Itoa(number)}
	var archived *time.Time
	// The table and column come from prefixes, never from the text.
	err := q.QueryRow(ctx, "SELECT id::text, "+name+", archived_at FROM "+l.table+" WHERE number = $1",
		number).Scan(&r.ID, &r.Name, &archived)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	r.Archived = archived != nil
	return &r, nil
}
