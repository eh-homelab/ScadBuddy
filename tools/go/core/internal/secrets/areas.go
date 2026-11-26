// Package secrets is the agent's settings, Claude credentials, payload data keys and
// input-hash key: api/crud_agent_secrets.py (removed in #7667) and api/crud_agent_keys.py, statement for
// statement (areas settings, credentials, payload-keys and keys). The API seals and opens
// here with the KEK; a secret travels only in these routes' bodies, and every CRUD answer is
// no-store.
package secrets

import (
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The reasons a client branches on.
const (
	credentialRefused = "credential-refused"
	credentialSealed  = "credential-sealed"
	subjectForgotten  = "subject-forgotten"
	keyNotHeld        = "key-not-held"
)

// bodyMax is the API's JSON body gate (api/limits.py MAX_TEXT_BODY_BYTES).
const bodyMax = 8 * 1024 * 1024

// Store is the areas' routes. Keys is nil without a KEK: then only settings is mounted.
type Store struct {
	Pool *pgxpool.Pool
	Keys *seal.Keys
}

// Areas are the package's routes by area name.
func Areas(pool *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route {
	s := &Store{Pool: pool, Keys: keys}
	settingsRead := []crud.Route{{Method: "GET", Path: "/settings", Handle: s.readSettings}}
	credentialsRead := []crud.Route{
		{Method: "GET", Path: "/credentials/key", Handle: s.key},
		{Method: "GET", Path: "/credentials", Handle: s.list},
		{Method: "GET", Path: "/credentials/one", Handle: s.get},
	}
	post := func(path string, h crud.Handler) crud.Route {
		return crud.Route{Method: "POST", Path: path, Handle: h, MaxBody: bodyMax}
	}
	created := post("/credentials", s.create)
	created.Status = 201
	return map[string][]crud.Route{
		"settings:read":    settingsRead,
		"settings":         append(settingsRead, post("/settings", s.writeSettings)),
		"credentials:read": credentialsRead,
		"credentials": append(credentialsRead,
			created,
			post("/credentials/save", s.save),
			post("/credentials/delete", s.delete),
			post("/credentials/order", s.order),
			post("/credentials/reset", s.reset),
			post("/credentials/name", s.rename),
			post("/credentials/events", s.events),
			post("/credentials/reveal", s.reveal),
		),
		"payload-keys": {post("/payload-keys/{subject}", s.payloadKey)},
		// POST /fetch-grants stays in Python: it reads a URL as library/url_import.py does.
		"keys": {{Method: "GET", Path: "/keys/input-hash", Handle: s.inputHashKey}},
	}
}
