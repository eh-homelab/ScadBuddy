package transcripts

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	subpathMax    = 1024
	projectKeyMax = 4096
	// One append's entries; one entry's JSON text; one body. A tool result's images stay in
	// the entry as base64 (8 of 5 MB at most), and the agent sends a larger entry alone.
	entriesMax    = 1000
	entryCharsMax = 48 * 1024 * 1024
	bodyBytesMax  = 64 * 1024 * 1024
	// ownMax is the uuids a warm process wrote itself, which moved leaves out.
	ownMax  = 20_000
	uuidMax = 256
	// lastCwdRows is how many of a session's newest user and assistant rows lastCwd opens first.
	lastCwdRows = 20
	// transcriptTries is how often a load is read again when an ancestor was forgotten while
	// it read.
	transcriptTries = 3
	maxSeq          = 1<<63 - 1
)

var (
	// entryType is plaintext, so only a short token-like type is stored.
	entryType    = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,63}$`)
	sincePattern = regexp.MustCompile(`^[0-9]{1,19}$`)
)

// Store is the transcript routes over the pool, sealing with the KEKs.
type Store struct {
	Pool *pgxpool.Pool
	Keys *seal.Keys
}

// Reads are the area's GET routes.
func (s *Store) Reads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/sessions/{session_id}/transcript", Handle: s.load},
		{Method: "GET", Path: "/sessions/{session_id}/entries", Handle: s.loadSubpath},
		{Method: "GET", Path: "/sessions/{session_id}/entries/last", Handle: s.lastOfType},
		{Method: "GET", Path: "/sessions/{session_id}/cwd", Handle: s.lastCwd},
		{Method: "GET", Path: "/transcripts", Handle: s.listSessions},
		{Method: "GET", Path: "/sessions/{session_id}/subkeys", Handle: s.subkeys},
		{Method: "GET", Path: "/sessions/{session_id}/transcript/head", Handle: s.head},
		{Method: "GET", Path: "/sessions/{session_id}/entries/landed", Handle: s.landed},
	}
}

// Writes are the area's POST routes.
func (s *Store) Writes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/sessions/{session_id}/entries", Handle: s.append, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/entries/delete", Handle: s.delete, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/transcript/moved", Handle: s.moved, MaxBody: bodyBytesMax},
	}
}

// forgotten is the 410 of a session whose key is gone, or was never made and is not to be.
func forgotten(detail string) error {
	return httpx.WithReason(http.StatusGone, detail, "forgotten")
}

// unavailable is the 503 of a key sealed under a KEK this API does not hold.
func unavailable(detail string) error {
	return httpx.WithReason(http.StatusServiceUnavailable, detail, "unavailable")
}

func entryContext(session, subpath, bound string) string {
	return "ai_session_entries:" + session + ":" + subpath + ":" + bound
}

// parsed is the entry's members; none when it is no object.
func parsed(text string) (map[string]json.RawMessage, error) {
	var value any
	if err := json.Unmarshal([]byte(text), &value); err != nil {
		return nil, err
	}
	var object map[string]json.RawMessage
	if _, ok := value.(map[string]any); ok {
		_ = json.Unmarshal([]byte(text), &object)
	}
	return object, nil
}

// stringMember is the object's member name when it is a string.
func stringMember(object map[string]json.RawMessage, name string) *string {
	var s *string
	if json.Unmarshal(object[name], &s) != nil {
		return nil
	}
	return s
}

func entryTypeOf(entry map[string]json.RawMessage) *string {
	if kind := stringMember(entry, "type"); kind != nil && entryType.MatchString(*kind) {
		return kind
	}
	return nil
}

// keyIn is payload_keys.py key_in: the subject's data key, made first with create, in the
// caller's transaction, with no audit row; nil when it has none.
func (s *Store) keyIn(ctx context.Context, tx pgx.Tx, subject string, create bool) ([]byte, error) {
	sealed, kekID, err := keyRow(ctx, tx, subject)
	if err != nil {
		return nil, err
	}
	if sealed == nil && create {
		dek := make([]byte, seal.KEKBytes)
		_, _ = rand.Read(dek)
		made := seal.SealBytes(s.Keys.Current.Key(), dek, seal.PayloadKeyContext(subject))
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`,
			seal.PayloadKeyLock(subject)); err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)
			 SELECT $1, $2, $3
			 WHERE NOT EXISTS (SELECT 1 FROM ai_forgotten_subjects WHERE subject = $1)
			 AND (NOT $4 OR EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = substr($1, 9)))
			 ON CONFLICT (subject) DO NOTHING`,
			subject, made, s.Keys.Current.ID, false); err != nil {
			return nil, err
		}
		// Whoever inserted first, this is the key every encoder of the subject uses.
		if sealed, kekID, err = keyRow(ctx, tx, subject); err != nil {
			return nil, err
		}
	}
	if sealed == nil {
		return nil, nil
	}
	opener := s.Keys.Opener(kekID)
	if opener == nil {
		return nil, unavailable(fmt.Sprintf("the payload key of %s was sealed with key %s,"+
			" which the API does not hold", subject, kekID))
	}
	dek, err := seal.OpenBytes(opener.Key(), sealed, seal.PayloadKeyContext(subject))
	if err != nil {
		return nil, unavailable(fmt.Sprintf("the payload key of %s does not open", subject))
	}
	return dek, nil
}

func keyRow(ctx context.Context, tx pgx.Tx, subject string) ([]byte, string, error) {
	var sealed []byte
	var kekID string
	err := tx.QueryRow(ctx, `SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = $1`,
		subject).Scan(&sealed, &kekID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, "", nil
	}
	return sealed, kekID, err
}

// key is a session's data key for a read: never made.
func (s *Store) key(ctx context.Context, session string) ([]byte, error) {
	var dek []byte
	err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) (err error) {
		dek, err = s.keyIn(ctx, tx, "session-"+session, false)
		return err
	})
	if err != nil {
		return nil, err
	}
	if dek == nil {
		return nil, forgotten(fmt.Sprintf("session-%s was forgotten", session))
	}
	return dek, nil
}

type fence struct{ attempt, turnID string }

type written struct {
	// Written is false when the fence refused it.
	Written bool `json:"written"`
}

func (s *Store) append(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, err := readForm(r, "project_key", "subpath", "entries", "fence")
	if err != nil {
		return nil, err
	}
	projectKey := f.text("project_key", 0, projectKeyMax, false, nil)
	subpath := f.text("subpath", 0, subpathMax, false, nil)
	texts := f.texts("entries", 1, entriesMax, entryCharsMax)
	var held *fence
	if raw, ok := f.fields["fence"]; ok && string(raw) != "null" {
		nested := &form{at: []string{"body", "fence"}}
		nested.object(raw, []string{"attempt", "turn_id"})
		attempt := nested.text("attempt", 1, 500, false, nil)
		turn := nested.text("turn_id", 1, 500, false, nil)
		f.issues = append(f.issues, nested.issues...)
		if attempt != nil && turn != nil {
			held = &fence{*attempt, *turn}
		}
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	entries := make([]map[string]json.RawMessage, len(texts))
	for i, text := range texts {
		if entries[i], err = parsed(text); err != nil {
			return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: "an entry is not JSON"}
		}
	}
	ok := true
	subject := "session-" + session
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		dek, err := s.keyIn(ctx, tx, subject, true)
		if err != nil {
			return err
		}
		if dek == nil {
			return forgotten(subject + " was forgotten: its transcript is not written")
		}
		// Every row's id is reserved first, in order, so a batch keeps its order and a row
		// without a uuid is bound by its own id.
		rows, err := tx.Query(ctx, `SELECT nextval(pg_get_serial_sequence('ai_session_entries', 'id'))
			FROM generate_series(1, $1)`, len(texts))
		if err != nil {
			return err
		}
		ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
		if err != nil {
			return err
		}
		batch := &pgx.Batch{}
		for i, text := range texts {
			uuid := stringMember(entries[i], "uuid")
			bound := "#" + strconv.FormatInt(ids[i], 10)
			if uuid != nil {
				bound = *uuid
			}
			sealed := seal.SealBytes(dek, []byte(text), entryContext(session, *subpath, bound))
			batch.Queue(`INSERT INTO ai_session_entries
				(id, project_key, session_id, subpath, uuid, entry_type, sealed)
				VALUES ($1, $2, $3, $4, $5, $6, $7)
				ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING`,
				ids[i], *projectKey, session, *subpath, uuid, entryTypeOf(entries[i]), sealed)
		}
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock_shared(hashtext($1))`,
			seal.PayloadKeyLock(subject)); err != nil {
			return err
		}
		var attempt, turn *string
		if held != nil {
			attempt, turn = &held.attempt, &held.turnID
		}
		var gone, holds bool
		if err := tx.QueryRow(ctx,
			`SELECT EXISTS (SELECT 1 FROM ai_forgotten_subjects WHERE subject = $1),
			 ($3::text IS NULL OR EXISTS (SELECT 1 FROM (
			   SELECT 1 FROM ai_sessions WHERE id::text = $2 AND turn_attempt = $3
			   AND (durable_turn_id::text = $4 OR durable_turn_id IS NULL) FOR SHARE) held))`,
			subject, session, attempt, turn).Scan(&gone, &holds); err != nil {
			return err
		}
		if gone {
			return forgotten(subject + " was forgotten: its transcript is not written")
		}
		if !holds {
			ok = false
			return nil
		}
		return tx.SendBatch(ctx, batch).Close()
	})
	if err != nil {
		return nil, err
	}
	return written{ok}, nil
}

type row struct {
	owner     string
	id        int64
	uuid      *string
	entry     *string
	sealed    []byte
	inherited bool
}

// open is the rows' JSON texts, a sealed row opened under owner's key (never made to read).
func (s *Store) open(ctx context.Context, owner, subpath string, rows []row) ([]string, error) {
	var dek []byte
	if slices.ContainsFunc(rows, func(r row) bool { return r.sealed != nil }) {
		var err error
		if dek, err = s.key(ctx, owner); err != nil {
			return nil, err
		}
	}
	out := make([]string, 0, len(rows))
	for _, r := range rows {
		if r.sealed == nil {
			if r.entry == nil {
				return nil, errors.New("a transcript row holds no entry")
			}
			out = append(out, *r.entry)
			continue
		}
		bound := "#" + strconv.FormatInt(r.id, 10)
		if r.uuid != nil {
			bound = *r.uuid
		}
		plain, err := seal.OpenBytes(dek, r.sealed, entryContext(owner, subpath, bound))
		if err != nil {
			return nil, err
		}
		if !utf8.Valid(plain) {
			return nil, errors.New("a transcript entry is not UTF-8")
		}
		out = append(out, string(plain))
	}
	return out, nil
}

// chainRows is a session's fork chain and the main-transcript rows a load takes, in one
// statement (store.ts chainRows).
const chainRows = `WITH RECURSIVE chain (id, parent, cut, depth, bound) AS (
   SELECT id, fork_parent_id, fork_cut, 0, NULL::bigint FROM ai_sessions WHERE id = $1::text::uuid
   UNION ALL
   SELECT s.id, s.fork_parent_id, s.fork_cut, c.depth + 1, LEAST(c.bound, c.cut)
   FROM ai_sessions s JOIN chain c ON s.id = c.parent)
 SELECT c.id::text AS owner, c.cut, e.id, e.uuid, e.entry, e.sealed, e.inherited
 FROM chain c LEFT JOIN LATERAL (
   SELECT id, uuid, entry, sealed, inherited FROM ai_session_entries
   WHERE session_id = c.id::text AND subpath = '' ORDER BY inherited DESC, id LIMIT c.bound
 ) e ON true ORDER BY c.depth, e.inherited DESC, e.id`

type segment struct {
	owner string
	rows  []row
}

type link struct {
	owner string
	cut   *int64
}

func firstRows(segments []segment, n int64) []segment {
	var out []segment
	left := n
	for _, s := range segments {
		if left <= 0 {
			break
		}
		kept := s.rows[:min(int64(len(s.rows)), left)]
		left -= int64(len(kept))
		out = append(out, segment{s.owner, kept})
	}
	return out
}

// segments is segmentsOf: the rows of the main transcript in the order it loads, each run
// with its owner: inherited rows, then the parent's through the cut, then its own.
func (s *Store) segments(ctx context.Context, session string) ([]segment, error) {
	found, err := s.Pool.Query(ctx, chainRows, session)
	if err != nil {
		return nil, err
	}
	var chain []link
	var rows []row
	for found.Next() {
		var owner string
		var cut, id *int64
		var r row
		var inherited *bool
		if err := found.Scan(&owner, &cut, &id, &r.uuid, &r.entry, &r.sealed, &inherited); err != nil {
			found.Close()
			return nil, err
		}
		if len(chain) == 0 || chain[len(chain)-1].owner != owner {
			chain = append(chain, link{owner, cut})
		}
		if id != nil {
			r.owner, r.id, r.inherited = owner, *id, *inherited
			rows = append(rows, r)
		}
	}
	if err := found.Err(); err != nil {
		return nil, err
	}
	if len(chain) == 0 {
		// No session row: the plain rows of the key, as before forks.
		chain = []link{{session, nil}}
		plain, err := s.Pool.Query(ctx, `SELECT id, uuid, entry, sealed, inherited
			FROM ai_session_entries WHERE session_id = $1 AND subpath = '' ORDER BY inherited DESC, id`,
			session)
		if err != nil {
			return nil, err
		}
		for plain.Next() {
			r := row{owner: session}
			if err := plain.Scan(&r.id, &r.uuid, &r.entry, &r.sealed, &r.inherited); err != nil {
				plain.Close()
				return nil, err
			}
			rows = append(rows, r)
		}
		if err := plain.Err(); err != nil {
			return nil, err
		}
	}
	var through []segment
	for i := len(chain) - 1; i >= 0; i-- {
		owner, cut := chain[i].owner, chain[i].cut
		var inherited, own []row
		for _, r := range rows {
			if r.owner != owner {
				continue
			}
			if r.inherited {
				inherited = append(inherited, r)
			} else {
				own = append(own, r)
			}
		}
		built := []segment{{owner, inherited}}
		if cut != nil {
			built = append(built, firstRows(through, *cut)...)
		}
		built = append(built, segment{owner, own})
		through = nil
		for _, seg := range built {
			if len(seg.rows) > 0 {
				through = append(through, seg)
			}
		}
	}
	return through, nil
}

// transcript is transcriptOf: the main transcript as it loads, each run opened under its
// owner's key, read again when an ancestor's key went after the snapshot.
func (s *Store) transcript(ctx context.Context, session string) ([]string, error) {
	for attempt := 1; ; attempt++ {
		segs, err := s.segments(ctx, session)
		if err != nil {
			return nil, err
		}
		var out []string
		for _, seg := range segs {
			var texts []string
			if texts, err = s.open(ctx, seg.owner, "", seg.rows); err != nil {
				break
			}
			out = append(out, texts...)
		}
		if err == nil {
			return out, nil
		}
		var p *httpx.Problem
		ancestor := slices.ContainsFunc(segs, func(g segment) bool { return g.owner != session })
		if !errors.As(err, &p) || p.Status != http.StatusGone || !ancestor || attempt >= transcriptTries {
			return nil, err
		}
	}
}

type entries struct {
	// Entries are each entry's JSON text, in order; null for a transcript never written.
	Entries []string `json:"entries"`
}

func orNull(texts []string) entries {
	if len(texts) == 0 {
		return entries{nil}
	}
	return entries{texts}
}

func (s *Store) load(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	texts, err := s.transcript(r.Context(), session)
	if err != nil {
		return nil, err
	}
	return orNull(texts), nil
}

// query is the query parameter name: given is false when the request does not name it. As
// Starlette, the last one counts.
func query(r *http.Request, name string) (value string, given bool) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return "", false
	}
	return values[len(values)-1], true
}

// queryText is the query parameter name, least to most characters matching pattern; fallback
// when it is not given, or a 422 when there is none.
func queryText(r *http.Request, name string, least, most int, pattern *regexp.Regexp, fallback *string) (string, error) {
	value, given := query(r, name)
	issue := httpx.Issue{Loc: []string{"query", name}}
	n := utf8.RuneCountInString(value)
	switch {
	case !given && fallback != nil:
		return *fallback, nil
	case !given:
		issue.Msg = "Field required"
	case n < least:
		issue.Msg = fmt.Sprintf("String should have at least %d character%s", least, plural(least))
	case n > most:
		issue.Msg = fmt.Sprintf("String should have at most %d character%s", most, plural(most))
	case pattern != nil && !pattern.MatchString(value):
		issue.Msg = fmt.Sprintf("String should match pattern '%s'", pattern)
	default:
		return value, nil
	}
	return "", httpx.Invalid(issue)
}

const rowColumns = `SELECT id, uuid, entry, sealed FROM ai_session_entries`

func (s *Store) rows(ctx context.Context, owner, sql string, args ...any) ([]row, error) {
	found, err := s.Pool.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	var out []row
	for found.Next() {
		r := row{owner: owner}
		if err := found.Scan(&r.id, &r.uuid, &r.entry, &r.sealed); err != nil {
			found.Close()
			return nil, err
		}
		out = append(out, r)
	}
	return out, found.Err()
}

func (s *Store) loadSubpath(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	subpath, err := queryText(r, "subpath", 1, subpathMax, nil, nil)
	if err != nil {
		return nil, err
	}
	rows, err := s.rows(r.Context(), session,
		rowColumns+` WHERE session_id = $1 AND subpath = $2 ORDER BY id`, session, subpath)
	if err != nil {
		return nil, err
	}
	texts, err := s.open(r.Context(), session, subpath, rows)
	if err != nil {
		return nil, err
	}
	return orNull(texts), nil
}

func (s *Store) lastOfType(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	kind, err := queryText(r, "type", 0, 1<<30, entryType, nil)
	if err != nil {
		return nil, err
	}
	empty := ""
	subpath, err := queryText(r, "subpath", 0, subpathMax, nil, &empty)
	if err != nil {
		return nil, err
	}
	// The newest row without a uuid of this type, by its plaintext entry_type, or a row from
	// before sealing whose text names it.
	rows, err := s.rows(r.Context(), session,
		rowColumns+` WHERE session_id = $1 AND subpath = $2 AND uuid IS NULL AND NOT inherited
		 AND (entry_type = $3 OR (entry_type IS NULL AND entry LIKE $4))
		 ORDER BY id DESC LIMIT 1`, session, subpath, kind, `%"`+kind+`"%`)
	if err != nil {
		return nil, err
	}
	out := struct {
		Entry *string `json:"entry"`
	}{}
	texts, err := s.open(r.Context(), session, subpath, rows)
	if err != nil {
		return nil, err
	}
	if len(texts) > 0 {
		out.Entry = &texts[0]
	}
	return out, nil
}

// cwdOf is the newest non-empty cwd the texts name, oldest first.
func cwdOf(texts []string) *string {
	for i := len(texts) - 1; i >= 0; i-- {
		entry, err := parsed(texts[i])
		if err != nil {
			continue
		}
		if cwd := stringMember(entry, "cwd"); cwd != nil && *cwd != "" {
			return cwd
		}
	}
	return nil
}

// lastCwd is lastCwd: the newest cwd the session's own user and assistant rows name, else its
// transcript's as it loads (a fork before its first turn).
func (s *Store) lastCwd(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	out := struct {
		Cwd *string `json:"cwd"`
	}{}
	rows, err := s.rows(r.Context(), session,
		rowColumns+` WHERE session_id = $1 AND subpath = '' AND NOT inherited
		 AND (entry_type IN ('user', 'assistant') OR entry_type IS NULL)
		 ORDER BY id DESC LIMIT $2`, session, lastCwdRows)
	if err != nil {
		return nil, err
	}
	if len(rows) > 0 {
		texts, err := s.open(r.Context(), session, "", rows)
		if err != nil {
			return nil, err
		}
		slices.Reverse(texts)
		if out.Cwd = cwdOf(texts); out.Cwd != nil {
			return out, nil
		}
	}
	texts, err := s.transcript(r.Context(), session)
	if err != nil {
		return nil, err
	}
	out.Cwd = cwdOf(texts)
	return out, nil
}

type projectSession struct {
	SessionID string `json:"session_id"`
	Mtime     int64  `json:"mtime"`
}

func (s *Store) listSessions(r *http.Request, _ crud.Params) (any, error) {
	projectKey, err := queryText(r, "project_key", 0, projectKeyMax, nil, nil)
	if err != nil {
		return nil, err
	}
	found, err := s.Pool.Query(r.Context(),
		`SELECT session_id, floor(extract(epoch FROM max(created_at)) * 1000)::bigint AS mtime
		 FROM ai_session_entries WHERE project_key = $1 AND subpath = '' GROUP BY session_id`,
		projectKey)
	if err != nil {
		return nil, err
	}
	sessions, err := pgx.CollectRows(found, pgx.RowToStructByPos[projectSession])
	if err != nil {
		return nil, err
	}
	if sessions == nil {
		sessions = []projectSession{}
	}
	return struct {
		Sessions []projectSession `json:"sessions"`
	}{sessions}, nil
}

// delete takes a subpath, or the main transcript with its subagent transcripts (null).
func (s *Store) delete(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, err := readForm(r, "subpath")
	if err != nil {
		return nil, err
	}
	subpath := f.text("subpath", 0, subpathMax, true, nil)
	if err := f.err(); err != nil {
		return nil, err
	}
	if subpath == nil {
		_, err = s.Pool.Exec(r.Context(), `DELETE FROM ai_session_entries WHERE session_id = $1`, session)
	} else {
		_, err = s.Pool.Exec(r.Context(),
			`DELETE FROM ai_session_entries WHERE session_id = $1 AND subpath = $2`, session, *subpath)
	}
	if err != nil {
		return nil, err
	}
	return struct {
		OK bool `json:"ok"`
	}{true}, nil
}

func (s *Store) subkeys(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	found, err := s.Pool.Query(r.Context(), `SELECT DISTINCT subpath FROM ai_session_entries
		WHERE session_id = $1 AND subpath <> '' ORDER BY subpath`, session)
	if err != nil {
		return nil, err
	}
	keys, err := pgx.CollectRows(found, pgx.RowTo[string])
	if err != nil {
		return nil, err
	}
	if keys == nil {
		keys = []string{}
	}
	return struct {
		Subkeys []string `json:"subkeys"`
	}{keys}, nil
}

// head says whether there is a main transcript to resume (rows of its own, or a fork's
// range), and its own newest row with a uuid, a bigint as text.
func (s *Store) head(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	out := struct {
		Exists bool    `json:"exists"`
		Since  *string `json:"since"`
	}{}
	err = s.Pool.QueryRow(r.Context(),
		`SELECT EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id = $1 AND subpath = '')
		 OR EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = $1
		 AND fork_parent_id IS NOT NULL AND fork_cut > 0),
		 (SELECT max(id) FROM ai_session_entries WHERE session_id = $1 AND subpath = ''
		 AND uuid IS NOT NULL AND NOT inherited)::text`, session).Scan(&out.Exists, &out.Since)
	if err != nil {
		return nil, err
	}
	return out, nil
}

// moved is warm.ts tailMoved: whether a row with a uuid the process did not write landed in
// the main transcript after since.
func (s *Store) moved(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	f, err := readForm(r, "since", "own")
	if err != nil {
		return nil, err
	}
	since := f.text("since", 0, 1<<30, true, sincePattern)
	own := f.texts("own", 0, ownMax, uuidMax)
	if err := f.err(); err != nil {
		return nil, err
	}
	mark := "0"
	if since != nil {
		mark = *since
	}
	var one int
	err = s.Pool.QueryRow(r.Context(),
		`SELECT 1 FROM ai_session_entries WHERE session_id = $1 AND subpath = ''
		 AND uuid IS NOT NULL AND NOT inherited AND id > $2::text::numeric
		 AND NOT (uuid = ANY($3::text[])) LIMIT 1`, session, mark, own).Scan(&one)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		Moved bool `json:"moved"`
	}{err == nil}, nil
}

// landed is store.ts userLanded: whether a user entry landed in the main transcript after
// row after; a legacy row's own top-level type is read.
func (s *Store) landed(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	after, given, err := crud.QueryInt(r, "after", 0, maxSeq)
	if err != nil {
		return nil, err
	}
	if !given {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", "after"}, Msg: "Field required"})
	}
	found, err := s.Pool.Query(r.Context(),
		`SELECT entry_type = 'user', entry FROM ai_session_entries
		 WHERE session_id = $1 AND subpath = '' AND id > $2 AND NOT inherited
		 AND (entry_type = 'user' OR (entry_type IS NULL AND entry LIKE '%"user"%'))
		 ORDER BY id`, session, after)
	if err != nil {
		return nil, err
	}
	defer found.Close()
	out := struct {
		Landed bool `json:"landed"`
	}{}
	for found.Next() {
		var typed *bool
		var entry *string
		if err := found.Scan(&typed, &entry); err != nil {
			return nil, err
		}
		if typed != nil && *typed {
			out.Landed = true
			return out, nil
		}
		text := ""
		if entry != nil {
			text = *entry
		}
		if object, err := parsed(text); err == nil {
			if kind := stringMember(object, "type"); kind != nil && *kind == "user" {
				out.Landed = true
				return out, nil
			}
		}
	}
	return out, found.Err()
}
