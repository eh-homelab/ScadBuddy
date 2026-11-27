// Package activity is the activity projection of the agent's command record:
// backend/scadbuddy/activity/index.py ActivityIndex.ingest_agent_operation_in and the
// upsert_in it runs, statement for statement, in the caller's transaction (#5299 step 8).
//
// A failed write is logged and the caller's own write still commits, as Python's savepoint
// keeps it. The row's activity.changed event is logged and NOTIFYed in the same transaction,
// so it is heard on commit and never after a rollback.
package activity

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

const (
	// eventLogLock is core/pg_events.py EVENT_LOG_LOCK.
	eventLogLock = int64(0x5343_4144_4556_4E54)
	// payloadMax is pg_events.py MAX_PAYLOAD_BYTES.
	payloadMax = 4000
	channel    = "scadbuddy_events"
)

// statuses is index.py _AGENT_STATUS.
var statuses = map[string]string{"running": "running", "succeeded": "succeeded", "failed": "failed"}

// finished is activity/models.py FINISHED.
var finished = map[string]bool{"succeeded": true, "failed": true, "cancelled": true}

// Row is the activity row of one ai_operations row: what ingest_agent_operation_in builds.
type Row struct {
	ID, Kind, Subject, Status, Title, SourceID, WorkflowID, RunID string
	// Detail is the JSON text of {"request", "result", "error"}, its values as Python
	// read them from the operation's jsonb (jsonv.Loads).
	Detail string
	// Principal is the attribution's principal ("browser" when the operation names none).
	Principal        string
	Session, ViaTool *string
}

// Title is the row's title: the kind with its underscores as spaces, capitalised as
// str.capitalize does, then the subject, stripped.
func Title(kind, subject string) string {
	return pyStrip(pyCapitalize(strings.ReplaceAll(kind, "_", " ")) + " " + subject)
}

// IngestOperation mirrors the ai_operations row id in tx: false when there is none, or when
// the activity write failed (logged; tx goes on).
func IngestOperation(ctx context.Context, tx pgx.Tx, id string) bool {
	row, err := operationRow(ctx, tx, id)
	if err != nil {
		slog.ErrorContext(ctx, "an activity write failed", "activity_id", "agent_operation:"+id, "error", err.Error())
		return false
	}
	if row == nil {
		return false
	}
	written, err := upsert(ctx, tx, row)
	if err != nil {
		slog.ErrorContext(ctx, "an activity write failed", "activity_id", row.ID, "error", err.Error())
		return false
	}
	return written
}

func operationRow(ctx context.Context, tx pgx.Tx, id string) (*Row, error) {
	var kind, status, workflowID, runID string
	var subject, principal, session, viaTool *string
	var request, result, failure *string
	err := tx.QueryRow(ctx, `SELECT kind, subject, status, request::text, result::text,
		error::text, principal, session, via_tool, workflow_id, workflow_run_id
		FROM ai_operations WHERE id = $1`, id).Scan(&kind, &subject, &status, &request,
		&result, &failure, &principal, &session, &viaTool, &workflowID, &runID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	detail := jsonv.Object{}
	for _, m := range []struct {
		name string
		text *string
	}{{"request", request}, {"result", result}, {"error", failure}} {
		var value any
		if m.text != nil {
			if value, err = jsonv.Loads([]byte(*m.text)); err != nil {
				return nil, err
			}
		}
		detail = append(detail, jsonv.Member{Key: m.name, Value: value})
	}
	row := &Row{
		ID: "agent_operation:" + id, Kind: "agent." + kind, Status: statuses[status],
		Title: Title(kind, deref(subject)), SourceID: id, WorkflowID: workflowID, RunID: runID,
		Subject: deref(subject), Detail: jsonv.Dumps(detail), Principal: "browser",
		Session: session, ViaTool: viaTool,
	}
	if principal != nil && *principal != "" {
		row.Principal = *principal
	}
	return row, nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// upsert is upsert_in's write, not a repair's, under a savepoint.
func upsert(ctx context.Context, tx pgx.Tx, row *Row) (written bool, err error) {
	err = pgx.BeginFunc(ctx, tx, func(sp pgx.Tx) error {
		// The event log's lock before the row (#3221).
		if _, err := sp.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, eventLogLock); err != nil {
			return err
		}
		var principal *string
		err := sp.QueryRow(ctx, `INSERT INTO activity (id, kind, subject, status, title, parent_id,
			facets, detail, source, source_id, workflow_id, run_id, traceparent, finished_at)
			VALUES ($1, $2, $3, $4, $5, NULL, '{}', $6::jsonb, 'ai_operations', $7, $8, $9, NULL,
			CASE WHEN $10::boolean THEN now() END)
			ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind, subject = EXCLUDED.subject,
			status = EXCLUDED.status, title = EXCLUDED.title, parent_id = EXCLUDED.parent_id,
			facets = EXCLUDED.facets, detail = EXCLUDED.detail, source = EXCLUDED.source,
			source_id = EXCLUDED.source_id, workflow_id = EXCLUDED.workflow_id,
			run_id = EXCLUDED.run_id, traceparent = EXCLUDED.traceparent, updated_at = now(),
			finished_at = CASE WHEN $10::boolean THEN coalesce(activity.finished_at, now()) END
			WHERE activity.finished_at IS NULL OR EXCLUDED.finished_at IS NOT NULL
			RETURNING principal`,
			row.ID, row.Kind, row.Subject, row.Status, row.Title, row.Detail, row.SourceID,
			row.WorkflowID, row.RunID, finished[row.Status]).Scan(&principal)
		if errors.Is(err, pgx.ErrNoRows) {
			// A late or retried write would have reopened a finished row.
			return nil
		}
		if err != nil {
			return err
		}
		if err := sp.QueryRow(ctx, `UPDATE activity SET principal = coalesce(principal, $1),
			session = coalesce(session, $2), via_tool = coalesce(via_tool, $3)
			WHERE id = $4 RETURNING principal`,
			row.Principal, row.Session, row.ViaTool, row.ID).Scan(&principal); err != nil {
			return err
		}
		written = true
		publishIn(ctx, sp, "activity.changed",
			httpx.Field{Key: "activity_id", Value: row.ID}, httpx.Field{Key: "principal", Value: principal})
		return nil
	})
	return written, err
}

// publishIn logs and NOTIFYs an event in tx under a savepoint, as pg_events.py publish_in:
// a failure is logged and the caller's change goes on.
func publishIn(ctx context.Context, tx pgx.Tx, kind string, fields ...httpx.Field) {
	id := make([]byte, 16)
	_, _ = rand.Read(id)
	at := time.Now()
	head := []httpx.Field{
		{Key: "id", Value: hex.EncodeToString(id)}, {Key: "at", Value: wire.Time(at)}, {Key: "kind", Value: kind},
	}
	payload, err := encode(append(head, fields...))
	if err == nil && len(payload) > payloadMax {
		slog.ErrorContext(ctx, "refused to publish an event over the NOTIFY payload cap", "kind", kind)
		return
	}
	if err == nil {
		err = pgx.BeginFunc(ctx, tx, func(sp pgx.Tx) error {
			if _, err := sp.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, eventLogLock); err != nil {
				return err
			}
			if _, err := sp.Exec(ctx, `INSERT INTO events (event_id, kind, at, payload)
				VALUES ($1, $2, $3, $4::jsonb)`, hex.EncodeToString(id), kind, at, payload); err != nil {
				return err
			}
			_, err := sp.Exec(ctx, `SELECT pg_notify($1, $2)`, channel, payload)
			return err
		})
	}
	if err != nil {
		slog.ErrorContext(ctx, "could not publish an event", "kind", kind, "error", err.Error())
	}
}

func encode(fields []httpx.Field) (string, error) {
	out := []byte{'{'}
	for i, f := range fields {
		if i > 0 {
			out = append(out, ',')
		}
		key, err := httpx.Marshal(f.Key)
		if err != nil {
			return "", err
		}
		value, err := httpx.Marshal(f.Value)
		if err != nil {
			return "", err
		}
		out = append(append(append(out, key...), ':'), value...)
	}
	return string(append(out, '}')), nil
}

// pySpace is str.isspace: Unicode White_Space with the information separators U+001C..U+001F.
func pySpace(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }

func pyStrip(s string) string { return strings.TrimFunc(s, pySpace) }

// pyCapitalize is str.capitalize: the first character in title case, the rest lower case,
// with Python's special cases for capital dotted I and the final sigma.
func pyCapitalize(s string) string {
	if s == "" {
		return s
	}
	first, n := utf8.DecodeRuneInString(s)
	var b strings.Builder
	b.WriteString(pyTitle(first))
	rest := []rune(s[n:])
	for i, r := range rest {
		switch {
		case r == 'İ':
			b.WriteString("i̇")
		case r == 'Σ' && finalSigma(append([]rune{first}, rest...), i+1):
			b.WriteRune('ς')
		default:
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return b.String()
}

// pyTitle is the full title-case mapping of the characters whose mapping is more than one.
func pyTitle(r rune) string {
	switch r {
	case 'ß':
		return "Ss"
	case 'ŉ':
		return "ʼN"
	case 'ﬀ':
		return "Ff"
	case 'ﬁ':
		return "Fi"
	case 'ﬂ':
		return "Fl"
	case 'ﬃ':
		return "Ffi"
	case 'ﬄ':
		return "Ffl"
	case 'ﬅ', 'ﬆ':
		return "St"
	}
	return string(unicode.ToTitle(r))
}

// finalSigma is Unicode's Final_Sigma condition at i: a cased letter before it (past any
// case-ignorable ones) and none after it.
func finalSigma(s []rune, i int) bool {
	before := false
	for j := i - 1; j >= 0; j-- {
		if caseIgnorable(s[j]) {
			continue
		}
		before = cased(s[j])
		break
	}
	if !before {
		return false
	}
	for j := i + 1; j < len(s); j++ {
		if caseIgnorable(s[j]) {
			continue
		}
		return !cased(s[j])
	}
	return true
}

func cased(r rune) bool {
	return unicode.IsUpper(r) || unicode.IsLower(r) || unicode.IsTitle(r)
}

func caseIgnorable(r rune) bool {
	return unicode.In(r, unicode.Mn, unicode.Me, unicode.Cf, unicode.Lm, unicode.Sk) ||
		r == '\'' || r == '.' || r == ':' || r == '^' || r == '`' || r == '·' || r == '’'
}
