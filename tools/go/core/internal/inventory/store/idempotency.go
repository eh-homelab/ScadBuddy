package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/problem"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	// lockClass is crud/idempotency.py LOCK_CLASS ("SCRQ"), the advisory lock class of a
	// (caller, key).
	lockClass = int32(0x5343_5251)
	// keyProblem is crud/idempotency.py KEY_PROBLEM.
	keyProblem = "https://scadbuddy.dev/problems/crud/idempotency-key-reused"

	// requestSelectSQL and requestInsertSQL are crud/shared/crud_requests_select.sql and
	// crud_requests_insert.sql, which crud/idempotency.py runs (#7667 §5.1).
	requestSelectSQL = `SELECT route, body_sha256, status, body FROM crud_requests WHERE caller = $1 AND key = $2`
	requestInsertSQL = `INSERT INTO crud_requests (caller, key, route, body_sha256, status, body)
VALUES ($1, $2, $3, $4, $5, $6) RETURNING body`
)

// WriteReq is one write: the caller and its Idempotency-Key, the route (or activity) it
// is, its input as JSON, and who it is by.
type WriteReq struct {
	Caller, Key, Route string
	Input              []byte
	Actor              Actor
}

// Answer is what a write gives, a success or a problem, as crud_requests keeps it: a
// retry with the same key gets the same bytes.
type Answer struct {
	Status int             `json:"status"`
	Body   json.RawMessage `json:"body"`
}

// Refused is a write's refusal (problem.Refused): an answer, not a failure (ruling C4).
type Refused = problem.Refused

// Write runs do once per (w.Caller, w.Key), in one transaction (Tx) with its crud_requests
// row, as crud/idempotency.py idempotent_write does. It takes the key's lock, so a second
// request with the key waits and then replays the first answer. A key stored with another
// route or input is 422 idempotency-key-reused, and nothing runs. A *Refused from do is its
// problem, recorded. An answer of 400 or more from do is recorded too, but what do wrote
// does not commit, so a retry never replays a half-done write. Any other error from do
// records nothing, commits nothing and is returned, so the caller (a Temporal activity) can
// run it again.
//
// Go takes the event log's lock before the key's lock; Python's idempotent_write takes the
// key's lock first. They never share a caller, so they meet only on a hashtext collision,
// and then Postgres aborts one of the two as a deadlock (a retry).
func Write(ctx context.Context, pool *pgxpool.Pool, w WriteReq, do func(pgx.Tx) (status int, body any, err error)) (Answer, error) {
	digest, err := inputDigest(w.Input)
	if err != nil {
		return Answer{}, err
	}
	var answer Answer
	err = Tx(ctx, pool, w.Actor, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1::integer, hashtext($2))`,
			lockClass, w.Caller+"\x1f"+w.Key); err != nil {
			return err
		}
		var route, sum string
		err := tx.QueryRow(ctx, requestSelectSQL, w.Caller, w.Key).Scan(&route, &sum, &answer.Status, &answer.Body)
		if err == nil {
			if route != w.Route || sum != digest {
				answer, err = reused()
			}
			return err
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		status, body, err := run(ctx, tx, do)
		if err != nil {
			return err
		}
		text, err := httpx.Marshal(body)
		if err != nil {
			return err
		}
		answer.Status = status
		return tx.QueryRow(ctx, requestInsertSQL, w.Caller, w.Key, w.Route, digest, status, json.RawMessage(text)).
			Scan(&answer.Body)
	})
	if err != nil {
		return Answer{}, err
	}
	return answer, nil
}

// run is do in a savepoint, so a refusal (a *Refused, or a status of 400 or more) rolls back
// what do wrote before it.
func run(ctx context.Context, tx pgx.Tx, do func(pgx.Tx) (int, any, error)) (int, any, error) {
	sp, err := tx.Begin(ctx)
	if err != nil {
		return 0, nil, err
	}
	status, body, err := do(sp)
	var refused *Refused
	if errors.As(err, &refused) {
		if err := sp.Rollback(ctx); err != nil {
			return 0, nil, err
		}
		return refused.Status, refused.Body(), nil
	}
	if err != nil {
		return 0, nil, err
	}
	if status >= 400 {
		return status, body, sp.Rollback(ctx)
	}
	return status, body, sp.Commit(ctx)
}

// reused is crud/idempotency.py's 422 for a key used for another request.
func reused() (Answer, error) {
	body, err := httpx.Marshal(map[string]any{
		"type":   keyProblem,
		"title":  httpx.Title(http.StatusUnprocessableEntity),
		"status": http.StatusUnprocessableEntity,
		"detail": "This Idempotency-Key was used for another request.",
	})
	return Answer{Status: http.StatusUnprocessableEntity, Body: body}, err
}

// inputDigest is the SHA-256 hex of input in canonical JSON (seal.Canonical, R-K1-2). Only
// Go writes the inventory caller's rows, so it need not be crud/idempotency.py _digest.
func inputDigest(input []byte) (string, error) {
	canonical, err := seal.Canonical(string(input), true)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256([]byte(canonical))
	return hex.EncodeToString(sum[:]), nil
}
