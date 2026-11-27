package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// machineCredential is a credential row of caller bound to machine, as the exchange issues
// it for the proof file <caller>.<machine>; it answers the token and the row's id.
func machineCredential(t *testing.T, pool *pgxpool.Pool, caller, machine string) (token, id string) {
	t.Helper()
	raw, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(raw)
	_, _ = rand.Read(secret)
	id = hex.EncodeToString(raw)
	token = "sbsvc_" + id + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(token))
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof, machine)
		 VALUES ($1, $2, '{crud.fleet-rotator}', $3, now() + interval '15 minutes', 'bootstrap', $4)`,
		id, caller, hash[:], machine); err != nil {
		t.Fatal(err)
	}
	return token, id
}

func revoked(t *testing.T, pool *pgxpool.Pool, id string) bool {
	t.Helper()
	var at bool
	if err := pool.QueryRow(context.Background(),
		`SELECT revoked_at IS NOT NULL FROM service_credentials WHERE id = $1`, id).Scan(&at); err != nil {
		t.Fatal(err)
	}
	return at
}

func proofFile(t *testing.T, dir, name string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte("a-bootstrap-secret-of-at-least-32-characters"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// A fleet-rotator credential passes while its machine's proof file is there; once the file
// goes it is refused and revoked at once, as Python's resolve does (has_machine).
func TestAMachineCredentialNeedsItsProofFile(t *testing.T) {
	pool := testdb.New(t)
	dir := t.TempDir()
	path := proofFile(t, dir, "fleet-rotator.devbox")
	r := &Resolver{Pool: pool, BootstrapDir: dir}
	token, id := machineCredential(t, pool, "fleet-rotator", "devbox")
	s := r.Resolve(context.Background(), "Bearer "+token)
	if s == nil || s.Machine != "devbox" {
		t.Fatalf("with its proof file, the credential is %v", s)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if s := r.Resolve(context.Background(), "Bearer "+token); s != nil {
		t.Fatalf("without its proof file, the credential is %v", s)
	}
	if !revoked(t, pool, id) {
		t.Error("the refused credential was not revoked")
	}
	// Revoked, it stays refused when the file comes back.
	proofFile(t, dir, "fleet-rotator.devbox")
	if s := r.Resolve(context.Background(), "Bearer "+token); s != nil {
		t.Errorf("a revoked credential is %v", s)
	}
}

// With no bootstrap directory, every credential that names a machine is refused.
func TestAMachineCredentialWithNoBootstrapDirIsRefused(t *testing.T) {
	pool := testdb.New(t)
	r := &Resolver{Pool: pool}
	token, id := machineCredential(t, pool, "fleet-rotator", "devbox")
	if s := r.Resolve(context.Background(), "Bearer "+token); s != nil {
		t.Fatalf("with no bootstrap directory, the credential is %v", s)
	}
	if !revoked(t, pool, id) {
		t.Error("the refused credential was not revoked")
	}
}

// A directory in place of the proof file is no proof. (The table's CHECK keeps a machine to
// a fleet-rotator credential and to a machine's name; lookup checks the name again.)
func TestAMachineCredentialsProofMustBeAFile(t *testing.T) {
	pool := testdb.New(t)
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "fleet-rotator.folder"), 0o700); err != nil {
		t.Fatal(err)
	}
	r := &Resolver{Pool: pool, BootstrapDir: dir}
	token, id := machineCredential(t, pool, "fleet-rotator", "folder")
	if s := r.Resolve(context.Background(), "Bearer "+token); s != nil {
		t.Errorf("with a directory for its proof, the credential is %v", s)
	}
	if !revoked(t, pool, id) {
		t.Error("the refused credential was not revoked")
	}
}

func TestMachineProofName(t *testing.T) {
	for machine, want := range map[string]bool{
		"devbox": true, "a-1": true, "": false, "../render": false, "a/b": false, "..": false,
		"Devbox": false, "-a": false, "a.b": false,
	} {
		if _, ok := proofName("fleet-rotator", machine); ok != want {
			t.Errorf("%q: %v, want %v", machine, ok, want)
		}
	}
	if _, ok := proofName("render", "devbox"); ok {
		t.Error("render has one secret for all its pods, and no machine")
	}
}
