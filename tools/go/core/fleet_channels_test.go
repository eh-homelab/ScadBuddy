package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

// Channel names (#8149), held to fleetchat's vectors: the routes and the fleet_channels CHECK.

type channelVectors struct {
	Valid, Legacy, Invalid []string
	Segments               map[string]string // a reference's path segment
	BadSegments            []string
}

func readChannelVectors(t *testing.T) channelVectors {
	t.Helper()
	raw, err := os.ReadFile("../fleetchat/testdata/channel-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v channelVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Valid) == 0 || len(v.Legacy) == 0 || len(v.Invalid) == 0 || len(v.Segments) == 0 || len(v.BadSegments) == 0 {
		t.Fatal("channel-vectors.json lacks a list")
	}
	return v
}

// memberRoute is a join's or leave's route for channel: the vectors' segment for a reference,
// else the name itself.
func memberRoute(v channelVectors, session, channel string) string {
	segment, ok := v.Segments[channel]
	if !ok {
		segment = channel
	}
	return "/channels/" + segment + "/members/" + session
}

func TestANewChannelTakesEveryValidName(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	v := readChannelVectors(t)
	for _, ch := range v.Valid {
		expect(t, a.fleet("PUT", memberRoute(v, ann, ch), "", 200).body["joined"], ch)
		if got := a.send(ann, ch, "hi"); got.status != 200 {
			t.Errorf("a post to %q: %d %s", ch, got.status, got.raw)
		}
		expect(t, a.count(`SELECT count(*) FROM fleet_members WHERE channel = $1`, ch), 1)
		expect(t, a.fleet("DELETE", memberRoute(v, ann, ch), "", 200).body["left"], ch)
	}
	// A plain name sent encoded is the same channel.
	a.fleet("PUT", "/channels/agent-fleet/members/"+ann, "", 200)
	a.fleet("DELETE", "/channels/~"+base64.RawURLEncoding.EncodeToString([]byte("agent-fleet"))+"/members/"+ann, "", 200)
	expect(t, a.count(`SELECT count(*) FROM fleet_members WHERE channel = 'agent-fleet'`), 0)
}

func TestNoChannelTakesAnInvalidName(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	v := readChannelVectors(t)
	for _, ch := range v.Invalid {
		route := fleetBase + "/channels/~" + base64.RawURLEncoding.EncodeToString([]byte(ch)) + "/members/" + ann
		if got := a.do("PUT", route, ""); got.status != 422 {
			t.Errorf("a join of %q: %d %s; want 422", ch, got.status, got.raw)
		}
		if got := a.send(ann, ch, "hi"); ch != "" && got.status != 422 {
			t.Errorf("a post to %q: %d %s; want 422", ch, got.status, got.raw)
		}
	}
	// A segment that needs no escape reaches core, which refuses it; one with an escape in
	// it goes to Python, so internal/fleet's own test holds channelOf to every bad segment.
	for _, seg := range v.BadSegments {
		if url.PathEscape(seg) != seg {
			continue
		}
		if got := a.do("PUT", fleetBase+"/channels/"+seg+"/members/"+ann, ""); got.status != 422 {
			t.Errorf("a join of segment %q: %d %s; want 422", seg, got.status, got.raw)
		}
	}
	expect(t, a.count(`SELECT count(*) FROM fleet_channels`), 0)
}

func TestAnOldStyleNameIsKeptOnlyByAChannelMadeBefore(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	v := readChannelVectors(t)
	for _, ch := range v.Legacy {
		refused := a.fleet("PUT", memberRoute(v, ann, ch), "", 422)
		expect(t, refused.body["reason"], "legacy-channel-name")
		if _, err := a.pool.Exec(context.Background(), `INSERT INTO fleet_channels (name) VALUES ($1)`, ch); err != nil {
			t.Fatalf("a channel made before #8149, %q: %v", ch, err)
		}
		a.fleet("PUT", memberRoute(v, ann, ch), "", 200)
		if got := a.send(ann, ch, "hi"); got.status != 200 {
			t.Errorf("a post to %q: %d %s", ch, got.status, got.raw)
		}
		a.fleet("DELETE", memberRoute(v, ann, ch), "", 200)
	}
}

func TestTheChannelCheckHoldsToTheVectors(t *testing.T) {
	a := newFleet(t)
	ctx := context.Background()
	v := readChannelVectors(t)
	for _, ch := range append(append([]string{}, v.Valid...), v.Legacy...) {
		if _, err := a.pool.Exec(ctx, `INSERT INTO fleet_channels (name) VALUES ($1)`, ch); err != nil {
			t.Errorf("the CHECK refused %q: %v", ch, err)
		}
	}
	for _, ch := range v.Invalid {
		_, err := a.pool.Exec(ctx, `INSERT INTO fleet_channels (name) VALUES ($1)`, ch)
		var pg *pgconn.PgError
		if !errors.As(err, &pg) || pg.Code != "23514" {
			t.Errorf("the CHECK took %q: %v", ch, err)
		}
	}
}
