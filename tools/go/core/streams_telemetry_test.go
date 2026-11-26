package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// A change stream's span ends when the stream starts, named by its route, and the hub
// counts its subscribers, frames and resyncs.
func TestAStreamIsTracedAtItsStartAndMeasured(t *testing.T) {
	rec := recordTelemetry(t)
	pool := testdb.New(t)
	hub := bus.NewHub(pool, rec.tel.Meter())
	t.Cleanup(hub.Close)
	router, err := newRouter(pool, http.NotFoundHandler(), nil, "changes", "", &streams{hub: hub, env: func(string) string { return "" }})
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
