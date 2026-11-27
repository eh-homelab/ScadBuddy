package plugins

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The vectors Python writes from its own functions (backend/tests/test_plugin_seal_vectors.py,
// UPDATE_PLUGIN_SEAL_VECTORS=1): the AADs, the plaintexts and envelopes sealed by Python.
const vectorsFile = "../../../../../backend/tests/fixtures/plugin-seal-vectors.json"

type sealedVector struct {
	AAD          string `json:"aad"`
	Plaintext    string `json:"plaintext"`
	SecretSealed string `json:"secret_sealed_b64"`
	DEKSealed    string `json:"dek_sealed_b64"`
	KEKID        string `json:"kek_id"`
}

func (v sealedVector) envelope(t *testing.T) seal.Envelope {
	t.Helper()
	secret, err := base64.StdEncoding.DecodeString(v.SecretSealed)
	if err != nil {
		t.Fatal(err)
	}
	dek, err := base64.StdEncoding.DecodeString(v.DEKSealed)
	if err != nil {
		t.Fatal(err)
	}
	return seal.Envelope{SecretSealed: secret, DEKSealed: dek, KEKID: v.KEKID}
}

type vectors struct {
	KEK     string `json:"kek_b64"`
	KEKID   string `json:"kek_id"`
	Plugins []struct {
		sealedVector
		Name       string `json:"name"`
		URL        string `json:"url"`
		AuthHeader string `json:"auth_header"`
		Secret     string `json:"secret"`
	} `json:"plugins"`
	Grants []struct {
		sealedVector
		Plugin        string      `json:"plugin"`
		PluginURL     string      `json:"plugin_url"`
		Resource      string      `json:"resource"`
		Issuer        string      `json:"issuer"`
		TokenEndpoint string      `json:"token_endpoint"`
		ClientID      string      `json:"client_id"`
		Secret        grantSecret `json:"secret"`
	} `json:"grants"`
	Flows []struct {
		sealedVector
		StateHash string          `json:"state_hash"`
		Plugin    string          `json:"plugin"`
		PluginURL string          `json:"plugin_url"`
		Meta      json.RawMessage `json:"meta"`
		Secret    flowSecret      `json:"secret"`
	} `json:"flows"`
}

func loadVectors(t *testing.T) (vectors, *seal.KEK) {
	t.Helper()
	raw, err := os.ReadFile(vectorsFile)
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	kek, err := seal.FromBase64(v.KEK)
	if err != nil {
		t.Fatal(err)
	}
	if kek.ID != v.KEKID || len(v.Plugins) == 0 || len(v.Grants) == 0 || len(v.Flows) == 0 {
		t.Fatalf("the vectors are not the file Python writes")
	}
	return v, kek
}

func checkOpens(t *testing.T, s *Plugins, v sealedVector, aad, text string) {
	t.Helper()
	if aad != v.AAD {
		t.Fatalf("AAD %q, Python writes %q", aad, v.AAD)
	}
	if text != v.Plaintext {
		t.Fatalf("plaintext %q, Python writes %q", text, v.Plaintext)
	}
	opened, err := s.open(v.envelope(t), aad)
	if err != nil || opened != v.Plaintext {
		t.Fatalf("Python's envelope does not open: %v", err)
	}
	// What Go seals under the AAD opens as Python's does (the format is seal's, held to
	// secret-vectors.json).
	mine := seal.SealSecret(s.Keys.Current, text, aad)
	if back, err := seal.OpenSecret(s.Keys.Current, mine, v.AAD); err != nil || back != text {
		t.Fatalf("Go's envelope does not open under Python's AAD: %v", err)
	}
}

func TestPythonsPluginSealsOpenUnderTheSameAADs(t *testing.T) {
	v, kek := loadVectors(t)
	s := &Plugins{Keys: &seal.Keys{Current: kek}}
	for _, p := range v.Plugins {
		checkOpens(t, s, p.sealedVector, pluginAAD(p.Name, p.URL, p.AuthHeader), p.Secret)
	}
	for _, g := range v.Grants {
		aad := grantAAD(grantKey{g.Plugin, g.PluginURL, g.Resource, g.Issuer, g.TokenEndpoint, g.ClientID})
		checkOpens(t, s, g.sealedVector, aad, g.Secret.text())
		back, err := parseGrantSecret(g.Plaintext)
		if err != nil || back.text() != g.Plaintext {
			t.Fatalf("the opened grant does not read back: %v", err)
		}
	}
	for _, f := range v.Flows {
		var key flowKey
		if err := json.Unmarshal(f.Meta, &key); err != nil {
			t.Fatal(err)
		}
		checkOpens(t, s, f.sealedVector, flowAAD(f.StateHash, f.Plugin, f.PluginURL, key), f.Secret.text())
		back, err := parseFlowSecret(f.Plaintext)
		if err != nil || back.text() != f.Plaintext {
			t.Fatalf("the opened flow does not read back: %v", err)
		}
	}
}

func TestAnOpenedSecretOfAnotherShapeIsAnErrorThatNamesNothing(t *testing.T) {
	for _, text := range []string{`[]`, `{"access_token":""}`, `{"access_token":"a","x":1}`, `nope`} {
		if _, err := parseGrantSecret(text); err != errOpenedShape {
			t.Fatalf("%s: %v", text, err)
		}
	}
}
