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
	once.Do(func() { template, buildErr = build(server) })
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

// Main runs a package's tests and drops the template database after them.
func Main(m *testing.M) {
	code := m.Run()
	if template != "" {
		_ = admin(context.Background(), os.Getenv(Env), "DROP DATABASE IF EXISTS "+template+" WITH (FORCE)")
	}
	os.Exit(code)
}

// build migrates a template database: the backend's files, then the agent's.
func build(server string) (string, error) {
	_, file, _, _ := runtime.Caller(0)
	backend := filepath.Join(filepath.Dir(file), "../../../../../backend/scadbuddy")
	name := "core_template_" + random()
	ctx := context.Background()
	if err := admin(ctx, server, "CREATE DATABASE "+name); err != nil {
		return "", err
	}
	conn, err := pgx.Connect(ctx, with(server, name))
	if err != nil {
		return name, err
	}
	defer conn.Close(ctx)
	for _, l := range [][2]string{
		{"migrations", "scadbuddy_migrations"},
		{"migrations_agent", "ai_migrations"},
	} {
		dir, ledger := l[0], l[1]
		_, err := conn.Exec(ctx, "CREATE TABLE "+ledger+" (id text PRIMARY KEY, version integer UNIQUE,"+
			" story text, checksum text, applied_at timestamptz NOT NULL DEFAULT now())")
		if err != nil {
			return name, err
		}
		files, err := filepath.Glob(filepath.Join(backend, dir, "*.sql"))
		if err != nil {
			return name, err
		}
		slices.Sort(files)
		for _, f := range files {
			sql, err := os.ReadFile(f)
			if err != nil {
				return name, err
			}
			if _, err := conn.Exec(ctx, string(sql), pgx.QueryExecModeSimpleProtocol); err != nil {
				return name, &migrationError{filepath.Base(f), err}
			}
			id := strings.TrimSuffix(filepath.Base(f), ".sql")
			if _, err := conn.Exec(ctx, "INSERT INTO "+ledger+" (id) VALUES ($1)", id); err != nil {
				return name, err
			}
		}
	}
	return name, nil
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
