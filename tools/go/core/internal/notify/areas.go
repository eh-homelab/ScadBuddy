// Package notify is the agent's Web Push records and notification channels:
// backend/scadbuddy/api/crud_agent_notify.py, statement for statement (area notify).
//
// The API seals and opens their secrets with the KEK under the agent's own AADs
// (agent_data/notify.py), so a row either side writes opens on the other. GET /push/waiting
// stays in Python: it reads the flow grants (flow_triggers).
package notify

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	subscriptionsPerOwner = 5
	sentKeepDays          = 2
	endpointMax           = 2048
	tokenMax              = 4096
	errorMax              = 500
	vapidAAD              = "push-vapid:1"
)

var (
	pointHex   = regexp.MustCompile(`^04[0-9a-f]{128}$`)
	b64url     = regexp.MustCompile(`^[A-Za-z0-9_-]+={0,2}$`)
	pollName   = regexp.MustCompile(`^[a-z][a-z0-9-]*$`)
	httpURL    = regexp.MustCompile(`^https?://`)
	targetName = regexp.MustCompile(`^[a-z0-9_]{1,100}$`)
	oneLine    = regexp.MustCompile(`^[^\r\n]+$`)
	ownerKinds = []string{"browser", "bearer", "oidc", "anonymous", "flow"}
)

func subscriptionAAD(id, endpoint string) string { return "push-subscription:" + id + ":" + endpoint }
func channelAAD(id, kind, url string) string     { return "notify-channel:" + id + ":" + kind + ":" + url }

// last4 is secrets.ts last4: empty for a secret under 12 characters.
func last4(secret string) string {
	r := []rune(secret)
	if len(r) < 12 {
		return ""
	}
	return string(r[len(r)-4:])
}

// Store is the area's routes.
type Store struct {
	Pool *pgxpool.Pool
	Keys *seal.Keys
}

// Areas are the package's routes by area name.
func Areas(pool *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route {
	s := &Store{Pool: pool, Keys: keys}
	reads := []crud.Route{
		{Method: "GET", Path: "/push/vapid", Handle: s.vapid},
		{Method: "GET", Path: "/push/subscriptions", Handle: s.subscriptions},
		{Method: "GET", Path: "/notify-channels", Handle: s.channels},
		{Method: "GET", Path: "/notify-channels/{channel_id}", Handle: s.channel},
	}
	return map[string][]crud.Route{
		"notify:read": reads,
		"notify": append(reads,
			crud.Route{Method: "POST", Path: "/push/vapid", Handle: s.storeVapid},
			crud.Route{Method: "POST", Path: "/push/subscriptions", Handle: s.subscribe},
			crud.Route{Method: "POST", Path: "/push/subscriptions/remove", Handle: s.unsubscribe},
			crud.Route{Method: "POST", Path: "/push/sent", Handle: s.sent},
			crud.Route{Method: "POST", Path: "/push/poll-lease", Handle: s.pollLease},
			crud.Route{Method: "POST", Path: "/notify-channels", Handle: s.create, Status: http.StatusCreated},
			crud.Route{Method: "POST", Path: "/notify-channels/open-enabled", Handle: s.openEnabled},
			crud.Route{Method: "POST", Path: "/notify-channels/{channel_id}/update", Handle: s.update},
			crud.Route{Method: "POST", Path: "/notify-channels/{channel_id}/delete", Handle: s.delete},
			crud.Route{Method: "POST", Path: "/notify-channels/{channel_id}/open", Handle: s.open},
			crud.Route{Method: "POST", Path: "/notify-channels/{channel_id}/record", Handle: s.record},
		),
	}
}

func (s *Store) kek() (*seal.KEK, error) {
	if s.Keys == nil || s.Keys.Current == nil {
		return nil, httpx.WithReason(http.StatusServiceUnavailable,
			"The API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE).", "kek-missing")
	}
	return s.Keys.Current, nil
}

func (s *Store) inTx(ctx context.Context, fn func(pgx.Tx) error) error {
	return pgx.BeginFunc(ctx, s.Pool, fn)
}

func invalid(detail string) error {
	return &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: detail}
}

type done struct {
	Done bool `json:"done"`
}

// -- Web Push ------------------------------------------------------------------------------

type vapid struct {
	PublicKey  string `json:"public_key"`
	PrivateKey string `json:"private_key"`
}

type vapidAnswer struct {
	Vapid *vapid `json:"vapid"`
}

func readVapid(ctx context.Context, tx pgx.Tx, kek *seal.KEK) (*vapid, error) {
	var public []byte
	var e seal.Envelope
	err := tx.QueryRow(ctx, `SELECT public_key, secret_sealed, dek_sealed, kek_id
		FROM ai_push_vapid WHERE id = 1`).Scan(&public, &e.SecretSealed, &e.DEKSealed, &e.KEKID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	private, err := seal.OpenSecret(kek, e, vapidAAD)
	if err != nil {
		return nil, &httpx.Problem{Status: http.StatusConflict, Detail: "the VAPID key does not open: " + err.Error()}
	}
	return &vapid{hex.EncodeToString(public), private}, nil
}

func (s *Store) vapid(r *http.Request, _ crud.Params) (any, error) {
	kek, err := s.kek()
	if err != nil {
		return nil, err
	}
	var out *vapid
	err = s.inTx(r.Context(), func(tx pgx.Tx) (err error) {
		out, err = readVapid(r.Context(), tx, kek)
		return err
	})
	if err != nil {
		return nil, err
	}
	return vapidAnswer{out}, nil
}

// storeVapid stores the pair the agent made unless one is stored, and answers the stored one.
func (s *Store) storeVapid(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "public_key", "private_key")
	if err != nil {
		return nil, err
	}
	public := f.text("public_key", true, false, 0, 1<<30, pointHex)
	private := f.text("private_key", true, false, 40, 64, b64url)
	if err := f.err(); err != nil {
		return nil, err
	}
	kek, err := s.kek()
	if err != nil {
		return nil, err
	}
	sealed := seal.SealSecret(kek, *private, vapidAAD)
	point, _ := hex.DecodeString(*public)
	var out *vapid
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		if _, err := tx.Exec(r.Context(),
			`INSERT INTO ai_push_vapid (id, public_key, secret_sealed, dek_sealed, kek_id)
			 VALUES (1, $1, $2, $3, $4) ON CONFLICT (id) DO NOTHING`,
			point, sealed.SecretSealed, sealed.DEKSealed, sealed.KEKID); err != nil {
			return err
		}
		var err error
		out, err = readVapid(r.Context(), tx, kek)
		return err
	})
	if err != nil {
		return nil, err
	}
	return vapidAnswer{out}, nil
}

type owner struct{ kind, id, label string }

// readOwner is the object name as an Owner; nil when it is absent, null or wrong.
func readOwner(f *form, name string, required bool) *owner {
	n := f.nested(name, required, !required, "kind", "id", "label")
	if n == nil {
		return nil
	}
	kind := n.choice("kind", ownerKinds...)
	id := n.text("id", true, false, 1, 500, nil)
	label := n.text("label", true, false, 0, 500, nil)
	if id == nil || label == nil || kind == "" {
		return nil
	}
	return &owner{kind, *id, *label}
}

func (s *Store) subscribe(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "owner", "endpoint", "p256dh", "auth")
	if err != nil {
		return nil, err
	}
	who := readOwner(f, "owner", true)
	endpoint := f.text("endpoint", true, false, 8, endpointMax, nil)
	p256dh := f.text("p256dh", true, false, 86, 88, b64url)
	auth := f.text("auth", true, false, 22, 24, b64url)
	if err := f.err(); err != nil {
		return nil, err
	}
	kek, err := s.kek()
	if err != nil {
		return nil, err
	}
	// The plaintext the agent seals (pushStore.ts subscribe), so either side opens it.
	keys, _ := json.Marshal(struct {
		P256dh string `json:"p256dh"`
		Auth   string `json:"auth"`
	}{*p256dh, *auth})
	secret := string(keys)
	var id string
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		ctx := r.Context()
		var held, kind, heldBy string
		err := tx.QueryRow(ctx, `SELECT id::text, owner_kind, owner_id FROM ai_push_subscriptions
			WHERE endpoint = $1 FOR UPDATE`, *endpoint).Scan(&held, &kind, &heldBy)
		found := err == nil
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if found && (kind != who.kind || heldBy != who.id) {
			return httpx.WithReason(http.StatusConflict,
				"this push endpoint is subscribed for someone else", "subscription-taken")
		}
		if found {
			// The same browser again: the same id, so what was pushed to it is not pushed
			// again; its keys sealed again, and it counts as the newest.
			id = held
			sealed := seal.SealSecret(kek, secret, subscriptionAAD(id, *endpoint))
			_, err := tx.Exec(ctx, `UPDATE ai_push_subscriptions SET owner_label = $1,
				secret_sealed = $2, dek_sealed = $3, kek_id = $4, created_at = now()
				WHERE id = $5::text::uuid`,
				who.label, sealed.SecretSealed, sealed.DEKSealed, sealed.KEKID, id)
			return err
		}
		id = newUUID()
		sealed := seal.SealSecret(kek, secret, subscriptionAAD(id, *endpoint))
		if _, err := tx.Exec(ctx, `INSERT INTO ai_push_subscriptions (id, owner_kind, owner_id,
			owner_label, endpoint, secret_sealed, dek_sealed, kek_id)
			VALUES ($1::text::uuid, $2, $3, $4, $5, $6, $7, $8)`,
			id, who.kind, who.id, who.label, *endpoint, sealed.SecretSealed, sealed.DEKSealed,
			sealed.KEKID); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `DELETE FROM ai_push_subscriptions WHERE id IN (
			SELECT id FROM ai_push_subscriptions WHERE owner_kind = $1 AND owner_id = $2
			ORDER BY created_at DESC, id OFFSET $3)`, who.kind, who.id, subscriptionsPerOwner)
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		ID string `json:"id"`
	}{id}, nil
}

func (s *Store) unsubscribe(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "endpoint", "owner")
	if err != nil {
		return nil, err
	}
	endpoint := f.text("endpoint", true, false, 1, endpointMax, nil)
	who := readOwner(f, "owner", false)
	if err := f.err(); err != nil {
		return nil, err
	}
	var gone bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		var err error
		var tag interface{ RowsAffected() int64 }
		if who == nil {
			tag, err = tx.Exec(r.Context(),
				`DELETE FROM ai_push_subscriptions WHERE endpoint = $1`, *endpoint)
		} else {
			tag, err = tx.Exec(r.Context(), `DELETE FROM ai_push_subscriptions
				WHERE endpoint = $1 AND owner_kind = $2 AND owner_id = $3`, *endpoint, who.kind, who.id)
		}
		if err == nil {
			gone = tag.RowsAffected() > 0
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{gone}, nil
}

type openSubscription struct {
	ID    string `json:"id"`
	Owner struct {
		Kind  string `json:"kind"`
		ID    string `json:"id"`
		Label string `json:"label"`
	} `json:"owner"`
	Endpoint string `json:"endpoint"`
	P256dh   string `json:"p256dh"`
	Auth     string `json:"auth"`
}

// subscriptions are every subscription whose keys the KEK opens; one it cannot is skipped.
func (s *Store) subscriptions(r *http.Request, _ crud.Params) (any, error) {
	kek, err := s.kek()
	if err != nil {
		return nil, err
	}
	type row struct {
		openSubscription
		e seal.Envelope
	}
	var rows []row
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		found, err := tx.Query(r.Context(), `SELECT id::text AS id, owner_kind, owner_id,
			owner_label, endpoint, secret_sealed, dek_sealed, kek_id
			FROM ai_push_subscriptions ORDER BY created_at`)
		if err != nil {
			return err
		}
		defer found.Close()
		for found.Next() {
			var x row
			o := &x.Owner
			if err := found.Scan(&x.ID, &o.Kind, &o.ID, &o.Label, &x.Endpoint,
				&x.e.SecretSealed, &x.e.DEKSealed, &x.e.KEKID); err != nil {
				return err
			}
			rows = append(rows, x)
		}
		return found.Err()
	})
	if err != nil {
		return nil, err
	}
	out := []openSubscription{}
	for _, x := range rows {
		text, err := seal.OpenSecret(kek, x.e, subscriptionAAD(x.ID, x.Endpoint))
		if err != nil {
			continue
		}
		var keys struct {
			P256dh *string `json:"p256dh"`
			Auth   *string `json:"auth"`
		}
		if json.Unmarshal([]byte(text), &keys) != nil {
			continue
		}
		if keys.P256dh == nil || keys.Auth == nil {
			return nil, fmt.Errorf("subscription %s holds no keys", x.ID)
		}
		x.P256dh, x.Auth = *keys.P256dh, *keys.Auth
		out = append(out, x.openSubscription)
	}
	return struct {
		Subscriptions []openSubscription `json:"subscriptions"`
	}{out}, nil
}

// sent claims telling target of request_id: true for the one caller that sends it. Rows older
// than sentKeepDays go at the same time.
func (s *Store) sent(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "request_id", "target")
	if err != nil {
		return nil, err
	}
	request := f.text("request_id", true, false, 1, 1024, nil)
	target := f.text("target", true, false, 1, 300, nil)
	if err := f.err(); err != nil {
		return nil, err
	}
	var claimed bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		if _, err := tx.Exec(r.Context(), `DELETE FROM ai_notifications_sent
			WHERE sent_at < now() - make_interval(days => $1)`, sentKeepDays); err != nil {
			return err
		}
		var one int
		err := tx.QueryRow(r.Context(), `INSERT INTO ai_notifications_sent (request_id, target)
			VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING 1`, *request, *target).Scan(&one)
		claimed = err == nil
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Claimed bool `json:"claimed"`
	}{claimed}, nil
}

// pollLease lets one agent replica a window run a timed poll (#6399): taken when the row is new
// or its last take is at least seconds old by the database's clock.
func (s *Store) pollLease(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "name", "seconds")
	if err != nil {
		return nil, err
	}
	name := f.text("name", true, false, 1, 64, pollName)
	seconds := f.integer("seconds", 1, 3600)
	if err := f.err(); err != nil {
		return nil, err
	}
	var held bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		var one int
		err := tx.QueryRow(r.Context(), `INSERT INTO ai_notifications_sent (request_id, target)
			VALUES ($1, 'lease') ON CONFLICT (request_id, target) DO UPDATE SET sent_at = now()
			WHERE ai_notifications_sent.sent_at <= now() - make_interval(secs => $2)
			RETURNING 1`, "lease:"+*name, float64(seconds)).Scan(&one)
		held = err == nil
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Held bool `json:"held"`
	}{held}, nil
}

// -- notification channels ----------------------------------------------------------------

type channelFields struct {
	kind, name, url string
	target, token   *string
	enabled         bool
	keepToken       bool
}

func readChannel(r *http.Request, update bool) (*channelFields, error) {
	names := []string{"kind", "name", "url", "target", "enabled", "token"}
	if update {
		names = append(names, "keep_token")
	}
	f, err := readForm(r, names...)
	if err != nil {
		return nil, err
	}
	c := &channelFields{
		kind:    f.choice("kind", "ntfy", "home_assistant"),
		target:  f.text("target", false, true, 0, 1<<30, targetName),
		enabled: f.flag("enabled", true),
		token:   f.text("token", false, true, 1, tokenMax, oneLine),
	}
	if name := f.text("name", true, false, 1, 100, nil); name != nil {
		c.name = *name
	}
	if url := f.text("url", true, false, 8, 2048, httpURL); url != nil {
		c.url = *url
	}
	if update {
		c.keepToken = f.flag("keep_token", false)
	}
	return c, f.err()
}

func checkTarget(c *channelFields, hasToken bool) error {
	if (c.kind == "home_assistant") != (c.target != nil) {
		return invalid("a Home Assistant channel names its notify service; ntfy names none")
	}
	if c.kind == "home_assistant" && !hasToken {
		return invalid("Home Assistant needs a long-lived access token")
	}
	return nil
}

type channelView struct {
	ID         string  `json:"id"`
	Kind       string  `json:"kind"`
	Name       string  `json:"name"`
	URL        string  `json:"url"`
	Target     *string `json:"target"`
	Enabled    bool    `json:"enabled"`
	HasToken   bool    `json:"has_token"`
	TokenLast4 string  `json:"token_last4"`
	LastError  *string `json:"last_error"`
	LastSentAt *stamp  `json:"last_sent_at"`
}

const view = `id::text AS id, kind, name, url, target, enabled,
	secret_sealed IS NOT NULL AS has_token, token_last4, last_error, last_sent_at`

func scanView(row pgx.CollectableRow) (channelView, error) {
	var v channelView
	var sent *time.Time
	err := row.Scan(&v.ID, &v.Kind, &v.Name, &v.URL, &v.Target, &v.Enabled, &v.HasToken,
		&v.TokenLast4, &v.LastError, &sent)
	if sent != nil {
		v.LastSentAt = (*stamp)(sent)
	}
	return v, err
}

func (s *Store) channels(r *http.Request, _ crud.Params) (any, error) {
	var out []channelView
	err := s.inTx(r.Context(), func(tx pgx.Tx) error {
		rows, err := tx.Query(r.Context(),
			`SELECT `+view+` FROM ai_notify_channels ORDER BY created_at, id`)
		if err != nil {
			return err
		}
		out, err = pgx.CollectRows(rows, scanView)
		return err
	})
	if err != nil {
		return nil, err
	}
	if out == nil {
		out = []channelView{}
	}
	return struct {
		Channels []channelView `json:"channels"`
	}{out}, nil
}

func (s *Store) channel(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("channel_id")
	if err != nil {
		return nil, err
	}
	var out *channelView
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		rows, err := tx.Query(r.Context(),
			`SELECT `+view+` FROM ai_notify_channels WHERE id = $1::text::uuid`, id)
		if err != nil {
			return err
		}
		found, err := pgx.CollectRows(rows, scanView)
		if len(found) > 0 {
			out = &found[0]
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Channel *channelView `json:"channel"`
	}{out}, nil
}

// sealedToken is the token's sealed columns, all null for no token.
func sealedToken(kek *seal.KEK, id string, c *channelFields) []any {
	if c.token == nil {
		return []any{nil, nil, nil, ""}
	}
	e := seal.SealSecret(kek, *c.token, channelAAD(id, c.kind, c.url))
	return []any{e.SecretSealed, e.DEKSealed, e.KEKID, last4(*c.token)}
}

func (s *Store) create(r *http.Request, _ crud.Params) (any, error) {
	c, err := readChannel(r, false)
	if err != nil {
		return nil, err
	}
	if err := checkTarget(c, c.token != nil); err != nil {
		return nil, err
	}
	var kek *seal.KEK
	if c.token != nil {
		if kek, err = s.kek(); err != nil {
			return nil, err
		}
	}
	id := newUUID()
	sealed := sealedToken(kek, id, c)
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `INSERT INTO ai_notify_channels (id, kind, name, url, target,
			enabled, secret_sealed, dek_sealed, kek_id, token_last4)
			VALUES ($1::text::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
			append([]any{id, c.kind, c.name, c.url, c.target, c.enabled}, sealed...)...)
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		ID string `json:"id"`
	}{id}, nil
}

// update changes a channel under its row lock. keep_token keeps the sealed token only while
// the kind and URL are the row's (409 channel-moved otherwise), and resets last_error.
func (s *Store) update(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("channel_id")
	if err != nil {
		return nil, err
	}
	c, err := readChannel(r, true)
	if err != nil {
		return nil, err
	}
	if c.keepToken && c.token != nil {
		return nil, invalid("keep_token takes no token")
	}
	var kek *seal.KEK
	if c.token != nil {
		if kek, err = s.kek(); err != nil {
			return nil, err
		}
	}
	var changed bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		ctx := r.Context()
		var kind, url string
		var hasToken bool
		err := tx.QueryRow(ctx, `SELECT kind, url, secret_sealed IS NOT NULL AS has_token
			FROM ai_notify_channels WHERE id = $1::text::uuid FOR UPDATE`, id).Scan(&kind, &url, &hasToken)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		changed = true
		if c.keepToken {
			if kind != c.kind || url != c.url || !hasToken {
				return httpx.WithReason(http.StatusConflict,
					"the channel changed since it was read; its token no longer applies", "channel-moved")
			}
			if err := checkTarget(c, true); err != nil {
				return err
			}
			_, err := tx.Exec(ctx, `UPDATE ai_notify_channels SET name = $1, target = $2,
				enabled = $3, last_error = NULL WHERE id = $4::text::uuid`, c.name, c.target, c.enabled, id)
			return err
		}
		if err := checkTarget(c, c.token != nil); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE ai_notify_channels SET kind = $1, name = $2, url = $3,
			target = $4, enabled = $5, secret_sealed = $6, dek_sealed = $7, kek_id = $8,
			token_last4 = $9, last_error = NULL WHERE id = $10::text::uuid`,
			append(append([]any{c.kind, c.name, c.url, c.target, c.enabled}, sealedToken(kek, id, c)...), id)...)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{changed}, nil
}

func (s *Store) delete(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("channel_id")
	if err != nil {
		return nil, err
	}
	var gone bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		tag, err := tx.Exec(r.Context(), `DELETE FROM ai_notify_channels WHERE id = $1::text::uuid`, id)
		gone = err == nil && tag.RowsAffected() > 0
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{gone}, nil
}

type openChannel struct {
	ID     string  `json:"id"`
	Kind   string  `json:"kind"`
	URL    string  `json:"url"`
	Target *string `json:"target"`
	Token  *string `json:"token"`
	sealed *seal.Envelope
}

const opened = `id::text AS id, kind, url, target, secret_sealed, dek_sealed, kek_id`

func scanOpen(row pgx.CollectableRow) (openChannel, error) {
	var c openChannel
	var e seal.Envelope
	var kekID *string
	if err := row.Scan(&c.ID, &c.Kind, &c.URL, &c.Target, &e.SecretSealed, &e.DEKSealed, &kekID); err != nil {
		return c, err
	}
	if e.SecretSealed != nil {
		if kekID != nil {
			e.KEKID = *kekID
		}
		c.sealed = &e
	}
	return c, nil
}

// opens fills in the channel's token; a seal error when kek is nil or does not open it.
func (c *openChannel) opens(kek *seal.KEK) error {
	if c.sealed == nil {
		return nil
	}
	if kek == nil {
		return errors.New("no key-encryption key")
	}
	token, err := seal.OpenSecret(kek, *c.sealed, channelAAD(c.ID, c.Kind, c.URL))
	if err != nil {
		return err
	}
	c.Token = &token
	return nil
}

func (s *Store) readOpen(ctx context.Context, where string, args ...any) ([]openChannel, error) {
	var out []openChannel
	err := s.inTx(ctx, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT `+opened+` FROM ai_notify_channels `+where, args...)
		if err != nil {
			return err
		}
		out, err = pgx.CollectRows(rows, scanOpen)
		return err
	})
	return out, err
}

// open is one channel with its token opened, for a send. A token the KEK does not open is 409.
func (s *Store) open(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("channel_id")
	if err != nil {
		return nil, err
	}
	rows, err := s.readOpen(r.Context(), `WHERE id = $1::text::uuid`, id)
	if err != nil {
		return nil, err
	}
	answer := struct {
		Channel *openChannel `json:"channel"`
	}{}
	if len(rows) == 0 {
		return answer, nil
	}
	c := rows[0]
	var kek *seal.KEK
	if c.sealed != nil {
		if kek, err = s.kek(); err != nil {
			return nil, err
		}
	}
	if err := c.opens(kek); err != nil {
		return nil, &httpx.Problem{Status: http.StatusConflict,
			Detail: "the channel's token does not open: " + err.Error()}
	}
	answer.Channel = &c
	return answer, nil
}

// openEnabled are the enabled channels whose tokens the KEK opens; without a KEK, those with
// no token.
func (s *Store) openEnabled(r *http.Request, _ crud.Params) (any, error) {
	kek, _ := s.kek()
	rows, err := s.readOpen(r.Context(), `WHERE enabled ORDER BY created_at, id`)
	if err != nil {
		return nil, err
	}
	out := []openChannel{}
	for _, c := range rows {
		if c.opens(kek) == nil {
			out = append(out, c)
		}
	}
	return struct {
		Channels []openChannel `json:"channels"`
	}{out}, nil
}

func (s *Store) record(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("channel_id")
	if err != nil {
		return nil, err
	}
	f, err := readForm(r, "error")
	if err != nil {
		return nil, err
	}
	failure := f.text("error", false, true, 0, 10_000, nil)
	if err := f.err(); err != nil {
		return nil, err
	}
	var changed bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		var err error
		var tag interface{ RowsAffected() int64 }
		if failure == nil {
			tag, err = tx.Exec(r.Context(), `UPDATE ai_notify_channels SET last_error = NULL,
				last_sent_at = now() WHERE id = $1::text::uuid`, id)
		} else {
			cut := []rune(*failure)
			if len(cut) > errorMax {
				cut = cut[:errorMax]
			}
			tag, err = tx.Exec(r.Context(), `UPDATE ai_notify_channels SET last_error = $1
				WHERE id = $2::text::uuid`, string(cut), id)
		}
		if err == nil {
			changed = tag.RowsAffected() > 0
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{changed}, nil
}
