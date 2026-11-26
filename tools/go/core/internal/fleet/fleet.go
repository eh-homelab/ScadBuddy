// Package fleet is the agent fleet chat's routes (area `fleet`, caller `fleet`, spec
// 2026-11-25-agent-fleet-messaging-design §4.2): one route per method of fleetchat's Store,
// each one transaction. A send publishes `fleet.message` after its commit.
package fleet

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

// Caller is the service these routes belong to.
const Caller = "fleet"

const (
	// textMax is fleetchat's default MaxText, in bytes.
	textMax = 16 << 20
	// messageBodyMax bounds a send's body: the text, escaped, and the rest.
	messageBodyMax = 2*textMax + 1<<20
	smallBodyMax   = 1 << 10
	pageDefault    = 50
	pageMax        = 200
	// pageBytes bounds the message text of one inbox page: a page stops before the
	// message that would take it past this, but always holds at least one.
	pageBytes = 4 << 20
	// sendLock orders the sends, so seqs commit in order and an inbox's cursor never passes
	// a seq that commits later.
	sendLock        = int64(0x464c_4545_5453_4e44) // "FLEETSND"
	announceTimeout = 10 * time.Second
	kind            = "fleet.message"
)

var (
	channelPattern = regexp.MustCompile(`^[a-z0-9-]{1,40}$`)
	handlePattern  = regexp.MustCompile(`^[0-9a-f]{8}$`)
)

// Fleet serves the routes over the API's database.
type Fleet struct {
	Pool *pgxpool.Pool
}

// Routes are area fleet's routes, under /api/v1/internal/crud/fleet.
func Routes(pool *pgxpool.Pool) []crud.Route {
	f := &Fleet{Pool: pool}
	return []crud.Route{
		{Method: "POST", Path: "/agents/{session_id}/touch", Handle: f.touch},
		{Method: "GET", Path: "/agents", Handle: f.agents},
		{Method: "PUT", Path: "/channels/{name}/members/{session_id}", Handle: f.join},
		{Method: "DELETE", Path: "/channels/{name}/members/{session_id}", Handle: f.leave},
		{Method: "GET", Path: "/agents/{session_id}/channels", Handle: f.channels},
		{Method: "POST", Path: "/messages", Handle: f.send, MaxBody: messageBodyMax},
		{Method: "GET", Path: "/agents/{session_id}/inbox", Handle: f.inbox},
		{Method: "PUT", Path: "/agents/{session_id}/cursor", Handle: f.rewind, MaxBody: smallBodyMax},
	}
}

func unknownAgent() error {
	return httpx.WithReason(http.StatusNotFound, "no fleet agent has that session id or handle", "unknown-agent")
}

func notMember(channel string) error {
	return httpx.WithReason(http.StatusForbidden,
		fmt.Sprintf(`the agent is not a member of channel "%s"`, channel), "not-member")
}

func channelOf(p crud.Params) (string, error) {
	if name := p["name"]; channelPattern.MatchString(name) {
		return name, nil
	}
	return "", httpx.Invalid(httpx.Issue{
		Loc: []string{"path", "name"},
		Msg: fmt.Sprintf("String should match pattern '%s'", channelPattern),
	})
}

// cursorOf is the agent's cursor, or unknownAgent. With lock, its row is locked FOR NO KEY
// UPDATE, which a send's foreign key check (FOR KEY SHARE) does not wait for.
func cursorOf(ctx context.Context, tx pgx.Tx, session string, lock bool) (int64, error) {
	query := `SELECT cursor FROM fleet_agents WHERE session_id = $1`
	if lock {
		query += ` FOR NO KEY UPDATE`
	}
	var cursor int64
	err := tx.QueryRow(ctx, query, session).Scan(&cursor)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, unknownAgent()
	}
	return cursor, err
}

// touch makes the agent on its first call and records that it was seen. Its handle is the
// first 8 hex characters of its session id, derived here.
func (f *Fleet) touch(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	handle := session[:8]
	_, err = f.Pool.Exec(r.Context(), `INSERT INTO fleet_agents (session_id, handle) VALUES ($1, $2)
		ON CONFLICT (session_id) DO UPDATE SET last_seen = now()`, session, handle)
	var pg *pgconn.PgError
	if errors.As(err, &pg) && pg.Code == "23505" {
		return nil, httpx.WithReason(http.StatusConflict,
			fmt.Sprintf(`another session has the handle "%s"`, handle), "handle-taken")
	}
	if err != nil {
		return nil, err
	}
	return map[string]string{"handle": handle}, nil
}

type agentInfo struct {
	Handle   string    `json:"handle"`
	LastSeen wire.Time `json:"last_seen"`
}

func (f *Fleet) agents(r *http.Request, _ crud.Params) (any, error) {
	rows, err := f.Pool.Query(r.Context(), `SELECT handle, last_seen FROM fleet_agents ORDER BY handle`)
	if err != nil {
		return nil, err
	}
	agents, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (agentInfo, error) {
		var a agentInfo
		return a, row.Scan(&a.Handle, &a.LastSeen)
	})
	if err != nil {
		return nil, err
	}
	if agents == nil {
		agents = []agentInfo{}
	}
	return map[string]any{"agents": agents}, nil
}

// membership is the channel and the agent of a join or leave, the agent's row checked.
func membership(r *http.Request, tx pgx.Tx, p crud.Params) (string, string, error) {
	channel, err := channelOf(p)
	if err != nil {
		return "", "", err
	}
	session, err := p.UUID("session_id")
	if err != nil {
		return "", "", err
	}
	_, err = cursorOf(r.Context(), tx, session, false)
	return channel, session, err
}

// join adds the agent to the channel, making the channel.
func (f *Fleet) join(r *http.Request, p crud.Params) (any, error) {
	var channel string
	err := pgx.BeginFunc(r.Context(), f.Pool, func(tx pgx.Tx) error {
		var session string
		var err error
		if channel, session, err = membership(r, tx, p); err != nil {
			return err
		}
		if _, err := tx.Exec(r.Context(),
			`INSERT INTO fleet_channels (name) VALUES ($1) ON CONFLICT DO NOTHING`, channel); err != nil {
			return err
		}
		_, err = tx.Exec(r.Context(), `INSERT INTO fleet_members (channel, session_id) VALUES ($1, $2)
			ON CONFLICT DO NOTHING`, channel, session)
		return err
	})
	if err != nil {
		return nil, err
	}
	return map[string]string{"joined": channel}, nil
}

// leave takes the agent out of the channel; the channel stays.
func (f *Fleet) leave(r *http.Request, p crud.Params) (any, error) {
	var channel string
	err := pgx.BeginFunc(r.Context(), f.Pool, func(tx pgx.Tx) error {
		var session string
		var err error
		if channel, session, err = membership(r, tx, p); err != nil {
			return err
		}
		tag, err := tx.Exec(r.Context(),
			`DELETE FROM fleet_members WHERE channel = $1 AND session_id = $2`, channel, session)
		if err == nil && tag.RowsAffected() == 0 {
			err = notMember(channel)
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return map[string]string{"left": channel}, nil
}

type channelInfo struct {
	Name    string   `json:"name"`
	Members []string `json:"members"`
	Unread  int      `json:"unread"`
}

// channels is every channel, its members' handles, and the agent's unread count in it.
func (f *Fleet) channels(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	var out []channelInfo
	err = pgx.BeginTxFunc(r.Context(), f.Pool, pgx.TxOptions{AccessMode: pgx.ReadOnly}, func(tx pgx.Tx) error {
		cursor, err := cursorOf(r.Context(), tx, session, false)
		if err != nil {
			return err
		}
		rows, err := tx.Query(r.Context(), `SELECT c.name,
				coalesce(array_agg(a.handle ORDER BY a.handle) FILTER (WHERE a.handle IS NOT NULL), '{}'),
				(SELECT count(*) FROM fleet_deliveries d JOIN fleet_messages m USING (seq)
				 WHERE d.session_id = $1 AND d.seq > $2 AND m.channel = c.name)
			FROM fleet_channels c
			LEFT JOIN fleet_members mb ON mb.channel = c.name
			LEFT JOIN fleet_agents a ON a.session_id = mb.session_id
			GROUP BY c.name ORDER BY c.name`, session, cursor)
		if err != nil {
			return err
		}
		out, err = pgx.CollectRows(rows, func(row pgx.CollectableRow) (channelInfo, error) {
			var c channelInfo
			return c, row.Scan(&c.Name, &c.Members, &c.Unread)
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	if out == nil {
		out = []channelInfo{}
	}
	return map[string]any{"channels": out}, nil
}

// recipient is a session id and its handle.
type recipient struct{ Session, Handle string }

// send stores a message and its deliveries: a DM's to its target and its sender, a channel
// post's to the channel's members at send time, sender included. The sender must be a member.
func (f *Fleet) send(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "from", "channel", "agent", "text")
	if err != nil {
		return nil, err
	}
	from := b.UUID("from")
	channel := b.OptText("channel", 1, 40, channelPattern)
	agent := b.OptText("agent", 8, 8, handlePattern)
	// The length is checked below, in bytes, with a detail that names the limit.
	text := b.Text("text", 1, math.MaxInt)
	if err := b.Err(); err != nil {
		return nil, err
	}
	if (channel == nil) == (agent == nil) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "give exactly one of channel or agent"})
	}
	if len(text) > textMax {
		p := httpx.WithReason(http.StatusUnprocessableEntity,
			fmt.Sprintf("text is longer than %d bytes", textMax), "text-too-long")
		p.Extensions = append(p.Extensions, httpx.Field{Key: "errors", Value: []httpx.Issue{{
			Loc: []string{"body", "text"}, Msg: fmt.Sprintf("text is at most %d bytes", textMax),
		}}})
		return nil, p
	}
	ctx := r.Context()
	var seq int64
	var recipients []recipient
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, sendLock); err != nil {
			return err
		}
		if _, err := cursorOf(ctx, tx, from, false); err != nil {
			return err
		}
		if agent != nil {
			var to string
			err := tx.QueryRow(ctx, `SELECT session_id FROM fleet_agents WHERE handle = $1`, *agent).Scan(&to)
			if errors.Is(err, pgx.ErrNoRows) {
				return unknownAgent()
			}
			if err != nil {
				return err
			}
			if err := tx.QueryRow(ctx, `INSERT INTO fleet_messages (from_session, to_session, text)
				VALUES ($1, $2, $3) RETURNING seq`, from, to, text).Scan(&seq); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO fleet_deliveries (session_id, seq)
				SELECT DISTINCT s, $2::bigint FROM unnest($1::uuid[]) s`, []string{to, from}, seq); err != nil {
				return err
			}
		} else {
			// The sender's member row is locked FOR SHARE, so a leave cannot slip between this
			// check and the deliveries below.
			var one int
			err := tx.QueryRow(ctx, `SELECT 1 FROM fleet_members
				WHERE channel = $1 AND session_id = $2 FOR SHARE`, *channel, from).Scan(&one)
			if errors.Is(err, pgx.ErrNoRows) {
				return notMember(*channel)
			}
			if err != nil {
				return err
			}
			if err := tx.QueryRow(ctx, `INSERT INTO fleet_messages (from_session, channel, text)
				VALUES ($1, $2, $3) RETURNING seq`, from, *channel, text).Scan(&seq); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO fleet_deliveries (session_id, seq)
				SELECT session_id, $2 FROM fleet_members WHERE channel = $1`, *channel, seq); err != nil {
				return err
			}
		}
		rows, err := tx.Query(ctx, `SELECT a.session_id::text, a.handle FROM fleet_deliveries d
			JOIN fleet_agents a USING (session_id) WHERE d.seq = $1 ORDER BY a.handle`, seq)
		if err != nil {
			return err
		}
		recipients, err = pgx.CollectRows(rows, func(row pgx.CollectableRow) (recipient, error) {
			var rc recipient
			return rc, row.Scan(&rc.Session, &rc.Handle)
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	target := ""
	if channel != nil {
		target = *channel
	}
	f.announce(r, seq, recipients, target)
	ids := make([]string, len(recipients))
	for i, rc := range recipients {
		ids[i] = rc.Session
	}
	return map[string]any{"seq": seq, "recipients": ids}, nil
}

// announce publishes the committed send as fleet.message events. As Python's outbox, it does
// not stop when the request ends, and a failure is the bus's to log: the rows are the record.
func (f *Fleet) announce(r *http.Request, seq int64, recipients []recipient, channel string) {
	for _, fields := range events(seq, recipients, channel) {
		// Each event has its own budget: a slow commit must not starve the later ones.
		ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), announceTimeout)
		bus.Publish(ctx, f.Pool, kind, fields...)
		cancel()
	}
}

// events are the fields of the fleet.message events that tell of message seq: every
// recipient named in one of them, each event within the bus's payload and topic caps.
// channel is a channel post's channel, "" for a DM.
func events(seq int64, recipients []recipient, channel string) [][]httpx.Field {
	fields := func(chunk []recipient) []httpx.Field {
		ids := make([]string, len(chunk))
		var topics []string
		if channel != "" {
			topics = append(topics, "fleet:channel:"+channel)
		}
		for i, rc := range chunk {
			ids[i] = rc.Session
			topics = append(topics, "fleet:agent:"+rc.Handle)
		}
		return []httpx.Field{{Key: "seq", Value: seq}, {Key: "recipients", Value: ids}, {Key: "topics", Value: topics}}
	}
	fits := func(chunk []recipient) bool {
		topics := len(chunk)
		if channel != "" {
			topics++
		}
		return topics <= bus.TopicsMax && bus.Fits(kind, fields(chunk)...)
	}
	var out [][]httpx.Field
	start := 0
	for end := 1; end <= len(recipients); end++ {
		// A chunk of one always goes out: the bus refuses it, and logs, if it does not fit.
		if end-start > 1 && !fits(recipients[start:end]) {
			out = append(out, fields(recipients[start:end-1]))
			start = end - 1
		}
	}
	if start < len(recipients) {
		out = append(out, fields(recipients[start:]))
	}
	return out
}

type message struct {
	Seq     int64     `json:"seq"`
	From    string    `json:"from"`
	Channel string    `json:"channel,omitempty"`
	To      string    `json:"to,omitempty"`
	Text    string    `json:"text"`
	SentAt  wire.Time `json:"sent_at"`
}

// inbox is up to limit deliveries above the agent's cursor, oldest first, and whether more
// wait. A page also stops before the message that would take its text past pageBytes, but
// always holds at least one. Unless peek, the cursor moves to the last one returned, in the
// same transaction.
func (f *Fleet) inbox(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	limit, err := pageLimit(r)
	if err != nil {
		return nil, err
	}
	peek, err := queryBool(r, "peek")
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out []message
	more := false
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		cursor, err := cursorOf(ctx, tx, session, !peek)
		if err != nil {
			return err
		}
		// page is the next limit+1 deliveries with the running sum of their text's bytes
		// (octet_length reads a stored text's size without reading the text). The window runs
		// over those rows only, so an agent far behind pays for one page, not its backlog. Only
		// the rows that fit read their text.
		rows, err := tx.Query(ctx, `WITH next AS (
				SELECT seq FROM fleet_deliveries
				WHERE session_id = $1 AND seq > $2
				ORDER BY seq LIMIT $3),
			page AS (
				SELECT x.seq, row_number() OVER w AS n, sum(octet_length(m.text)) OVER w AS bytes
				FROM next x JOIN fleet_messages m USING (seq)
				WINDOW w AS (ORDER BY x.seq))
			SELECT p.n = 1 OR p.bytes <= $4, m.seq, f.handle, coalesce(m.channel, ''),
				coalesce(t.handle, ''), CASE WHEN p.n = 1 OR p.bytes <= $4 THEN m.text ELSE '' END,
				m.sent_at
			FROM page p
			JOIN fleet_messages m USING (seq)
			JOIN fleet_agents f ON f.session_id = m.from_session
			LEFT JOIN fleet_agents t ON t.session_id = m.to_session
			ORDER BY p.seq`, session, cursor, limit+1, pageBytes)
		if err != nil {
			return err
		}
		var fits []bool
		out, err = pgx.CollectRows(rows, func(row pgx.CollectableRow) (message, error) {
			var m message
			var fit bool
			err := row.Scan(&fit, &m.Seq, &m.From, &m.Channel, &m.To, &m.Text, &m.SentAt)
			fits = append(fits, fit)
			return m, err
		})
		if err != nil {
			return err
		}
		n := 0
		for n < len(out) && int64(n) < limit && fits[n] {
			n++
		}
		out, more = out[:n], n < len(out)
		if peek || len(out) == 0 {
			return nil
		}
		_, err = tx.Exec(ctx, `UPDATE fleet_agents SET cursor = $2 WHERE session_id = $1`,
			session, out[len(out)-1].Seq)
		return err
	})
	if err != nil {
		return nil, err
	}
	if out == nil {
		out = []message{}
	}
	return map[string]any{"messages": out, "more": more}, nil
}

// rewind sets the agent's cursor back to seq. A seq past the cursor is a 422 (reason
// seq-ahead): a rewind only moves the cursor back, and the inbox moves it forward.
func (f *Fleet) rewind(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	b, err := crud.ReadBody(r, "seq")
	if err != nil {
		return nil, err
	}
	seq := b.Int("seq", 0)
	if err := b.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		cursor, err := cursorOf(ctx, tx, session, true)
		if err != nil {
			return err
		}
		if seq > cursor {
			return httpx.WithReason(http.StatusUnprocessableEntity,
				fmt.Sprintf("seq %d is past the agent's cursor (%d): a rewind only moves it back", seq, cursor),
				"seq-ahead")
		}
		_, err = tx.Exec(ctx, `UPDATE fleet_agents SET cursor = $2 WHERE session_id = $1`, session, seq)
		return err
	})
	if err != nil {
		return nil, err
	}
	return map[string]int64{"seq": seq}, nil
}

// pageLimit is the query's limit: absent or 0 is pageDefault, anything above pageMax is
// pageMax (however large), and a negative or non-numeric one is a 422.
func pageLimit(r *http.Request) (int64, error) {
	values := r.URL.Query()["limit"]
	if len(values) == 0 {
		return pageDefault, nil
	}
	raw := strings.TrimSpace(values[len(values)-1])
	n, err := strconv.ParseInt(raw, 10, 64)
	var numErr *strconv.NumError
	switch {
	case err == nil && n == 0:
		return pageDefault, nil
	case err == nil && n > 0:
		return min(n, pageMax), nil
	case errors.As(err, &numErr) && numErr.Err == strconv.ErrRange && !strings.HasPrefix(raw, "-"):
		return pageMax, nil
	case err == nil:
		return 0, httpx.Invalid(httpx.Issue{
			Loc: []string{"query", "limit"}, Msg: "Input should be greater than or equal to 0",
		})
	}
	return 0, httpx.Invalid(httpx.Issue{
		Loc: []string{"query", "limit"}, Msg: "Input should be a valid integer, unable to parse string as an integer",
	})
}

// queryBool is the query parameter name as pydantic reads a bool; false when absent.
func queryBool(r *http.Request, name string) (bool, error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return false, nil
	}
	switch strings.ToLower(strings.TrimSpace(values[len(values)-1])) {
	case "1", "true", "t", "yes", "y", "on":
		return true, nil
	case "0", "false", "f", "no", "n", "off":
		return false, nil
	}
	return false, httpx.Invalid(httpx.Issue{
		Loc: []string{"query", name}, Msg: "Input should be a valid boolean, unable to interpret input",
	})
}
