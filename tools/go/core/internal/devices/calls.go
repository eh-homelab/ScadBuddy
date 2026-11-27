package devices

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The event kinds a device posts while a call runs. Core publishes device.call_requested,
// device.call_done, device.call_failed and device.revoked itself.
var deviceKinds = []string{"tag_found", "tag_progress", "tag_lost"}

// reserved are the event fields core sets; a device's data may not name them.
var reserved = []string{"id", "at", "kind", "topics", "retention_seconds", "call", "device"}

const (
	eventsMax = 20
	// eventDataMax bounds one event's data.
	eventDataMax = 1024
	// replayMax is the most events a call's GET answers.
	replayMax = 1000
	// toolName is the approval's tool for an assistant's or a flow's nfc.write.
	toolName = "device_call"
)

// Call is a call as the routes answer it.
type Call struct {
	ID              string          `json:"id"`
	DeviceID        string          `json:"device_id"`
	Method          string          `json:"method"`
	Args            json.RawMessage `json:"args"`
	CallerKind      string          `json:"caller_kind"`
	Outcome         *string         `json:"outcome"`
	CreatedAt       crud.Time       `json:"created_at"`
	Deadline        crud.Time       `json:"deadline"`
	SentAt          *crud.Time      `json:"sent_at"`
	EndedAt         *crud.Time      `json:"ended_at"`
	PendingActionID *string         `json:"pending_action_id"`

	caller, owner string
	timeoutS      int
	result        json.RawMessage
}

// Event is one event of a call, replayed from the event log.
type Event struct {
	ID   string                     `json:"id"`
	Kind string                     `json:"kind"`
	At   string                     `json:"at"`
	Data map[string]json.RawMessage `json:"data"`
}

// View is a call, its events and its result: GET /api/v1/devices/calls/{id}.
type View struct {
	Call   Call            `json:"call"`
	Events []Event         `json:"events"`
	Result json.RawMessage `json:"result"`
}

const callColumns = `c.id::text, c.device_id::text, c.method, c.args, c.caller_kind, c.outcome, c.created_at,
	c.deadline, c.sent_at, c.ended_at, c.approval_id::text, c.caller, d.owner, c.timeout_s, c.result`

func (s *Store) loadCall(ctx context.Context, q interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, id, lock string) (*Call, error) {
	var c Call
	var created, deadline time.Time
	var sent, ended *time.Time
	err := q.QueryRow(ctx, `SELECT `+callColumns+` FROM device_calls c JOIN devices d ON d.id = c.device_id
		WHERE c.id = $1::uuid`+lock, id).Scan(&c.ID, &c.DeviceID, &c.Method, &c.Args, &c.CallerKind, &c.Outcome,
		&created, &deadline, &sent, &ended, &c.PendingActionID, &c.caller, &c.owner, &c.timeoutS, &c.result)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("call")
	}
	if err != nil {
		return nil, err
	}
	c.CreatedAt, c.Deadline, c.SentAt, c.EndedAt = crud.Time(created), crud.Time(deadline), crud.TimeOf(sent), crud.TimeOf(ended)
	return &c, nil
}

// callerOf is who a person route acts for, and as what: a person (a session cookie, or no
// one with auth off), the assistant (a person's access token, or the agent's service
// credential), or a flow (the projects worker's credential). A service names the person it
// acts for in on_behalf_of, and only a service that holds the delegation grant may: its
// credential's scope DelegateScope, or its caller named in SCADBUDDY_DEVICE_DELEGATES
// (agent and projects by default). Any other service is a 403.
func (s *Store) callerOf(r *http.Request, b *crud.Body) (kind, who string, err error) {
	c := auth.CallerOf(r.Context())
	switch c.Kind {
	case auth.KindService:
		if !c.Service.Has(DelegateScope) && !slices.Contains(s.Delegates, c.Service.Caller) {
			return "", "", httpx.WithReason(http.StatusForbidden,
				"This service may not call a device for a person.", "no_delegation")
		}
		kind = "assistant"
		if c.Service.Caller == "projects" {
			kind = "flow"
		}
		if b == nil {
			return "", "", httpx.Invalid(httpx.Issue{Loc: []string{"body", "on_behalf_of"}, Msg: "Field required"})
		}
		return kind, b.Text("on_behalf_of", 1, 200), nil
	case auth.KindToken:
		return "assistant", c.Principal, nil
	}
	if b != nil && b.Present("on_behalf_of") {
		b.Fail([]string{"on_behalf_of"}, "only a service acts on another's behalf")
	}
	return "person", principal(r.Context()), nil
}

func offline() error {
	return &httpx.Problem{Status: http.StatusConflict, Detail: "The device is not online. Open the app on it.",
		Extensions: []httpx.Field{{Key: "reason", Value: "offline"}, {Key: "error", Value: "offline"}}}
}

// call starts a call of a device's method. It answers 202 {call_id} at once, or, for an
// assistant's or a flow's nfc.write, 202 {call_id, pending_action_id}: the call goes to the
// device only after a person approves it and the caller confirms it. With ?wait=true it
// answers when the call ends (200), or at its deadline (504).
func (s *Store) call(r *http.Request) (any, error) {
	ctx := r.Context()
	id := r.PathValue("id")
	if !crud.IsUUID(id) {
		return nil, notFound("device")
	}
	wait, err := crud.QueryBool(r, "wait")
	if err != nil {
		return nil, err
	}
	b, err := crud.ReadBody(r, "method", "args", "timeout_s", "on_behalf_of")
	if err != nil {
		return nil, err
	}
	kind, who, err := s.callerOf(r, b)
	if err != nil {
		return nil, err
	}
	method := b.Choice("method", Methods...)
	args := json.RawMessage(`{}`)
	if b.Present("args") {
		args = b.RawObject("args")
	}
	timeout := b.IntOr("timeout_s", DefaultTimeout, 1, MaxTimeout)
	if err := b.Err(); err != nil {
		return nil, err
	}
	if args, err = checkArgs(method, args); err != nil {
		return nil, err
	}
	var (
		owner, name string
		caps        []string
		seen        *time.Time
	)
	err = s.Pool.QueryRow(ctx, `SELECT owner, name, capabilities, last_seen_at FROM devices
		WHERE id = $1::uuid AND revoked_at IS NULL`, id).Scan(&owner, &name, &caps, &seen)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("device")
	}
	if err != nil {
		return nil, err
	}
	if owner != who {
		return nil, httpx.WithReason(http.StatusForbidden, "That device is another person's.", "not_your_device")
	}
	if !slices.Contains(caps, method) {
		return nil, httpx.WithReason(http.StatusUnprocessableEntity,
			"The device did not announce "+method+".", "method_not_announced")
	}
	if !s.online(seen) {
		return nil, offline()
	}
	now := s.now()
	deadline := now.Add(time.Duration(timeout) * time.Second)
	if !bus.Fits("device.call_requested", requested("00000000-0000-0000-0000-000000000000", id, method, args, deadline)...) {
		return nil, httpx.WithReason(http.StatusRequestEntityTooLarge, "The call's args are too large.", "args_too_large")
	}
	approve := method == "nfc.write" && kind != "person"
	if approve && s.Keys == nil {
		return nil, httpx.WithReason(http.StatusServiceUnavailable,
			"An approval needs "+seal.Variable+", which this core does not have.", "not_configured")
	}
	if approve {
		// The approval waits for a person; the call's deadline starts again at the confirm.
		deadline = now.Add(ApprovalTTL)
	}
	var callID string
	var pendingID *string
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if approve {
			p, err := s.approval(ctx, tx, kind, who, name, id, method, args, now)
			if err != nil {
				return err
			}
			pendingID = &p
		}
		return tx.QueryRow(ctx, `INSERT INTO device_calls (device_id, caller, caller_kind, method, args, timeout_s,
			approval_id, created_at, deadline) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::uuid, $8, $9) RETURNING id::text`,
			id, who, kind, method, args, timeout, pendingID, now, deadline).Scan(&callID)
	})
	if err != nil {
		return nil, err
	}
	s.schedule(callID, deadline)
	if approve {
		return &crud.Reply{Status: http.StatusAccepted, Value: map[string]any{"call_id": callID, "pending_action_id": *pendingID}}, nil
	}
	if err := s.send(ctx, callID, id, method, args, deadline); err != nil {
		return nil, err
	}
	if wait {
		return s.wait(ctx, callID)
	}
	return &crud.Reply{Status: http.StatusAccepted, Value: map[string]any{"call_id": callID}}, nil
}

// checkArgs refuses args a method does not take. nfc.write takes NDEF records only, never
// raw blocks, sectors or keys: core never asks a device to write a MIFARE sector, so it can
// never write a Bambu tag. The device refuses a Bambu tag too.
func checkArgs(method string, args json.RawMessage) (json.RawMessage, error) {
	if len(args) > argsMax {
		return nil, httpx.WithReason(http.StatusRequestEntityTooLarge, "The call's args are too large.", "args_too_large")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(args, &fields); err != nil {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "args"}, Msg: "Input should be a valid dictionary"})
	}
	allowed := map[string][]string{"nfc.write": {"records"}}[method]
	var issues []httpx.Issue
	keys := make([]string, 0, len(fields))
	for k := range fields {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if !slices.Contains(allowed, k) {
			issues = append(issues, httpx.Issue{Loc: []string{"body", "args", k}, Msg: "Extra inputs are not permitted"})
		}
	}
	if method == "nfc.write" {
		var records []map[string]json.RawMessage
		if json.Unmarshal(fields["records"], &records) != nil || len(records) < 1 || len(records) > 16 {
			issues = append(issues, httpx.Issue{Loc: []string{"body", "args", "records"},
				Msg: "a list of 1 to 16 NDEF records"})
		}
	}
	if len(issues) > 0 {
		return nil, httpx.Invalid(issues...)
	}
	return args, nil
}

func requested(call, device, method string, args json.RawMessage, deadline time.Time) []httpx.Field {
	return []httpx.Field{
		{Key: "call", Value: call}, {Key: "device", Value: device}, {Key: "method", Value: method},
		{Key: "args", Value: args}, {Key: "deadline", Value: crud.Time(deadline)},
		{Key: "topics", Value: []string{"device:" + device}}, {Key: "retention_seconds", Value: Retention},
	}
}

// send marks the call sent and publishes device.call_requested on the device's topic: the
// device's realtime socket, on whichever pod holds it, delivers it.
func (s *Store) send(ctx context.Context, call, device, method string, args json.RawMessage, deadline time.Time) error {
	if _, err := s.Pool.Exec(ctx, `UPDATE device_calls SET sent_at = $2, deadline = $3 WHERE id = $1::uuid`,
		call, s.now(), deadline); err != nil {
		return err
	}
	bus.Publish(ctx, s.Pool, "device.call_requested", requested(call, device, method, args, deadline)...)
	return nil
}

// schedule ends the call at its deadline if it is still open then. A pod that stops before
// then leaves it to the next read of the call (expire runs on every read).
func (s *Store) schedule(call string, deadline time.Time) {
	s.after(time.Until(deadline)+100*time.Millisecond, func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if _, err := s.expire(ctx, call); err != nil {
			logFailed(ctx, "could not end a call at its deadline", err)
		}
	})
}

// expire ends the call when its deadline has passed and it is still open: offline when the
// device has gone, timeout when it is still online, denied when its approval never came. It
// says whether it ended the call.
func (s *Store) expire(ctx context.Context, call string) (bool, error) {
	now := s.now()
	var outcome, device string
	err := s.Pool.QueryRow(ctx, `UPDATE device_calls c SET ended_at = $2, outcome = CASE
			WHEN c.sent_at IS NULL THEN 'denied'
			WHEN d.revoked_at IS NULL AND d.last_seen_at > $3 THEN 'timeout'
			ELSE 'offline' END
		FROM devices d WHERE c.id = $1::uuid AND d.id = c.device_id AND c.outcome IS NULL AND c.deadline <= $2
		RETURNING c.outcome, c.device_id::text`, call, now, now.Add(-OnlineWindow)).Scan(&outcome, &device)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	s.publishFailed(ctx, call, device, outcome, "the call's deadline passed")
	return true, nil
}

func (s *Store) publishFailed(ctx context.Context, call, device, outcome, why string) {
	bus.Publish(ctx, s.Pool, "device.call_failed", httpx.Field{Key: "call", Value: call},
		httpx.Field{Key: "device", Value: device}, httpx.Field{Key: "outcome", Value: outcome},
		httpx.Field{Key: "error", Value: why},
		httpx.Field{Key: "topics", Value: []string{"device:" + device, "device:call:" + call}},
		httpx.Field{Key: "retention_seconds", Value: Retention})
}

// view is the call, its events and its result, for the person it is visible to.
func (s *Store) view(ctx context.Context, id string) (*View, error) {
	if _, err := s.expire(ctx, id); err != nil {
		return nil, err
	}
	c, err := s.loadCall(ctx, s.Pool, id, "")
	if err != nil {
		return nil, err
	}
	rows, err := s.Pool.Query(ctx, `SELECT payload::text FROM events WHERE kind LIKE 'device.%'
		AND payload ->> 'call' = $1 ORDER BY seq LIMIT $2`, id, replayMax)
	if err != nil {
		return nil, err
	}
	out := &View{Call: *c, Events: []Event{}, Result: c.result}
	var text string
	_, err = pgx.ForEachRow(rows, []any{&text}, func() error {
		var fields map[string]json.RawMessage
		if json.Unmarshal([]byte(text), &fields) != nil {
			return nil
		}
		e := Event{Data: map[string]json.RawMessage{}}
		_ = json.Unmarshal(fields["id"], &e.ID)
		_ = json.Unmarshal(fields["kind"], &e.Kind)
		_ = json.Unmarshal(fields["at"], &e.At)
		for k, v := range fields {
			if k != "id" && k != "at" && k != "kind" && k != "topics" && k != "retention_seconds" {
				e.Data[k] = v
			}
		}
		out.Events = append(out.Events, e)
		return nil
	})
	if err != nil {
		return nil, err
	}
	if out.Result == nil {
		out.Result = json.RawMessage("null")
	}
	return out, nil
}

// getCall answers a call to the person who owns its device.
func (s *Store) getCall(r *http.Request) (any, error) {
	id := r.PathValue("id")
	if !crud.IsUUID(id) {
		return nil, notFound("call")
	}
	v, err := s.view(r.Context(), id)
	if err != nil {
		return nil, err
	}
	if v.Call.owner != principal(r.Context()) && v.Call.caller != principal(r.Context()) {
		return nil, notFound("call")
	}
	return v, nil
}

// wait holds the request until the call ends (200 with the view) or its deadline passes (504).
func (s *Store) wait(ctx context.Context, id string) (any, error) {
	poll := s.Poll
	if poll <= 0 {
		poll = 250 * time.Millisecond
	}
	for {
		v, err := s.view(ctx, id)
		if err != nil {
			return nil, err
		}
		if v.Call.Outcome != nil {
			if *v.Call.Outcome == "done" || *v.Call.Outcome == "error" {
				return v, nil
			}
			return nil, &httpx.Problem{Status: http.StatusGatewayTimeout,
				Detail:     "The call ended at its deadline: " + *v.Call.Outcome + ".",
				Extensions: []httpx.Field{{Key: "reason", Value: *v.Call.Outcome}, {Key: "call_id", Value: id}}}
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(poll):
		}
	}
}

// openCall is the device's own call while it is open, locked in tx.
func (s *Store) openCall(ctx context.Context, tx pgx.Tx, id string, d Device) (*Call, error) {
	if !crud.IsUUID(id) {
		return nil, notFound("call")
	}
	c, err := s.loadCall(ctx, tx, id, " FOR UPDATE OF c")
	if err != nil {
		return nil, err
	}
	if c.DeviceID != d.ID {
		return nil, notFound("call")
	}
	if c.Outcome != nil || c.SentAt == nil || !s.now().Before(time.Time(c.Deadline)) {
		return nil, httpx.WithReason(http.StatusConflict, "That call is not open.", "call_closed")
	}
	return c, nil
}

// events takes a few events of a running call from its device and publishes each on the
// bus, in order, as device.<kind> with the topics device:<device> and device:call:<call>.
func (s *Store) events(r *http.Request, d Device) (any, error) {
	ctx := r.Context()
	id := r.PathValue("id")
	_, _ = s.expire(ctx, id)
	b, err := crud.ReadBody(r, "events")
	if err != nil {
		return nil, err
	}
	items := b.Objects("events", false, eventsMax, "kind", "data")
	type posted struct {
		kind string
		data map[string]json.RawMessage
	}
	var all []posted
	for _, n := range items {
		p := posted{kind: n.Choice("kind", deviceKinds...), data: map[string]json.RawMessage{}}
		if n.Present("data") {
			raw := n.RawObject("data")
			if len(raw) > eventDataMax {
				n.Fail([]string{"data"}, "at most 1024 bytes")
			}
			_ = json.Unmarshal(raw, &p.data)
			for k := range p.data {
				if slices.Contains(reserved, k) {
					n.Fail([]string{"data", k}, "core sets this field")
				}
			}
		}
		n.Done()
		all = append(all, p)
	}
	if err := b.Err(); err != nil {
		return nil, err
	}
	var call *Call
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		call, err = s.openCall(ctx, tx, id, d)
		return err
	})
	if err != nil {
		return nil, err
	}
	var fields [][]httpx.Field
	for _, p := range all {
		f := []httpx.Field{{Key: "call", Value: call.ID}, {Key: "device", Value: d.ID}}
		keys := make([]string, 0, len(p.data))
		for k := range p.data {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			f = append(f, httpx.Field{Key: k, Value: p.data[k]})
		}
		f = append(f, httpx.Field{Key: "topics", Value: []string{"device:" + d.ID, "device:call:" + call.ID}},
			httpx.Field{Key: "retention_seconds", Value: Retention})
		if !bus.Fits("device."+p.kind, f...) {
			return nil, httpx.WithReason(http.StatusRequestEntityTooLarge, "An event is too large.", "event_too_large")
		}
		fields = append(fields, f)
	}
	for i, p := range all {
		bus.Publish(ctx, s.Pool, "device."+p.kind, fields[i]...)
	}
	return &crud.Reply{Status: http.StatusNoContent}, nil
}

// end ends an open call in tx with outcome and result.
func end(ctx context.Context, tx pgx.Tx, id, outcome string, result any, now time.Time) error {
	raw, err := json.Marshal(result)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE device_calls SET outcome = $2, result = $3, ended_at = $4
		WHERE id = $1::uuid AND outcome IS NULL`, id, outcome, raw, now)
	return err
}

// approval makes the pending action an assistant's or a flow's nfc.write waits for: an
// ai_approvals row outside a session, as the MCP prepare step makes one. The UI shows it with
// the other pending actions; a person approves or denies it there; the caller then confirms
// the call (POST …/calls/{id}/confirm), which uses the approval once.
func (s *Store) approval(ctx context.Context, tx pgx.Tx, kind, who, deviceName, device, method string,
	args json.RawMessage, now time.Time) (string, error) {
	inputHash, err := inputHash(s.Keys.Current, device, method, args)
	if err != nil {
		return "", err
	}
	var id string
	err = tx.QueryRow(ctx, `INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary,
		input_hash, tier, requested_by_kind, requested_by_id, requested_by_label, created_at, expires_at)
		VALUES (gen_random_uuid(), NULL, NULL, 'device_call:' || gen_random_uuid()::text, $1, $2, $3, 'outward',
		$4, $5, $6, $7, $8) RETURNING id::text`,
		toolName, summary(method, deviceName, args), inputHash, kind, who, kind+" for "+who, now,
		now.Add(ApprovalTTL)).Scan(&id)
	return id, err
}

// inputHash is the approval's HMAC of the call's device, method and args (seal.InputHash:
// canonical JSON, so the args read back from jsonb hash the same).
func inputHash(kek *seal.KEK, device, method string, args json.RawMessage) (string, error) {
	raw, err := json.Marshal(map[string]any{"device_id": device, "method": method, "args": args})
	if err != nil {
		return "", err
	}
	return seal.InputHash(seal.InputHashKey(kek), toolName, string(raw))
}

const (
	// summaryValueMax and summaryMax bound what an approval shows of the records to write.
	summaryValueMax = 80
	summaryMax      = 1000
)

// summary is what the person approves: the method, the device, and each record's type and
// its text or URI, each value cut to summaryValueMax runes and the whole to summaryMax.
func summary(method, deviceName string, args json.RawMessage) string {
	var parsed struct {
		Records []map[string]any `json:"records"`
	}
	_ = json.Unmarshal(args, &parsed)
	parts := []string{}
	for _, r := range parsed.Records {
		kind, _ := r["type"].(string)
		value := ""
		for _, k := range []string{"text", "uri", "url", "value"} {
			if v, ok := r[k].(string); ok {
				value = v
				break
			}
		}
		parts = append(parts, cut(kind, 20)+" "+strconv.Quote(cut(value, summaryValueMax)))
	}
	out := method + " on " + deviceName
	if len(parts) > 0 {
		out += ": " + strings.Join(parts, "; ")
	}
	return cut(out, summaryMax)
}

// cut is s at most n runes, with an ellipsis when it was cut.
func cut(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n-1]) + "…"
}

// confirm sends an approved call: the caller that made it uses its approval once. A denied
// or expired approval ends the call denied.
func (s *Store) confirm(r *http.Request) (any, error) {
	ctx := r.Context()
	id := r.PathValue("id")
	if !crud.IsUUID(id) {
		return nil, notFound("call")
	}
	var b *crud.Body
	if r.ContentLength != 0 {
		var err error
		if b, err = crud.ReadBody(r, "on_behalf_of"); err != nil {
			return nil, err
		}
	}
	kind, who, err := s.callerOf(r, b)
	if err != nil {
		return nil, err
	}
	if b != nil {
		if err := b.Err(); err != nil {
			return nil, err
		}
	}
	now := s.now()
	var c *Call
	var decision *string
	usable := false
	sent := false
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		var err error
		c, err = s.loadCall(ctx, tx, id, " FOR UPDATE OF c")
		if err != nil {
			return err
		}
		if c.caller != who || c.CallerKind != kind {
			return notFound("call")
		}
		if c.PendingActionID == nil || c.SentAt != nil || c.Outcome != nil {
			return httpx.WithReason(http.StatusConflict, "That call waits for no approval.", "no_approval")
		}
		var approved string
		err = tx.QueryRow(ctx, `SELECT decision, coalesce(decision = 'approved' AND consumed_at IS NULL
			AND revoked_at IS NULL AND usable_until > $2, false), input_hash FROM ai_approvals WHERE id = $1::uuid`,
			*c.PendingActionID, now).Scan(&decision, &usable, &approved)
		if errors.Is(err, pgx.ErrNoRows) {
			decision = ptr("cancelled")
			err = nil
		}
		if err != nil || decision == nil {
			return err
		}
		// What is sent must be what was approved: the call's args hash again to the
		// approval's input hash, under the KEK or the one before it.
		if usable && !s.sameInput(approved, c) {
			decision, usable = ptr("mismatched"), false
		}
		if !usable {
			return end(ctx, tx, id, "denied", map[string]any{"error": "approval_" + *decision}, now)
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_approvals SET consumed_at = $2 WHERE id = $1::uuid`,
			*c.PendingActionID, now); err != nil {
			return err
		}
		sent = true
		return nil
	})
	if err != nil {
		return nil, err
	}
	switch {
	case decision == nil:
		return nil, httpx.WithReason(http.StatusConflict, "No person has approved the call yet.", "approval_pending")
	case !sent:
		s.publishFailed(ctx, id, c.DeviceID, "denied", "the approval was "+*decision)
		reason := "approval_denied"
		if *decision == "mismatched" {
			reason = "approval_mismatch"
		}
		return nil, httpx.WithReason(http.StatusConflict, "The call was not approved: "+*decision+".", reason)
	}
	online, err := s.Online(ctx, c.DeviceID)
	if err != nil {
		return nil, err
	}
	if !online {
		err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
			return end(ctx, tx, id, "offline", map[string]any{"error": "offline"}, now)
		})
		if err != nil {
			return nil, err
		}
		s.publishFailed(ctx, id, c.DeviceID, "offline", "the device was offline at the confirm")
		return nil, offline()
	}
	deadline := now.Add(time.Duration(c.timeoutS) * time.Second)
	if err := s.send(ctx, id, c.DeviceID, c.Method, c.Args, deadline); err != nil {
		return nil, err
	}
	s.schedule(id, deadline)
	return &crud.Reply{Status: http.StatusAccepted, Value: map[string]any{"call_id": id}}, nil
}

// sameInput says whether the call's device, method and args hash to approved.
func (s *Store) sameInput(approved string, c *Call) bool {
	for _, kek := range []*seal.KEK{s.Keys.Current, s.Keys.Previous} {
		if kek == nil {
			continue
		}
		if got, err := inputHash(kek, c.DeviceID, c.Method, c.Args); err == nil && got == approved {
			return true
		}
	}
	return false
}

func ptr[T any](v T) *T { return &v }
