package secrets

// The fleet's logins (#7984; spec 2026-11-26-fleet-session-registry-and-rotator-design §6.2):
// caller fleet-rotator's lease, event and rotation routes, and a person's route that adds a
// fleet login. A lease reveals a claude_login's secret to the one machine that holds it; it is
// the only answer of these routes that carries a secret. The agent's routes never see a fleet
// row (agentsOwn), and these routes see nothing but fleet rows.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/fleet"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

// FleetLoginsArea names the lease, event and rotation routes and the person route.
const FleetLoginsArea = "fleet-logins"

const (
	rotatorBodyMax = 64 << 10
	// rotationSessionsMax is the most sessions one rotation's check names.
	rotationSessionsMax = 500
)

var (
	// plainText is a text with no control character (C0, DEL or C1).
	plainText = regexp.MustCompile(`^[^\x00-\x1f\x7f\x{80}-\x{9f}]+$`)
	// absolutePath is an absolute path with no control character.
	absolutePath = regexp.MustCompile(`^/[^\x00-\x1f\x7f\x{80}-\x{9f}]*$`)
)

// fleetLogin is a row the rotator may lease: a claude_login the fleet uses.
const fleetLogin = "kind = 'claude_login' AND scope IN ('fleet', 'both')"

// accountCooling is a cooldown of any login of the row's account (alias c): a usage limit is
// the account's, so every login of it waits (#7995 §6.1).
const accountCooling = `EXISTS (SELECT 1 FROM ai_credentials s WHERE s.kind = 'claude_login'
	AND s.account_uuid = c.account_uuid AND s.status = 'cooling_down' AND s.cooldown_until > now())`

// RotatorRoutes are caller fleet-rotator's lease, event and rotation routes, under
// /api/v1/internal/crud/fleet-rotator. main mounts them for the service credential and for a
// fleet access token (crud.Router.AddForToken). Each names the caller's machine, which the
// caller's token or proof must own (fleet.OwnMachine).
func RotatorRoutes(pool *pgxpool.Pool, keys *seal.Keys) []crud.Route {
	s := &Store{Pool: pool, Keys: keys}
	return []crud.Route{
		{Method: "POST", Path: "/leases", Handle: s.lease, MaxBody: rotatorBodyMax},
		{Method: "PUT", Path: "/leases/{credential}", Handle: s.writeBack, MaxBody: rotatorBodyMax},
		{Method: "DELETE", Path: "/leases/{credential}", Handle: s.release},
		{Method: "POST", Path: "/events", Handle: s.fleetEvent, MaxBody: rotatorBodyMax},
		{Method: "POST", Path: "/rotations", Handle: s.createRotation, MaxBody: rotatorBodyMax, Status: 201},
		{Method: "PUT", Path: "/rotations/{id}", Handle: s.putRotation, MaxBody: rotatorBodyMax},
	}
}

// FleetLoginRoutes is the person's route that adds a fleet login: POST /api/v1/fleet/logins.
func FleetLoginRoutes(pool *pgxpool.Pool, keys *seal.Keys) []auth.Public {
	s := &Store{Pool: pool, Keys: keys}
	return []auth.Public{{
		Pattern: "POST /api/v1/fleet/logins",
		Route:   auth.Route{Class: "person", Template: "/api/v1/fleet/logins"},
		Handler: crud.Serve(s.addFleetLogin, rotatorBodyMax),
	}}
}

// fleetAudit writes one ai_audit row of kind credential for the request's machine credential.
func fleetAudit(ctx context.Context, tx pgx.Tx, r *http.Request, action, outcome, detail string) error {
	kind, id, label := fleet.Principal(r)
	if runes := []rune(detail); len(runes) > detailMax {
		detail = string(runes[:detailMax])
	}
	_, err := tx.Exec(ctx, `INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,
		principal_label, outcome, detail, started_at, finished_at)
		VALUES ('credential', $1, 'http', $2, $3, $4, $5, $6, now(), now())`,
		action, kind, id, label, outcome, detail)
	return err
}

// short is a machine id's first 12 characters, enough to name it in an audit row.
func short(machine string) string { return machine[:12] }

func readMachine(b *crud.Body) string { return b.Match("machine_id", 64, 64, fleet.MachinePattern) }

// leased is a lease's answer: the row, the lease's epoch and the claudeAiOauth login.
type leased struct {
	Credential stored          `json:"credential"`
	LeaseEpoch int64           `json:"lease_epoch"`
	Login      json.RawMessage `json:"login"`
}

// lease leases the first usable fleet login by priority to the machine and reveals it: not
// disabled, not cooling down, no login of its account cooling down, held by no machine, and
// sealed under the current KEK. With none it answers recovers_at, the soonest one is usable
// again, or null when none recovers on its own.
func (s *Store) lease(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "machine_id")
	if err != nil {
		return nil, err
	}
	machine := readMachine(b)
	if err := b.Err(); err != nil {
		return nil, err
	}
	ctx, kek := r.Context(), s.Keys.Current
	out := struct {
		Lease      *leased `json:"lease"`
		RecoversAt *string `json:"recovers_at"`
	}{}
	var sealErr error
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if err := fleet.OwnMachine(ctx, tx, r, machine); err != nil {
			return err
		}
		found, err := tx.Query(ctx, `SELECT `+columns+`, secret_sealed, dek_sealed FROM ai_credentials c
			WHERE `+fleetLogin+` AND leased_to IS NULL AND status <> 'disabled'
			AND NOT (status = 'cooling_down' AND cooldown_until > now()) AND NOT `+accountCooling+`
			AND kek_id = $1 AND get_byte(secret_sealed, 0) <> $2
			ORDER BY priority LIMIT 1 FOR UPDATE SKIP LOCKED`, kek.ID, seal.V1)
		if err != nil {
			return err
		}
		rows, err := pgx.CollectRows(found, pgx.RowToStructByPos[row])
		if err != nil {
			return err
		}
		if len(rows) == 0 {
			var at *time.Time
			if err := tx.QueryRow(ctx, `SELECT min(GREATEST(
					CASE WHEN status = 'cooling_down' AND cooldown_until > now() THEN cooldown_until END,
					(SELECT max(s.cooldown_until) FROM ai_credentials s WHERE s.kind = 'claude_login'
					 AND s.account_uuid = c.account_uuid AND s.status = 'cooling_down'
					 AND s.cooldown_until > now())))
				FROM ai_credentials c WHERE `+fleetLogin+` AND leased_to IS NULL AND status <> 'disabled'
				AND kek_id = $1`, kek.ID).Scan(&at); err != nil {
				return err
			}
			out.RecoversAt = iso(at)
			return fleetAudit(ctx, tx, r, "fleet.lease", "refused",
				"no usable Claude login for machine "+short(machine))
		}
		got := &rows[0]
		o, err := open(kek, got)
		if err != nil {
			sealErr = err
			return fleetAudit(ctx, tx, r, "fleet.reveal", "error", got.label()+" did not open")
		}
		if err := tx.QueryRow(ctx, `UPDATE ai_credentials SET leased_to = $2, leased_at = now(),
			lease_epoch = lease_epoch + 1 WHERE id = $1 RETURNING lease_epoch`, got.ID, machine,
		).Scan(&got.LeaseEpoch); err != nil {
			return err
		}
		now, err := first(ctx, tx, &got.ID)
		if err != nil {
			return err
		}
		out.Lease = &leased{Credential: now.stored(), LeaseEpoch: now.LeaseEpoch, Login: json.RawMessage(o.Secret)}
		if err := fleetAudit(ctx, tx, r, "fleet.lease", "ok",
			fmt.Sprintf("%s leased to machine %s (lease %d)", got.label(), short(machine), now.LeaseEpoch)); err != nil {
			return err
		}
		return fleetAudit(ctx, tx, r, "fleet.reveal", "ok", got.label()+" to machine "+short(machine))
	})
	if err != nil {
		return nil, err
	}
	if sealErr != nil {
		return nil, httpx.WithReason(http.StatusConflict, sealErr.Error(), credentialSealed)
	}
	return out, nil
}

func leaseStale() error {
	return httpx.WithReason(http.StatusConflict,
		"the machine does not hold that lease at that lease_epoch: lease again", "lease-stale")
}

func unknownLogin() error {
	return httpx.WithReason(http.StatusNotFound, "no fleet Claude login has that id", "unknown-credential")
}

// held locks a fleet login's row and is nil when machine holds it at epoch.
func held(ctx context.Context, tx pgx.Tx, id, machine string, epoch int64) (*row, error) {
	found, err := tx.Query(ctx, `SELECT `+columns+`, NULL::bytea, NULL::bytea FROM ai_credentials
		WHERE id = $1 AND `+fleetLogin+` FOR UPDATE`, id)
	if err != nil {
		return nil, err
	}
	rows, err := pgx.CollectRows(found, pgx.RowToStructByPos[row])
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, unknownLogin()
	}
	if rows[0].LeasedTo == nil || *rows[0].LeasedTo != machine || rows[0].LeaseEpoch != epoch {
		return nil, leaseStale()
	}
	return &rows[0], nil
}

func credentialParam(p crud.Params) (string, error) { return p.Text("credential", 1, credentialIDMax) }

// writeBack seals the machine's current login over the row it leases (the refresh token
// changes on each refresh), while it holds the lease at lease_epoch. The login must be of the
// row's account. The row's health and epoch stay: it is the same login, refreshed.
func (s *Store) writeBack(r *http.Request, p crud.Params) (any, error) {
	id, err := credentialParam(p)
	if err != nil {
		return nil, err
	}
	b, err := crud.ReadBody(r, "machine_id", "lease_epoch", "login")
	if err != nil {
		return nil, err
	}
	machine := readMachine(b)
	epoch := b.Int("lease_epoch", 0)
	raw := b.RawObject("login")
	if err := b.Err(); err != nil {
		return nil, err
	}
	secret := pyStrip(string(raw))
	acct, tail, err := loginAccount(secret)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if err := fleet.OwnMachine(ctx, tx, r, machine); err != nil {
			return err
		}
		current, err := held(ctx, tx, id, machine, epoch)
		if err != nil {
			return err
		}
		if current.AccountUUID != nil && acct.AccountUUID != nil && *current.AccountUUID != *acct.AccountUUID {
			return httpx.WithReason(http.StatusConflict,
				"that login is of another account than the row's: add it as a login of its own", "another-account")
		}
		envelope := seal.SealSecret(s.Keys.Current, secret, credentialAAD(id, claudeLogin, nil))
		if _, err := tx.Exec(ctx, `UPDATE ai_credentials SET secret_sealed = $2, dek_sealed = $3, kek_id = $4,
			last4 = $5, account_uuid = COALESCE($6, account_uuid),
			organization_uuid = COALESCE($7, organization_uuid),
			subscription_type = COALESCE($8, subscription_type), rate_limit_tier = COALESCE($9, rate_limit_tier),
			updated_at = now() WHERE id = $1`,
			id, envelope.SecretSealed, envelope.DEKSealed, envelope.KEKID, tail, acct.AccountUUID,
			acct.OrganizationUUID, acct.SubscriptionType, acct.RateLimitTier); err != nil {
			return err
		}
		if out, err = first(ctx, tx, &id); err != nil {
			return err
		}
		return fleetAudit(ctx, tx, r, "fleet.write_back", "ok",
			fmt.Sprintf("%s written back by machine %s (lease %d)", out.label(), short(machine), epoch))
	})
	if err != nil {
		return nil, err
	}
	return one(out), nil
}

// release ends the machine's lease of the row at lease_epoch; the next lease bumps it again.
func (s *Store) release(r *http.Request, p crud.Params) (any, error) {
	id, err := credentialParam(p)
	if err != nil {
		return nil, err
	}
	machine := r.URL.Query().Get("machine_id")
	if !fleet.MachinePattern.MatchString(machine) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", "machine_id"},
			Msg: "String should match pattern '" + fleet.MachinePattern.String() + "'"})
	}
	epoch, given, err := crud.QueryInt(r, "lease_epoch", 0, 1<<62)
	if err != nil {
		return nil, err
	}
	if !given {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", "lease_epoch"}, Msg: "Field required"})
	}
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if err := fleet.OwnMachine(ctx, tx, r, machine); err != nil {
			return err
		}
		if _, err := held(ctx, tx, id, machine, epoch); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_credentials SET leased_to = NULL, leased_at = NULL,
			lease_epoch = lease_epoch + 1 WHERE id = $1`, id); err != nil {
			return err
		}
		if out, err = first(ctx, tx, &id); err != nil {
			return err
		}
		return fleetAudit(ctx, tx, r, "fleet.release", "ok",
			fmt.Sprintf("%s released by machine %s (lease %d)", out.label(), short(machine), epoch))
	})
	if err != nil {
		return nil, err
	}
	return one(out), nil
}

// fleetKinds are a transcript event's kinds (tools/go/claudecreds Kind, and recovery), and the
// agent's health write each one is.
var fleetKinds = map[string]string{
	"rate_limit": "cooling_down", "permanent": "disabled", "transient": "transient", "recovery": "used",
}

// fleetEvent records a credential event a machine read in a transcript, once per entry uuid,
// with the agent's health-write rules (recordIn: keyed by the row's epoch, a later cooldown
// wins). The row must be a fleet login the machine leases. A rate limit cools every other
// login of the row's account too, by the same later-wins rule.
func (s *Store) fleetEvent(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "machine_id", "entry_uuid", "session_id", "credential_id", "epoch",
		"kind", "reason", "until", "reset_source")
	if err != nil {
		return nil, err
	}
	machine := readMachine(b)
	entry := b.UUID("entry_uuid")
	session := b.OptUUID("session_id")
	id := b.Text("credential_id", 1, credentialIDMax)
	epoch := b.IntRange("epoch", 0, 1<<31-1)
	kind := b.Choice("kind", "rate_limit", "permanent", "transient", "recovery")
	reason := b.OptText("reason", 1, 2000, nil)
	until := b.OptTime("until")
	source := b.OptChoice("reset_source", "epoch", "text", "default")
	if err := b.Err(); err != nil {
		return nil, err
	}
	switch {
	case kind == "rate_limit" && until == nil:
		return nil, httpx.WithReason(422, "a rate_limit event needs until", credentialRefused)
	case (kind == "permanent" || kind == "transient") && reason == nil:
		return nil, httpx.WithReason(422, fmt.Sprintf("a %s event needs a reason", kind), credentialRefused)
	case kind == "rate_limit" && reason == nil:
		limited := "rate limited"
		reason = &limited
	}
	type transition struct {
		Before    string `json:"before"`
		After     string `json:"after"`
		Recovered bool   `json:"recovered"`
	}
	out := struct {
		Duplicate  bool        `json:"duplicate"`
		Transition *transition `json:"transition"`
		Cooled     []string    `json:"cooled"`
	}{Cooled: []string{}}
	ctx := r.Context()
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if err := fleet.OwnMachine(ctx, tx, r, machine); err != nil {
			return err
		}
		var leasedTo, account *string
		err := tx.QueryRow(ctx, `SELECT leased_to, account_uuid FROM ai_credentials WHERE id = $1 AND `+fleetLogin,
			id).Scan(&leasedTo, &account)
		if errors.Is(err, pgx.ErrNoRows) {
			return unknownLogin()
		}
		if err != nil {
			return err
		}
		if leasedTo == nil || *leasedTo != machine {
			return httpx.WithReason(http.StatusConflict,
				"the machine does not lease that login, so it records no event on it", "not-leased")
		}
		tag, err := tx.Exec(ctx, `INSERT INTO fleet_credential_events (entry_uuid, machine_id, session_id,
			credential_id, kind, reset_at, reset_source, reason) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
			ON CONFLICT (entry_uuid) DO NOTHING`, entry, machine, session, id, kind, until, source, reason)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			out.Duplicate = true
			return nil
		}
		found, err := recordIn(ctx, tx, id, epoch, fleetKinds[kind], reason, until, nil, false)
		if err != nil || found == nil {
			return err
		}
		out.Transition = &transition{found.Before, found.After, found.Raw == "cooling_down" && found.After == "active"}
		if kind != "rate_limit" || account == nil {
			return nil
		}
		cooled, err := tx.Query(ctx, `UPDATE ai_credentials c SET
				status = CASE WHEN c.status = 'disabled' THEN 'disabled' ELSE 'cooling_down' END,
				cooldown_until = CASE WHEN c.status = 'disabled' THEN NULL ELSE GREATEST($3::timestamptz,
					CASE WHEN c.status = 'cooling_down' AND c.cooldown_until > now() THEN c.cooldown_until END) END,
				last_error = $4, last_error_at = now()
			WHERE c.kind = 'claude_login' AND c.account_uuid = $2 AND c.id <> $1
			RETURNING c.id`, id, *account, until, reason)
		if err != nil {
			return err
		}
		out.Cooled, err = pgx.CollectRows(cooled, pgx.RowTo[string])
		if out.Cooled == nil {
			out.Cooled = []string{}
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// rotation is a fleet_rotations row.
type rotation struct {
	ID               string          `json:"id"`
	MachineID        string          `json:"machine_id"`
	ConfigDir        *string         `json:"config_dir"`
	FromCredentialID *string         `json:"from_credential_id"`
	ToCredentialID   *string         `json:"to_credential_id"`
	Reason           string          `json:"reason"`
	RotatedAt        wire.Time       `json:"rotated_at"`
	Sessions         json.RawMessage `json:"sessions"`
	VerifiedAt       *crud.Time      `json:"verified_at"`
	CreatedAt        wire.Time       `json:"created_at"`
	UpdatedAt        wire.Time       `json:"updated_at"`
}

const rotationColumns = `id::text, machine_id, config_dir, from_credential_id, to_credential_id, reason,
	rotated_at, sessions, verified_at, created_at, updated_at`

func scanRotation(row pgx.Row) (rotation, error) {
	var (
		o        rotation
		verified *time.Time
		sessions []byte
	)
	err := row.Scan(&o.ID, &o.MachineID, &o.ConfigDir, &o.FromCredentialID, &o.ToCredentialID, &o.Reason,
		&o.RotatedAt, &sessions, &verified, &o.CreatedAt, &o.UpdatedAt)
	o.Sessions, o.VerifiedAt = sessions, crud.TimeOf(verified)
	return o, err
}

// createRotation records a rotation of one of the machine's config dirs: from one login to
// another, why, and when.
func (s *Store) createRotation(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "machine_id", "config_dir", "from_credential_id", "to_credential_id",
		"reason", "rotated_at")
	if err != nil {
		return nil, err
	}
	machine := readMachine(b)
	configDir := b.OptText("config_dir", 1, 4096, absolutePath)
	from := b.OptText("from_credential_id", 1, credentialIDMax, nil)
	to := b.OptText("to_credential_id", 1, credentialIDMax, nil)
	reason := b.Match("reason", 1, 200, plainText)
	at := b.OptTime("rotated_at")
	if err := b.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out rotation
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if err := fleet.OwnMachine(ctx, tx, r, machine); err != nil {
			return err
		}
		var err error
		out, err = scanRotation(tx.QueryRow(ctx, `INSERT INTO fleet_rotations (id, machine_id, config_dir,
			from_credential_id, to_credential_id, reason, rotated_at)
			VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, now())) RETURNING `+rotationColumns,
			newUUID(), machine, configDir, from, to, reason, at))
		if err != nil {
			return err
		}
		return fleetAudit(ctx, tx, r, "fleet.rotation", "ok", fmt.Sprintf("rotation %s on machine %s: %s to %s (%s)",
			out.ID, short(machine), orNone(from), orNone(to), reason))
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// utc is t as a rotation's other times are written (wire.Time): RFC 3339 in UTC.
func utc(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := t.UTC().Format(time.RFC3339Nano)
	return &s
}

func orNone(s *string) string {
	if s == nil {
		return "none"
	}
	return *s
}

// putRotation writes a rotation's check whole: each told session's result, and verified_at
// (null clears it). Only the rotation's machine's owner writes it.
func (s *Store) putRotation(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("id")
	if err != nil {
		return nil, err
	}
	b, err := crud.ReadBody(r, "sessions", "verified_at")
	if err != nil {
		return nil, err
	}
	type told struct {
		SessionID string  `json:"session_id"`
		Result    string  `json:"result"`
		At        *string `json:"at"`
	}
	sessions := []told{}
	for _, n := range b.Objects("sessions", false, rotationSessionsMax, "session_id", "result", "at") {
		t := told{SessionID: n.UUID("session_id"),
			Result: n.Choice("result", "pending", "passed", "failed", "no-answer", "unreachable"),
			At:     utc(n.OptTime("at"))}
		n.Done()
		sessions = append(sessions, t)
	}
	verified := b.OptTime("verified_at")
	if err := b.Err(); err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(sessions)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out rotation
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		var machine string
		err := tx.QueryRow(ctx, `SELECT machine_id FROM fleet_rotations WHERE id = $1 FOR UPDATE`, id).Scan(&machine)
		if errors.Is(err, pgx.ErrNoRows) {
			return httpx.WithReason(http.StatusNotFound, "no rotation has that id", "unknown-rotation")
		}
		if err != nil {
			return err
		}
		if err := fleet.OwnMachine(ctx, tx, r, machine); err != nil {
			return err
		}
		out, err = scanRotation(tx.QueryRow(ctx, `UPDATE fleet_rotations SET sessions = $2::jsonb,
			verified_at = $3, updated_at = now() WHERE id = $1 RETURNING `+rotationColumns, id, string(encoded), verified))
		if err != nil {
			return err
		}
		detail := fmt.Sprintf("rotation %s on machine %s: %d session(s) checked", id, short(machine), len(sessions))
		if verified != nil {
			detail += ", verified"
		}
		return fleetAudit(ctx, tx, r, "fleet.rotation_check", "ok", detail)
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// addFleetLogin is a person's create of a fleet credential (the issue's agent=False path): a
// claude_login (scope fleet), or another kind whose scope is fleet or both. Only a person signed
// in themselves may call it, in every SCADBUDDY_AUTH_MODE: a token, a service or no one is
// refused here, not only in enforce. It answers the stored row, never the secret.
func (s *Store) addFleetLogin(r *http.Request) (any, error) {
	c := auth.CallerOf(r.Context())
	if reason := auth.WouldRefuse("person", c, r.Header.Get(auth.RequestHeader) == "1", true, r.Method); reason != "" {
		_, bearer := r.Header["Authorization"]
		return nil, auth.Refusal("person", reason, bearer)
	}
	f, err := readForm(r, updateFields...)
	if err != nil {
		return nil, err
	}
	u := readUpdate(f)
	if err := f.err(); err != nil {
		return nil, err
	}
	if u.kind != claudeLogin && (u.scope == nil || (*u.scope != "fleet" && *u.scope != "both")) {
		return nil, refused(422, `a fleet login is a claude_login, or another kind whose scope is "fleet" or "both"`)
	}
	if u.secret == nil {
		return nil, refused(400, "secret is required for a new credential")
	}
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if err := lock(ctx, tx); err != nil {
			return err
		}
		var err error
		// agent false: the fleet's own path, which may write a claude_login or a fleet row.
		if out, err = s.insert(ctx, tx, u, false); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,
			principal_label, outcome, detail, started_at, finished_at)
			VALUES ('credential', 'fleet.add', 'http', 'person', $1, $1, 'ok', $2, now(), now())`,
			c.Principal, out.label()+" added for the fleet")
		return err
	})
	if err != nil {
		return nil, err
	}
	return one(out), nil
}
