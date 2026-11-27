package bus

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Event is an event of the log as core/events.py decode_event reads it: a typed kind (one
// of Kinds), or an opaque one (an unknown kind whose envelope is valid).
type Event struct {
	ID   string
	Kind string
	// Opaque is true for a kind Python has no model for.
	Opaque bool
	// fields are a typed kind's values after id, at and kind, in its model's order.
	fields []Value
	at     string
	// raw is the whole object, for an opaque event.
	raw Value
}

// ErrUndecodable is a payload Python's decode_event refuses.
var ErrUndecodable = errors.New("an event this build cannot decode")

// Decode reads text as core/events.py decode_event does: a typed kind is checked against its
// model (extra=forbid, pydantic's lax JSON mode), an unknown kind against OpaqueEvent.
func Decode(text string) (*Event, error) {
	v, err := ParseJSON(text)
	if err != nil || v.Kind != 'o' {
		return nil, ErrUndecodable
	}
	kind := v.Get("kind")
	if kind == nil || kind.Kind != 's' {
		return nil, ErrUndecodable
	}
	fields, known := Kinds[kind.Str]
	if !known {
		return decodeOpaque(v, kind.Str)
	}
	e := &Event{Kind: kind.Str, fields: make([]Value, len(fields))}
	given := make([]bool, len(fields))
	for i, name := range v.Keys {
		value := v.Vals[i]
		switch name {
		case "kind":
		case "id":
			if value.Kind != 's' {
				return nil, ErrUndecodable
			}
			e.ID = value.Str
		case "at":
			at, ok := pyDatetime(value)
			if !ok {
				return nil, ErrUndecodable
			}
			e.at = at
		default:
			j := slices.IndexFunc(fields, func(f Field) bool { return f.Name == name })
			if j < 0 {
				return nil, ErrUndecodable // extra="forbid"
			}
			lax, ok := fields[j].read(value)
			if !ok {
				return nil, ErrUndecodable
			}
			e.fields[j], given[j] = lax, true
		}
	}
	for j, f := range fields {
		if given[j] {
			continue
		}
		if f.Required {
			return nil, ErrUndecodable
		}
		e.fields[j] = f.zero()
	}
	// The model's default factories: a fresh id and the time now.
	if v.Get("id") == nil {
		id := make([]byte, 16)
		_, _ = rand.Read(id)
		e.ID = hex.EncodeToString(id)
	}
	if v.Get("at") == nil {
		e.at, _ = pyDatetime(Value{Kind: 's', Str: time.Now().UTC().Format("2006-01-02T15:04:05.000000Z")})
	}
	return e, nil
}

// opaqueKind is core/events.py OPAQUE_KIND_PATTERN.
var opaqueKind = regexp.MustCompile(`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`)

func decodeOpaque(v Value, kind string) (*Event, error) {
	id, at := v.Get("id"), v.Get("at")
	if id == nil || id.Kind != 's' || at == nil || !opaqueKind.MatchString(kind) {
		return nil, ErrUndecodable
	}
	if _, ok := pyDatetime(*at); !ok {
		return nil, ErrUndecodable
	}
	if topics := v.Get("topics"); topics != nil {
		if topics.Kind != 'a' || len(topics.Items) > TopicsMax {
			return nil, ErrUndecodable
		}
		for _, t := range topics.Items {
			if t.Kind != 's' || utf8.RuneCountInString(t.Str) > TopicMax {
				return nil, ErrUndecodable
			}
		}
	}
	if r := v.Get("retention_seconds"); r != nil && r.Kind != 'n' {
		f, ok := number(*r)
		if !ok || f < 0 || math.IsNaN(f) {
			return nil, ErrUndecodable
		}
	}
	return &Event{ID: id.Str, Kind: kind, Opaque: true, raw: v}, nil
}

func number(v Value) (float64, bool) {
	switch v.Kind {
	case 'i':
		f, err := strconv.ParseFloat(v.Int, 64)
		return f, err == nil
	case 'f':
		return v.Float, true
	}
	return 0, false
}

// NotifyOnly says whether the kind is one only the agent or scadbuddy-core NOTIFYs and the log never holds
// (agent_changes/hub.py NOTIFY_ONLY): its change frame carries no id.
func (e *Event) NotifyOnly() bool {
	return !e.Opaque && (strings.HasPrefix(e.Kind, "session.") ||
		e.Kind == "ai_operation.changed" || e.Kind == "inventory_operation.changed")
}

// Encoded is core/events.py encode_event of the event: a typed one as pydantic's
// model_dump_json writes it, an opaque one as json.dumps writes the object it came as.
func (e *Event) Encoded() string {
	if e.Opaque {
		return Dumps(e.raw, true, false)
	}
	fields := Kinds[e.Kind]
	v := Value{Kind: 'o',
		Keys: []string{"id", "at", "kind"},
		Vals: []Value{{Kind: 's', Str: e.ID}, {Kind: 's', Str: e.at}, {Kind: 's', Str: e.Kind}}}
	for i, f := range fields {
		v.Keys, v.Vals = append(v.Keys, f.Name), append(v.Vals, e.fields[i])
	}
	return Dumps(v, false, true)
}

// Text is a field's string value, "" when it has none.
func (e *Event) Text(name string) string {
	if v := e.field(name); v != nil && v.Kind == 's' {
		return v.Str
	}
	return ""
}

// Null says whether a field is null.
func (e *Event) Null(name string) bool {
	v := e.field(name)
	return v == nil || v.Kind == 'n'
}

func (e *Event) field(name string) *Value {
	for i, f := range Kinds[e.Kind] {
		if f.Name == name {
			return &e.fields[i]
		}
	}
	return nil
}

// Topics is api/realtime.py topics_of: every topic the event is news for.
func (e *Event) Topics() []string {
	if e.Opaque {
		return nil // an unknown kind is not browser news
	}
	t := e.Text
	switch e.Kind {
	case "job.pending", "job.running", "job.done", "job.failed", "job.superseded", "job.cancelled", "job.progress":
		return []string{"job:" + t("job_id")}
	case "model.created", "model.updated", "model.deleted":
		return []string{"models", "model:" + t("slug")}
	case "source.changed", "version.committed", "upstream.available", "presets.changed":
		return []string{"model:" + t("slug")}
	case "output.created", "output.deleted", "output.updated":
		return []string{"outputs", "model:" + t("slug")}
	case "print.progress", "print.settled", "print.run", "print.outcome":
		return []string{"print:" + t("output_id")}
	case "flow_run.changed":
		return []string{"workflow-runs", "workflow-run:" + t("run_id")}
	case "flow.changed":
		return []string{"flows"}
	case "task.changed":
		topics := []string{"tasks", "task:" + t("task_id")}
		if place := t("place"); place != "" {
			topics = append(topics, "tasks:"+place)
		}
		return topics
	case "preview_slice.progress", "preview_slice.settled":
		return []string{"preview-slice:" + t("slicer") + ":" + e.field("job_id").Int}
	case "printer.report", "printer.changed", "camera.feed", "camera.reconnect":
		return []string{"printers", "printer:" + t("printer_id")}
	case "library.changed":
		return []string{"libraries", "model:" + t("slug")}
	case "library.removed":
		return []string{"libraries"}
	case "font.installed":
		return []string{"fonts"}
	case "settings.changed":
		return []string{"settings"}
	case "plugin_packages.changed":
		return []string{"plugin-packages"}
	case "analyzer.decision":
		return []string{"analyzers"}
	case "activity.changed":
		return []string{"activity", "activity:" + t("activity_id")}
	case "order.changed", "order_item.changed":
		return []string{"orders", "order:" + t("order_id")}
	case "order.bank_deposits":
		return []string{"order:" + t("order_id")}
	}
	return nil
}

// OwnTopics is an opaque event's own topics field (the envelope's topics), nil for a typed
// event or one with none. Python's socket sends no opaque event; core's sends one only of a
// namespace it opts in (realtime, device.*).
func (e *Event) OwnTopics() []string {
	if !e.Opaque {
		return nil
	}
	var out []string
	if topics := e.raw.Get("topics"); topics != nil && topics.Kind == 'a' {
		for _, t := range topics.Items {
			if t.Kind == 's' {
				out = append(out, t.Str)
			}
		}
	}
	return out
}

// OpaqueFrame is an opaque event's socket frame, as Frame writes a typed one: its kind and
// id, and as data every field but id, at, kind, topics and retention_seconds, in the order
// it was logged.
func (e *Event) OpaqueFrame(topics []string) string {
	data := Value{Kind: 'o', Keys: []string{}, Vals: []Value{}}
	for i, k := range e.raw.Keys {
		switch k {
		case "id", "at", "kind", "topics", "retention_seconds":
			continue
		}
		data.Keys, data.Vals = append(data.Keys, k), append(data.Vals, e.raw.Vals[i])
	}
	names := Value{Kind: 'a', Items: []Value{}}
	for _, t := range topics {
		names.Items = append(names.Items, Value{Kind: 's', Str: t})
	}
	frame := Value{Kind: 'o',
		Keys: []string{"type", "id", "kind", "topics", "data"},
		Vals: []Value{{Kind: 's', Str: "event"}, {Kind: 's', Str: e.ID}, {Kind: 's', Str: e.Kind}, names, data}}
	return Dumps(frame, false, true)
}

// Frame is api/realtime.py event_frame, as the socket sends it (json.dumps, compact, no
// ensure_ascii): the kind and ids, and the fields but id, at, kind and cart; an order's
// deleted only when true.
func (e *Event) Frame(topics []string) string {
	data := Value{Kind: 'o', Keys: []string{}, Vals: []Value{}}
	for i, f := range Kinds[e.Kind] {
		if f.Name == "cart" || f.Name == "deleted" && e.Kind == "order.changed" && !e.fields[i].Bool {
			continue
		}
		data.Keys, data.Vals = append(data.Keys, f.Name), append(data.Vals, e.fields[i])
	}
	names := Value{Kind: 'a', Items: []Value{}}
	for _, t := range topics {
		names.Items = append(names.Items, Value{Kind: 's', Str: t})
	}
	frame := Value{Kind: 'o',
		Keys: []string{"type", "id", "kind", "topics", "data"},
		Vals: []Value{{Kind: 's', Str: "event"}, {Kind: 's', Str: e.ID}, {Kind: 's', Str: e.Kind}, names, data}}
	return Dumps(frame, false, true)
}

// read is a field's value as pydantic's lax JSON mode takes it, normalised to what
// model_dump_json writes.
func (f Field) read(v Value) (Value, bool) {
	if v.Kind == 'n' {
		return v, f.Nullable
	}
	switch f.Type {
	case "str":
		if v.Kind != 's' || len(f.Choices) > 0 && !slices.Contains(f.Choices, v.Str) {
			return Value{}, false
		}
		return v, true
	case "int":
		return laxInt(v)
	case "bool":
		return laxBool(v)
	case "list[str]":
		if v.Kind != 'a' {
			return Value{}, false
		}
		for _, item := range v.Items {
			if item.Kind != 's' {
				return Value{}, false
			}
		}
		return v, true
	}
	return Value{}, false
}

func (f Field) zero() Value {
	if f.Type == "bool" {
		return Value{Kind: 'b'}
	}
	return Value{Kind: 'n'}
}

var intText = regexp.MustCompile(`^[+-]?[0-9]+$`)

func laxInt(v Value) (Value, bool) {
	switch v.Kind {
	case 'i':
		return v, true
	case 'f':
		if v.Float != math.Trunc(v.Float) || math.IsInf(v.Float, 0) || math.Abs(v.Float) > 1<<63 {
			return Value{}, false
		}
		return Value{Kind: 'i', Int: strconv.FormatFloat(v.Float, 'f', 0, 64)}, true
	case 's':
		s := strings.TrimSpace(v.Str)
		if !intText.MatchString(s) {
			return Value{}, false
		}
		n, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			return Value{}, false
		}
		return Value{Kind: 'i', Int: strconv.FormatInt(n, 10)}, true
	}
	return Value{}, false
}

func laxBool(v Value) (Value, bool) {
	switch v.Kind {
	case 'b':
		return v, true
	case 'i':
		if v.Int == "0" || v.Int == "1" {
			return Value{Kind: 'b', Bool: v.Int == "1"}, true
		}
	case 'f':
		if v.Float == 0 || v.Float == 1 {
			return Value{Kind: 'b', Bool: v.Float == 1}, true
		}
	case 's':
		switch strings.ToLower(v.Str) {
		case "0", "off", "f", "false", "n", "no":
			return Value{Kind: 'b'}, true
		case "1", "on", "t", "true", "y", "yes":
			return Value{Kind: 'b', Bool: true}, true
		}
	}
	return Value{}, false
}

// datetimeText is the ISO 8601 text pydantic takes for a datetime.
var datetimeText = regexp.MustCompile(
	`^(\d{4})-(\d{2})-(\d{2})[Tt _](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?(?:([Zz])|([+-])(\d{2}):?(\d{2}))?$`)

// pyDatetime is a datetime as pydantic reads it (an ISO 8601 text, or a Unix time in
// seconds, milliseconds past 2e10) and writes it back: microseconds only when there are
// any, "Z" for UTC, the offset otherwise, nothing for a naive one.
func pyDatetime(v Value) (string, bool) {
	if f, ok := number(v); ok {
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return "", false
		}
		if math.Abs(f) > 2e10 {
			f /= 1000
		}
		sec := math.Floor(f)
		micro := math.Round((f - sec) * 1e6)
		t := time.Unix(int64(sec), int64(micro)*1000).UTC()
		return formatAt(t, "Z"), true
	}
	if v.Kind != 's' {
		return "", false
	}
	m := datetimeText.FindStringSubmatch(v.Str)
	if m == nil {
		return "", false
	}
	n := func(s string) int { i, _ := strconv.Atoi(s); return i }
	frac := m[7]
	if len(frac) > 6 {
		frac = frac[:6]
	}
	micro := 0
	if frac != "" {
		micro = n(frac + strings.Repeat("0", 6-len(frac)))
	}
	year, month, day, hour, minute, second := n(m[1]), n(m[2]), n(m[3]), n(m[4]), n(m[5]), n(m[6])
	t := time.Date(year, time.Month(month), day, hour, minute, second, micro*1000, time.UTC)
	if t.Year() != year || int(t.Month()) != month || t.Day() != day || hour > 23 || minute > 59 || second > 59 {
		return "", false
	}
	zone := ""
	switch {
	case m[8] != "":
		zone = "Z"
	case m[9] != "":
		oh, om := n(m[10]), n(m[11])
		if oh > 23 || om > 59 {
			return "", false
		}
		zone = fmt.Sprintf("%s%02d:%02d", m[9], oh, om)
		if oh == 0 && om == 0 {
			zone = "Z"
		}
	}
	return formatAt(t, zone), true
}

func formatAt(t time.Time, zone string) string {
	layout := "2006-01-02T15:04:05"
	if t.Nanosecond() != 0 {
		layout += ".000000"
	}
	return t.Format(layout) + zone
}
