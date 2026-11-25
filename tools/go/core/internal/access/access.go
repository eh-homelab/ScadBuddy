package access

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	sourceMax       = 600
	itemMax         = 1200
	byMax           = 600
	sourcesMax      = 1000
	editsMax        = 200
	toolsPerEditMax = 1024
	// listingToolsMax and listingTotalMax: the agent caps a listing exactly
	// (access/listings.ts LISTING_CAPS); this bound is twice its total.
	listingToolsMax = 512
	listingTotalMax = 2 * 2_097_152
	// sourceItem is the row that switches a whole source (access/model.ts SOURCE_ITEM).
	sourceItem = "*"
	// warmEvictChannel is sessions/warmEvict.ts WARM_EVICT_CHANNEL.
	warmEvictChannel = "scadbuddy_warm_evict"
	leaseLost        = "lease-lost"
)

var (
	// sourcePattern is access/store.ts SOURCE_RE.
	sourcePattern = regexp.MustCompile(`^(builtin:(scadbuddy|browser|http|own-plugin|claude-code)` +
		`|remote:[a-z][a-z0-9-]*|package:[^\x00]+)$`)
	pluginName   = regexp.MustCompile(`^[a-z][a-z0-9-]{0,30}[a-z0-9]$`)
	packageName  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
	contentHash  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	accessValues = []string{"off", "on", "read", "write", "outward"}
	toolAccess   = []string{"off", "read", "write", "outward"}
	omittedNames = []string{"description", "inputSchema", "outputSchema", "other", "title", "annotations", "keys"}
)

func (s *Store) inTx(ctx context.Context, fn func(pgx.Tx) error) error {
	return pgx.BeginFunc(ctx, s.Pool, fn)
}

type row struct {
	Source    string `json:"source"`
	Item      string `json:"item"`
	Access    string `json:"access"`
	UpdatedBy string `json:"updated_by"`
}

// sources reads a body {"sources": [...]}.
func sources(r *http.Request) ([]string, error) {
	raw, err := readBody(r)
	if err != nil {
		return nil, err
	}
	f := &form{}
	fields := f.object([]string{"body"}, raw, "sources")
	out := f.texts([]string{"body", "sources"}, f.required([]string{"body"}, fields, "sources"),
		0, sourcesMax, sourceMax, sourcePattern)
	return out, f.err()
}

func (s *Store) accessRows(r *http.Request, _ crud.Params) (any, error) {
	wanted, err := sources(r)
	if err != nil {
		return nil, err
	}
	rows := []row{}
	if len(wanted) > 0 {
		err = s.inTx(r.Context(), func(tx pgx.Tx) error {
			found, err := tx.Query(r.Context(),
				`SELECT source, item, access, updated_by FROM ai_access WHERE source = ANY($1)
				 ORDER BY source, item`, wanted)
			if err != nil {
				return err
			}
			rows, err = pgx.CollectRows(found, pgx.RowToStructByPos[row])
			return err
		})
	}
	if err != nil {
		return nil, err
	}
	return struct {
		Rows []row `json:"rows"`
	}{nonNil(rows)}, nil
}

func (s *Store) accessRow(r *http.Request, _ crud.Params) (any, error) {
	f := &form{}
	var source, item string
	if v, ok := f.query(r, "source"); ok {
		source = f.text([]string{"query", "source"}, quoted(v), 1, sourceMax, sourcePattern)
	}
	if v, ok := f.query(r, "item"); ok {
		item = f.text([]string{"query", "item"}, quoted(v), 1, itemMax, nil)
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	var out *row
	err := s.inTx(r.Context(), func(tx pgx.Tx) error {
		var found row
		err := tx.QueryRow(r.Context(), `SELECT source, item, access, updated_by FROM ai_access
			WHERE source = $1 AND item = $2`, source, item).
			Scan(&found.Source, &found.Item, &found.Access, &found.UpdatedBy)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		out = &found
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Row *row `json:"row"`
	}{out}, nil
}

func quoted(s string) json.RawMessage {
	raw, _ := json.Marshal(s)
	return raw
}

// edit is one edit of an access write: items (an access, or nil to delete the row), replace
// (the tools) or forget.
type edit struct {
	source string
	kind   string
	items  []itemAccess
}

type itemAccess struct {
	item   string
	access *string
}

func readEdit(f *form, loc []string, raw json.RawMessage) edit {
	list, ok := members(raw)
	if !ok {
		f.fail(loc, "Input should be a valid dictionary or object to extract fields from")
		return edit{}
	}
	var kind *string
	for _, m := range list {
		if m.key == "kind" {
			_ = json.Unmarshal(m.raw, &kind)
		}
	}
	if kind == nil || !slices.Contains([]string{"items", "replace", "forget"}, *kind) {
		f.fail(at(loc), "Unable to extract tag using discriminator 'kind'")
		return edit{}
	}
	loc = at(loc, *kind)
	var fields map[string]json.RawMessage
	switch *kind {
	case "items":
		fields = f.object(loc, raw, "source", "kind", "items")
	case "replace":
		fields = f.object(loc, raw, "source", "kind", "tools")
	default:
		fields = f.object(loc, raw, "source", "kind")
	}
	e := edit{kind: *kind}
	e.source = f.text(at(loc, "source"), f.required(loc, fields, "source"), 1, sourceMax, sourcePattern)
	if *kind == "forget" {
		return e
	}
	name := map[string]string{"items": "items", "replace": "tools"}[*kind]
	mapRaw := f.required(loc, fields, name)
	if mapRaw == nil {
		return e
	}
	entries, ok := members(mapRaw)
	if !ok {
		f.fail(at(loc, name), "Input should be a valid dictionary")
		return e
	}
	if len(entries) > toolsPerEditMax {
		f.fail(at(loc, name), "Dictionary should have at most "+strconv.Itoa(toolsPerEditMax)+" items after validation")
	}
	for _, m := range entries {
		if msg := lengthIssue(m.key, 1, itemMax); msg != "" {
			f.fail(at(loc, name, m.key, "[key]"), msg)
		}
		values := accessValues
		if *kind == "replace" {
			values = toolAccess
		} else if isNull(m.raw) {
			e.items = append(e.items, itemAccess{m.key, nil})
			continue
		}
		var v *string
		if json.Unmarshal(m.raw, &v) != nil || v == nil || !slices.Contains(values, *v) {
			f.fail(at(loc, name, m.key), "Input should be one of the access values")
			continue
		}
		e.items = append(e.items, itemAccess{m.key, v})
	}
	if *kind == "replace" && slices.ContainsFunc(e.items, func(i itemAccess) bool { return i.item == sourceItem }) {
		f.fail(at(loc, name), "Value error, a tool row cannot be the source's switch")
	}
	return e
}

// apply is access/store.ts applyEdit: rows as they stand after the edit.
func apply(rows []row, e edit, by string) []row {
	switch e.kind {
	case "forget":
		return slices.DeleteFunc(slices.Clone(rows), func(r row) bool { return r.Source == e.source })
	case "replace":
		kept := slices.DeleteFunc(slices.Clone(rows), func(r row) bool {
			return r.Source == e.source && r.Item != sourceItem
		})
		for _, i := range e.items {
			kept = append(kept, row{e.source, i.item, *i.access, by})
		}
		return kept
	}
	out := slices.Clone(rows)
	for _, i := range e.items {
		out = slices.DeleteFunc(out, func(r row) bool { return r.Source == e.source && r.Item == i.item })
		if i.access != nil {
			out = append(out, row{e.source, i.item, *i.access, by})
		}
	}
	return out
}

func shape(rows []row, source string) []string {
	out := []string{}
	for _, r := range rows {
		if r.Source == source {
			out = append(out, r.Item+"="+r.Access)
		}
	}
	slices.Sort(out)
	return out
}

func runEdit(ctx context.Context, tx pgx.Tx, e edit, by string) error {
	switch e.kind {
	case "forget": // access.py forget_source
		if _, err := tx.Exec(ctx, `DELETE FROM ai_access WHERE source = $1`, e.source); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `DELETE FROM ai_tool_listings WHERE source = $1`, e.source)
		return err
	case "replace": // access.py write_tools
		if _, err := tx.Exec(ctx, `DELETE FROM ai_access WHERE source = $1 AND item <> $2`,
			e.source, sourceItem); err != nil {
			return err
		}
		for _, i := range e.items {
			if _, err := tx.Exec(ctx, `INSERT INTO ai_access (source, item, access, updated_by)
				VALUES ($1, $2, $3, $4)`, e.source, i.item, *i.access, by); err != nil {
				return err
			}
		}
		return nil
	}
	for _, i := range e.items {
		var err error
		if i.access == nil {
			_, err = tx.Exec(ctx, `DELETE FROM ai_access WHERE source = $1 AND item = $2`, e.source, i.item)
		} else {
			_, err = tx.Exec(ctx, `INSERT INTO ai_access (source, item, access, updated_by)
				VALUES ($1, $2, $3, $4) ON CONFLICT (source, item) DO UPDATE
				SET access = EXCLUDED.access, updated_by = EXCLUDED.updated_by, updated_at = now()`,
				e.source, i.item, *i.access, by)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) accessWrite(r *http.Request, _ crud.Params) (any, error) {
	raw, err := readBody(r)
	if err != nil {
		return nil, err
	}
	f := &form{}
	loc := []string{"body"}
	fields := f.object(loc, raw, "by", "edits", "skip_unchanged")
	by := f.text(at(loc, "by"), f.required(loc, fields, "by"), 1, byMax, nil)
	items := f.list(at(loc, "edits"), f.required(loc, fields, "edits"), 1, editsMax)
	edits := make([]edit, len(items))
	for i, item := range items {
		edits[i] = readEdit(f, at(loc, "edits", strconv.Itoa(i)), item)
	}
	skip := f.boolean(at(loc, "skip_unchanged"), fields["skip_unchanged"], false)
	if err := f.err(); err != nil {
		return nil, err
	}
	var applied []int
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		ctx := r.Context()
		applied = make([]int, len(edits))
		for i := range edits {
			applied[i] = i
		}
		if skip {
			var wanted []string
			for _, e := range edits {
				wanted = append(wanted, e.source)
			}
			slices.Sort(wanted)
			found, err := tx.Query(ctx, `SELECT source, item, access, updated_by FROM ai_access
				WHERE source = ANY($1) FOR UPDATE`, slices.Compact(wanted))
			if err != nil {
				return err
			}
			current, err := pgx.CollectRows(found, pgx.RowToStructByPos[row])
			if err != nil {
				return err
			}
			applied = applied[:0]
			for i, e := range edits {
				if !slices.Equal(shape(apply(current, e, by), e.source), shape(current, e.source)) {
					applied = append(applied, i)
				}
			}
			if len(applied) == 0 {
				return nil
			}
		}
		// First: the notification is queued in this transaction, so a failing write drops it.
		if _, err := tx.Exec(ctx, `SELECT pg_notify($1, $2)`, warmEvictChannel, "*"); err != nil {
			return err
		}
		for _, i := range applied {
			if err := runEdit(ctx, tx, edits[i], by); err != nil {
				return err
			}
		}
		return nil
	})
	var pg *pgconn.PgError
	if errors.As(err, &pg) && pg.Code == "23514" {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: "the access table refused the write: " + pgMessage(pg)}
	}
	if err != nil {
		return nil, err
	}
	return struct {
		Applied []int `json:"applied"`
	}{applied}, nil
}

// pgMessage is a server error as psycopg's str() of it reads.
func pgMessage(pg *pgconn.PgError) string {
	if pg.Detail == "" {
		return pg.Message
	}
	return pg.Message + "\nDETAIL:  " + pg.Detail
}

// -- tool listings -----------------------------------------------------------------------

type listing struct {
	Source    string          `json:"source"`
	Tools     json.RawMessage `json:"tools"`
	ListedAt  string          `json:"listed_at"`
	Truncated bool            `json:"truncated"`
}

func (s *Store) toolListings(r *http.Request, _ crud.Params) (any, error) {
	wanted, err := sources(r)
	if err != nil {
		return nil, err
	}
	out := []listing{}
	if len(wanted) > 0 {
		err = s.inTx(r.Context(), func(tx pgx.Tx) error {
			rows, err := tx.Query(r.Context(), `SELECT source, tools::text, listed_at, truncated
				FROM ai_tool_listings WHERE source = ANY($1)`, wanted)
			if err != nil {
				return err
			}
			defer rows.Close()
			for rows.Next() {
				var l listing
				var tools string
				var at time.Time
				if err := rows.Scan(&l.Source, &tools, &at, &l.Truncated); err != nil {
					return err
				}
				l.Tools, l.ListedAt = json.RawMessage(tools), isoTime(at)
				out = append(out, l)
			}
			return rows.Err()
		})
	}
	if err != nil {
		return nil, err
	}
	return struct {
		Listings []listing `json:"listings"`
	}{out}, nil
}

// listingPut is a connection test's listing: its tools as the JSON stored, and the lease.
type listingPut struct {
	tools     string
	truncated bool
	lease     string
}

// readListing reads the fields every listing put has, and the names of its own.
func readListing(r *http.Request, own ...string) (*form, map[string]json.RawMessage, listingPut, error) {
	raw, err := readBody(r)
	if err != nil {
		return nil, nil, listingPut{}, err
	}
	f := &form{}
	loc := []string{"body"}
	fields := f.object(loc, raw, append([]string{"tools", "truncated", "lease_token"}, own...)...)
	var put listingPut
	items := f.list(at(loc, "tools"), f.required(loc, fields, "tools"), 0, listingToolsMax)
	var tools bytes.Buffer
	tools.WriteByte('[')
	for i, item := range items {
		tloc := at(loc, "tools", strconv.Itoa(i))
		tf := f.object(tloc, item, "raw", "omitted")
		toolRaw := f.required(tloc, tf, "raw")
		if toolRaw != nil {
			named := map[string]json.RawMessage{}
			if json.Unmarshal(toolRaw, &named) != nil || named == nil {
				f.fail(at(tloc, "raw"), "Input should be a valid dictionary")
			} else if n := named["name"]; n == nil || isNull(n) || json.Unmarshal(n, new(string)) != nil {
				f.fail(at(tloc, "raw"), "Value error, a listed tool needs a string name")
			}
		}
		omitted := f.list(at(tloc, "omitted"), f.required(tloc, tf, "omitted"), 0, 7)
		for j, o := range omitted {
			var v *string
			if json.Unmarshal(o, &v) != nil || v == nil || !slices.Contains(omittedNames, *v) {
				f.fail(at(tloc, "omitted", strconv.Itoa(j)), "Input should be one of the omitted fields")
			}
		}
		if i > 0 {
			tools.WriteByte(',')
		}
		tools.WriteString(`{"raw":`)
		tools.Write(compact(toolRaw))
		tools.WriteString(`,"omitted":`)
		tools.Write(compact(tf["omitted"]))
		tools.WriteByte('}')
	}
	tools.WriteByte(']')
	if f.err() == nil && utf8.RuneCount(tools.Bytes()) > listingTotalMax {
		f.fail(at(loc, "tools"), "Value error, the listing is larger than a listing can be")
	}
	put.tools = tools.String()
	put.truncated = f.boolean(at(loc, "truncated"), fields["truncated"], false)
	put.lease = f.uuid(at(loc, "lease_token"), f.required(loc, fields, "lease_token"))
	return f, fields, put, nil
}

// compact is a JSON value without spaces and with its own characters unescaped, as Python's
// json.dumps(ensure_ascii=False, separators=(",", ":")) writes it; null for none.
func compact(raw json.RawMessage) []byte {
	if raw == nil {
		return []byte("null")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if dec.Decode(&v) != nil {
		return []byte("null")
	}
	out, err := httpx.Marshal(v)
	if err != nil {
		return []byte("null")
	}
	return out
}

func leaseLostProblem() error {
	return httpx.WithReason(http.StatusConflict, "The lease is not held by this token.", leaseLost)
}

// heldIn is leases.py held_in: whether token holds key, the row locked FOR SHARE.
func heldIn(ctx context.Context, tx pgx.Tx, key, token string) (bool, error) {
	var one int
	err := tx.QueryRow(ctx, `SELECT 1 FROM agent_leases WHERE key = $1 AND token = $2
		AND expires_at > now() FOR SHARE`, key, token).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func upsertListing(ctx context.Context, tx pgx.Tx, source string, put listingPut) error {
	_, err := tx.Exec(ctx, `INSERT INTO ai_tool_listings (source, tools, truncated)
		VALUES ($1, $2::text::jsonb, $3) ON CONFLICT (source) DO UPDATE SET tools = EXCLUDED.tools,
		truncated = EXCLUDED.truncated, listed_at = now()`, source, put.tools, put.truncated)
	return err
}

type stored struct {
	// Stored is false when the plugin or its pin changed since the test.
	Stored bool `json:"stored"`
}

func (s *Store) storeListing(r *http.Request, source string, put listingPut, lock string, args ...any) (any, error) {
	var out bool
	err := s.inTx(r.Context(), func(tx pgx.Tx) error {
		ctx := r.Context()
		held, err := heldIn(ctx, tx, "tool-listing:"+source, put.lease)
		if err != nil {
			return err
		}
		if !held {
			return leaseLostProblem()
		}
		var one int
		err = tx.QueryRow(ctx, lock, args...).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		out = true
		return upsertListing(ctx, tx, source, put)
	})
	if err != nil {
		return nil, err
	}
	return stored{out}, nil
}

func (s *Store) pluginListing(r *http.Request, _ crud.Params) (any, error) {
	f, fields, put, err := readListing(r, "name", "url")
	if err != nil {
		return nil, err
	}
	loc := []string{"body"}
	name := f.text(at(loc, "name"), f.required(loc, fields, "name"), 0, 1<<30, pluginName)
	url := f.text(at(loc, "url"), f.required(loc, fields, "url"), 1, 2048, nil)
	if err := f.err(); err != nil {
		return nil, err
	}
	return s.storeListing(r, "remote:"+name, put,
		`SELECT 1 FROM ai_plugins WHERE name = $1 AND url = $2 FOR UPDATE`, name, url)
}

func (s *Store) packageServerListing(r *http.Request, _ crud.Params) (any, error) {
	f, fields, put, err := readListing(r, "pkg", "server", "content_hash")
	if err != nil {
		return nil, err
	}
	loc := []string{"body"}
	pkg := f.text(at(loc, "pkg"), f.required(loc, fields, "pkg"), 0, 1<<30, packageName)
	server := f.text(at(loc, "server"), f.required(loc, fields, "server"), 1, sourceMax, nil)
	hash := f.text(at(loc, "content_hash"), f.required(loc, fields, "content_hash"), 0, 1<<30, contentHash)
	if err := f.err(); err != nil {
		return nil, err
	}
	return s.storeListing(r, "package:"+pkg+"/mcp:"+server, put,
		`SELECT 1 FROM ai_plugin_packages WHERE name = $1 AND content_hash = $2 FOR UPDATE`, pkg, hash)
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}
