package fleetchat

import (
	"context"
	"errors"
	"strings"
	"testing"
)

const (
	sidA = "0a1b2c3d-0000-4000-8000-000000000001"
	sidB = "1b2c3d4e-0000-4000-8000-000000000002"
	sidC = "2c3d4e5f-0000-4000-8000-000000000003"
)

func TestHandle(t *testing.T) {
	h, err := Handle(sidA)
	if err != nil || h != "0a1b2c3d" {
		t.Fatalf("Handle(%q) = %q, %v; want 0a1b2c3d", sidA, h, err)
	}
	for _, bad := range []string{
		"",
		"0A1B2C3D-0000-4000-8000-000000000001", // upper case
		"0a1b2c3d000040008000000000000001",     // no dashes
		"{0a1b2c3d-0000-4000-8000-000000000001}",
		"0a1b2c3d-0000-4000-8000-00000000000g",
		"0a1b2c3d-0000-4000-8000-0000000000011",
	} {
		if _, err := Handle(bad); !errors.Is(err, ErrBadSession) {
			t.Errorf("Handle(%q) error = %v; want ErrBadSession", bad, err)
		}
	}
}

func TestValidation(t *testing.T) {
	if got := DefaultConfig().MaxText; got != 16<<20 {
		t.Fatalf("DefaultConfig().MaxText = %d; want %d", got, 16<<20)
	}
	ctx := context.Background()
	cfg := DefaultConfig()
	cfg.MaxText = 10
	st, bus := newFakeStore(), newFakeBus()
	svc := NewService(st, bus, cfg)

	if err := svc.Join(ctx, "nope", "ops"); !errors.Is(err, ErrBadSession) {
		t.Errorf("Join with a bad session: %v; want ErrBadSession", err)
	}
	for _, ch := range []string{"", "Ops", "a b", "ops_1", strings.Repeat("a", 41)} {
		if err := svc.Join(ctx, sidA, ch); !errors.Is(err, ErrBadChannel) {
			t.Errorf("Join(%q): %v; want ErrBadChannel", ch, err)
		}
	}
	if err := svc.Join(ctx, sidA, strings.Repeat("a", 40)); err != nil {
		t.Errorf("Join with a 40-character channel: %v", err)
	}
	for _, tg := range []Target{{}, {Channel: "ops", Agent: "1b2c3d4e"}, {Agent: "1B2C3D4E"}, {Agent: "1b2c3d4"}} {
		if _, err := svc.Send(ctx, sidA, tg, "hi"); !errors.Is(err, ErrBadTarget) {
			t.Errorf("Send to %+v: %v; want ErrBadTarget", tg, err)
		}
	}
	if _, err := svc.Send(ctx, sidA, Target{Channel: "Bad!"}, "hi"); !errors.Is(err, ErrBadChannel) {
		t.Errorf("Send to a bad channel: %v; want ErrBadChannel", err)
	}
	if _, err := svc.Send(ctx, sidA, Target{Channel: "ops"}, ""); !errors.Is(err, ErrEmptyText) {
		t.Errorf("Send with empty text: %v; want ErrEmptyText", err)
	}
	if _, err := svc.Send(ctx, sidA, Target{Channel: "ops"}, strings.Repeat("x", 11)); !errors.Is(err, ErrTooLong) {
		t.Errorf("Send with 11 bytes over a 10-byte limit: %v; want ErrTooLong", err)
	}
	if len(st.messages) != 0 || len(bus.publishedCalls()) != 0 {
		t.Errorf("an invalid send reached the store or the bus")
	}
	if err := svc.Rewind(ctx, sidA, -1); !errors.Is(err, ErrBadSeq) {
		t.Errorf("Rewind(-1): %v; want ErrBadSeq", err)
	}
	if _, err := svc.Send(ctx, sidA, Target{Agent: "ffffffff"}, "hi"); !errors.Is(err, ErrUnknownAgent) {
		t.Errorf("Send to an unknown agent: %v; want ErrUnknownAgent", err)
	}
	if _, err := svc.Send(ctx, sidA, Target{Channel: "nobody-joined"}, "hi"); !errors.Is(err, ErrUnknownChannel) {
		t.Errorf("Send to an unknown channel: %v; want ErrUnknownChannel", err)
	}
	// A name that is no handle reaches the store, which knows the virtual senders.
	if _, err := svc.Send(ctx, sidA, Target{Agent: "rotator"}, "hi"); !errors.Is(err, ErrVirtualSender) || err.Error() != rotatorReject {
		t.Errorf("Send to a virtual sender: %v; want ErrVirtualSender with its reject message", err)
	}
	if _, err := svc.Send(ctx, sidA, Target{Agent: "nobody"}, "hi"); !errors.Is(err, ErrUnknownAgent) {
		t.Errorf("Send to an unknown name: %v; want ErrUnknownAgent", err)
	}
	for _, name := range []string{"Rotator", "a_b", "1b2c3d4", "deadbeef0", strings.Repeat("z", 41)} {
		if _, err := svc.Send(ctx, sidA, Target{Agent: name}, "hi"); !errors.Is(err, ErrBadTarget) {
			t.Errorf("Send to %q: %v; want ErrBadTarget", name, err)
		}
	}
	if err := svc.Leave(ctx, sidA, "nobody-joined"); !errors.Is(err, ErrUnknownChannel) {
		t.Errorf("Leave an unknown channel: %v; want ErrUnknownChannel", err)
	}
}

func TestSendPublishes(t *testing.T) {
	ctx := context.Background()
	st, bus := newFakeStore(), newFakeBus()
	svc := NewService(st, bus, DefaultConfig())
	for _, sid := range []string{sidA, sidB} {
		if err := svc.Join(ctx, sid, "ops"); err != nil {
			t.Fatal(err)
		}
	}
	seq, err := svc.Send(ctx, sidA, Target{Channel: "ops"}, "hello")
	if err != nil || seq != 1 {
		t.Fatalf("Send = %d, %v; want 1, nil", seq, err)
	}
	if _, err := svc.Send(ctx, sidB, Target{Agent: "0a1b2c3d"}, "dm"); err != nil {
		t.Fatal(err)
	}
	got := bus.publishedCalls()
	if len(got) != 2 || strings.Join(got[0], ",") != sidA+","+sidB || strings.Join(got[1], ",") != sidA {
		t.Fatalf("published %v; want [[A B] [A]]", got)
	}
	if st.agents[sidA].Handle != "0a1b2c3d" {
		t.Errorf("the sender was not touched with its handle: %+v", st.agents)
	}
}

func TestPublishErrorKeepsSend(t *testing.T) {
	ctx := context.Background()
	st, bus := newFakeStore(), newFakeBus()
	bus.publishErr = errBusDown
	svc := NewService(st, bus, DefaultConfig())
	if err := svc.Join(ctx, sidA, "ops"); err != nil {
		t.Fatal(err)
	}
	seq, err := svc.Send(ctx, sidA, Target{Channel: "ops"}, "hello")
	if err != nil || seq != 1 {
		t.Fatalf("Send with a failing bus = %d, %v; want 1, nil", seq, err)
	}
	msgs, _, err := svc.Inbox(ctx, sidA, 0, true)
	if err != nil || len(msgs) != 1 {
		t.Fatalf("the message was not kept: %v, %v", msgs, err)
	}
}

func TestPageCap(t *testing.T) {
	ctx := context.Background()
	st := newFakeStore()
	svc := NewService(st, newFakeBus(), DefaultConfig())
	for _, c := range []struct{ in, want int }{{0, 50}, {-3, 50}, {7, 7}, {200, 200}, {201, 200}, {10000, 200}} {
		if _, _, err := svc.Inbox(ctx, sidA, c.in, true); err != nil {
			t.Fatal(err)
		}
		if st.lastLimit != c.want {
			t.Errorf("Inbox(limit=%d) asked the store for %d; want %d", c.in, st.lastLimit, c.want)
		}
	}
}
