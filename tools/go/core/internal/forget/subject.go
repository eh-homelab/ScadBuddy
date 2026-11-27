package forget

import (
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/transcripts"
)

const (
	forksTakeRanges = "forks-take-ranges"
	tombstone       = `INSERT INTO ai_forgotten_subjects (subject) VALUES ($1) ON CONFLICT (subject) DO NOTHING`
)

// subjectPattern is payload_keys.py SUBJECT_PATTERN.
var subjectPattern = regexp.MustCompile(
	`^(session|flow|bridge)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// forget is forgetSubject's key transaction (agent_data/forget.py forget): under the subject's
// lock, a session's forks first take their ranges (transcripts.CopyForkRanges), then the
// tombstone, a flow run's images and the key. Whether it had a key. With no KEK and a fork
// that takes transcript entries it is 409 forks-take-ranges: the agent copies its forks' ranges
// and forgets it in its own transaction.
func (f *Forget) forget(r *http.Request, p crud.Params) (any, error) {
	subject, err := p.Match("subject", subjectPattern)
	if err != nil {
		return nil, err
	}
	if strings.HasPrefix(subject, "bridge-") {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: "a browser call's key is dropped, not forgotten"}
	}
	ctx := r.Context()
	key := false
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, lock, payloadKeyLockPre+subject); err != nil {
			return err
		}
		if session, ok := strings.CutPrefix(subject, "session-"); ok {
			if _, err := transcripts.CopyForkRanges(ctx, tx, f.Keys, session); err != nil {
				return err
			}
		}
		if _, err := tx.Exec(ctx, tombstone, subject); err != nil {
			return err
		}
		if strings.HasPrefix(subject, "flow-") {
			if _, err := tx.Exec(ctx, `DELETE FROM ai_flow_blobs WHERE subject = $1`, subject); err != nil {
				return err
			}
		}
		tag, err := tx.Exec(ctx, deleteKey, subject)
		key = tag.RowsAffected() > 0
		return err
	})
	if errors.Is(err, transcripts.ErrKeyUnavailable) {
		return nil, httpx.WithReason(http.StatusConflict, fmt.Sprintf("%s has forks, which take "+
			"their ranges of its transcript first, and the API has no SCADBUDDY_SECRET_KEY_FILE.",
			subject), forksTakeRanges)
	}
	if err != nil {
		return nil, err
	}
	return struct {
		Key bool `json:"key"`
	}{key}, nil
}
