package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

// Channel names (#8149), held to fleetchat's vectors: the routes and the fleet_channels CHECK.

type channelVectors struct{ Valid, Legacy, Invalid []string }

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
	if len(v.Valid) == 0 || len(v.Legacy) == 0 || len(v.Invalid) == 0 {
		t.Fatal("channel-vectors.json lacks a list")
	}
	return v
}

func memberRoute(session, channel string) string {
	return "/members/" + session + "?" + url.Values{"channel": {channel}}.Encode()
}

func TestANewChannelTakesEveryValidName(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	for _, ch := range readChannelVectors(t).Valid {
		expect(t, a.fleet("PUT", memberRoute(ann, ch), "", 200).body["joined"], ch)
		if got := a.send(ann, ch, "hi"); got.status != 200 {
			t.Errorf("a post to %q: %d %s", ch, got.status, got.raw)
		}
		expect(t, a.fleet("DELETE", memberRoute(ann, ch), "", 200).body["left"], ch)
	}
	// A plain name still goes in the path, as before.
	a.fleet("PUT", "/channels/agent-fleet/members/"+ann, "", 200)
	expect(t, a.count(`SELECT count(*) FROM fleet_members WHERE channel = 'agent-fleet'`), 1)
}

func TestNoChannelTakesAnInvalidName(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	for _, ch := range readChannelVectors(t).Invalid {
		if got := a.do("PUT", fleetBase+memberRoute(ann, ch), ""); got.status != 422 {
			t.Errorf("a join of %q: %d %s; want 422", ch, got.status, got.raw)
		}
		if got := a.send(ann, ch, "hi"); ch != "" && got.status != 422 {
			t.Errorf("a post to %q: %d %s; want 422", ch, got.status, got.raw)
		}
	}
	a.fleet("PUT", "/members/"+ann, "", 422)
	expect(t, a.count(`SELECT count(*) FROM fleet_channels`), 0)
}

func TestAnOldStyleNameIsKeptOnlyByAChannelMadeBefore(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	for _, ch := range readChannelVectors(t).Legacy {
		refused := a.fleet("PUT", memberRoute(ann, ch), "", 422)
		expect(t, refused.body["reason"], "legacy-channel-name")
		if _, err := a.pool.Exec(context.Background(), `INSERT INTO fleet_channels (name) VALUES ($1)`, ch); err != nil {
			t.Fatalf("a channel made before #8149, %q: %v", ch, err)
		}
		a.fleet("PUT", memberRoute(ann, ch), "", 200)
		if got := a.send(ann, ch, "hi"); got.status != 200 {
			t.Errorf("a post to %q: %d %s", ch, got.status, got.raw)
		}
		a.fleet("DELETE", memberRoute(ann, ch), "", 200)
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
