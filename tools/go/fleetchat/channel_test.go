package fleetchat

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
)

// channelVectors are testdata/channel-vectors.json, which core's internal/fleet reads too.
type channelVectors struct{ Valid, Legacy, Invalid []string }

func readChannelVectors(t *testing.T) channelVectors {
	t.Helper()
	raw, err := os.ReadFile("testdata/channel-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v channelVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Valid) == 0 || len(v.Legacy) == 0 || len(v.Invalid) == 0 {
		t.Fatal("channel-vectors.json lacks a list")
	}
	return v
}

func TestChannelRulesHoldToTheVectors(t *testing.T) {
	v := readChannelVectors(t)
	for _, ch := range v.Valid {
		if !ValidChannel(ch) || checkChannel(ch) != nil {
			t.Errorf("%q: ValidChannel %v, checkChannel %v; want valid", ch, ValidChannel(ch), checkChannel(ch))
		}
	}
	for _, ch := range v.Legacy {
		if ValidChannel(ch) || !legacyOnly(ch) || checkChannel(ch) != nil {
			t.Errorf("%q: ValidChannel %v, legacyOnly %v, checkChannel %v; want legacy only",
				ch, ValidChannel(ch), legacyOnly(ch), checkChannel(ch))
		}
	}
	for _, ch := range v.Invalid {
		if ValidChannel(ch) || legacyOnly(ch) || !errors.Is(checkChannel(ch), ErrBadChannel) {
			t.Errorf("%q: ValidChannel %v, legacyOnly %v, checkChannel %v; want invalid",
				ch, ValidChannel(ch), legacyOnly(ch), checkChannel(ch))
		}
	}
}

func TestAQualifiedChannelIsJoinedAndPostedTo(t *testing.T) {
	ctx := context.Background()
	svc := NewService(newFakeStore(), newFakeBus(), DefaultConfig())
	for _, ch := range []string{"eh-homelab/clusters#1234", "eh-homelab/clusters/runs/12345678901", "fleet.ops"} {
		if err := svc.Join(ctx, sidA, ch); err != nil {
			t.Fatalf("Join(%q): %v", ch, err)
		}
		if _, err := svc.Send(ctx, sidA, Target{Channel: ch}, "hi"); err != nil {
			t.Errorf("Send to %q: %v", ch, err)
		}
		if err := svc.Leave(ctx, sidA, ch); err != nil {
			t.Errorf("Leave(%q): %v", ch, err)
		}
	}
}

func TestAnOldStyleNameIsKeptOnlyByAnExistingChannel(t *testing.T) {
	ctx := context.Background()
	st := newFakeStore()
	svc := NewService(st, newFakeBus(), DefaultConfig())
	err := svc.Join(ctx, sidA, "-ops")
	if !errors.Is(err, ErrBadChannel) || !strings.Contains(err.Error(), "only a channel made before #8149") {
		t.Fatalf("a join that would make \"-ops\": %v; want ErrBadChannel naming the old rule", err)
	}
	if len(st.members) != 0 {
		t.Fatalf("a refused join made a channel: %v", st.members)
	}
	st.members["-ops"] = map[string]struct{}{sidB: {}} // made before #8149
	if err := svc.Join(ctx, sidA, "-ops"); err != nil {
		t.Fatalf("a join of an existing \"-ops\": %v", err)
	}
	if _, err := svc.Send(ctx, sidA, Target{Channel: "-ops"}, "hi"); err != nil {
		t.Errorf("a post to \"-ops\": %v", err)
	}
	if err := svc.Leave(ctx, sidA, "-ops"); err != nil {
		t.Errorf("a leave of \"-ops\": %v", err)
	}
}

func TestAnUpperCaseChannelIsRefusedNotLowerCased(t *testing.T) {
	ctx := context.Background()
	st := newFakeStore()
	svc := NewService(st, newFakeBus(), DefaultConfig())
	err := svc.Join(ctx, sidA, "eh-homelab/Clusters#1234")
	if !errors.Is(err, ErrBadChannel) {
		t.Fatalf("Join with upper case: %v; want ErrBadChannel", err)
	}
	if !strings.Contains(err.Error(), `has upper case; write it as "eh-homelab/clusters#1234"`) {
		t.Errorf("the error does not name the lower-case form: %v", err)
	}
	if len(st.members) != 0 {
		t.Errorf("a refused join made a channel: %v", st.members)
	}
	if err := svc.Join(ctx, sidA, "Bad!"); !errors.Is(err, ErrBadChannel) || strings.Contains(err.Error(), "upper case") {
		t.Errorf("Join(\"Bad!\"): %v; want ErrBadChannel without the upper-case hint", err)
	}
}

func TestADirectMessageNameIsStillAPlainName(t *testing.T) {
	ctx := context.Background()
	svc := NewService(newFakeStore(), newFakeBus(), DefaultConfig())
	for _, agent := range []string{"eh-homelab/clusters#1", "fleet.ops"} {
		if _, err := svc.Send(ctx, sidA, Target{Agent: agent}, "hi"); !errors.Is(err, ErrBadTarget) {
			t.Errorf("a DM to %q: %v; want ErrBadTarget", agent, err)
		}
	}
}
