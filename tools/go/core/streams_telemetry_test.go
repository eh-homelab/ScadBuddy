package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/realtime"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry/pgxtrace"
)

// A change stream's span ends when the stream starts, named by its route, and the hub
// counts its subscribers, frames and resyncs.
func TestAStreamIsTracedAtItsStartAndMeasured(t *testing.T) {
	rec := recordTelemetry(t)
	pool := testdb.New(t)
	hub := bus.NewHub(pool, rec.tel.Meter())
	t.Cleanup(hub.Close)
	router, err := newRouter(pool, http.NotFoundHandler(), nil, "changes", "", nil, nil, nil, nil, withStreams(&streams{hub: hub, env: func(string) string { return "" }}))
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(rec.tel.Server(router, untraced...))
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "crud.agent")
	s := &streamAPI{api: a, hub: hub}

	stream := s.open("agent", a.token, map[string]string{"Last-Event-ID": "unknown-place"})
	expect(t, stream.next(), ready(true, true, "0"))
	// Named as the API's FastAPI span is, while the stream is still open.
	spans := rec.named("GET /api/v1/internal/crud/agent/changes")
	if len(spans) != 1 {
		t.Fatalf("%d spans for the open stream", len(spans))
	}
	expect(t, value(spans[0], "http.status_code").AsInt64(), int64(200))

	hub.Gap()
	expect(t, stream.next(), bus.SSE("reconnected", bus.Object("resync", false), "0"))
	deadline := time.Now().Add(5 * time.Second)
	var got map[string]metricdata.Metrics
	for time.Now().Before(deadline) {
		got = rec.metrics(t)
		if _, ok := got["scadbuddy.events.frames"]; ok {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	subs, ok := got["scadbuddy.events.subscribers"].Data.(metricdata.Sum[int64])
	if !ok || len(subs.DataPoints) != 1 || subs.DataPoints[0].Value != 1 {
		t.Errorf("subscribers %+v", got["scadbuddy.events.subscribers"].Data)
	}
	frames, ok := got["scadbuddy.events.frames"].Data.(metricdata.Sum[int64])
	if !ok || len(frames.DataPoints) != 2 {
		t.Errorf("frames %+v", got["scadbuddy.events.frames"].Data)
	}
	resyncs, ok := got["scadbuddy.events.resyncs"].Data.(metricdata.Sum[int64])
	if !ok || len(resyncs.DataPoints) != 1 {
		t.Errorf("resyncs %+v", got["scadbuddy.events.resyncs"].Data)
	}
	if v, _ := resyncs.DataPoints[0].Attributes.Value("reason"); v.AsString() != "place" {
		t.Errorf("resync reason %q", v.AsString())
	}
}

// socketTelemetry is the realtime socket behind recorded telemetry, its queries traced.
func socketTelemetry(t *testing.T, env map[string]string) (*recorded, *streamAPI) {
	t.Helper()
	rec := recordTelemetry(t)
	pool := testdb.NewWith(t, func(c *pgxpool.Config) {
		c.ConnConfig.Tracer = pgxtrace.New(rec.tel.Tracer(), rec.tel.Meter())
	})
	hub := bus.NewHub(pool, rec.tel.Meter())
	t.Cleanup(hub.Close)
	st := &streams{hub: hub, env: func(name string) string { return env[name] }}
	router, err := newRouter(pool, http.NotFoundHandler(), nil, "ws", env["SCADBUDDY_AUTH_MODE"], nil, rec.tel.Meter(), nil, nil, withStreams(st))
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(rec.tel.Server(router, untraced...))
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	hub.Start(5 * time.Second)
	return rec, &streamAPI{api: a, hub: hub}
}

// In observe a socket that enforce would refuse is served and counted, as auth/check.py
// counts it in scadbuddy_auth_would_refuse_total{class, reason}.
func TestAnObservedSocketCountsWhatEnforceWouldRefuse(t *testing.T) {
	rec, s := socketTelemetry(t, map[string]string{"SCADBUDDY_AUTH_MODE": "observe"})
	_, res := s.dial(nil)
	expect(t, res.StatusCode, 101)
	_, res = s.dial(map[string]string{"Authorization": "Bearer " + s.credential("agent", "crud.agent")})
	expect(t, res.StatusCode, 101)
	_, res = s.dial(map[string]string{"Authorization": "Bearer " + s.credential("agent", "api")})
	expect(t, res.StatusCode, 101)
	// A revoked access token: served in observe, and counted signed out as enforce would refuse it.
	dead := "sbpat_0000000000000002." + strings.Repeat("b", 43)
	s.sql(`INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash, expires_at, revoked_at)
		VALUES ('0000000000000002', 'user:i#s', 'n', 'read', sha256($1::bytea), now() + interval '1 day', now())`,
		[]byte(dead))
	_, res = s.dial(map[string]string{"Authorization": "Bearer " + dead})
	expect(t, res.StatusCode, 101)
	got, ok := rec.metrics(t)["scadbuddy.auth.would_refuse"].Data.(metricdata.Sum[int64])
	if !ok {
		t.Fatal("no scadbuddy.auth.would_refuse")
	}
	counts := map[string]int64{}
	for _, p := range got.DataPoints {
		class, _ := p.Attributes.Value("class")
		reason, _ := p.Attributes.Value("reason")
		counts[class.AsString()+" "+reason.AsString()] = p.Value
	}
	expect(t, len(counts), 2)
	expect(t, counts["api signed_out"], int64(2))
	expect(t, counts["api out_of_scope"], int64(1))
	// One instrument in the process: the gate's, which the public routes count in too.
	var rm metricdata.ResourceMetrics
	if err := rec.reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	var scopes []string
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name == auth.WouldRefuseMetric {
				scopes = append(scopes, sm.Scope.Name)
			}
		}
	}
	expect(t, strings.Join(scopes, ","), "github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth")
}

// A socket's span ends as it is accepted, with status 101. A check's queries run later, so
// they are under a span of their own, linked to the socket's, never under the ended one.
func TestASocketIsTracedAs101AndItsChecksApart(t *testing.T) {
	rec, s := socketTelemetry(t, nil)
	sock, res := s.dial(nil)
	expect(t, res.StatusCode, 101)
	servers := rec.named("GET " + realtime.Path)
	if len(servers) != 1 {
		t.Fatalf("%d spans for the socket", len(servers))
	}
	server := servers[0]
	expect(t, value(server, "http.status_code").AsInt64(), int64(101))

	// Subscribed first, so the event's frame comes before the check's answer, never after it.
	sock.send(1, `{"type":"subscribe","topics":["models"]}`)
	expect(t, sock.text(), `{"type":"subscribed","topics":["models"]}`)
	id := s.publish("model.updated", httpx.Field{Key: "slug", Value: "a"})
	expect(t, sock.text(), `{"type":"event","id":"`+id+`","kind":"model.updated","topics":["models"],"data":{"slug":"a"}}`)
	sock.send(1, `{"type":"check","id":1,"since":["`+id+`"],"topics":["models"]}`)
	expect(t, sock.text(), `{"type":"changed","id":1,"topics":[]}`)
	checks := rec.named("ws check")
	if len(checks) != 1 {
		t.Fatalf("%d check spans", len(checks))
	}
	check := checks[0]
	if check.Parent.IsValid() {
		t.Errorf("a check span has a parent: %v", check.Parent)
	}
	if len(check.Links) != 1 || check.Links[0].SpanContext.SpanID() != server.SpanContext.SpanID() {
		t.Errorf("a check span's links %+v", check.Links)
	}
	queries := 0
	for _, span := range rec.spans.GetSpans() {
		if span.Parent.SpanID() == server.SpanContext.SpanID() && span.EndTime.After(server.EndTime) {
			t.Errorf("%s ended under the socket's span after it ended", span.Name)
		}
		if span.Parent.SpanID() == check.SpanContext.SpanID() {
			queries++
		}
	}
	if queries == 0 {
		t.Error("a check's queries are not under its span")
	}
}
