package secrets

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// A durable subject's payload data key released to the agent (agent_data/payload_keys.py),
// the ai_audit row of each release (agent_data/audit.py (removed in #7667)), and the approvals' input-hash key
// (api/crud_agent_keys.py). Never the KEK.

const (
	releaseAuditWindow = "10 minutes"
	detailMax          = 500
)

var subjectPattern = regexp.MustCompile(
	`^(session|flow|bridge)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// audited are the session and turn an audit row names.
type audited struct{ session, turn *string }

// audit writes one ai_audit row in the caller's transaction, as the agent service.
func audit(ctx context.Context, tx pgx.Tx, kind, action, outcome, detail string, ids audited) error {
	if runes := []rune(detail); len(runes) > detailMax {
		detail = string(runes[:detailMax])
	}
	_, err := tx.Exec(ctx, `INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,
		principal_label, session_id, turn_id, outcome, detail, started_at, finished_at)
		VALUES ($1, $2, 'system', 'service', 'agent', 'agent service', $3, $4, $5, $6, now(), now())`,
		kind, action, ids.session, ids.turn, outcome, detail)
	return err
}

// recordedLately says whether an ok row of kind with one of actions (and detail, when given)
// was committed inside window, so the caller writes none. Without one it tries an advisory
// lock of its own and reads again; when another caller holds that lock it never waits and
// answers false, so it writes a row too.
func recordedLately(ctx context.Context, tx pgx.Tx, kind string, actions []string, window string,
	detail *string) (bool, error) {
	in, err := inWindow(ctx, tx, kind, actions, window, detail)
	if err != nil || in {
		return in, err
	}
	d := ""
	if detail != nil {
		d = *detail
	}
	var held bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock(hashtext($1)) AS got`,
		fmt.Sprintf("scadbuddy:agent_audit:%s:%s:%s", kind, strings.Join(actions, ","), d),
	).Scan(&held); err != nil || !held {
		return false, err
	}
	return inWindow(ctx, tx, kind, actions, window, detail)
}

func inWindow(ctx context.Context, tx pgx.Tx, kind string, actions []string, window string,
	detail *string) (bool, error) {
	var one int
	err := tx.QueryRow(ctx, `SELECT 1 FROM ai_audit WHERE kind = $1 AND action = ANY($2) AND outcome = 'ok'
		AND ($3::text IS NULL OR detail = $3) AND at > now() - $4::interval LIMIT 1`,
		kind, actions, detail, window).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func (s *Store) payloadKey(r *http.Request, p crud.Params) (any, error) {
	subject := p["subject"]
	if !subjectPattern.MatchString(subject) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"path", "subject"},
			Msg: fmt.Sprintf("String should match pattern '%s'", subjectPattern)})
	}
	f, err := readForm(r, "create", "only_for_standing_session")
	if err != nil {
		return nil, err
	}
	create, standing := f.boolean("create", false), f.boolean("only_for_standing_session", false)
	if err := f.err(); err != nil {
		return nil, err
	}
	if standing && !strings.HasPrefix(subject, "session-") {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: "only a session's key is made for a standing session"}
	}
	var ids audited
	if id, ok := strings.CutPrefix(subject, "session-"); ok {
		ids.session = &id
	}
	var key []byte
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		var made bool
		key, made, err = s.keyIn(ctx, tx, subject, create, standing)
		if err != nil {
			return err
		}
		if key == nil {
			return audit(ctx, tx, "payload_key", "payload_key.release", "refused", subject+": no key", ids)
		}
		if !made {
			lately, err := recordedLately(ctx, tx, "payload_key",
				[]string{"payload_key.release", "payload_key.create"}, releaseAuditWindow, &subject)
			if err != nil || lately {
				return err
			}
		}
		action := "payload_key.release"
		if made {
			action = "payload_key.create"
		}
		return audit(ctx, tx, "payload_key", action, "ok", subject, ids)
	})
	if err != nil {
		return nil, err
	}
	if key == nil {
		return nil, httpx.WithReason(http.StatusGone,
			subject+" has no payload key: it was forgotten, or never made.", subjectForgotten)
	}
	return struct {
		DataKey string `json:"data_key"`
	}{base64.StdEncoding.EncodeToString(key)}, nil
}

// keyIn is the subject's data key (made first with create) and whether this call made it, in
// the caller's transaction; nil when it has none.
func (s *Store) keyIn(ctx context.Context, tx pgx.Tx, subject string, create, standing bool) ([]byte, bool, error) {
	sealed, kekID, err := keyRow(ctx, tx, subject)
	if err != nil {
		return nil, false, err
	}
	made := false
	if sealed == nil && create {
		if made, err = s.createKey(ctx, tx, subject, standing); err != nil {
			return nil, false, err
		}
		// Whoever inserted first, this is the key every encoder of the subject uses.
		if sealed, kekID, err = keyRow(ctx, tx, subject); err != nil {
			return nil, false, err
		}
	}
	if sealed == nil {
		return nil, false, nil
	}
	opener := s.Keys.Opener(kekID)
	if opener == nil {
		return nil, false, httpx.WithReason(http.StatusConflict, fmt.Sprintf(
			"the payload key of %s was sealed with key %s, which the API does not hold", subject, kekID),
			keyNotHeld)
	}
	key, err := seal.OpenBytes(opener.Key(), sealed, seal.PayloadKeyContext(subject))
	if err != nil {
		return nil, false, httpx.WithReason(http.StatusConflict,
			fmt.Sprintf("the payload key of %s does not open", subject), keyNotHeld)
	}
	return key, made, nil
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

// createKey makes the key unless the subject has one, was forgotten, or (standing) its session
// is gone; whether this call made it. Under the subject's lock, which forgetSubject also takes.
func (s *Store) createKey(ctx context.Context, tx pgx.Tx, subject string, standing bool) (bool, error) {
	dek := make([]byte, seal.KEKBytes)
	_, _ = rand.Read(dek)
	kek := s.Keys.Current
	sealed := seal.SealBytes(kek.Key(), dek, seal.PayloadKeyContext(subject))
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, seal.PayloadKeyLock(subject)); err != nil {
		return false, err
	}
	tag, err := tx.Exec(ctx, `INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)
		SELECT $1, $2, $3
		WHERE NOT EXISTS (SELECT 1 FROM ai_forgotten_subjects WHERE subject = $1)
		AND (NOT $4 OR EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = substr($1, 9)))
		ON CONFLICT (subject) DO NOTHING`, subject, sealed, kek.ID, standing)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

func (s *Store) inputHashKey(_ *http.Request, _ crud.Params) (any, error) {
	return struct {
		Key string `json:"key"`
	}{base64.StdEncoding.EncodeToString(seal.InputHashKey(s.Keys.Current))}, nil
}

func newUUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	h := hex.EncodeToString(b)
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}
