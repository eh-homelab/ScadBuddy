package fetchgrants

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

type vectors struct {
	KEK   string `json:"kek"`
	Hosts []struct {
		URL  string  `json:"url"`
		Host *string `json:"host"`
	} `json:"hosts"`
	Grants []struct {
		Host    string `json:"host"`
		URL     string `json:"url"`
		Expires int64  `json:"expires"`
		Nonce   string `json:"nonce"`
		Grant   string `json:"grant"`
	} `json:"grants"`
}

func readVectors(t *testing.T) vectors {
	t.Helper()
	raw, err := os.ReadFile("../../../../../backend/tests/fixtures/fetch-grant-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Hosts) == 0 || len(v.Grants) == 0 {
		t.Fatal("no vectors")
	}
	return v
}

func TestTheHostIsTheOneTheAssetRouteReads(t *testing.T) {
	for _, c := range readVectors(t).Hosts {
		host, refusal := URLHost(c.URL)
		switch {
		case c.Host == nil && refusal == "":
			t.Errorf("%q: host %q, want a refusal", c.URL, host)
		case c.Host != nil && refusal != "":
			t.Errorf("%q: refused (%s), want host %q", c.URL, refusal, *c.Host)
		case c.Host != nil && host != *c.Host:
			t.Errorf("%q: host %q, want %q", c.URL, host, *c.Host)
		}
	}
}

func TestAGrantIsMintedAsPythonMintsIt(t *testing.T) {
	v := readVectors(t)
	kek, err := seal.FromBase64(v.KEK)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range v.Grants {
		if got := Mint(Key(kek), c.Host, c.URL, c.Expires, c.Nonce); got != c.Grant {
			t.Errorf("%s %s: %s, want %s", c.Host, c.URL, got, c.Grant)
		}
	}
}
