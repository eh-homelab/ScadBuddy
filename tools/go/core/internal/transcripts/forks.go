package transcripts

import (
	"context"
	"errors"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	// copyBatch is transcripts.py COPY_BATCH: at most this many rows in one insert.
	copyBatch = 1000
	// eventBatch is events.py copy_fork_events' batch.
	eventBatch = 500
)

// ErrKeyUnavailable is transcripts.py KeyUnavailableError: a fork takes transcript entries and
// the API holds no KEK to copy them with.
var ErrKeyUnavailable = errors.New("the API has no SCADBUDDY_SECRET_KEY_FILE")

// forkEventTypes is events.py FORK_EVENT_TYPES.
var forkEventTypes = []string{"user.turn", "assistant.text.delta", "assistant.text.done", "tool.call", "tool.result"}

// eventChain is events.py _CHAIN: a session's fork chain by its event cuts.
const eventChain = `WITH RECURSIVE chain (id, parent, cut, bound, depth) AS (
 SELECT id, fork_parent_id, fork_event_cut, NULL::bigint, 0 FROM ai_sessions WHERE id = $1::uuid
 UNION ALL
 SELECT s.id, s.fork_parent_id, s.fork_event_cut, LEAST(c.bound, c.cut), c.depth + 1
 FROM ai_sessions s JOIN chain c ON s.id = c.parent WHERE c.cut IS NOT NULL)`

// CopyForkRanges is transcripts.py copy_fork_ranges (store.ts copyForkRanges), in the forget's
// transaction tx under the parent's lock: each fork that reads through parent takes the range
// it references as rows of its own, inherited and sealed under its own key, with its events and
// the images they name, and its link is cleared. A fork forgotten itself gets no rows, only its
// link cleared. How many forks there were; ErrKeyUnavailable without a KEK when a fork takes
// transcript entries.
func CopyForkRanges(ctx context.Context, tx pgx.Tx, keys *seal.Keys, parent string) (int, error) {
	type child struct {
		id  string
		cut *int64
	}
	rows, err := tx.Query(ctx, `SELECT id::text AS id, fork_cut AS cut FROM ai_sessions
		WHERE fork_parent_id = $1::uuid ORDER BY id FOR UPDATE`, parent)
	if err != nil {
		return 0, err
	}
	children, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (child, error) {
		var c child
		return c, row.Scan(&c.id, &c.cut)
	})
	if err != nil || len(children) == 0 {
		return 0, err
	}
	takesEntries := false
	for _, c := range children {
		takesEntries = takesEntries || (c.cut != nil && *c.cut > 0)
	}
	if takesEntries && (keys == nil || keys.Current == nil) {
		return 0, ErrKeyUnavailable
	}
	s := &Store{Keys: keys, tx: tx}
	var texts []string
	if takesEntries {
		segs, err := s.segments(ctx, parent)
		if err != nil {
			return 0, err
		}
		for _, seg := range segs {
			opened, err := s.open(ctx, seg.owner, "", seg.rows)
			var gone *httpx.Problem
			if errors.As(err, &gone) && gone.Status == http.StatusGone {
				// Python's SubjectForgottenError is no refusal here: the forget fails whole.
				return 0, errors.New(gone.Detail)
			}
			if err != nil {
				return 0, err
			}
			texts = append(texts, opened...)
		}
	}
	for _, c := range children {
		subject := "session-" + c.id
		var taken []string
		if c.cut != nil && *c.cut > 0 {
			taken = texts[:min(int64(len(texts)), *c.cut)]
		}
		var dek []byte
		if len(taken) > 0 {
			if dek, err = s.keyIn(ctx, tx, subject, true); err != nil {
				return 0, err
			}
		}
		// As an append does: a forget of the child either went first, and its tombstone is seen
		// here, or waits for this to commit and then deletes these rows with the rest.
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock_shared(hashtext($1))`,
			seal.PayloadKeyLock(subject)); err != nil {
			return 0, err
		}
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM ai_forgotten_subjects WHERE subject = $1`, subject).Scan(&one)
		forgotten := err == nil
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return 0, err
		}
		if dek != nil && !forgotten {
			if err := insertTaken(ctx, tx, c.id, dek, taken); err != nil {
				return 0, err
			}
		}
		if !forgotten {
			copied, err := copyForkEvents(ctx, tx, c.id)
			if err != nil {
				return 0, err
			}
			if err := copyForkBlobs(ctx, tx, c.id, blobNames(copied)); err != nil {
				return 0, err
			}
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_sessions SET fork_parent_id = NULL, fork_cut = NULL,
			fork_event_cut = NULL WHERE id = $1::uuid`, c.id); err != nil {
			return 0, err
		}
	}
	return len(children), nil
}

// insertTaken writes the child's inherited rows, sealed under its key, a batch at a time; a
// uuid the fork already holds as its own row stays there.
func insertTaken(ctx context.Context, tx pgx.Tx, child string, dek []byte, taken []string) error {
	for at := 0; at < len(taken); at += copyBatch {
		chunk := taken[at:min(at+copyBatch, len(taken))]
		rows, err := tx.Query(ctx, `SELECT nextval(pg_get_serial_sequence('ai_session_entries', 'id'))
			AS id FROM generate_series(1, $1)`, len(chunk))
		if err != nil {
			return err
		}
		ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
		if err != nil {
			return err
		}
		batch := &pgx.Batch{}
		for i, text := range chunk {
			entry, err := parsed(text)
			if err != nil {
				return err
			}
			uuid := stringMember(entry, "uuid")
			bound := "#" + strconv.FormatInt(ids[i], 10)
			if uuid != nil {
				bound = *uuid
			}
			sealed := seal.SealBytes(dek, []byte(text), entryContext(child, "", bound))
			batch.Queue(`INSERT INTO ai_session_entries (id, project_key, session_id, subpath, uuid,
				entry_type, sealed, inherited) VALUES ($1, '', $2, '', $3, $4, $5, true)
				ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING`,
				ids[i], child, uuid, entryTypeOf(entry), sealed)
		}
		if err := tx.SendBatch(ctx, batch).Close(); err != nil {
			return err
		}
	}
	return nil
}

// copyForkEvents is events.py copy_fork_events: the events the session reads through its
// chain become rows of its own, at the same seqs, re-addressed to it (sessionId), each written
// again as Python's json.dumps writes it. The events' texts.
func copyForkEvents(ctx context.Context, tx pgx.Tx, session string) ([]string, error) {
	rows, err := tx.Query(ctx, eventChain+` SELECT e.seq, e.event FROM chain c
		JOIN ai_session_events e ON e.session_id = c.id
		WHERE c.depth > 0 AND e.seq <= c.bound AND e.event::jsonb->>'type' = ANY($2)
		ORDER BY e.seq`, session, forkEventTypes)
	if err != nil {
		return nil, err
	}
	type logged struct {
		seq   int64
		event string
	}
	found, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (logged, error) {
		var l logged
		return l, row.Scan(&l.seq, &l.event)
	})
	if err != nil {
		return nil, err
	}
	seqs := make([]int64, len(found))
	texts := make([]string, len(found))
	for i, row := range found {
		value, err := jsonv.Loads([]byte(row.event))
		if err != nil {
			return nil, err
		}
		event, _ := value.(jsonv.Object)
		replaced := false
		for j := range event {
			if event[j].Key == "sessionId" {
				event[j].Value, replaced = session, true
			}
		}
		if !replaced {
			event = append(event, jsonv.Member{Key: "sessionId", Value: session})
		}
		seqs[i], texts[i] = row.seq, jsonv.Dumps(event)
	}
	for at := 0; at < len(found); at += eventBatch {
		end := min(at+eventBatch, len(found))
		if _, err := tx.Exec(ctx, `INSERT INTO ai_session_events (session_id, seq, event)
			SELECT $1::uuid, e.seq, e.event FROM unnest($2::bigint[], $3::text[]) AS e(seq, event)`,
			session, seqs[at:end], texts[at:end]); err != nil {
			return nil, err
		}
	}
	return texts, nil
}

// blobNames is forkPoint.ts blobNames: the images the events name, first seen first.
func blobNames(texts []string) []string {
	var names []string
	seen := map[string]bool{}
	add := func(v any) {
		if name, ok := v.(string); ok && !seen[name] {
			seen[name] = true
			names = append(names, name)
		}
	}
	for _, text := range texts {
		value, _ := jsonv.Parse([]byte(text))
		event, _ := value.(jsonv.Object)
		kind, _ := event.Text("type")
		images, _ := event.Get("images")
		list, _ := images.([]any)
		for _, item := range list {
			image, _ := item.(jsonv.Object)
			switch kind {
			case "tool.result":
				name, _ := image.Get("name")
				add(name)
			case "user.turn":
				if name, ok := image.Get("name"); ok && name != nil {
					add(name)
				}
				preview, _ := image.Get("preview")
				previewObject, _ := preview.(jsonv.Object)
				name, _ := previewObject.Get("name")
				add(name)
			}
		}
	}
	return names
}

// copyForkBlobs is events.py copy_fork_blobs: the images names lists, found through the chain,
// become rows of the session's own; a new one keeps its created_at, one the session already
// has is stamped now (#6413).
func copyForkBlobs(ctx context.Context, tx pgx.Tx, session string, names []string) error {
	if len(names) == 0 {
		return nil
	}
	_, err := tx.Exec(ctx, eventChain+` INSERT INTO ai_session_blobs (session_id, name, media_type,
		data, created_at) SELECT DISTINCT ON (b.name) $1::uuid, b.name, b.media_type, b.data,
		b.created_at FROM chain c JOIN ai_session_blobs b ON b.session_id = c.id
		WHERE c.depth > 0 AND b.name = ANY($2) ORDER BY b.name, c.depth
		ON CONFLICT (session_id, name) DO UPDATE SET created_at = clock_timestamp()`, session, names)
	return err
}
