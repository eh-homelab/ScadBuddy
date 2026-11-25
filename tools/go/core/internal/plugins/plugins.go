// Package plugins is the routes of backend/scadbuddy/api/crud_agent_plugins.py (area
// plugins) that neither seal nor open a value with the KEK: a plugin's delete, and a
// sign-in's mark and forget. Statement for statement, with the warm-evict NOTIFY in the
// transaction where Python sends it.
//
// Still Python's, as they seal or open with the KEK or answer whether the KEK opens a row
// (opens): the plugin reads, create, update, tool-access, reveal and open-enabled, and the
// sign-in flows, the grant's store, open and refresh, and stored-secrets.
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
	textMax      = 2048
	bodyBytesMax = 8 * 1024 * 1024 // api/limits.py MAX_TEXT_BODY_BYTES
)

// namePattern is registry.ts PLUGIN_NAME_RE.
var namePattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,30}[a-z0-9]$`)

// Plugins is the area's routes Go has.
type Plugins struct{ Pool *pgxpool.Pool }

// Areas are the package's routes by area name.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	return map[string][]crud.Route{"plugins": (&Plugins{Pool: pool}).Routes()}
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
