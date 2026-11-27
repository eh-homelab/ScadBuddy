package plugins

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"unicode/utf16"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

// The routes' reasons (crud_agent_plugins.py).
const (
	kekMissing     = "kek-missing"
	pluginRefused  = "plugin-refused"
	pluginGone     = "plugin-gone"
	leaseLost      = "lease-lost"
	maxToolEntries = 256 // registry.ts MAX_TOOL_ENTRIES
	defaultHeader  = "Authorization"
	noKEKToOpen    = "no key-encryption key is configured to open the plugin secret"
	sourceItem     = "*" // access/model.ts SOURCE_ITEM
)

var (
	toolAccess = []string{"off", "read", "write", "outward"}
	tiers      = []string{"read", "write", "outward"}
)

func remoteSource(name string) string { return "remote:" + name }

// kek is the current KEK, nil when the API holds none.
func (s *Plugins) kek() *seal.KEK {
	if s.Keys == nil {
		return nil
	}
	return s.Keys.Current
}

func (s *Plugins) needKEK() (*seal.KEK, error) {
	if k := s.kek(); k != nil {
		return k, nil
	}
	return nil, httpx.WithReason(http.StatusServiceUnavailable,
		"The API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE).", kekMissing)
}

// open opens e under the KEK its row names, the current or the previous one; under the
// current one when it names neither, so the error is Python's.
func (s *Plugins) open(e seal.Envelope, aad string) (string, error) {
	kek := s.Keys.Opener(e.KEKID)
	if kek == nil {
		kek = s.Keys.Current
	}
	return seal.OpenSecret(kek, e, aad)
}

// opens says whether the API holds the KEK kekID names (the Settings row's usable).
func (s *Plugins) opens(kekID *string) bool {
	return kekID == nil || s.Keys != nil && s.Keys.Opener(*kekID) != nil
}

func refused(status int, detail string) error {
	return httpx.WithReason(status, detail, pluginRefused)
}

func (s *Plugins) inTx(ctx context.Context, fn func(pgx.Tx) error) error {
	return pgx.BeginFunc(ctx, s.Pool, fn)
}

// last4 is secrets.ts last4: empty for a secret under 12 characters.
func last4(secret string) string {
	r := []rune(secret)
	if len(r) < 12 {
		return ""
	}
	return string(r[len(r)-4:])
}

type accessRow struct {
	Source    string `json:"source"`
	Item      string `json:"item"`
	Access    string `json:"access"`
	UpdatedBy string `json:"updated_by"`
}

// oauthView is a sign-in's public columns (registry.ts OAUTH_ROW): never a sealed one.
type oauthView struct {
	Status          string     `json:"status"`
	LastError       *string    `json:"last_error"`
	Issuer          string     `json:"issuer"`
	Scope           *string    `json:"scope"`
	Registration    string     `json:"registration"`
	HasRefresh      bool       `json:"has_refresh"`
	AccessExpiresAt *wire.Time `json:"access_expires_at"`
	ConnectedAt     wire.Time  `json:"connected_at"`
	GrantID         string     `json:"grant_id"`
	PluginURL       string     `json:"plugin_url"`
}

// pluginView is a plugin as registry.ts reads it, with no secret (PluginRow).
type pluginView struct {
	Name       string      `json:"name"`
	Kind       string      `json:"kind"`
	URL        string      `json:"url"`
	AuthHeader *string     `json:"auth_header"`
	KEKID      *string     `json:"kek_id"`
	Last4      *string     `json:"last4"`
	CreatedAt  wire.Time   `json:"created_at"`
	UpdatedAt  wire.Time   `json:"updated_at"`
	AccessRows []accessRow `json:"access_rows"`
	OAuth      *oauthView  `json:"oauth"`
	Opens      bool        `json:"opens"`
}

type header struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// openedView is a plugin with its header value opened (OpenedPlugin).
type openedView struct {
	pluginView
	Header    *header `json:"header"`
	SealError *string `json:"seal_error"`
}

// plugin is an ai_plugins row with its access rows and sign-in.
type plugin struct {
	view         pluginView
	secretSealed []byte
	dekSealed    []byte
}

const columns = `name, kind, url, auth_header, secret_sealed, dek_sealed, kek_id, last4,
	created_at, updated_at`

// selectPlugins is _select: every plugin, or the one named.
func selectPlugins(ctx context.Context, tx pgx.Tx, name *string) ([]*plugin, error) {
	sql, args := `SELECT `+columns+` FROM ai_plugins ORDER BY name`, []any{}
	if name != nil {
		sql, args = `SELECT `+columns+` FROM ai_plugins WHERE name = $1`, []any{*name}
	}
	rows, err := tx.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	var out []*plugin
	var names, sources []string
	for rows.Next() {
		p := &plugin{}
		v := &p.view
		if err := rows.Scan(&v.Name, &v.Kind, &v.URL, &v.AuthHeader, &p.secretSealed,
			&p.dekSealed, &v.KEKID, &v.Last4, &v.CreatedAt, &v.UpdatedAt); err != nil {
			rows.Close()
			return nil, err
		}
		v.AccessRows = []accessRow{}
		out = append(out, p)
		names = append(names, v.Name)
		sources = append(sources, remoteSource(v.Name))
	}
	if err := rows.Err(); err != nil || len(out) == 0 {
		return out, err
	}
	bySource := map[string]*plugin{}
	byName := map[string]*plugin{}
	for _, p := range out {
		bySource[remoteSource(p.view.Name)] = p
		byName[p.view.Name] = p
	}
	rows, err = tx.Query(ctx, `SELECT source, item, access, updated_by FROM ai_access
		WHERE source = ANY($1) ORDER BY source, item`, sources)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var a accessRow
		if err := rows.Scan(&a.Source, &a.Item, &a.Access, &a.UpdatedBy); err != nil {
			rows.Close()
			return nil, err
		}
		if p := bySource[a.Source]; p != nil {
			p.view.AccessRows = append(p.view.AccessRows, a)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	rows, err = tx.Query(ctx, `SELECT plugin, status, last_error, issuer, scope, registration,
		has_refresh, access_expires_at, connected_at, grant_id::text, plugin_url
		FROM ai_plugin_oauth WHERE plugin = ANY($1)`, names)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var name string
		o := &oauthView{}
		if err := rows.Scan(&name, &o.Status, &o.LastError, &o.Issuer, &o.Scope, &o.Registration,
			&o.HasRefresh, &o.AccessExpiresAt, &o.ConnectedAt, &o.GrantID, &o.PluginURL); err != nil {
			rows.Close()
			return nil, err
		}
		byName[name].view.OAuth = o
	}
	return out, rows.Err()
}

func selectOne(ctx context.Context, tx pgx.Tx, name string) (*plugin, error) {
	rows, err := selectPlugins(ctx, tx, &name)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, fmt.Errorf("plugin %s is gone inside its own transaction", name)
	}
	return rows[0], nil
}

func (s *Plugins) viewOf(p *plugin) pluginView {
	v := p.view
	v.Opens = s.opens(v.KEKID)
	return v
}

// opened is registry.ts openPlugin's header, opened here.
func (s *Plugins) opened(p *plugin) openedView {
	out := openedView{pluginView: s.viewOf(p)}
	if p.secretSealed != nil && p.view.AuthHeader != nil {
		var msg string
		if s.kek() == nil {
			msg = noKEKToOpen
		} else {
			e := seal.Envelope{SecretSealed: p.secretSealed, DEKSealed: p.dekSealed}
			if p.view.KEKID != nil {
				e.KEKID = *p.view.KEKID
			}
			value, err := s.open(e, pluginAAD(p.view.Name, p.view.URL, *p.view.AuthHeader))
			if err != nil {
				msg = err.Error()
			} else {
				out.Header = &header{Name: *p.view.AuthHeader, Value: value}
			}
		}
		if msg != "" {
			out.SealError = &msg
		}
	}
	return out
}

// stored is the plugin's access as ai_access gives it: (tiers, disabled tools, enabled).
func (p *plugin) stored() (map[string]string, []string, bool) {
	source := remoteSource(p.view.Name)
	tiers, disabled := map[string]string{}, []string{}
	enabled, decided := false, false
	for _, row := range p.view.AccessRows {
		if row.Source != source {
			continue
		}
		if row.Item == sourceItem {
			if !decided && (row.Access == "on" || row.Access == "off") {
				enabled, decided = row.Access == "on", true
			}
			continue
		}
		if !slices.Contains(toolAccess, row.Access) {
			continue
		}
		if row.Access == "off" {
			disabled = append(disabled, row.Item)
		} else {
			tiers[row.Item] = row.Access
		}
	}
	return tiers, jsSort(disabled), enabled
}

// jsSort is the items, once each, as JavaScript's sort orders strings: by UTF-16 code unit.
func jsSort(items []string) []string {
	out := []string{}
	for _, item := range items {
		if !slices.Contains(out, item) {
			out = append(out, item)
		}
	}
	slices.SortFunc(out, func(a, b string) int {
		return slices.Compare(utf16.Encode([]rune(a)), utf16.Encode([]rune(b)))
	})
	return out
}

func writeEnabled(ctx context.Context, tx pgx.Tx, source string, enabled bool, by string) error {
	access := "off"
	if enabled {
		access = "on"
	}
	_, err := tx.Exec(ctx, `INSERT INTO ai_access (source, item, access, updated_by)
		VALUES ($1, $2, $3, $4) ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access,
		updated_by = EXCLUDED.updated_by, updated_at = now()`, source, sourceItem, access, by)
	return err
}

// writeTools replaces every tool row of source (not *) with tools.
func writeTools(ctx context.Context, tx pgx.Tx, source string, tools []entry, by string) error {
	if _, err := tx.Exec(ctx, `DELETE FROM ai_access WHERE source = $1 AND item <> $2`,
		source, sourceItem); err != nil {
		return err
	}
	for _, t := range tools {
		if _, err := tx.Exec(ctx, `INSERT INTO ai_access (source, item, access, updated_by)
			VALUES ($1, $2, $3, $4)`, source, t.key, t.value, by); err != nil {
			return err
		}
	}
	return nil
}

// toolRows is registry.ts toolRows: a disabled tool is off, whatever its tier.
func toolRows(tiers map[string]string, disabled []string) []entry {
	out := []entry{}
	for tool, tier := range tiers {
		if !slices.Contains(disabled, tool) {
			out = append(out, entry{tool, tier})
		}
	}
	for _, tool := range disabled {
		out = append(out, entry{tool, "off"})
	}
	return out
}

// writeAccess is PluginStore.writeAccess: the access rows the write names, then the eviction.
func writeAccess(ctx context.Context, tx pgx.Tx, name string, tiers map[string]string,
	disabled []string, enabled bool, by string, doEnabled, doTools bool) error {
	if _, err := tx.Exec(ctx, `UPDATE ai_plugins SET updated_at = now() WHERE name = $1`, name); err != nil {
		return err
	}
	source := remoteSource(name)
	if doEnabled {
		if err := writeEnabled(ctx, tx, source, enabled, by); err != nil {
			return err
		}
	}
	if doTools {
		if err := writeTools(ctx, tx, source, toolRows(tiers, disabled), by); err != nil {
			return err
		}
	}
	return wire.NotifyWarmEvict(ctx, tx)
}

type pluginAnswer struct {
	Plugin *pluginView `json:"plugin"`
}

type pluginList struct {
	Plugins []pluginView `json:"plugins"`
}

type openedAnswer struct {
	Plugin *openedView `json:"plugin"`
}

type openedList struct {
	Plugins []openedView `json:"plugins"`
}

func (s *Plugins) read(r *http.Request, name *string) ([]*plugin, error) {
	var rows []*plugin
	err := s.inTx(r.Context(), func(tx pgx.Tx) error {
		var err error
		rows, err = selectPlugins(r.Context(), tx, name)
		return err
	})
	return rows, err
}

func (s *Plugins) list(r *http.Request, _ crud.Params) (any, error) {
	rows, err := s.read(r, nil)
	if err != nil {
		return nil, err
	}
	out := pluginList{Plugins: []pluginView{}}
	for _, p := range rows {
		out.Plugins = append(out.Plugins, s.viewOf(p))
	}
	return out, nil
}

func (s *Plugins) one(r *http.Request, p crud.Params) (any, error) {
	name, err := p.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	rows, err := s.read(r, &name)
	if err != nil || len(rows) == 0 {
		return pluginAnswer{}, err
	}
	v := s.viewOf(rows[0])
	return pluginAnswer{&v}, nil
}

func (s *Plugins) reveal(r *http.Request, p crud.Params) (any, error) {
	name, err := p.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	rows, err := s.read(r, &name)
	if err != nil || len(rows) == 0 {
		return openedAnswer{}, err
	}
	v := s.opened(rows[0])
	return openedAnswer{&v}, nil
}

// openEnabled is every plugin ai_access turns on, opened, for a turn (PluginStore.enabled).
func (s *Plugins) openEnabled(r *http.Request, _ crud.Params) (any, error) {
	rows, err := s.read(r, nil)
	if err != nil {
		return nil, err
	}
	out := openedList{Plugins: []openedView{}}
	for _, p := range rows {
		if _, _, on := p.stored(); on {
			out.Plugins = append(out.Plugins, s.opened(p))
		}
	}
	return out, nil
}

// create is PluginStore.create: a plugin the agent checked.
func (s *Plugins) create(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "name", "url", "auth_header", "secret", "tools", "by")
	if err != nil {
		return nil, err
	}
	name := f.text("name", required, 0, 1<<30, namePattern)
	url := f.text("url", required, 1, urlMax, nil)
	authHeader := f.text("auth_header", optional, 1, 64, nil)
	secret := f.text("secret", optional, 1, 4096, nil)
	toolsMode := optional
	if f.given("tools") {
		toolsMode = required
	}
	tools := f.dict("tools", toolsMode, 2*maxToolEntries, 1, toolMax, toolAccess)
	_ = f.text("by", required, 1, byMax, nil) // checked, as Python does; the rows are "system"'s
	if err := f.err(); err != nil {
		return nil, err
	}
	if (secret == nil) != (authHeader == nil) {
		return nil, refused(http.StatusBadRequest, "auth_header needs a secret")
	}
	var sealed *seal.Envelope
	var four *string
	if secret != nil {
		kek, err := s.needKEK()
		if err != nil {
			return nil, err
		}
		e := seal.SealSecret(kek, *secret, pluginAAD(*name, *url, *authHeader))
		sealed, four = &e, some(last4(*secret))
	}
	var out *plugin
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		var made string
		var args []any
		if sealed != nil {
			args = []any{*name, *url, *authHeader, sealed.SecretSealed, sealed.DEKSealed, sealed.KEKID, *four}
		} else {
			args = []any{*name, *url, nil, nil, nil, nil, nil}
		}
		err := tx.QueryRow(ctx, `INSERT INTO ai_plugins (name, kind, url, auth_header,
			secret_sealed, dek_sealed, kek_id, last4) VALUES ($1, 'remote_mcp', $2, $3, $4, $5, $6, $7)
			ON CONFLICT (name) DO NOTHING RETURNING name`, args...).Scan(&made)
		if errors.Is(err, pgx.ErrNoRows) {
			return refused(http.StatusConflict, fmt.Sprintf("a plugin named %q already exists", *name))
		}
		if err != nil {
			return err
		}
		source := remoteSource(*name)
		// A row an earlier plugin of this name left behind is not this one's.
		if err := forgetSource(ctx, tx, source); err != nil {
			return err
		}
		if err := writeEnabled(ctx, tx, source, false, "system"); err != nil {
			return err
		}
		if err := writeTools(ctx, tx, source, tools, "system"); err != nil {
			return err
		}
		if err := wire.NotifyWarmEvict(ctx, tx); err != nil {
			return err
		}
		out, err = selectOne(ctx, tx, *name)
		return err
	})
	if err != nil {
		return nil, err
	}
	return s.viewOf(out), nil
}

// update is PluginStore.update's patch as the agent checked it. A field left out is kept;
// secret null removes the header and its value.
func (s *Plugins) update(r *http.Request, p crud.Params) (any, error) {
	name, err := p.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	f, err := readForm(r, "url", "auth_header", "secret", "enabled", "tool_tiers",
		"disabled_tools", "by")
	if err != nil {
		return nil, err
	}
	newURL := f.text("url", optional, 1, urlMax, nil)
	authHeader := f.text("auth_header", optional, 1, 64, nil)
	secret := f.text("secret", optional, 1, 4096, nil)
	enabledIn := f.boolean("enabled", optional)
	tierEntries := f.dict("tool_tiers", optional, maxToolEntries, 1, toolMax, tiers)
	disabledIn := f.strings("disabled_tools", optional, maxToolEntries, 1, toolMax)
	by := f.textOr("by", "system", 1, byMax)
	if err := f.err(); err != nil {
		return nil, err
	}
	secretSent := f.given("secret")
	authSent := f.given("auth_header")
	var out *plugin
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		var locked string
		err := tx.QueryRow(ctx, `SELECT name FROM ai_plugins WHERE name = $1 FOR UPDATE`, name).Scan(&locked)
		if errors.Is(err, pgx.ErrNoRows) {
			return refused(http.StatusNotFound, fmt.Sprintf("no plugin named %q", name))
		}
		if err != nil {
			return err
		}
		current, err := selectOne(ctx, tx, name)
		if err != nil {
			return err
		}
		cur := current.view
		toolTiers, disabled, enabled := current.stored()
		url := cur.URL
		if newURL != nil {
			url = *newURL
		}
		if tierEntries != nil {
			toolTiers = map[string]string{}
			for _, e := range tierEntries {
				toolTiers[e.key] = e.value
			}
		}
		if disabledIn != nil {
			disabled = jsSort(disabledIn)
		}
		if enabledIn != nil {
			enabled = *enabledIn
		}
		// The secret: a new URL or header name re-binds it, which needs the value again.
		var cols []any
		switch {
		case secretSent && secret == nil:
			if authHeader != nil {
				return refused(http.StatusBadRequest, "auth_header needs a secret")
			}
			cols = []any{nil, nil, nil, nil, nil}
		case secret != nil:
			h := defaultHeader
			if authHeader != nil {
				h = *authHeader
			} else if cur.AuthHeader != nil {
				h = *cur.AuthHeader
			}
			kek, err := s.needKEK()
			if err != nil {
				return err
			}
			e := seal.SealSecret(kek, *secret, pluginAAD(name, url, h))
			cols = []any{h, e.SecretSealed, e.DEKSealed, e.KEKID, last4(*secret)}
		default:
			h := cur.AuthHeader
			if authSent {
				h = authHeader
			}
			if cur.AuthHeader == nil {
				if h != nil {
					return refused(http.StatusBadRequest, "auth_header needs a secret")
				}
			} else if url != cur.URL || h == nil || *h != *cur.AuthHeader {
				return refused(http.StatusConflict, "changing url or auth_header needs the secret"+
					" again (or secret: null to remove it): the stored one is not sent to a new destination")
			}
			cols = []any{cur.AuthHeader, current.secretSealed, current.dekSealed, cur.KEKID, cur.Last4}
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_plugins SET url = $1, auth_header = $2,
			secret_sealed = $3, dek_sealed = $4, kek_id = $5, last4 = $6, updated_at = now()
			WHERE name = $7`, append(append([]any{url}, cols...), name)...); err != nil {
			return err
		}
		// The last listing was the old server's (#4994).
		if url != cur.URL {
			if _, err := tx.Exec(ctx, `DELETE FROM ai_tool_listings WHERE source = $1`,
				remoteSource(name)); err != nil {
				return err
			}
		}
		// A sign-in was for the old resource; a header replaces it (#780).
		if url != cur.URL || secret != nil {
			if err := dropSignIn(ctx, tx, name); err != nil {
				return err
			}
		}
		if err := writeAccess(ctx, tx, name, toolTiers, disabled, enabled, by,
			enabledIn != nil, tierEntries != nil || disabledIn != nil); err != nil {
			return err
		}
		out, err = selectOne(ctx, tx, name)
		return err
	})
	if err != nil {
		return nil, err
	}
	return s.viewOf(out), nil
}

func dropSignIn(ctx context.Context, tx pgx.Tx, plugin string) error {
	if _, err := tx.Exec(ctx, `DELETE FROM ai_plugin_oauth_flows WHERE plugin = $1`, plugin); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `DELETE FROM ai_plugin_oauth WHERE plugin = $1`, plugin)
	return err
}

// keptRow is a tool's access row as it stood, put back after the rewrite.
type keptRow struct {
	Item      string `json:"item"`
	Access    string `json:"access"`
	UpdatedBy string `json:"updated_by"`
	UpdatedAt string `json:"updated_at"`
}

// toolAccess is PluginStore.setToolAccess: off disables the tool, a tier sets it, null drops
// both.
func (s *Plugins) toolAccess(r *http.Request, p crud.Params) (any, error) {
	name, err := p.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	f, err := readForm(r, "tool", "access", "by")
	if err != nil {
		return nil, err
	}
	tool := f.text("tool", required, 1, toolMax, nil)
	access := f.literal("access", nullable, toolAccess...)
	by := f.textOr("by", "system", 1, byMax)
	if err := f.err(); err != nil {
		return nil, err
	}
	var out *plugin
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		var locked string
		err := tx.QueryRow(ctx, `SELECT name FROM ai_plugins WHERE name = $1 FOR UPDATE`, name).Scan(&locked)
		if errors.Is(err, pgx.ErrNoRows) {
			return refused(http.StatusNotFound, fmt.Sprintf("no plugin named %q", name))
		}
		if err != nil {
			return err
		}
		current, err := selectOne(ctx, tx, name)
		if err != nil {
			return err
		}
		toolTiers, disabled, enabled := current.stored()
		source := remoteSource(name)
		// Each tool row as it stands, so only this tool's is attributed to this write.
		rows, err := tx.Query(ctx, `SELECT item, access, updated_by, updated_at::text
			FROM ai_access WHERE source = $1 AND item <> $2`, source, sourceItem)
		if err != nil {
			return err
		}
		kept := []keptRow{}
		for rows.Next() {
			var k keptRow
			if err := rows.Scan(&k.Item, &k.Access, &k.UpdatedBy, &k.UpdatedAt); err != nil {
				rows.Close()
				return err
			}
			if k.Item != *tool {
				kept = append(kept, k)
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
		delete(toolTiers, *tool)
		disabled = slices.DeleteFunc(disabled, func(t string) bool { return t == *tool })
		if access != nil && *access == "off" {
			disabled = append(disabled, *tool)
		} else if access != nil {
			toolTiers[*tool] = *access
		}
		if len(disabled) > maxToolEntries {
			return refused(http.StatusBadRequest, fmt.Sprintf("At most %d tools of plugin %q can"+
				" be off: reset one of them first", maxToolEntries, name))
		}
		if len(toolTiers) > maxToolEntries {
			return refused(http.StatusBadRequest, fmt.Sprintf("At most %d tools of plugin %q can"+
				" have an access other than off or the default: reset one of them first",
				maxToolEntries, name))
		}
		if err := writeAccess(ctx, tx, name, toolTiers, jsSort(disabled), enabled, by,
			false, true); err != nil {
			return err
		}
		// Every tool row was written as new: the rows this write left get their author back.
		if len(kept) > 0 {
			raw, err := json.Marshal(kept)
			if err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `UPDATE ai_access a SET updated_by = k.updated_by,
				updated_at = k.updated_at FROM jsonb_to_recordset($1::text::jsonb) AS k(item text,
				access text, updated_by text, updated_at timestamptz)
				WHERE a.source = $2 AND a.item = k.item AND a.access = k.access`,
				string(raw), source); err != nil {
				return err
			}
		}
		out, err = selectOne(ctx, tx, name)
		return err
	})
	if err != nil {
		return nil, err
	}
	return s.viewOf(out), nil
}
