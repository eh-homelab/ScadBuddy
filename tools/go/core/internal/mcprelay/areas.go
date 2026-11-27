// Package mcprelay is the agent's /mcp session relay: backend/scadbuddy/api/crud_agent_mcp.py (removed in #7667),
// statement for statement (area mcp-relay). Each route is one statement, or one transaction,
// of the agent's mcp/relayRows.ts PgRelayRows.
//
// A session id is a credential: the directory and every message carry its SHA-256 only, and
// every id is in the body, never the path. A header is checked against the relay's own shapes
// and refused 422 at the size Postgres refuses a NOTIFY payload, so nothing NOTIFYed here is
// one the agent would not have sent.
package mcprelay

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	// channel is the one the agent's relay LISTENs on (mcp/sessionRelay.ts MCP_CHANNEL).
	channel = "scadbuddy_mcp"
	// notifyPayloadMax: Postgres refuses a NOTIFY payload of 8000 bytes or more.
	notifyPayloadMax = 7999
	// staleMessages is the agent's STALE_MESSAGES: older relay rows are nobody's any more.
	staleMessages  = "10 minutes"
	callerKeyMax   = 1024
	idleSecondsMax = 7 * 24 * 3600
	headersMax     = 200
	headerNameMax  = 256
	headerValueMax = 8192
	inlineTextMax  = 8000
	// bodyBytesMax is the API's JSON gate (api/limits.py MAX_TEXT_BODY_BYTES).
	bodyBytesMax = 8 * 1024 * 1024
)

// Relay is the relay's routes.
type Relay struct{ Pool *pgxpool.Pool }

// Areas are the package's routes by area name.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	r := &Relay{Pool: pool}
	post := func(path string, h crud.Handler) crud.Route {
		return crud.Route{Method: "POST", Path: path, Handle: h, MaxBody: bodyBytesMax}
	}
	return map[string][]crud.Route{"mcp-relay": {
		post("/mcp-relay/sessions", r.register),
		post("/mcp-relay/sessions/touch", r.touch),
		post("/mcp-relay/sessions/remove", r.remove),
		post("/mcp-relay/sessions/owner", r.owner),
		post("/mcp-relay/counts", r.counts),
		post("/mcp-relay/sweep", r.sweep),
		post("/mcp-relay/send", r.send),
		post("/mcp-relay/rows/take", r.take),
		post("/mcp-relay/rows/withdraw", r.withdraw),
	}}
}

type done struct {
	Done bool `json:"done"`
}

func (r *Relay) inTx(req *http.Request, fn func(pgx.Tx) error) error {
	return pgx.BeginFunc(req.Context(), r.Pool, fn)
}

// -- the directory --

// Each route reads its body in a function of its own, which TestTheAgentsRelayBodiesAreTaken
// also runs over the bodies the agent's CrudRelayRows sends.

func registerBody(req *http.Request) (hash, replica, caller string, err error) {
	b, err := read(req, "hash", "replica", "caller_key")
	if err != nil {
		return "", "", "", err
	}
	hash, replica, caller = b.hash("hash"), b.uuid("replica"), b.text("caller_key", 1, callerKeyMax)
	return hash, replica, caller, b.err()
}

func (r *Relay) register(req *http.Request, _ crud.Params) (any, error) {
	hash, replica, caller, err := registerBody(req)
	if err != nil {
		return nil, err
	}
	err = r.inTx(req, func(tx pgx.Tx) error {
		_, err := tx.Exec(req.Context(),
			`INSERT INTO ai_mcp_sessions (id_hash, replica, caller_key) VALUES ($1, $2::text::uuid, $3)
			 ON CONFLICT (id_hash) DO UPDATE SET replica = EXCLUDED.replica,
			 caller_key = EXCLUDED.caller_key, last_seen = now()`, hash, replica, caller)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

// hashBody is the body of touch and owner: the session's hash.
func hashBody(req *http.Request) (string, error) {
	b, err := read(req, "hash")
	if err != nil {
		return "", err
	}
	hash := b.hash("hash")
	return hash, b.err()
}

func (r *Relay) touch(req *http.Request, _ crud.Params) (any, error) {
	hash, err := hashBody(req)
	if err != nil {
		return nil, err
	}
	err = r.inTx(req, func(tx pgx.Tx) error {
		_, err := tx.Exec(req.Context(),
			`UPDATE ai_mcp_sessions SET last_seen = now() WHERE id_hash = $1`, hash)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

func removeBody(req *http.Request) (hash string, replica *string, err error) {
	b, err := read(req, "hash", "replica")
	if err != nil {
		return "", nil, err
	}
	hash = b.hash("hash")
	replica = b.optionalUUID("replica")
	return hash, replica, b.err()
}

func (r *Relay) remove(req *http.Request, _ crud.Params) (any, error) {
	hash, replica, err := removeBody(req)
	if err != nil {
		return nil, err
	}
	var removed bool
	err = r.inTx(req, func(tx pgx.Tx) error {
		var tag pgconn.CommandTag
		var err error
		if replica == nil {
			tag, err = tx.Exec(req.Context(),
				`DELETE FROM ai_mcp_sessions WHERE id_hash = $1 RETURNING id_hash`, hash)
		} else {
			tag, err = tx.Exec(req.Context(),
				`DELETE FROM ai_mcp_sessions WHERE id_hash = $1 AND replica = $2::text::uuid
				 RETURNING id_hash`, hash, *replica)
		}
		removed = tag.RowsAffected() > 0
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{removed}, nil
}

func (r *Relay) owner(req *http.Request, _ crud.Params) (any, error) {
	hash, err := hashBody(req)
	if err != nil {
		return nil, err
	}
	var replica *string
	err = r.inTx(req, func(tx pgx.Tx) error {
		err := tx.QueryRow(req.Context(),
			`SELECT replica::text AS replica FROM ai_mcp_sessions WHERE id_hash = $1`, hash).Scan(&replica)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Replica *string `json:"replica"`
	}{replica}, nil
}

func countsBody(req *http.Request) (caller string, idle float64, err error) {
	b, err := read(req, "caller_key", "idle_seconds")
	if err != nil {
		return "", 0, err
	}
	caller, idle = b.text("caller_key", 1, callerKeyMax), b.idleSeconds("idle_seconds")
	return caller, idle, b.err()
}

func (r *Relay) counts(req *http.Request, _ crud.Params) (any, error) {
	caller, idle, err := countsBody(req)
	if err != nil {
		return nil, err
	}
	var out struct {
		Total  int `json:"total"`
		Caller int `json:"caller"`
	}
	err = r.inTx(req, func(tx pgx.Tx) error {
		return tx.QueryRow(req.Context(),
			`SELECT count(*)::int AS total,
			 (count(*) FILTER (WHERE caller_key = $1))::int AS caller
			 FROM ai_mcp_sessions WHERE last_seen > now() - make_interval(secs => $2)`,
			caller, idle).Scan(&out.Total, &out.Caller)
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func sweepBody(req *http.Request) (float64, error) {
	b, err := read(req, "idle_seconds")
	if err != nil {
		return 0, err
	}
	idle := b.idleSeconds("idle_seconds")
	return idle, b.err()
}

func (r *Relay) sweep(req *http.Request, _ crud.Params) (any, error) {
	idle, err := sweepBody(req)
	if err != nil {
		return nil, err
	}
	err = r.inTx(req, func(tx pgx.Tx) error {
		if _, err := tx.Exec(req.Context(),
			`DELETE FROM ai_mcp_relay_messages WHERE created_at < now() - $1::interval`,
			staleMessages); err != nil {
			return err
		}
		// A dead replica's sessions: idle by now, since nothing could use them.
		_, err := tx.Exec(req.Context(),
			`DELETE FROM ai_mcp_sessions WHERE last_seen < now() - make_interval(secs => $1)`, idle)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

// -- the messages --

// header is a relay header as the agent's relay reads it (HeaderSchema): its members in the
// order the Python model writes them, ids as lower-case text.
type header struct {
	fields []httpx.Field
	row    *string
}

// payload is the header as Python's json.dumps writes it: compact, no ASCII escapes.
func (h header) payload() string {
	out := "{"
	for i, f := range h.fields {
		if i > 0 {
			out += ","
		}
		out += seal.JSONString(f.Key) + ":" + jsonValue(f.Value)
	}
	return out + "}"
}

func jsonValue(v any) string {
	switch v := v.(type) {
	case string:
		return seal.JSONString(v)
	case bool:
		return fmt.Sprint(v)
	case int64:
		return fmt.Sprint(v)
	case [][2]string:
		out := "["
		for i, pair := range v {
			if i > 0 {
				out += ","
			}
			out += "[" + seal.JSONString(pair[0]) + "," + seal.JSONString(pair[1]) + "]"
		}
		return out + "]"
	}
	panic(fmt.Sprintf("a header member of type %T", v))
}

var tags = []string{"req", "ack", "beat", "head", "chunk", "end", "cancel"}

// readHeader is the discriminated union Header: its tag selects the shape.
func (b *body) readHeader(name string) header {
	raw, ok := b.required(name)
	if !ok {
		return header{}
	}
	h := &body{at: append(append([]string{}, b.at...), name)}
	if !h.object(raw) {
		b.issues = append(b.issues, h.issues...)
		return header{}
	}
	var tag *string
	if json.Unmarshal(h.fields["t"], &tag) != nil || tag == nil {
		b.fail([]string{name}, "Unable to extract tag using discriminator 't'")
		return header{}
	}
	known := false
	for _, t := range tags {
		known = known || t == *tag
	}
	if !known {
		b.fail([]string{name}, fmt.Sprintf("Input tag '%s' found using 't' does not match any of the"+
			" expected tags: 'req', 'ack', 'beat', 'head', 'chunk', 'end', 'cancel'", *tag))
		return header{}
	}
	h.at = append(h.at, *tag)
	names := map[string][]string{
		"req": {"row"}, "head": {"status", "headers"}, "chunk": {"text", "row"}, "end": {"error"},
	}[*tag]
	h.extra(append([]string{"req", "to", "t"}, names...))
	out := header{fields: []httpx.Field{
		{Key: "req", Value: h.uuid("req")}, {Key: "to", Value: h.uuid("to")}, {Key: "t", Value: *tag},
	}}
	switch *tag {
	case "req":
		row := h.uuid("row")
		out.row = &row
		out.fields = append(out.fields, httpx.Field{Key: "row", Value: row})
	case "head":
		out.fields = append(out.fields,
			httpx.Field{Key: "status", Value: h.intIn("status", 100, 599)},
			httpx.Field{Key: "headers", Value: h.pairs("headers")})
	case "chunk":
		if text := h.optionalText("text", inlineTextMax); text != nil {
			out.fields = append(out.fields, httpx.Field{Key: "text", Value: *text})
		}
		if row := h.optionalUUID("row"); row != nil {
			out.row = row
			out.fields = append(out.fields, httpx.Field{Key: "row", Value: *row})
		}
	case "end":
		if e := h.optionalBool("error"); e != nil {
			out.fields = append(out.fields, httpx.Field{Key: "error", Value: *e})
		}
	}
	b.issues = append(b.issues, h.issues...)
	return out
}

// sendBody is a send's header, its body, and the NOTIFY payload the header is sent as.
func sendBody(req *http.Request) (h header, message json.RawMessage, payload string, err error) {
	b, err := read(req, "header", "body")
	if err != nil {
		return header{}, nil, "", err
	}
	h = b.readHeader("header")
	message = b.optionalObject("body")
	if err := b.err(); err != nil {
		return header{}, nil, "", err
	}
	if (h.row == nil) != (message == nil) {
		return header{}, nil, "", &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: "a body is sent with the header that names its row, and only then"}
	}
	payload = h.payload()
	if len(payload) > notifyPayloadMax {
		return header{}, nil, "", &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: fmt.Sprintf("a relay header is at most %d bytes", notifyPayloadMax)}
	}
	return h, message, payload, nil
}

func (r *Relay) send(req *http.Request, _ crud.Params) (any, error) {
	h, message, payload, err := sendBody(req)
	if err != nil {
		return nil, err
	}
	err = r.inTx(req, func(tx pgx.Tx) error {
		if h.row != nil {
			if _, err := tx.Exec(req.Context(),
				`INSERT INTO ai_mcp_relay_messages (id, body) VALUES ($1::text::uuid, $2::text::jsonb)`,
				*h.row, string(message)); err != nil {
				return err
			}
		}
		_, err := tx.Exec(req.Context(), `SELECT pg_notify($1, $2)`, channel, payload)
		return err
	})
	var pg *pgconn.PgError
	if errors.As(err, &pg) && pg.Code == "23505" {
		return nil, &httpx.Problem{Status: http.StatusConflict,
			Detail: "a relay message with this row exists"}
	}
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

// rowBody is the body of take and withdraw: the relay row's id.
func rowBody(req *http.Request) (string, error) {
	b, err := read(req, "row")
	if err != nil {
		return "", err
	}
	row := b.uuid("row")
	return row, b.err()
}

func (r *Relay) take(req *http.Request, _ crud.Params) (any, error) {
	row, err := rowBody(req)
	if err != nil {
		return nil, err
	}
	var taken *string
	err = r.inTx(req, func(tx pgx.Tx) error {
		err := tx.QueryRow(req.Context(),
			`DELETE FROM ai_mcp_relay_messages WHERE id = $1::text::uuid RETURNING body::text`,
			row).Scan(&taken)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	out := struct {
		Body json.RawMessage `json:"body"`
	}{json.RawMessage("null")}
	if taken != nil {
		out.Body = json.RawMessage(*taken)
	}
	return out, nil
}

func (r *Relay) withdraw(req *http.Request, _ crud.Params) (any, error) {
	row, err := rowBody(req)
	if err != nil {
		return nil, err
	}
	var gone bool
	err = r.inTx(req, func(tx pgx.Tx) error {
		tag, err := tx.Exec(req.Context(),
			`DELETE FROM ai_mcp_relay_messages WHERE id = $1::text::uuid RETURNING id`, row)
		gone = tag.RowsAffected() > 0
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{gone}, nil
}
