// Package testdb gives a test a database of its own, migrated by the backend's own files.
package testdb

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Env names the Postgres server the tests use, as the backend's tests do.
const Env = "SCADBUDDY_TEST_DATABASE_URL"

// RequiredEnv makes a missing server a failure, for CI.
const RequiredEnv = "SCADBUDDY_TEST_POSTGRES_REQUIRED"

var (
	once     sync.Once
	template string
	buildErr error
)

// New is a pool on a fresh copy of the migrated database, dropped when the test ends.
func New(t *testing.T) *pgxpool.Pool {
	t.Helper()
	server := os.Getenv(Env)
	if server == "" {
		if os.Getenv(RequiredEnv) != "" {
			t.Fatalf("%s is not set", Env)
		}
		t.Skipf("%s is not set", Env)
	}
	once.Do(func() { template, buildErr = build(server, true) })
	if buildErr != nil {
		t.Fatal(buildErr)
	}
	name := "core_test_" + random()
	ctx := context.Background()
	if err := admin(ctx, server, "CREATE DATABASE "+name+" TEMPLATE "+template); err != nil {
		t.Fatal(err)
	}
	pool, err := pgxpool.New(ctx, with(server, name))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Close()
		if err := admin(ctx, server, "DROP DATABASE IF EXISTS "+name+" WITH (FORCE)"); err != nil {
			t.Error(err)
		}
	})
	return pool
}

// Migrated is a pool on a database of its own, migrated from nothing as the API migrates
// one: its event log keeps what the migrations published, which New's template drops. With
// before set (a migration file's id), it stops at the first file whose id is not less than
// before, and rest applies the remaining files, so a test can seed rows that an older build
// wrote. It is slower than New; it is for a test of what the migrations write.
func Migrated(t *testing.T, before string) (pool *pgxpool.Pool, rest func()) {
	t.Helper()
	server := os.Getenv(Env)
	if server == "" {
		if os.Getenv(RequiredEnv) != "" {
			t.Fatalf("%s is not set", Env)
		}
		t.Skipf("%s is not set", Env)
	}
	ctx := context.Background()
	name := "core_test_" + random()
	if err := admin(ctx, server, "CREATE DATABASE "+name); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := admin(ctx, server, "DROP DATABASE IF EXISTS "+name+" WITH (FORCE)"); err != nil {
			t.Error(err)
		}
	})
	if err := migrate(ctx, with(server, name), before); err != nil {
		t.Fatal(err)
	}
	pool, err := pgxpool.New(ctx, with(server, name))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return pool, func() {
		t.Helper()
		if err := migrate(ctx, with(server, name), ""); err != nil {
			t.Fatal(err)
		}
	}
}

// Main runs a package's tests and drops the template database after them.
func Main(m *testing.M) {
	code := m.Run()
	if template != "" {
		_ = admin(context.Background(), os.Getenv(Env), "DROP DATABASE IF EXISTS "+template+" WITH (FORCE)")
	}
	os.Exit(code)
}

// build migrates a template database. With emptyLog, the event log is emptied after it.
func build(server string, emptyLog bool) (string, error) {
	name := "core_template_" + random()
	ctx := context.Background()
	if err := admin(ctx, server, "CREATE DATABASE "+name); err != nil {
		return "", err
	}
	if err := migrate(ctx, with(server, name), ""); err != nil || !emptyLog {
		return name, err
	}
	// The migrations publish history events (#6949: the property history's back-fill, the
	// item kinds' schemas). A test starts with the log as it was before them: empty, from
	// seq 1, behind the history subscriber's cursor.
	return name, admin(ctx, with(server, name), "TRUNCATE events RESTART IDENTITY")
}

// migrate applies the backend's files, then the agent's, that its ledgers do not list, up
// to the first whose id is not less than before (every one when before is "").
func migrate(ctx context.Context, url, before string) error {
	_, file, _, _ := runtime.Caller(0)
	backend := filepath.Join(filepath.Dir(file), "../../../../../backend/scadbuddy")
	conn, err := pgx.Connect(ctx, url)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	for _, l := range [][2]string{
		{"migrations", "scadbuddy_migrations"},
		{"migrations_agent", "ai_migrations"},
	} {
		dir, ledger := l[0], l[1]
		_, err := conn.Exec(ctx, "CREATE TABLE IF NOT EXISTS "+ledger+" (id text PRIMARY KEY, version integer UNIQUE,"+
			" story text, checksum text, applied_at timestamptz NOT NULL DEFAULT now())")
		if err != nil {
			return err
		}
		files, err := filepath.Glob(filepath.Join(backend, dir, "*.sql"))
		if err != nil {
			return err
		}
		slices.Sort(files)
		for _, f := range files {
			id := strings.TrimSuffix(filepath.Base(f), ".sql")
			if before != "" && id >= before {
				break
			}
			var applied bool
			if err := conn.QueryRow(ctx, "SELECT EXISTS (SELECT 1 FROM "+ledger+" WHERE id = $1)", id).Scan(&applied); err != nil {
				return err
			}
			if applied {
				continue
			}
			sql, err := os.ReadFile(f)
			if err != nil {
				return err
			}
			if _, err := conn.Exec(ctx, string(sql), pgx.QueryExecModeSimpleProtocol); err != nil {
				return &migrationError{filepath.Base(f), err}
			}
			if _, err := conn.Exec(ctx, "INSERT INTO "+ledger+" (id) VALUES ($1)", id); err != nil {
				return err
			}
		}
	}
	return nil
}

type migrationError struct {
	file string
	err  error
}

func (e *migrationError) Error() string { return e.file + ": " + e.err.Error() }

func admin(ctx context.Context, server, sql string) error {
	conn, err := pgx.Connect(ctx, server)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	_, err = conn.Exec(ctx, sql, pgx.QueryExecModeSimpleProtocol)
	return err
}

func with(server, database string) string {
	u, err := url.Parse(server)
	if err != nil {
		panic(err)
	}
	u.Path = "/" + database
	return u.String()
}

func random() string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
