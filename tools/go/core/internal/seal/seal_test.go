package seal

import (
	"bytes"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// The agent's vector key (secret-vectors.json): 32 bytes of 0x5a.
const kekB64 = "WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo="

func fixture(t *testing.T, name string, v any) {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "../../../../../backend/tests/fixtures", name))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, v); err != nil {
		t.Fatal(err)
	}
}

func b64(t *testing.T, s string) []byte {
	t.Helper()
	out, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func vectorKEK(t *testing.T) *KEK {
	t.Helper()
	kek, err := FromBase64(kekB64)
	if err != nil {
		t.Fatal(err)
	}
	return kek
}

// counter hands out 0, 1, 2, ... as bytes: the agent's vector randomness.
type counter struct{ n int }

func (c *counter) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = byte(c.n + i)
	}
	c.n += len(p)
	return len(p), nil
}

func TestAValueTheAgentSealedOpensAndSealsAgainToTheSameBytes(t *testing.T) {
	kek := vectorKEK(t)
	want := Envelope{
		SecretSealed: b64(t, "AiAhIiMkJSYnKCkqK3JaZFmcgKiNpJqysgB/9HKwW8sSGfx+dzcKJ621d4bUuyyVsbexUd1Yw1olcbM="),
		DEKSealed:    b64(t, "AiwtLi8wMTIzNDU2N0a74FVqig/eVLPOgS1WBzZmdZ0BKUM5gwTMoHY8irjmCYNCQl3e8blqnyyzZhQCag=="),
		KEKID:        "60bf07c488aad18f",
	}
	const plain, aad = "bambuddy-vector-key-0123456789", "settings:bambuddy_api_key"
	got, err := OpenSecret(kek, want, aad)
	if err != nil || got != plain {
		t.Fatalf("%q %v", got, err)
	}
	again := sealSecretWith(kek, plain, aad, &counter{})
	if !bytes.Equal(again.SecretSealed, want.SecretSealed) || !bytes.Equal(again.DEKSealed, want.DEKSealed) ||
		again.KEKID != want.KEKID {
		t.Fatal("the seal is not the agent's")
	}
}

func TestEverySecretVectorOpens(t *testing.T) {
	var v struct {
		KEKB64      string `json:"kek_b64"`
		KEKID       string `json:"kek_id"`
		Credentials []struct {
			Name, ID, Kind, Plaintext string
			BaseURL                   *string `json:"base_url"`
			Secret                    string  `json:"secret_sealed_b64"`
			DEK                       string  `json:"dek_sealed_b64"`
			KEKID                     string  `json:"kek_id"`
			Opens                     bool
		}
		Secrets []struct {
			Name, AAD, Plaintext string
			Version              int
			Secret               string `json:"secret_sealed_b64"`
			DEK                  string `json:"dek_sealed_b64"`
			KEKID                string `json:"kek_id"`
		}
	}
	fixture(t, "secret-vectors.json", &v)
	kek, err := FromBase64(v.KEKB64)
	if err != nil || kek.ID != v.KEKID {
		t.Fatal(kek, err)
	}
	if len(v.Credentials) == 0 || len(v.Secrets) == 0 {
		t.Fatal("no vectors")
	}
	for _, c := range v.Credentials {
		base := "null"
		if c.BaseURL != nil {
			base = JSONString(*c.BaseURL)
		}
		aad := "ai_credentials:" + c.ID + `:{"kind":` + JSONString(c.Kind) + `,"base_url":` + base + "}"
		e := Envelope{b64(t, c.Secret), b64(t, c.DEK), c.KEKID}
		got, err := OpenSecret(kek, e, aad)
		// The credential store refuses a v1 row before it opens it (credentials.py open_row).
		opens := err == nil && got == c.Plaintext && e.SecretSealed[0] != V1
		if opens != c.Opens {
			t.Errorf("%s: opens %v, want %v (%v)", c.Name, opens, c.Opens, err)
		}
	}
	for _, s := range v.Secrets {
		e := Envelope{b64(t, s.Secret), b64(t, s.DEK), s.KEKID}
		if e.SecretSealed[0] != byte(s.Version) {
			t.Errorf("%s: version %d", s.Name, e.SecretSealed[0])
		}
		if got, err := OpenSecret(kek, e, s.AAD); err != nil || got != s.Plaintext {
			t.Errorf("%s: %q %v", s.Name, got, err)
		}
	}
}

type payloadView struct {
	Metadata map[string]string `json:"metadata"`
	Data     string            `json:"data"`
}

func (v payloadView) payload(t *testing.T) Payload {
	p := Payload{Metadata: map[string][]byte{}, Data: b64(t, v.Data)}
	for k, m := range v.Metadata {
		p.Metadata[k] = b64(t, m)
	}
	return p
}

func samePayload(a, b Payload) bool {
	if !bytes.Equal(a.Data, b.Data) || len(a.Metadata) != len(b.Metadata) {
		return false
	}
	for k, v := range a.Metadata {
		if !bytes.Equal(v, b.Metadata[k]) {
			return false
		}
	}
	return true
}

func TestEveryPayloadVectorOpensAndSealsAgainToTheSameBytes(t *testing.T) {
	var v struct {
		KEKB64   string `json:"kek_b64"`
		KEKID    string `json:"kek_id"`
		Subject  string
		DEK      string `json:"dek_sealed_b64"`
		Payloads []struct {
			Name    string
			Plain   payloadView
			IV      string `json:"iv_b64"`
			Encoded payloadView
		}
	}
	fixture(t, "payload-vectors.json", &v)
	kek, err := FromBase64(v.KEKB64)
	if err != nil || kek.ID != v.KEKID {
		t.Fatal(kek, err)
	}
	dek, err := OpenBytes(kek.Key(), b64(t, v.DEK), PayloadKeyContext(v.Subject))
	if err != nil {
		t.Fatal(err)
	}
	if len(v.Payloads) == 0 {
		t.Fatal("no vectors")
	}
	for _, p := range v.Payloads {
		encoded, plain := p.Encoded.payload(t), p.Plain.payload(t)
		if SealedSubject(encoded) != v.Subject {
			t.Errorf("%s: subject %q", p.Name, SealedSubject(encoded))
		}
		opened, err := OpenPayload(dek, v.Subject, encoded)
		if err != nil || !samePayload(opened, plain) {
			t.Errorf("%s: does not open (%v)", p.Name, err)
		}
		if again := SealPayload(dek, v.Subject, plain, b64(t, p.IV)); !samePayload(again, encoded) {
			t.Errorf("%s: sealed again to other bytes", p.Name)
		}
	}
}

func TestEveryWorkflowIDNamesTheSubjectTheAgentNames(t *testing.T) {
	var v []struct {
		WorkflowID string  `json:"workflow_id"`
		Subject    *string `json:"subject"`
	}
	fixture(t, "subject-vectors.json", &v)
	if len(v) == 0 {
		t.Fatal("no vectors")
	}
	for _, c := range v {
		want := ""
		if c.Subject != nil {
			want = *c.Subject
		}
		if got := SubjectOf(c.WorkflowID); got != want {
			t.Errorf("%s: %q, want %q", c.WorkflowID, got, want)
		}
	}
}

func TestEveryApprovalHashVectorHashesAsTheAgentDoes(t *testing.T) {
	var v struct {
		KEK     string `json:"kek_hex"`
		HashKey string `json:"hash_key_hex"`
		Cases   []struct {
			Tool, Canonical, Hash string
			Input                 string `json:"input_json"`
		}
	}
	fixture(t, "approval-hash-vectors.json", &v)
	raw, _ := hex.DecodeString(v.KEK)
	key := InputHashKey(NewKEK(raw))
	if hex.EncodeToString(key) != v.HashKey {
		t.Fatalf("key %x", key)
	}
	if len(v.Cases) == 0 {
		t.Fatal("no vectors")
	}
	for _, c := range v.Cases {
		canonical, err := Canonical(`{"tool":`+JSONString(c.Tool)+`,"input":`+c.Input+`}`, true)
		if err != nil || canonical != c.Canonical {
			t.Errorf("%s:\n got %s\nwant %s (%v)", c.Input, canonical, c.Canonical, err)
		}
		if got, err := InputHash(key, c.Tool, c.Input); err != nil || got != c.Hash {
			t.Errorf("%s: hash %s (%v)", c.Input, got, err)
		}
	}
	a, _ := InputHash(key, "render", `{"n":1}`)
	b, _ := InputHash(key, "render", `{"n":2}`)
	c, _ := InputHash(key, "write", `{"n":1}`)
	if a == b || a == c {
		t.Fatal("another input hashes the same")
	}
}

func TestEveryResponseHashVectorHashesAsTheAgentDoes(t *testing.T) {
	var v struct {
		Vectors []struct {
			Name, Canonical, Sha256 string
			Value                   json.RawMessage
		}
	}
	fixture(t, "response-hash-vectors.json", &v)
	if len(v.Vectors) == 0 {
		t.Fatal("no vectors")
	}
	for _, c := range v.Vectors {
		canonical, err := Canonical(string(c.Value), false)
		if err != nil || canonical != c.Canonical {
			t.Errorf("%s:\n got %s\nwant %s (%v)", c.Name, canonical, c.Canonical, err)
		}
		if got, err := ResponseHash(string(c.Value)); err != nil || got != c.Sha256 {
			t.Errorf("%s: hash %s (%v)", c.Name, got, err)
		}
	}
	for _, n := range []string{"1", "-0.0", "1e21", "1.5"} {
		if _, err := Canonical(`{"kind":"answer","n":`+n+`}`, false); !errors.Is(err, ErrNumber) {
			t.Errorf("%s: %v", n, err)
		}
	}
	if got, _ := Canonical("[true,false]", false); got != "[true,false]" {
		t.Error(got)
	}
}

func TestASealedSecretRoundTripsAndDiffersEachTime(t *testing.T) {
	kek := vectorKEK(t)
	first, second := SealSecret(kek, "k", "a"), SealSecret(kek, "k", "a")
	if bytes.Equal(first.SecretSealed, second.SecretSealed) || first.SecretSealed[0] != Version {
		t.Fatal("the seals are alike")
	}
	for _, e := range []Envelope{first, second} {
		if got, err := OpenSecret(kek, e, "a"); err != nil || got != "k" {
			t.Fatal(got, err)
		}
	}
	if _, err := OpenSecret(kek, first, "b"); err == nil {
		t.Fatal("the context is not authenticated")
	}
	first.SecretSealed[len(first.SecretSealed)-1] ^= 1
	if _, err := OpenSecret(kek, first, "a"); err == nil || !strings.Contains(err.Error(), "failed authentication") {
		t.Fatal(err)
	}
}

func TestAnotherKeyNamesBothIDsAndNothingElse(t *testing.T) {
	sealed := SealSecret(vectorKEK(t), "the-plaintext", "a")
	other := NewKEK(bytes.Repeat([]byte{1}, 32))
	_, err := OpenSecret(other, sealed, "a")
	if err == nil || !strings.Contains(err.Error(), sealed.KEKID) || !strings.Contains(err.Error(), other.ID) ||
		strings.Contains(err.Error(), "the-plaintext") {
		t.Fatal(err)
	}
}

func TestAMalformedKeyIsRefused(t *testing.T) {
	for _, text := range []string{"not base64!", base64.StdEncoding.EncodeToString([]byte("short")), strings.Repeat("a", 43)} {
		var ke *KeyError
		if _, err := FromBase64(text); !errors.As(err, &ke) || !strings.Contains(err.Error(), Variable) {
			t.Errorf("%q: %v", text, err)
		}
	}
}

func TestKeyFiles(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "secret.key")
	if err := os.WriteFile(path, []byte("\ufeff"+kekB64+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if kek, err := Load(path); err != nil || kek.ID != "60bf07c488aad18f" {
		t.Fatal(kek, err)
	}
	missing := filepath.Join(dir, "nowhere", "secret.key")
	if _, err := Load(missing); err == nil || !strings.Contains(err.Error(), Variable) ||
		strings.Contains(err.Error(), missing) {
		t.Fatal(err)
	}
	raw := filepath.Join(dir, "raw.key")
	_ = os.WriteFile(raw, bytes.Repeat([]byte("\xfe\xffsecretbytes"), 3), 0o600)
	if _, err := Load(raw); err == nil || strings.Contains(err.Error(), "secretbytes") {
		t.Fatal(err)
	}
	env := map[string]string{Variable: path}
	keys, err := LoadKeys(func(k string) string { return env[k] })
	if err != nil || keys.Current.ID != "60bf07c488aad18f" || keys.Previous != nil {
		t.Fatal(keys, err)
	}
	env[PreviousVariable] = missing
	if _, err := LoadKeys(func(k string) string { return env[k] }); err == nil ||
		!strings.Contains(err.Error(), PreviousVariable) {
		t.Fatal(err)
	}
	if keys, err := LoadKeys(func(string) string { return "" }); keys != nil || err != nil {
		t.Fatal(keys, err)
	}
}

func TestNumbersAreWrittenAsECMAScriptWritesThem(t *testing.T) {
	for x, want := range map[float64]string{
		1: "1", 1.5: "1.5", 1e21: "1e+21", 1e20: "100000000000000000000", 1e-7: "1e-7",
		0.0000123: "0.0000123", -2.5e-10: "-2.5e-10", 123456.789: "123456.789",
	} {
		if got := Number(x); got != want {
			t.Errorf("%v: %s, want %s", x, got, want)
		}
	}
}
