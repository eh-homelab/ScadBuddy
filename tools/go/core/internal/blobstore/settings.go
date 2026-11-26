package blobstore

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The store's settings, as backend/scadbuddy/library/settings_store.py resolves an
// env-seeded field: the value saved in Settings (its `settings` row), else the deployment's
// SCADBUDDY_<FIELD>, else the default. A saved JSON null clears a nullable field, and the
// environment does not override it. The secret access key is sealed under the KEK with
// the AAD "settings:<name>" (`_sealed`); one that does not open follows the environment, as
// Python's `_opened` drops it.
var settingNames = []string{
	"store_backend",
	"store_s3_endpoint",
	"store_s3_region",
	"store_s3_bucket",
	"store_s3_prefix",
	"store_s3_access_key_id",
	"store_s3_secret_access_key",
	"store_s3_path_style",
	"store_max_total_bytes",
	"store_max_count",
}

// nullable are the fields a saved null clears (core/settings.py: `str | None`).
var nullable = map[string]bool{
	"store_s3_endpoint": true, "store_s3_bucket": true, "store_s3_access_key_id": true,
	"store_s3_secret_access_key": true,
}

// defaults are core/settings.py's and core/config.py's (DEFAULT_STORE_MAX_*).
var defaults = map[string]string{
	"store_backend":         "local",
	"store_s3_region":       "us-east-1",
	"store_s3_prefix":       "scadbuddy",
	"store_s3_path_style":   "true",
	"store_max_total_bytes": strconv.FormatInt(50<<30, 10),
	"store_max_count":       "200000",
}

const secretField = "store_s3_secret_access_key"

// Store is the blob store as Python's API runs it.
type Store struct {
	// Backend is store_backend: local, bambuddy or s3.
	Backend string
	// S3 is set when Backend is s3 and every field the store needs is.
	S3 *S3
	// MaxTotalBytes and MaxCount are the caps; 0 is no limit.
	MaxTotalBytes int64
	MaxCount      int64
}

// loadStore reads the store's settings.
func loadStore(ctx context.Context, pool *pgxpool.Pool, keys *seal.Keys, env func(string) string) (*Store, error) {
	rows, err := pool.Query(ctx, "SELECT name, value FROM settings WHERE name = ANY($1)", settingNames)
	if err != nil {
		return nil, err
	}
	saved := map[string]json.RawMessage{}
	for rows.Next() {
		var name string
		var value json.RawMessage
		if err := rows.Scan(&name, &value); err != nil {
			rows.Close()
			return nil, err
		}
		saved[name] = value
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	value := func(name string) string {
		if raw, ok := saved[name]; ok {
			if v, ok := savedValue(name, raw, keys); ok {
				return v
			}
		}
		if env != nil {
			if v, ok := lookupEnv(env, "SCADBUDDY_"+strings.ToUpper(name)); ok {
				return v
			}
		}
		return defaults[name]
	}
	store := &Store{
		Backend:       value("store_backend"),
		MaxTotalBytes: atoiOr(value("store_max_total_bytes"), defaults["store_max_total_bytes"]),
		MaxCount:      atoiOr(value("store_max_count"), defaults["store_max_count"]),
	}
	s3 := S3{
		Endpoint:        value("store_s3_endpoint"),
		Region:          value("store_s3_region"),
		Bucket:          value("store_s3_bucket"),
		Prefix:          strings.Trim(value("store_s3_prefix"), "/"),
		AccessKeyID:     value("store_s3_access_key_id"),
		SecretAccessKey: value(secretField),
		PathStyle:       pyBool(value("store_s3_path_style")),
	}
	if store.Backend == "s3" && s3.Endpoint != "" && s3.Bucket != "" && s3.Prefix != "" &&
		s3.AccessKeyID != "" && s3.SecretAccessKey != "" {
		store.S3 = &s3
	}
	return store, nil
}

// lookupEnv is env's value of name; an empty one counts as set, as pydantic-settings reads it.
func lookupEnv(env func(string) string, name string) (string, bool) {
	v := env(name)
	return v, v != ""
}

// savedValue is a saved row's value as text, ok false when it falls back to the environment.
func savedValue(name string, raw json.RawMessage, keys *seal.Keys) (string, bool) {
	if string(raw) == "null" {
		// A clear of a nullable field; Python refuses a null for any other.
		return "", nullable[name]
	}
	if name == secretField {
		var sealed struct {
			Sealed *struct {
				Secret string `json:"secret"`
				DEK    string `json:"dek"`
				KEKID  string `json:"kek_id"`
			} `json:"sealed"`
		}
		if json.Unmarshal(raw, &sealed) == nil && sealed.Sealed != nil {
			return openSecret(name, sealed.Sealed.Secret, sealed.Sealed.DEK, sealed.Sealed.KEKID, keys)
		}
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s, true
	}
	var b bool
	if json.Unmarshal(raw, &b) == nil {
		return strconv.FormatBool(b), true
	}
	var n json.Number
	if json.Unmarshal(raw, &n) == nil {
		return n.String(), true
	}
	return "", false
}

func openSecret(name, secret, dek, kekID string, keys *seal.Keys) (string, bool) {
	if keys == nil {
		return "", false
	}
	kek := keys.Opener(kekID)
	if kek == nil {
		return "", false
	}
	secretBytes, err1 := base64.StdEncoding.DecodeString(secret)
	dekBytes, err2 := base64.StdEncoding.DecodeString(dek)
	if err1 != nil || err2 != nil {
		return "", false
	}
	plain, err := seal.OpenSecret(kek, seal.Envelope{SecretSealed: secretBytes, DEKSealed: dekBytes, KEKID: kekID},
		"settings:"+name)
	if err != nil {
		return "", false
	}
	return plain, true
}

// pyBool reads a boolean as pydantic does.
func pyBool(s string) bool {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "1", "true", "t", "yes", "y", "on":
		return true
	}
	return false
}

// atoiOr is s as a number, else fallback's.
func atoiOr(s, fallback string) int64 {
	n, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64)
	if err != nil {
		n, _ = strconv.ParseInt(fallback, 10, 64)
	}
	return n
}
