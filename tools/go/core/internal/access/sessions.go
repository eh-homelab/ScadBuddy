package access

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// The plugin sessions (plugins/pluginSessions.ts): an upstream MCP session seen and ended,
// the database's clock as the start mark, and the sweep's two reads.
const (
	ledgerMax    = 200
	sessionIDMax = 500
	replicaMax   = 300
	replicasMax  = 500
)

type ok struct {
	OK bool `json:"ok"`
}

// sessionBody reads {plugin, id} and, with replica, {replica} too.
func sessionBody(r *http.Request, replica bool) (plugin, id, host string, err error) {
	raw, err := readBody(r)
	if err != nil {
		return "", "", "", err
	}
	f := &form{}
	loc := []string{"body"}
	names := []string{"plugin", "id"}
	if replica {
		names = append(names, "replica")
	}
	fields := f.object(loc, raw, names...)
	plugin = f.text(at(loc, "plugin"), f.required(loc, fields, "plugin"), 1, ledgerMax, nil)
	id = f.text(at(loc, "id"), f.required(loc, fields, "id"), 1, sessionIDMax, nil)
	if replica {
		host = f.text(at(loc, "replica"), f.required(loc, fields, "replica"), 1, replicaMax, nil)
	}
	return plugin, id, host, f.err()
}

func (s *Store) sessionSeen(r *http.Request, _ crud.Params) (any, error) {
	plugin, id, replica, err := sessionBody(r, true)
	if err != nil {
		return nil, err
	}
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `INSERT INTO ai_plugin_sessions (plugin_id, upstream_session_id, replica)
			VALUES ($1, $2, $3) ON CONFLICT DO NOTHING`, plugin, id, replica)
		return err
	})
	if err != nil {
		return nil, err
	}
	return ok{true}, nil
}

func (s *Store) sessionEnded(r *http.Request, _ crud.Params) (any, error) {
	plugin, id, _, err := sessionBody(r, false)
	if err != nil {
		return nil, err
	}
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `DELETE FROM ai_plugin_sessions
			WHERE plugin_id = $1 AND upstream_session_id = $2`, plugin, id)
		return err
	})
	if err != nil {
		return nil, err
	}
	return ok{true}, nil
}

func (s *Store) mark(r *http.Request, _ crud.Params) (any, error) {
	var mark string
	err := pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		return tx.QueryRow(r.Context(), `SELECT now()::text AS t`).Scan(&mark)
	})
	if err != nil {
		return nil, err
	}
	return struct {
		// Mark is the database's clock as Postgres's text, to the microsecond.
		Mark string `json:"mark"`
	}{mark}, nil
}

type sessionRow struct {
	PluginID          string `json:"plugin_id"`
	UpstreamSessionID string `json:"upstream_session_id"`
	Old               bool   `json:"old"`
}

func (s *Store) sessionRows(r *http.Request, sql string, args ...any) (any, error) {
	var rows []sessionRow
	err := pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		found, err := tx.Query(r.Context(), sql, args...)
		if err != nil {
			return err
		}
		rows, err = pgx.CollectRows(found, pgx.RowToStructByPos[sessionRow])
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Rows []sessionRow `json:"rows"`
	}{nonNil(rows)}, nil
}

// sessionsDead is sweepDeadReplicas's read: the rows of replicas that do not poll, past the grace.
func (s *Store) sessionsDead(r *http.Request, _ crud.Params) (any, error) {
	raw, err := readBody(r)
	if err != nil {
		return nil, err
	}
	f := &form{}
	loc := []string{"body"}
	fields := f.object(loc, raw, "live", "grace_ms", "give_up_ms")
	items := f.list(at(loc, "live"), f.required(loc, fields, "live"), 1, replicasMax)
	live := make([]string, len(items))
	for i, item := range items {
		live[i] = f.text(at(loc, "live", strconv.Itoa(i)), item, 1, replicaMax, nil)
	}
	grace := f.integer(at(loc, "grace_ms"), f.required(loc, fields, "grace_ms"), 0, 7*24*3_600_000)
	giveUp := f.integer(at(loc, "give_up_ms"), f.required(loc, fields, "give_up_ms"), 0, 30*24*3_600_000)
	if err := f.err(); err != nil {
		return nil, err
	}
	return s.sessionRows(r, `SELECT plugin_id, upstream_session_id,
		created_at < now() - make_interval(secs => $1) AS old
		FROM ai_plugin_sessions
		WHERE replica <> ALL($2) AND created_at < now() - make_interval(secs => $3)`,
		float64(giveUp)/1000, live, float64(grace)/1000)
}

// sessionsOwn is endOwnSessions's read: this replica's rows from before mark.
func (s *Store) sessionsOwn(r *http.Request, _ crud.Params) (any, error) {
	raw, err := readBody(r)
	if err != nil {
		return nil, err
	}
	f := &form{}
	loc := []string{"body"}
	fields := f.object(loc, raw, "replica", "mark")
	replica := f.text(at(loc, "replica"), f.required(loc, fields, "replica"), 1, replicaMax, nil)
	mark := f.text(at(loc, "mark"), f.required(loc, fields, "mark"), 10, 64, nil)
	if err := f.err(); err != nil {
		return nil, err
	}
	out, err := s.sessionRows(r, `SELECT plugin_id, upstream_session_id, false AS old
		FROM ai_plugin_sessions WHERE replica = $1 AND created_at < $2::text::timestamptz`,
		replica, mark)
	var pg *pgconn.PgError
	if errors.As(err, &pg) && pg.Code == "22007" {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: "That is not a mark."}
	}
	return out, err
}
