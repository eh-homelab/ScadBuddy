// Package packages is the agent's plugin packages: backend/scadbuddy/api/
// crud_agent_packages.py, statement for statement (areas plugin-packages, plugin-feeds and
// the read of hosted-plugins). Each write takes the package row's FOR UPDATE, prunes what
// the current review no longer has (agent_data/packages.py prune_stale_rows), releases the
// mirror holds no row names and sends the warm-evict NOTIFY, in one transaction; once it
// commits, plugin_packages.changed goes out on the event bus. The record of a commit pin's
// mirror (#7907, POST /plugin-packages/{name}/mirror) changes no pin: it only releases the
// holds no row names.
//
// Still Python's: area package-mirrors and a hosted version's zip (the blob store).
package packages

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

const (
	urlMax  = 2048
	textMax = 1024
	// The largest JSON text of a pin's file list and of its review.
	filesJSONMax  = 16 * 1024 * 1024
	reviewJSONMax = 4 * 1024 * 1024
	feedPackageQ  = 64
	bodyBytesMax  = 8 * 1024 * 1024 // api/limits.py MAX_TEXT_BODY_BYTES
	// announceTimeout bounds a write's event once the write has committed.
	announceTimeout = 10 * time.Second

	packageExists         = "package-exists"
	packageNotFound       = "package-not-found"
	packagePinned         = "package-pinned"
	packageNameDiffers    = "package-name-differs"
	pinMismatch           = "pin-mismatch"
	refusedNeedsAllow     = "refused-needs-allow"
	packageUnapproved     = "package-unapproved"
	mirrorMissing         = "mirror-missing"
	feedNotFound          = "feed-not-found"
	hostedVersionMismatch = "hosted-version-mismatch"
	hostedNameTaken       = "hosted-name-taken"

	// mirrorHolder is agent_data/package_mirrors.py HOLDER; unnamedGrace its UNNAMED_GRACE.
	mirrorHolder = "plugin_package_mirror"
	unnamedGrace = "1 day"
)

var (
	namePattern   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
	commitPattern = regexp.MustCompile(`^([0-9a-f]{40}|[0-9a-f]{64})$`)
	hashPattern   = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	sha256Pattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
	feedPattern   = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	sourceKinds   = []string{"git", "marketplace", "marketplace-url", "archive", "npm", "hosted"}
	pinKinds      = []string{"commit", "bytes", "version"}
	// pinField is the column each pin kind names its pin by.
	pinField = map[string]string{"commit": "commit_sha", "bytes": "bytes_sha256", "version": "version"}
)

// pinFits is the table's ai_plugin_packages_source_pin_check: which pin a source can have.
func pinFits(source, pin string) bool {
	switch source {
	case "git":
		return pin == "commit"
	case "archive", "npm":
		return pin == "bytes"
	case "hosted":
		return pin == "version"
	}
	return pin == "commit" || pin == "bytes"
}

func marketplaceKind(k string) bool { return k == "marketplace" || k == "marketplace-url" }

// refKind is a kind whose ref names what was asked for (a branch, tag or commit).
func refKind(k string) bool { return k == "git" || k == "marketplace" }

// Packages is the areas' routes.
type Packages struct{ Pool *pgxpool.Pool }

// Areas are the package's routes by area name.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	s := &Packages{Pool: pool}
	return map[string][]crud.Route{
		"plugin-packages:read": s.Reads(),
		"plugin-packages":      append(s.Reads(), s.Writes()...),
		"hosted-plugins:read":  s.Hosted(),
		"plugin-feeds:read":    s.FeedReads(),
		"plugin-feeds":         s.Feeds(),
	}
}

// Reads are area plugin-packages' GETs.
func (s *Packages) Reads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/plugin-packages", Handle: s.list},
		{Method: "GET", Path: "/plugin-packages/{name}", Handle: s.one},
	}
}

// Writes are area plugin-packages' POSTs.
func (s *Packages) Writes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/plugin-packages", Handle: s.install, MaxBody: bodyBytesMax, Status: http.StatusCreated},
		{Method: "POST", Path: "/plugin-packages/{name}/pending", Handle: s.pending, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/plugin-packages/{name}/discard-pending", Handle: s.discard},
		{Method: "POST", Path: "/plugin-packages/{name}/approve", Handle: s.approve, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/plugin-packages/{name}/enabled", Handle: s.enabled, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/plugin-packages/{name}/delete", Handle: s.delete},
		{Method: "POST", Path: "/plugin-packages/{name}/mirror", Handle: s.mirror, MaxBody: bodyBytesMax},
	}
}

// FeedReads are area plugin-feeds' GET.
func (s *Packages) FeedReads() []crud.Route {
	return []crud.Route{{Method: "GET", Path: "/plugin-feeds/{feed}", Handle: s.feed}}
}

// Feeds is area plugin-feeds.
func (s *Packages) Feeds() []crud.Route {
	return append(s.FeedReads(),
		crud.Route{Method: "PUT", Path: "/plugin-feeds/{feed}/packages/{name}", Handle: s.feedAdd},
		crud.Route{Method: "DELETE", Path: "/plugin-feeds/{feed}/packages/{name}", Handle: s.feedRemove},
	)
}

// Hosted is area hosted-plugins' read of a plugin; a version's zip is still Python's.
func (s *Packages) Hosted() []crud.Route {
	return []crud.Route{{Method: "GET", Path: "/hosted-plugins/{name}", Handle: s.hosted}}
}

// columns are every column of ai_plugin_packages, in row's order: Python's SELECT *.
const columns = `name, source_kind, source_url, source_ref, marketplace_entry, fetch_url, fetch_path,
	pin_kind, commit_sha, bytes_sha256, version, content_hash, files, review, approved_at,
	allow_refused, enabled, pending_ref, pending_fetch_url, pending_fetch_path, pending_pin_kind,
	pending_commit_sha, pending_bytes_sha256, pending_version, pending_content_hash, pending_files,
	pending_review, mirror_sha256, pending_mirror_sha256, created_at, updated_at`

// row is one ai_plugin_packages row, every column, as every route answers it.
type row struct {
	Name               string          `json:"name"`
	SourceKind         string          `json:"source_kind"`
	SourceURL          string          `json:"source_url"`
	SourceRef          string          `json:"source_ref"`
	MarketplaceEntry   *string         `json:"marketplace_entry"`
	FetchURL           string          `json:"fetch_url"`
	FetchPath          string          `json:"fetch_path"`
	PinKind            string          `json:"pin_kind"`
	CommitSHA          *string         `json:"commit_sha"`
	BytesSHA256        *string         `json:"bytes_sha256"`
	Version            *int64          `json:"version"`
	ContentHash        string          `json:"content_hash"`
	Files              json.RawMessage `json:"files"`
	Review             json.RawMessage `json:"review"`
	ApprovedAt         *wire.Time      `json:"approved_at"`
	AllowRefused       bool            `json:"allow_refused"`
	Enabled            bool            `json:"enabled"`
	PendingRef         *string         `json:"pending_ref"`
	PendingFetchURL    *string         `json:"pending_fetch_url"`
	PendingFetchPath   *string         `json:"pending_fetch_path"`
	PendingPinKind     *string         `json:"pending_pin_kind"`
	PendingCommitSHA   *string         `json:"pending_commit_sha"`
	PendingBytesSHA256 *string         `json:"pending_bytes_sha256"`
	PendingVersion     *int64          `json:"pending_version"`
	PendingContentHash *string         `json:"pending_content_hash"`
	PendingFiles       json.RawMessage `json:"pending_files"`
	PendingReview      json.RawMessage `json:"pending_review"`
	// MirrorSHA256 is a commit pin's mirror (#7907): the sha256 of a zip of its files.
	MirrorSHA256        *string   `json:"mirror_sha256"`
	PendingMirrorSHA256 *string   `json:"pending_mirror_sha256"`
	CreatedAt           wire.Time `json:"created_at"`
	UpdatedAt           wire.Time `json:"updated_at"`
	// HostedLatestVersion is a hosted package's plugin's latest_version: a version neither
	// the pin nor the re-pin names is "not offered".
	HostedLatestVersion *int64 `json:"hosted_latest_version"`
}

func (r *row) scan(rows pgx.CollectableRow) error {
	return rows.Scan(&r.Name, &r.SourceKind, &r.SourceURL, &r.SourceRef, &r.MarketplaceEntry,
		&r.FetchURL, &r.FetchPath, &r.PinKind, &r.CommitSHA, &r.BytesSHA256, &r.Version,
		&r.ContentHash, (*[]byte)(&r.Files), (*[]byte)(&r.Review), &r.ApprovedAt, &r.AllowRefused,
		&r.Enabled, &r.PendingRef, &r.PendingFetchURL, &r.PendingFetchPath, &r.PendingPinKind,
		&r.PendingCommitSHA, &r.PendingBytesSHA256, &r.PendingVersion, &r.PendingContentHash,
		(*[]byte)(&r.PendingFiles), (*[]byte)(&r.PendingReview), &r.MirrorSHA256, &r.PendingMirrorSHA256,
		&r.CreatedAt, &r.UpdatedAt)
}

// query is the rows a statement gives, each with its hosted plugin's latest version.
func query(ctx context.Context, tx pgx.Tx, sql string, args ...any) ([]*row, error) {
	rows, err := tx.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, func(rows pgx.CollectableRow) (*row, error) {
		r := &row{}
		return r, r.scan(rows)
	})
	if err != nil {
		return nil, err
	}
	var hosted []string
	for _, r := range out {
		if r.SourceKind == "hosted" {
			hosted = append(hosted, r.Name)
		}
	}
	if len(hosted) == 0 {
		return out, nil
	}
	latest := map[string]*int64{}
	found, err := tx.Query(ctx, `SELECT name, latest_version FROM ai_hosted_plugins WHERE name = ANY($1)`, hosted)
	if err != nil {
		return nil, err
	}
	var name string
	var version *int64
	if _, err := pgx.ForEachRow(found, []any{&name, &version}, func() error {
		latest[name] = version
		return nil
	}); err != nil {
		return nil, err
	}
	for _, r := range out {
		if r.SourceKind == "hosted" {
			r.HostedLatestVersion = latest[r.Name]
		}
	}
	return out, nil
}

// queryOne is the one row a statement gives, nil for none.
func queryOne(ctx context.Context, tx pgx.Tx, sql string, args ...any) (*row, error) {
	rows, err := query(ctx, tx, sql, args...)
	if err != nil || len(rows) == 0 {
		return nil, err
	}
	return rows[0], nil
}

func notFound(name string) error {
	return httpx.WithReason(http.StatusNotFound, fmt.Sprintf(`no plugin package named "%s"`, name), packageNotFound)
}

// pinID is the row's (or with pending, its re-pin's) commit, bytes sha256 or version, as
// text; nil for none.
func (r *row) pinID(pending bool) *string {
	kind, commit, bytes, version := &r.PinKind, r.CommitSHA, r.BytesSHA256, r.Version
	if pending {
		kind, commit, bytes, version = r.PendingPinKind, r.PendingCommitSHA, r.PendingBytesSHA256, r.PendingVersion
	}
	if kind == nil {
		return nil
	}
	switch *kind {
	case "commit":
		return commit
	case "bytes":
		return bytes
	case "version":
		if version != nil {
			s := strconv.FormatInt(*version, 10)
			return &s
		}
	}
	return nil
}

func (s *Packages) list(r *http.Request, _ crud.Params) (any, error) {
	enabled, err := crud.QueryBool(r, "enabled")
	if err != nil {
		return nil, err
	}
	where := ""
	if enabled {
		where = " WHERE enabled"
	}
	var out []*row
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) (err error) {
		out, err = query(r.Context(), tx, `SELECT `+columns+` FROM ai_plugin_packages`+where+` ORDER BY name`)
		return err
	})
	if err != nil {
		return nil, err
	}
	if out == nil {
		out = []*row{}
	}
	return struct {
		Packages []*row `json:"packages"`
	}{out}, nil
}

func (s *Packages) one(r *http.Request, p crud.Params) (any, error) {
	name, err := p.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	var out *row
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) (err error) {
		out, err = queryOne(r.Context(), tx, `SELECT `+columns+` FROM ai_plugin_packages WHERE name = $1`, name)
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Package *row `json:"package"`
	}{out}, nil
}

// pin is a fetched, vetted and hashed commit, file or hosted version, as the agent
// prepared it.
type pin struct {
	fetchURL, fetchPath, kind, contentHash string
	commit, bytes                          *string
	version                                *int64
	files, review                          json.RawMessage
}

// id is the pin's commit, bytes sha256 or version, as text.
func (p *pin) id() string {
	switch p.kind {
	case "commit":
		return *p.commit
	case "bytes":
		return *p.bytes
	}
	return strconv.FormatInt(*p.version, 10)
}

var pinFields = []string{"fetch_url", "fetch_path", "pin_kind", "commit_sha", "bytes_sha256", "version",
	"content_hash", "files", "review"}

// readPin reads Pin's fields and runs their validators. Its model validator, onePin, runs
// only once every field of the body passed.
func readPin(b *crud.Body) *pin {
	p := &pin{
		fetchURL:    b.Text("fetch_url", 1, urlMax),
		fetchPath:   b.Text("fetch_path", 0, textMax),
		kind:        b.ChoiceOr("pin_kind", "commit", pinKinds...),
		commit:      b.OptText("commit_sha", 0, 1<<30, commitPattern),
		bytes:       b.OptText("bytes_sha256", 0, 1<<30, sha256Pattern),
		version:     b.OptInt("version", 1),
		contentHash: b.Match("content_hash", 0, 1<<30, hashPattern),
		files:       b.RawObject("files"),
		review:      b.RawObject("review"),
	}
	if p.files != nil && pyDumpsLen(p.files) > filesJSONMax {
		b.Fail([]string{"files"}, fmt.Sprintf("files is larger than %d characters of JSON", filesJSONMax))
	}
	if p.review != nil && pyDumpsLen(p.review) > reviewJSONMax {
		b.Fail([]string{"review"}, fmt.Sprintf("review is larger than %d characters of JSON", reviewJSONMax))
	}
	return p
}

// onePin is Pin's model validator: a pin names its own field only.
func (p *pin) onePin(b *crud.Body) bool {
	named := map[string]bool{"commit": p.commit != nil, "bytes": p.bytes != nil, "version": p.version != nil}
	for kind, has := range named {
		if has != (kind == p.kind) {
			b.Fail(nil, fmt.Sprintf("a %s pin names its %s only", p.kind, pinField[p.kind]))
			return false
		}
	}
	return true
}

// reviewName is the review's name member: its text when it is a string (else one no
// package name can be), and the member as Python's str() writes it.
func reviewName(review json.RawMessage) (string, string) {
	var object map[string]json.RawMessage
	_ = json.Unmarshal(review, &object)
	raw, ok := object["name"]
	if !ok {
		return "", "None"
	}
	var s string
	if string(bytes.TrimSpace(raw)) != "null" && json.Unmarshal(raw, &s) == nil {
		return s, s
	}
	return "\x00", pyStr(raw)
}

// hostedHeld checks that a version pin names a published version of the hosted plugin name
// whose content hash is the one sent.
func hostedHeld(ctx context.Context, tx pgx.Tx, name string, p *pin) error {
	if p.version == nil {
		return nil
	}
	var one int
	err := tx.QueryRow(ctx, `SELECT 1 FROM ai_hosted_plugins h JOIN ai_hosted_plugin_versions v
		ON v.plugin_id = h.id WHERE h.name = $1 AND v.version = $2 AND v.content_hash = $3
		FOR KEY SHARE OF h`, name, *p.version, p.contentHash).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return httpx.WithReason(http.StatusConflict, fmt.Sprintf(
			`the hosted plugin "%s" has no version %d with the content hash %s`, name, *p.version, p.contentHash),
			hostedVersionMismatch)
	}
	return err
}

// mirrorHeld checks that a bytes pin's mirror is held for the package.
func mirrorHeld(ctx context.Context, tx pgx.Tx, name string, p *pin) error {
	if p.bytes == nil {
		return nil
	}
	return heldFor(ctx, tx, name, *p.bytes)
}

// heldFor is agent_data/package_mirrors.py held: the package name holds the mirror sha256,
// else 409 mirror-missing.
func heldFor(ctx context.Context, tx pgx.Tx, name, sha256 string) error {
	var one int
	err := tx.QueryRow(ctx, `SELECT 1 FROM blob_refs WHERE key = $1 AND holder_kind = $2 AND holder_id = $3`,
		"plugin-mirror-"+sha256, mirrorHolder, name+"/"+sha256).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return httpx.WithReason(http.StatusConflict,
			fmt.Sprintf("no mirror of the bytes %s is kept; send them first", sha256), mirrorMissing)
	}
	return err
}

// releaseUnnamed is agent_data/package_mirrors.py release_unnamed: it drops each mirror hold
// older than its grace that no row names, by its bytes pin or a commit pin's mirror (#7907),
// for the pin or for its re-pin.
func releaseUnnamed(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, `DELETE FROM blob_refs WHERE holder_kind = $1
		AND created_at < now() - interval '`+unnamedGrace+`'
		AND holder_id NOT IN (
		  SELECT name || '/' || bytes_sha256 FROM ai_plugin_packages WHERE bytes_sha256 IS NOT NULL
		  UNION SELECT name || '/' || pending_bytes_sha256 FROM ai_plugin_packages
		  WHERE pending_bytes_sha256 IS NOT NULL
		  UNION SELECT name || '/' || mirror_sha256 FROM ai_plugin_packages
		  WHERE mirror_sha256 IS NOT NULL
		  UNION SELECT name || '/' || pending_mirror_sha256 FROM ai_plugin_packages
		  WHERE pending_mirror_sha256 IS NOT NULL)`, mirrorHolder)
	return err
}

// written is what every package write ends with: the release of the mirrors no row names
// any more, and the warm-evict NOTIFY.
func written(ctx context.Context, tx pgx.Tx) error {
	if err := releaseUnnamed(ctx, tx); err != nil {
		return err
	}
	return wire.NotifyWarmEvict(ctx, tx)
}

// announced tells Settings → Tools & plugins of the committed write (#7460). As Python's
// outbox, it does not stop when the request ends.
func (s *Packages) announced(r *http.Request, name string) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), announceTimeout)
	defer cancel()
	bus.Publish(ctx, s.Pool, "plugin_packages.changed", httpx.Field{Key: "name", Value: name})
}

func locked(ctx context.Context, tx pgx.Tx, name string) (*row, error) {
	current, err := queryOne(ctx, tx, `SELECT `+columns+` FROM ai_plugin_packages WHERE name = $1 FOR UPDATE`, name)
	if err == nil && current == nil {
		err = notFound(name)
	}
	return current, err
}

func (s *Packages) install(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, append(pinFields, "name", "source_kind", "source_url", "source_ref",
		"marketplace_entry")...)
	if err != nil {
		return nil, err
	}
	p := readPin(body)
	name := body.Match("name", 0, 1<<30, namePattern)
	source := body.Choice("source_kind", sourceKinds...)
	sourceURL := body.Text("source_url", 1, urlMax)
	sourceRef := body.Text("source_ref", 0, textMax)
	entry := body.OptText("marketplace_entry", 0, textMax, nil)
	if body.Err() == nil && p.onePin(body) {
		// Install's own model validator.
		switch {
		case !pinFits(source, p.kind):
			body.Fail(nil, fmt.Sprintf("a %s source cannot have a %s pin", source, p.kind))
		case refKind(source) && sourceRef == "":
			body.Fail(nil, fmt.Sprintf("a %s source names its ref", source))
		}
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	if got, _ := reviewName(p.review); got != name {
		return nil, httpx.WithReason(http.StatusUnprocessableEntity, "a package's name is its review's name",
			packageNameDiffers)
	}
	if !marketplaceKind(source) {
		entry = nil
	}
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if err := lockName(ctx, tx, name); err != nil {
			return err
		}
		if source != "hosted" {
			var one int
			err := tx.QueryRow(ctx, `SELECT 1 FROM ai_hosted_plugins WHERE name = $1`, name).Scan(&one)
			if err == nil {
				return httpx.WithReason(http.StatusConflict, fmt.Sprintf(`"%s" is the name of a hosted plugin;`+
					` install that plugin from Hosted plugins, or a package of another name`, name), hostedNameTaken)
			}
			if !errors.Is(err, pgx.ErrNoRows) {
				return err
			}
		}
		if err := mirrorHeld(ctx, tx, name, p); err != nil {
			return err
		}
		if err := hostedHeld(ctx, tx, name, p); err != nil {
			return err
		}
		made, err := queryOne(ctx, tx, `INSERT INTO ai_plugin_packages (name, source_kind, source_url,
			source_ref, marketplace_entry, fetch_url, fetch_path, pin_kind, commit_sha, bytes_sha256,
			version, content_hash, files, review)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14::jsonb)
			ON CONFLICT (name) DO NOTHING RETURNING `+columns,
			name, source, sourceURL, sourceRef, entry, p.fetchURL, p.fetchPath, p.kind, p.commit, p.bytes,
			p.version, p.contentHash, string(p.files), string(p.review))
		if err != nil {
			return err
		}
		if made == nil {
			return httpx.WithReason(http.StatusConflict, fmt.Sprintf(
				`a plugin package named "%s" is already installed; re-pin it instead`, name), packageExists)
		}
		// What an older replica's remove left: a re-install starts at the defaults.
		if err := forgetPackage(ctx, tx, name); err != nil {
			return err
		}
		out = made
		return written(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	s.announced(r, out.Name)
	return out, nil
}

// lockName is hosted_plugins/store.py lock_name: the package name held until the
// transaction ends.
func lockName(ctx context.Context, tx pgx.Tx, name string) error {
	_, err := tx.Exec(ctx,
		`SELECT pg_advisory_xact_lock(hashtextextended('ai_plugin_package_name:' || $1, 0))`, name)
	return err
}

func (s *Packages) pending(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, append(pinFields, "ref")...)
	if err != nil {
		return nil, err
	}
	p := readPin(body)
	ref := body.Text("ref", 0, textMax)
	if body.Err() == nil {
		p.onePin(body)
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	if got, value := reviewName(p.review); got != name {
		return nil, httpx.WithReason(http.StatusConflict, fmt.Sprintf(
			`the new commit names the plugin "%s", not "%s"`, value, name), packageNameDiffers)
	}
	pinID := p.id()
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		current, err := locked(ctx, tx, name)
		if err != nil {
			return err
		}
		kind := current.SourceKind
		if !pinFits(kind, p.kind) {
			return &httpx.Problem{Status: http.StatusUnprocessableEntity,
				Detail: fmt.Sprintf("a %s source cannot have a %s pin", kind, p.kind)}
		}
		if refKind(kind) && ref == "" {
			return &httpx.Problem{Status: http.StatusUnprocessableEntity,
				Detail: fmt.Sprintf("a %s source names its ref", kind)}
		}
		if err := mirrorHeld(ctx, tx, name, p); err != nil {
			return err
		}
		if err := hostedHeld(ctx, tx, name, p); err != nil {
			return err
		}
		if id := current.pinID(false); current.PinKind == p.kind && id != nil && *id == pinID &&
			current.ContentHash == p.contentHash && current.FetchURL == p.fetchURL &&
			current.FetchPath == p.fetchPath {
			return httpx.WithReason(http.StatusConflict,
				fmt.Sprintf(`"%s" is already pinned to %s`, name, pinID), packagePinned)
		}
		out, err = queryOne(ctx, tx, `UPDATE ai_plugin_packages SET pending_ref = $1, pending_fetch_url = $2,
			pending_fetch_path = $3, pending_pin_kind = $4, pending_commit_sha = $5,
			pending_bytes_sha256 = $6, pending_version = $7, pending_content_hash = $8,
			pending_files = $9::jsonb, pending_review = $10::jsonb, pending_mirror_sha256 = NULL,
			updated_at = now()
			WHERE name = $11 RETURNING `+columns,
			ref, p.fetchURL, p.fetchPath, p.kind, p.commit, p.bytes, p.version, p.contentHash,
			string(p.files), string(p.review), name)
		if err != nil {
			return err
		}
		if _, err := pruneStaleRows(ctx, tx, name, out.Review); err != nil {
			return err
		}
		return written(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	s.announced(r, out.Name)
	return out, nil
}

func (s *Packages) discard(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) (err error) {
		out, err = queryOne(ctx, tx, `UPDATE ai_plugin_packages SET pending_ref = NULL,
			pending_fetch_url = NULL, pending_fetch_path = NULL, pending_pin_kind = NULL,
			pending_commit_sha = NULL, pending_bytes_sha256 = NULL, pending_version = NULL,
			pending_content_hash = NULL, pending_files = NULL, pending_review = NULL,
			pending_mirror_sha256 = NULL, updated_at = now()
			WHERE name = $1 RETURNING `+columns, name)
		if err != nil {
			return err
		}
		if out == nil {
			return notFound(name)
		}
		if _, err := pruneStaleRows(ctx, tx, name, out.Review); err != nil {
			return err
		}
		return written(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	s.announced(r, out.Name)
	return out, nil
}

// refused is store.ts withRefused: how many the review lists as refused.
func refused(review json.RawMessage) int {
	var object map[string]json.RawMessage
	_ = json.Unmarshal(review, &object)
	var list []json.RawMessage
	if json.Unmarshal(object["refused"], &list) != nil {
		return 0
	}
	return len(list)
}

func (s *Packages) approve(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "commit_sha", "bytes_sha256", "version", "content_hash", "allow_refused")
	if err != nil {
		return nil, err
	}
	commit := body.OptText("commit_sha", 0, 1<<30, commitPattern)
	bytes := body.OptText("bytes_sha256", 0, 1<<30, sha256Pattern)
	version := body.OptInt("version", 1)
	contentHash := body.Match("content_hash", 0, 1<<30, hashPattern)
	allowRefused := body.Bool("allow_refused", false)
	var kind, pinID string
	named := 0
	if commit != nil {
		kind, pinID, named = "commit", *commit, named+1
	}
	if bytes != nil {
		kind, pinID, named = "bytes", *bytes, named+1
	}
	if version != nil {
		kind, pinID, named = "version", strconv.FormatInt(*version, 10), named+1
	}
	if body.Err() == nil && named != 1 {
		body.Fail(nil, "name the pin by one of commit_sha, bytes_sha256 or version")
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		current, err := locked(ctx, tx, name)
		if err != nil {
			return err
		}
		pending, now := current.pinID(true), current.pinID(false)
		isPending := current.PendingPinKind != nil && *current.PendingPinKind == kind && pending != nil &&
			*pending == pinID && current.PendingContentHash != nil && *current.PendingContentHash == contentHash
		isCurrent := current.PinKind == kind && now != nil && *now == pinID && current.ContentHash == contentHash
		if !isPending && !isCurrent {
			return httpx.WithReason(http.StatusConflict, "the commit and content hash do not match the pin"+
				" under review; reload the review and approve what it shows", pinMismatch)
		}
		// Only what the admin was shown is allowed: a pin with nothing refused is stored
		// without the flag, so a later rule still refuses it.
		review := current.Review
		if isPending {
			review = current.PendingReview
		}
		count := refused(review)
		if count > 0 && !allowRefused {
			return httpx.WithReason(http.StatusConflict, fmt.Sprintf("the review lists what the rules refuse (%d);"+
				" approve with allow_refused to load the package as it is", count), refusedNeedsAllow)
		}
		allow := allowRefused && count > 0
		if isPending {
			// A new commit from the same place keeps enabled; one fetched from another
			// repository or path is a new source to trust, approved disabled.
			moved := current.PendingFetchURL == nil || *current.PendingFetchURL != current.FetchURL ||
				current.PendingFetchPath == nil || *current.PendingFetchPath != current.FetchPath
			out, err = queryOne(ctx, tx, `UPDATE ai_plugin_packages SET enabled = enabled AND NOT $1,
				source_ref = pending_ref, fetch_url = pending_fetch_url,
				fetch_path = pending_fetch_path, pin_kind = $2,
				commit_sha = pending_commit_sha, bytes_sha256 = pending_bytes_sha256,
				version = pending_version,
				content_hash = pending_content_hash, files = pending_files,
				review = pending_review,
				-- A re-pin of the same commit and hash whose zip was not sent keeps the pin's mirror.
				mirror_sha256 = COALESCE(pending_mirror_sha256, CASE WHEN
					pending_commit_sha = commit_sha AND pending_content_hash = content_hash
					THEN mirror_sha256 END),
				approved_at = now(), allow_refused = $3,
				pending_ref = NULL, pending_fetch_url = NULL, pending_fetch_path = NULL,
				pending_pin_kind = NULL, pending_commit_sha = NULL,
				pending_bytes_sha256 = NULL, pending_version = NULL,
				pending_content_hash = NULL, pending_mirror_sha256 = NULL,
				pending_files = NULL, pending_review = NULL, updated_at = now()
				WHERE name = $4 RETURNING `+columns, moved, *current.PendingPinKind, allow, name)
			if err != nil {
				return err
			}
			if _, err := pruneStaleRows(ctx, tx, name, out.Review); err != nil {
				return err
			}
			return written(ctx, tx)
		}
		if current.ApprovedAt != nil && current.AllowRefused == allow {
			out = current
			pruned, err := pruneStaleRows(ctx, tx, name, current.Review)
			if err != nil || !pruned {
				return err
			}
			return wire.NotifyWarmEvict(ctx, tx)
		}
		out, err = queryOne(ctx, tx, `UPDATE ai_plugin_packages SET approved_at = now(), allow_refused = $1,
			updated_at = now() WHERE name = $2 RETURNING `+columns, allow, name)
		if err != nil {
			return err
		}
		if _, err := pruneStaleRows(ctx, tx, name, out.Review); err != nil {
			return err
		}
		return written(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	s.announced(r, out.Name)
	return out, nil
}

func (s *Packages) enabled(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "enabled")
	if err != nil {
		return nil, err
	}
	enabled := body.RequiredBool("enabled")
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		current, err := locked(ctx, tx, name)
		if err != nil {
			return err
		}
		if enabled && current.ApprovedAt == nil {
			return httpx.WithReason(http.StatusConflict, `approve the pin before enabling the package`+
				` (issue #297, "Review before enable")`, packageUnapproved)
		}
		out, err = queryOne(ctx, tx, `UPDATE ai_plugin_packages SET enabled = $1, updated_at = now()
			WHERE name = $2 RETURNING `+columns, enabled, name)
		if err != nil {
			return err
		}
		if _, err := pruneStaleRows(ctx, tx, name, out.Review); err != nil {
			return err
		}
		return written(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	s.announced(r, out.Name)
	return out, nil
}

func (s *Packages) delete(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var gone bool
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `DELETE FROM ai_plugin_packages WHERE name = $1`, name)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		gone = true
		if err := forgetPackage(ctx, tx, name); err != nil {
			return err
		}
		return written(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	if gone {
		s.announced(r, name)
	}
	return struct {
		Done bool `json:"done"`
	}{gone}, nil
}

// mirror records a commit pin's mirror (#7907): the sha256 of a zip of its files that the
// package holds, as mirror_sha256 when the commit and content hash are the pin's and as
// pending_mirror_sha256 when they are the re-pin's (both when both are). No pin, approval
// or switch changes, so nothing is announced and no warm Claude Code is evicted.
func (s *Packages) mirror(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "commit_sha", "content_hash", "bytes_sha256")
	if err != nil {
		return nil, err
	}
	commit := body.Match("commit_sha", 0, 1<<30, commitPattern)
	contentHash := body.Match("content_hash", 0, 1<<30, hashPattern)
	sha256 := body.Match("bytes_sha256", 0, 1<<30, sha256Pattern)
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out *row
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		current, err := locked(ctx, tx, name)
		if err != nil {
			return err
		}
		if err := heldFor(ctx, tx, name, sha256); err != nil {
			return err
		}
		isCurrent := current.PinKind == "commit" && current.CommitSHA != nil && *current.CommitSHA == commit &&
			current.ContentHash == contentHash
		isPending := current.PendingPinKind != nil && *current.PendingPinKind == "commit" &&
			current.PendingCommitSHA != nil && *current.PendingCommitSHA == commit &&
			current.PendingContentHash != nil && *current.PendingContentHash == contentHash
		if !isCurrent && !isPending {
			return httpx.WithReason(http.StatusConflict, fmt.Sprintf(`"%s" is not pinned to %s with that content hash,`+
				" and its re-pin is not either", name, commit), pinMismatch)
		}
		out, err = queryOne(ctx, tx, `UPDATE ai_plugin_packages SET
			mirror_sha256 = CASE WHEN $1 THEN $2 ELSE mirror_sha256 END,
			pending_mirror_sha256 = CASE WHEN $3 THEN $2 ELSE pending_mirror_sha256 END
			WHERE name = $4 RETURNING `+columns, isCurrent, sha256, isPending, name)
		if err != nil {
			return err
		}
		return releaseUnnamed(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// -- hosted plugins (area hosted-plugins) ---------------------------------------------------

type hostedVersion struct {
	Version     int64           `json:"version"`
	ZipSHA256   string          `json:"zip_sha256"`
	ContentHash string          `json:"content_hash"`
	Files       json.RawMessage `json:"files"`
	PublishedAt wire.Time       `json:"published_at"`
	PublishedBy string          `json:"published_by"`
}

type hostedPlugin struct {
	Name          string   `json:"name"`
	Description   string   `json:"description"`
	Category      *string  `json:"category"`
	Tags          []string `json:"tags"`
	LatestVersion *int64   `json:"latest_version"`
	// Latest is the latest version's row; null before the first publish.
	Latest *hostedVersion `json:"latest"`
}

func (s *Packages) hosted(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out *hostedPlugin
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		var id int64
		found := &hostedPlugin{}
		err := tx.QueryRow(ctx, `SELECT id, name, description, category, tags, latest_version
			FROM ai_hosted_plugins WHERE name = $1`, name).Scan(
			&id, &found.Name, &found.Description, &found.Category, &found.Tags, &found.LatestVersion)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		out = found
		if found.Tags == nil {
			found.Tags = []string{}
		}
		if found.LatestVersion == nil {
			return nil
		}
		v := &hostedVersion{}
		err = tx.QueryRow(ctx, `SELECT version, zip_sha256, content_hash, files, published_at, published_by
			FROM ai_hosted_plugin_versions WHERE plugin_id = $1 AND version = $2`, id, *found.LatestVersion).Scan(
			&v.Version, &v.ZipSHA256, &v.ContentHash, (*[]byte)(&v.Files), &v.PublishedAt, &v.PublishedBy)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		found.Latest = v
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Plugin *hostedPlugin `json:"plugin"`
	}{out}, nil
}

// -- marketplace feeds (area plugin-feeds) --------------------------------------------------

type feedHosted struct {
	Version   int64  `json:"version"`
	ZipSHA256 string `json:"zip_sha256"`
}

// feedPackage is a package the feed names, at its current pin (never its pending one).
type feedPackage struct {
	Name         string      `json:"name"`
	SourceKind   string      `json:"source_kind"`
	FetchURL     *string     `json:"fetch_url"`
	FetchPath    *string     `json:"fetch_path"`
	CommitSHA    *string     `json:"commit_sha"`
	ContentHash  string      `json:"content_hash"`
	PinKind      string      `json:"pin_kind"`
	BytesSHA256  *string     `json:"bytes_sha256"`
	Approved     bool        `json:"approved"`
	AllowRefused bool        `json:"allow_refused"`
	Description  *string     `json:"description"`
	Hosted       *feedHosted `json:"hosted"`
}

type feed struct {
	Slug  string `json:"slug"`
	Name  string `json:"name"`
	Owner string `json:"owner"`
}

func (s *Packages) feed(r *http.Request, params crud.Params) (any, error) {
	slug, err := params.Match("feed", feedPattern)
	if err != nil {
		return nil, err
	}
	only, err := crud.QueryOptText(r, "package", 0, feedPackageQ)
	if err != nil {
		return nil, err
	}
	type answer struct {
		// Feed is null when no feed has the slug.
		Feed     *feed         `json:"feed"`
		Packages []feedPackage `json:"packages"`
	}
	out := answer{Packages: []feedPackage{}}
	ctx := r.Context()
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		var id int64
		f := &feed{}
		err := tx.QueryRow(ctx, `SELECT id, slug, name, owner FROM ai_plugin_feeds WHERE slug = $1`, slug).Scan(
			&id, &f.Slug, &f.Name, &f.Owner)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		out.Feed = f
		rows, err := tx.Query(ctx, `SELECT p.name, p.source_kind, p.fetch_url, p.fetch_path, p.commit_sha,
			p.content_hash, p.pin_kind, p.bytes_sha256, p.allow_refused,
			p.approved_at IS NOT NULL AS approved, p.review->>'description' AS description,
			v.version AS hosted_version, v.zip_sha256 AS hosted_zip_sha256
			FROM ai_plugin_feed_packages fp
			JOIN ai_plugin_packages p ON p.name = fp.package_name
			LEFT JOIN ai_hosted_plugins h ON p.source_kind = 'hosted' AND h.name = p.name
			LEFT JOIN ai_hosted_plugin_versions v
			  ON v.plugin_id = h.id AND v.version = p.version AND v.content_hash = p.content_hash
			WHERE fp.feed_id = $1 AND ($2::text IS NULL OR p.name = $2)
			ORDER BY p.name`, id, only)
		if err != nil {
			return err
		}
		var pkg feedPackage
		var version *int64
		var zip *string
		_, err = pgx.ForEachRow(rows, []any{&pkg.Name, &pkg.SourceKind, &pkg.FetchURL, &pkg.FetchPath,
			&pkg.CommitSHA, &pkg.ContentHash, &pkg.PinKind, &pkg.BytesSHA256, &pkg.AllowRefused, &pkg.Approved,
			&pkg.Description, &version, &zip}, func() error {
			one := pkg
			if version != nil {
				one.Hosted = &feedHosted{Version: *version, ZipSHA256: *zip}
			}
			out.Packages = append(out.Packages, one)
			return nil
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func feedID(ctx context.Context, tx pgx.Tx, slug string) (int64, error) {
	var id int64
	err := tx.QueryRow(ctx, `SELECT id FROM ai_plugin_feeds WHERE slug = $1`, slug).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, httpx.WithReason(http.StatusNotFound, fmt.Sprintf(`no plugin feed "%s"`, slug), feedNotFound)
	}
	return id, err
}

func feedParams(params crud.Params) (string, string, error) {
	slug, err := params.Match("feed", feedPattern)
	if err != nil {
		return "", "", err
	}
	name, err := params.Match("name", namePattern)
	return slug, name, err
}

// feedAdd lists the package in the feed (#6936); done is false when it already was.
func (s *Packages) feedAdd(r *http.Request, params crud.Params) (any, error) {
	slug, name, err := feedParams(params)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var added bool
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		id, err := feedID(ctx, tx, slug)
		if err != nil {
			return err
		}
		var one int
		err = tx.QueryRow(ctx, `SELECT 1 FROM ai_plugin_packages WHERE name = $1 FOR KEY SHARE`, name).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return notFound(name)
		}
		if err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `INSERT INTO ai_plugin_feed_packages (feed_id, package_name) VALUES ($1, $2)
			ON CONFLICT DO NOTHING`, id, name)
		added = tag.RowsAffected() == 1
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Done bool `json:"done"`
	}{added}, nil
}

// feedRemove takes the package out of the feed (#6936); done is false when it was not in it.
func (s *Packages) feedRemove(r *http.Request, params crud.Params) (any, error) {
	slug, name, err := feedParams(params)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var removed bool
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		id, err := feedID(ctx, tx, slug)
		if err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `DELETE FROM ai_plugin_feed_packages WHERE feed_id = $1 AND package_name = $2`,
			id, name)
		removed = tag.RowsAffected() == 1
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Done bool `json:"done"`
	}{removed}, nil
}
