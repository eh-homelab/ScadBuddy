// Package plugins is the agent's remote plugins and their OAuth sign-ins:
// backend/scadbuddy/api/crud_agent_plugins.py, statement for statement, with the warm-evict
// NOTIFY in the transaction where Python sends it.
//
// Area plugins is the routes that neither seal nor open a value with the KEK: a plugin's
// delete, and a sign-in's mark and forget. Area remote-plugins is every route of the file:
// those, the plugin reads, create, update, tool-access, reveal and open-enabled, and the
// sign-in flows, the grant's store, open and refresh, and stored-secrets. These seal and
// open with the KEK under the agent's own AADs (agent_data/plugins.py, aad.go), so a row
// Python or Go writes opens on the other. A row sealed under the previous KEK opens too,
// during a rotation; Python's start (agent_data/rotation.py) re-wraps it.
//
// Only a route's own opened value carries a plaintext: no log record, span or refusal
// holds a token, a secret, a sealed value or an OAuth state.
package plugins

import (
	"context"
	"net/http"
	"regexp"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

const (
	urlMax       = 2048
	textMax      = 2048
	tokenMax     = 16384
	byMax        = 600
	toolMax      = 128
	bodyBytesMax = 8 * 1024 * 1024 // api/limits.py MAX_TEXT_BODY_BYTES
)

// namePattern is registry.ts PLUGIN_NAME_RE.
var namePattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,30}[a-z0-9]$`)

// Plugins is the areas' routes.
type Plugins struct {
	Pool *pgxpool.Pool
	Keys *seal.Keys
}

// Areas are the package's routes by area name.
func Areas(pool *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route {
	s := &Plugins{Pool: pool, Keys: keys}
	return map[string][]crud.Route{"plugins": s.Routes(), "remote-plugins": s.AllRoutes()}
}

// AllRoutes are every route of crud_agent_plugins.py.
func (s *Plugins) AllRoutes() []crud.Route {
	post := func(path string, h crud.Handler, status int) crud.Route {
		return crud.Route{Method: "POST", Path: path, Handle: h, MaxBody: bodyBytesMax, Status: status}
	}
	return append(s.Routes(),
		crud.Route{Method: "GET", Path: "/plugins", Handle: s.list},
		crud.Route{Method: "GET", Path: "/plugins/{name}", Handle: s.one},
		post("/plugins", s.create, http.StatusCreated),
		crud.Route{Method: "POST", Path: "/plugins/open-enabled", Handle: s.openEnabled},
		post("/plugins/{name}/update", s.update, 0),
		post("/plugins/{name}/tool-access", s.toolAccess, 0),
		crud.Route{Method: "POST", Path: "/plugins/{name}/reveal", Handle: s.reveal},
		post("/plugin-oauth/flows", s.flowStart, http.StatusCreated),
		post("/plugin-oauth/flows/authorize", s.flowAuthorize, 0),
		post("/plugin-oauth/flows/take", s.flowTake, 0),
		post("/plugin-oauth/grants", s.grantStore, 0),
		post("/plugin-oauth/grants/{plugin}/open", s.grantOpen, 0),
		post("/plugin-oauth/grants/{plugin}/refreshed", s.grantRefreshed, 0),
		crud.Route{Method: "POST", Path: "/plugin-oauth/stored-secrets", Handle: s.storedSecrets},
	)
}

// Routes are the plugins area's routes that need no KEK.
func (s *Plugins) Routes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/plugins/{name}/delete", Handle: s.delete},
		{Method: "POST", Path: "/plugin-oauth/grants/{plugin}/mark", Handle: s.mark, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/plugin-oauth/grants/{plugin}/forget", Handle: s.forget},
	}
}

type done struct {
	Done bool `json:"done"`
}

func (s *Plugins) delete(r *http.Request, p crud.Params) (any, error) {
	name, err := p.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var gone bool
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `DELETE FROM ai_plugins WHERE name = $1`, name)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		gone = true
		if err := forgetSource(ctx, tx, "remote:"+name); err != nil {
			return err
		}
		return wire.NotifyWarmEvict(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	return done{gone}, nil
}

// forgetSource is agent_data/access.py forget_source: everything stored for source, its
// tool listing included.
func forgetSource(ctx context.Context, tx pgx.Tx, source string) error {
	if _, err := tx.Exec(ctx, `DELETE FROM ai_access WHERE source = $1`, source); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `DELETE FROM ai_tool_listings WHERE source = $1`, source)
	return err
}

// mark marks the sign-in failed (PluginOAuth.mark): "Sign in again" in Settings. With a
// grant, only while that grant is the plugin's.
func (s *Plugins) mark(r *http.Request, p crud.Params) (any, error) {
	plugin, err := p.Match("plugin", namePattern)
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "reason", "grant")
	if err != nil {
		return nil, err
	}
	reason := body.Text("reason", 1, textMax)
	grant := body.OptUUID("grant")
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var marked bool
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		sql, args := `UPDATE ai_plugin_oauth SET status = 'error', last_error = $1
			WHERE plugin = $2 AND status = 'connected'`, []any{reason, plugin}
		if grant != nil {
			sql += ` AND grant_id = $3::text::uuid`
			args = append(args, *grant)
		}
		tag, err := tx.Exec(ctx, sql, args...)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		marked = true
		return wire.NotifyWarmEvict(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	return done{marked}, nil
}

// forget signs the plugin out (PluginOAuth.forget): the grant and any flow under way.
func (s *Plugins) forget(r *http.Request, p crud.Params) (any, error) {
	plugin, err := p.Match("plugin", namePattern)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var gone bool
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `DELETE FROM ai_plugin_oauth_flows WHERE plugin = $1`, plugin); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `DELETE FROM ai_plugin_oauth WHERE plugin = $1`, plugin)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		gone = true
		return wire.NotifyWarmEvict(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	return done{gone}, nil
}
