// Package report sends the registry rows that changed since they were last sent, and every
// row again after the refresh interval, so core's last_seen stays fresh.
package report

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/discover"
	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/registry"
)

// Registry is core's registry routes.
type Registry interface {
	PutMachine(ctx context.Context, m discover.Machine) error
	PutSession(ctx context.Context, machineID string, r discover.Row) error
}

type sent struct {
	row discover.Row
	at  time.Time
}

// refusal is a row core refused with 422, and why. Sending the same row again would only be
// refused again, so it waits until the row changes.
type refusal struct {
	row    discover.Row
	reason error
}

// Result counts one Report's live sessions and its registry writes, the machine's and the sessions'.
type Result struct {
	Sessions, Sent, Failed int
}

// Reporter remembers what core has. It is not safe for concurrent use.
type Reporter struct {
	reg     Registry
	machine discover.Machine
	refresh time.Duration
	now     func() time.Time

	machineAt time.Time // zero: send the machine
	rows      map[string]sent
	refused   map[string]refusal // by session: a row core refused with 422
	missing   bool               // a route-missing 404 was logged and nothing succeeded since

	logRouteMissing func()
	logRefused      func(session string, reason error)
	// Last is the last Report's counts.
	Last Result
}

// New is a Reporter of machine's rows to reg.
func New(reg Registry, machine discover.Machine, refresh time.Duration) *Reporter {
	return &Reporter{reg: reg, machine: machine, refresh: refresh, now: time.Now, rows: map[string]sent{},
		refused: map[string]refusal{},
		logRouteMissing: func() {
			slog.Warn("fleetd: core answered 404 with no reason, so it has no registry route yet (an older core during a rollout?); trying again at each scan")
		},
		logRefused: func(session string, reason error) {
			slog.Warn("fleetd: core refused a session's row; it is sent again only once the row changes",
				"session", session, "err", reason)
		}}
}

// Report sends the machine when due and each row that changed or is due. A route-missing
// 404 stops the scan: the rows not sent are tried again at the next one, and it is logged
// once until a send succeeds. An unknown-machine 404 registers the machine and sends the row
// once more. A row core refuses with 422 is logged once and not sent again until it changes.
// Other failures are logged and the row is tried again at the next scan.
func (r *Reporter) Report(ctx context.Context, rows []discover.Row) error {
	now := r.now()
	r.Last = Result{Sessions: len(rows)}
	if r.machineAt.IsZero() || now.Sub(r.machineAt) >= r.refresh {
		if err := r.putMachine(ctx, now); err != nil {
			return r.failed(err)
		}
	}
	live := make(map[string]bool, len(rows))
	var firstErr error
	for _, row := range rows {
		live[row.SessionID] = true
		if s, ok := r.rows[row.SessionID]; ok && s.row == row && now.Sub(s.at) < r.refresh {
			continue
		}
		if f, ok := r.refused[row.SessionID]; ok && f.row == row {
			continue
		}
		err := r.reg.PutSession(ctx, r.machine.ID, row)
		if errors.Is(err, registry.ErrUnknownMachine) {
			if err = r.putMachine(ctx, now); err == nil {
				err = r.reg.PutSession(ctx, r.machine.ID, row)
			}
		}
		if errors.Is(err, registry.ErrRouteMissing) {
			return r.failed(err)
		}
		var problem *registry.ProblemError
		if errors.As(err, &problem) && problem.Status == http.StatusUnprocessableEntity {
			r.Last.Failed++
			delete(r.rows, row.SessionID)
			r.refused[row.SessionID] = refusal{row: row, reason: err}
			r.logRefused(row.SessionID, err)
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		if err != nil {
			r.Last.Failed++
			delete(r.rows, row.SessionID)
			slog.Warn("fleetd: sending a session failed", "session", row.SessionID, "err", err)
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		r.missing = false
		r.Last.Sent++
		r.rows[row.SessionID] = sent{row: row, at: now}
		delete(r.refused, row.SessionID)
	}
	// A session gone from the scan is forgotten, so it is sent whole if it comes back.
	for id := range r.rows {
		if !live[id] {
			delete(r.rows, id)
		}
	}
	for id := range r.refused {
		if !live[id] {
			delete(r.refused, id)
		}
	}
	return firstErr
}

func (r *Reporter) putMachine(ctx context.Context, now time.Time) error {
	if err := r.reg.PutMachine(ctx, r.machine); err != nil {
		r.machineAt = time.Time{}
		return err
	}
	r.missing = false
	r.Last.Sent++
	r.machineAt = now
	return nil
}

func (r *Reporter) failed(err error) error {
	r.Last.Failed++
	if errors.Is(err, registry.ErrRouteMissing) {
		if !r.missing {
			r.missing = true
			r.logRouteMissing()
		}
		return err
	}
	slog.Warn("fleetd: registering the machine failed", "err", err)
	return err
}
