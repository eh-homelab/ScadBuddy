package report

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/discover"
	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/registry"
)

type call struct {
	kind, session, status string
}

type fakeRegistry struct {
	calls      []call
	sessionErr []error // answers to PutSession, in order; nil after
	machineErr []error
}

func (f *fakeRegistry) PutMachine(_ context.Context, m discover.Machine) error {
	f.calls = append(f.calls, call{kind: "machine"})
	if len(f.machineErr) > 0 {
		err := f.machineErr[0]
		f.machineErr = f.machineErr[1:]
		return err
	}
	return nil
}

func (f *fakeRegistry) PutSession(_ context.Context, _ string, r discover.Row) error {
	f.calls = append(f.calls, call{kind: "session", session: r.SessionID, status: r.Status})
	if len(f.sessionErr) > 0 {
		err := f.sessionErr[0]
		f.sessionErr = f.sessionErr[1:]
		return err
	}
	return nil
}

func (f *fakeRegistry) take() []call {
	c := f.calls
	f.calls = nil
	return c
}

type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

func rows(statuses ...string) []discover.Row {
	ids := []string{"aaaaaaaa-1111-4111-8111-111111111111", "bbbbbbbb-2222-4222-8222-222222222222"}
	var out []discover.Row
	for i, s := range statuses {
		out = append(out, discover.Row{SessionID: ids[i], PID: 100 + i, ProcStart: "1", Cwd: "/w", ConfigDir: "/c", Status: s})
	}
	return out
}

func newReporter(reg *fakeRegistry, c *clock) *Reporter {
	r := New(reg, discover.Machine{ID: "m"}, 5*time.Minute)
	r.now = c.now
	return r
}

func TestChangedOnly(t *testing.T) {
	reg := &fakeRegistry{}
	c := &clock{time.Unix(1_800_000_000, 0)}
	r := newReporter(reg, c)
	ctx := context.Background()

	if err := r.Report(ctx, rows("busy", "idle")); err != nil {
		t.Fatal(err)
	}
	if got := reg.take(); len(got) != 3 || got[0].kind != "machine" {
		t.Fatalf("first scan: %+v", got)
	}

	c.t = c.t.Add(10 * time.Second)
	_ = r.Report(ctx, rows("busy", "idle"))
	if got := reg.take(); len(got) != 0 {
		t.Fatalf("nothing changed, but sent %+v", got)
	}

	c.t = c.t.Add(10 * time.Second)
	_ = r.Report(ctx, rows("idle", "idle"))
	if got := reg.take(); len(got) != 1 || got[0].status != "idle" || got[0].session != rows("x")[0].SessionID {
		t.Fatalf("one changed: %+v", got)
	}

	// After the refresh interval, everything is sent again: the machine and each row.
	c.t = c.t.Add(5 * time.Minute)
	_ = r.Report(ctx, rows("idle", "idle"))
	if got := reg.take(); len(got) != 3 {
		t.Fatalf("refresh: %+v", got)
	}

	// A session that goes and comes back is sent again.
	c.t = c.t.Add(10 * time.Second)
	_ = r.Report(ctx, rows("idle"))
	_ = r.Report(ctx, rows("idle", "idle"))
	if got := reg.take(); len(got) != 1 || got[0].session != rows("x", "y")[1].SessionID {
		t.Fatalf("came back: %+v", got)
	}
}

func TestFailedSendIsRetried(t *testing.T) {
	reg := &fakeRegistry{sessionErr: []error{&registry.ProblemError{Status: 503}}}
	c := &clock{time.Unix(1_800_000_000, 0)}
	r := newReporter(reg, c)
	if err := r.Report(context.Background(), rows("busy")); err == nil {
		t.Fatal("the failure was not reported")
	}
	reg.take()
	c.t = c.t.Add(10 * time.Second)
	_ = r.Report(context.Background(), rows("busy"))
	if got := reg.take(); len(got) != 1 || got[0].kind != "session" {
		t.Fatalf("not retried: %+v", got)
	}
}

func TestUnknownMachineRegistersAgain(t *testing.T) {
	reg := &fakeRegistry{}
	c := &clock{time.Unix(1_800_000_000, 0)}
	r := newReporter(reg, c)
	_ = r.Report(context.Background(), rows("busy"))
	reg.take()

	reg.sessionErr = []error{registry.ErrUnknownMachine}
	if err := r.Report(context.Background(), rows("idle")); err != nil {
		t.Fatal(err)
	}
	got := reg.take()
	want := []string{"session", "machine", "session"}
	if len(got) != len(want) {
		t.Fatalf("%+v", got)
	}
	for i, k := range want {
		if got[i].kind != k {
			t.Fatalf("%+v, want %v", got, want)
		}
	}
}

func TestRouteMissingBacksOff(t *testing.T) {
	reg := &fakeRegistry{machineErr: []error{registry.ErrRouteMissing, registry.ErrRouteMissing}}
	c := &clock{time.Unix(1_800_000_000, 0)}
	r := newReporter(reg, c)
	var logged int
	r.logRouteMissing = func() { logged++ }

	for i := 0; i < 2; i++ {
		if err := r.Report(context.Background(), rows("busy", "idle")); !errors.Is(err, registry.ErrRouteMissing) {
			t.Fatalf("scan %d: %v", i, err)
		}
		// The scan stops at the first route-missing answer: no session is tried.
		if got := reg.take(); len(got) != 1 || got[0].kind != "machine" {
			t.Fatalf("scan %d: %+v", i, got)
		}
		c.t = c.t.Add(10 * time.Second)
	}
	if logged != 1 {
		t.Fatalf("logged %d times, want once", logged)
	}
	// The next scan, with the new core, sends everything.
	if err := r.Report(context.Background(), rows("busy", "idle")); err != nil {
		t.Fatal(err)
	}
	if got := reg.take(); len(got) != 3 {
		t.Fatalf("%+v", got)
	}
}

func TestARefusedRowWaitsUntilItChanges(t *testing.T) {
	refused := &registry.ProblemError{Status: 422, Detail: "String should have at most 40 characters"}
	reg := &fakeRegistry{sessionErr: []error{refused}}
	c := &clock{time.Unix(1_800_000_000, 0)}
	r := newReporter(reg, c)
	var logged []string
	r.logRefused = func(session string, reason error) {
		if !errors.Is(reason, refused) {
			t.Errorf("logged %v, want the 422", reason)
		}
		logged = append(logged, session)
	}
	id := rows("busy")[0].SessionID

	if err := r.Report(context.Background(), rows("busy")); !errors.Is(err, refused) {
		t.Fatalf("the refusal was not reported: %v", err)
	}
	reg.take()
	// The same row, at each scan and past the refresh: never sent again, logged once.
	for _, step := range []time.Duration{10 * time.Second, 10 * time.Second, 6 * time.Minute} {
		c.t = c.t.Add(step)
		if err := r.Report(context.Background(), rows("busy")); err != nil {
			t.Fatal(err)
		}
		for _, got := range reg.take() {
			if got.kind == "session" {
				t.Fatalf("the refused row was sent again after %v", step)
			}
		}
	}
	if len(logged) != 1 || logged[0] != id {
		t.Fatalf("logged %v, want %s once", logged, id)
	}
	// The row changes: it is sent, and once sent it is no longer held back.
	c.t = c.t.Add(10 * time.Second)
	if err := r.Report(context.Background(), rows("idle")); err != nil {
		t.Fatal(err)
	}
	if got := reg.take(); len(got) != 1 || got[0].status != "idle" {
		t.Fatalf("the changed row: %+v", got)
	}
	if len(r.refused) != 0 {
		t.Fatalf("still held back: %+v", r.refused)
	}
}

func TestARefusedSessionThatGoesIsForgotten(t *testing.T) {
	reg := &fakeRegistry{sessionErr: []error{&registry.ProblemError{Status: 422}}}
	c := &clock{time.Unix(1_800_000_000, 0)}
	r := newReporter(reg, c)
	r.logRefused = func(string, error) {}
	_ = r.Report(context.Background(), rows("busy"))
	_ = r.Report(context.Background(), nil)
	if len(r.refused) != 0 {
		t.Fatalf("a gone session is still held back: %+v", r.refused)
	}
	reg.take()
	_ = r.Report(context.Background(), rows("busy"))
	if got := reg.take(); len(got) != 1 || got[0].kind != "session" {
		t.Fatalf("the session that came back: %+v", got)
	}
}
