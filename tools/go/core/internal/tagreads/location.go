package tagreads

import (
	"encoding/json"
	"math"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// locationSkew is how far a position's time may be from now (the owner, 2026-11-27).
const locationSkew = 24 * time.Hour

// location is where a scan was made: the phone's position (navigator.geolocation), its
// accuracy in metres and when the phone took it.
type location struct {
	Lat       *float64   `json:"lat"`
	Lon       *float64   `json:"lon"`
	AccuracyM *float64   `json:"accuracy_m"`
	At        *time.Time `json:"at"`
}

// locationOf is the read's position, nil for none (the person refused it).
func locationOf(fields map[string]json.RawMessage, now time.Time) (*location, *httpx.Problem) {
	raw, ok := fields["location"]
	if !ok || isNull(raw) {
		return nil, nil
	}
	invalid := func(msg string) (*location, *httpx.Problem) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "location"}, Msg: msg})
	}
	var l location
	if err := json.Unmarshal(raw, &l); err != nil || l.Lat == nil || l.Lon == nil || l.At == nil {
		return invalid("location is {lat, lon, accuracy_m, at}")
	}
	finite := func(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
	switch {
	case !finite(*l.Lat) || *l.Lat < -90 || *l.Lat > 90:
		return invalid("lat is from -90 to 90")
	case !finite(*l.Lon) || *l.Lon < -180 || *l.Lon > 180:
		return invalid("lon is from -180 to 180")
	case l.AccuracyM != nil && (!finite(*l.AccuracyM) || *l.AccuracyM < 0 || *l.AccuracyM > 100000):
		return invalid("accuracy_m is from 0 to 100000")
	case l.At.Sub(now).Abs() > locationSkew:
		return invalid("at is within a day of now")
	}
	return &l, nil
}
