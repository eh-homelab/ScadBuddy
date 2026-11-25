// Package transcripts is a session's transcript, the Agent SDK's SessionStore rows in
// ai_session_entries: backend/scadbuddy/api/crud_agent_transcripts.py and
// agent_data/transcripts.py, statement for statement (area transcripts).
//
// The API seals and opens each entry under the session's data key (ai_payload_keys, subject
// session-<id>) with the AAD ai_session_entries:<session_id>:<subpath>:<uuid or #id>. A row
// written before sealing holds plaintext entry and is read as it is. An append takes the
// subject's payload-key lock shared before it reads the tombstone, so a forget either went
// first (and refuses it) or waits for it and deletes its rows.
package transcripts

import (
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// Areas are the package's routes by area name: none without a KEK.
func Areas(pool *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route {
	if keys == nil {
		return nil
	}
	s := &Store{Pool: pool, Keys: keys}
	return map[string][]crud.Route{
		"transcripts":      append(s.Reads(), s.Writes()...),
		"transcripts:read": s.Reads(),
	}
}
