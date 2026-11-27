package tagreads

import (
	"context"
	"errors"
	"net/http"
	"slices"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// recordIn is what one MIFARE read records.
type recordIn struct {
	// uid is the read's UID, cleaned (CleanUID); tray is block 9's tray UUID, cleaned, or ""
	// when block 9 was not read.
	uid, tray string
	tagType   *string
	actor     string
	// source is the read's source: phone, app, agent or ams.
	source   string
	dumpKey  string
	complete bool
	// bambu is a read that decodes to Bambu fields (a tray UUID or a filament type): only it
	// makes the tag a bambu_mifare tag.
	bambu bool
	// decoded is the decoder's JSON without the raw blocks, which the dump holds.
	decoded []byte
	scan    scanFacts
	// noTap is a read the page sent with tap false (a lookup of a tag it read before): the
	// tag is written, but no history row and no hold of its dump or its photo.
	noTap bool
}

type recorded struct {
	tagID, readID string
}

// tagRow is a tag as record locks it.
type tagRow struct {
	id   string
	tray *string
}

// tries bounds record's retries of a transaction that lost a race to make a tag or a UID.
const tries = 3

// record writes a MIFARE read in one transaction: the tag found or made, the UID added, a
// UID-only tag folded in, raw_blob from a complete read, the history row and the dump's hold.
func record(ctx context.Context, pool *pgxpool.Pool, in recordIn) (recorded, error) {
	var out recorded
	var err error
	for range tries {
		err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
			var err error
			out, err = write(ctx, tx, in)
			return err
		})
		var pg *pgconn.PgError
		if errors.As(err, &pg) && (pg.Code == "23505" || pg.Code == "40P01") {
			// Another read made the tag or took the UID first, or two reads that cross locked
			// the tray's tag and the UID's tag in turn (a deadlock): look again.
			continue
		}
		return out, err
	}
	return out, err
}

// trayTag and uidTag find the tag of a tray UUID and of a UID, without a lock.
const (
	trayTag = "SELECT id::text FROM filament_tags WHERE tray_uuid = $1"
	uidTag  = "SELECT tag_id::text FROM filament_tag_uids WHERE uid = $1"
)

// lockInOrder locks the tags of the read's tray UUID and UID in id order, so two reads that
// cross (each with the other's tray and UID) never wait on each other in turn. It answers
// the ids it locked; write checks them again under the locks.
func lockInOrder(ctx context.Context, tx pgx.Tx, in recordIn) ([]string, error) {
	var ids []string
	for _, q := range []struct{ sql, arg string }{{trayTag, in.tray}, {uidTag, in.uid}} {
		if q.arg == "" {
			continue
		}
		var id string
		err := tx.QueryRow(ctx, q.sql, q.arg).Scan(&id)
		if errors.Is(err, pgx.ErrNoRows) {
			continue
		}
		if err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	slices.Sort(ids)
	ids = slices.Compact(ids)
	if len(ids) == 0 {
		return ids, nil
	}
	_, err := tx.Exec(ctx, "SELECT 1 FROM filament_tags WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE", ids)
	return ids, err
}

func write(ctx context.Context, tx pgx.Tx, in recordIn) (recorded, error) {
	locked, err := lockInOrder(ctx, tx, in)
	if err != nil {
		return recorded{}, err
	}
	var byTray, byUID *tagRow
	if in.tray != "" {
		row, err := lock(ctx, tx, "SELECT id::text, tray_uuid FROM filament_tags WHERE tray_uuid = $1 FOR UPDATE", in.tray)
		if err != nil {
			return recorded{}, err
		}
		byTray = row
	}
	row, err := lock(ctx, tx, `SELECT t.id::text, t.tray_uuid FROM filament_tag_uids u
 JOIN filament_tags t ON t.id = u.tag_id WHERE u.uid = $1 FOR UPDATE OF t`, in.uid)
	if err != nil {
		return recorded{}, err
	}
	byUID = row
	// A tag that a commit gave the tray UUID or the UID between the lookup and the lock was not
	// locked in order: try the transaction again.
	for _, r := range []*tagRow{byTray, byUID} {
		if r != nil && !slices.Contains(locked, r.id) {
			return recorded{}, &pgconn.PgError{Code: "40P01", Message: "the read's tags moved before their locks"}
		}
	}

	var tagID string
	switch {
	case byTray == nil && byUID == nil:
		var tray any
		if in.tray != "" {
			tray = in.tray
		}
		// The trigger filament_tag_uids_keep_first adds the UID as 'tag'; this new tag's row is
		// this read's, so it takes the read's source and actor.
		if err := tx.QueryRow(ctx, `INSERT INTO filament_tags (kind, tag_uid, tray_uuid)
 VALUES (CASE WHEN $3 THEN 'bambu_mifare' ELSE 'other' END, $1, $2) RETURNING id::text`,
			in.uid, tray, in.bambu).Scan(&tagID); err != nil {
			return recorded{}, err
		}
		if _, err := tx.Exec(ctx, `UPDATE filament_tag_uids SET source = $3, added_by = $4
 WHERE uid = $1 AND tag_id = $2::uuid`, in.uid, tagID, in.source, in.actor); err != nil {
			return recorded{}, err
		}
	case byTray == nil:
		tagID = byUID.id
		if byUID.tray != nil && in.tray != "" && *byUID.tray != in.tray {
			return recorded{}, problem(http.StatusConflict, "tag_keys_clash",
				"this UID belongs to a tag of another tray UUID")
		}
		if byUID.tray == nil && in.tray != "" {
			if _, err := tx.Exec(ctx, "UPDATE filament_tags SET tray_uuid = $2 WHERE id = $1", tagID, in.tray); err != nil {
				return recorded{}, err
			}
			if err := enqueue(ctx, tx, tagID); err != nil {
				return recorded{}, err
			}
		}
	default:
		tagID = byTray.id
		if byUID != nil && byUID.id != byTray.id {
			if byUID.tray != nil {
				return recorded{}, problem(http.StatusConflict, "tag_keys_clash",
					"this UID belongs to a tag of another tray UUID")
			}
			if err := fold(ctx, tx, byUID.id, byTray.id); err != nil {
				return recorded{}, err
			}
			if err := enqueue(ctx, tx, tagID); err != nil {
				return recorded{}, err
			}
		}
	}
	// The UID's row first, with this read's source and actor; then the tag's own tag_uid,
	// whose trigger then finds the row there.
	if _, err := tx.Exec(ctx, `INSERT INTO filament_tag_uids (uid, tag_id, source, added_by)
 VALUES ($1, $2, $3, $4) ON CONFLICT (uid) DO NOTHING`, in.uid, tagID, in.source, in.actor); err != nil {
		return recorded{}, err
	}
	// A read that committed meanwhile may have given the UID to another tag: then the insert
	// did nothing, and the transaction is tried again, as the trigger's refusal would be.
	var holder string
	if err := tx.QueryRow(ctx, `SELECT tag_id::text FROM filament_tag_uids WHERE uid = $1`, in.uid).Scan(&holder); err != nil {
		return recorded{}, err
	}
	if holder != tagID {
		return recorded{}, &pgconn.PgError{Code: "23505", ConstraintName: "filament_tag_uids_pkey",
			Message: "the UID was given to another tag meanwhile"}
	}
	if _, err := tx.Exec(ctx, `UPDATE filament_tags SET tag_uid = $2 WHERE id = $1 AND tag_uid IS NULL`,
		tagID, in.uid); err != nil {
		return recorded{}, err
	}
	// A read with Bambu fields names a Bambu tag; its raw dump only from a complete read that
	// is a tap, since only the history row holds the dump (a tap false read holds nothing).
	if _, err := tx.Exec(ctx, `UPDATE filament_tags SET kind = CASE WHEN $4 THEN 'bambu_mifare' ELSE kind END,
 raw_blob = CASE WHEN $2 THEN $3 ELSE raw_blob END WHERE id = $1`, tagID, in.complete && !in.noTap, in.dumpKey, in.bambu); err != nil {
		return recorded{}, err
	}
	if in.noTap {
		return recorded{tagID: tagID}, nil
	}
	readID, err := insertRead(ctx, tx, historyRow{
		tagID: tagID, source: in.source, actor: in.actor, uid: in.uid, tray: in.tray,
		tagType: in.tagType, dumpKey: in.dumpKey, decoded: in.decoded, scan: in.scan,
	})
	if err != nil {
		return recorded{}, err
	}
	return recorded{tagID: tagID, readID: readID}, nil
}

// historyRow is one filament_tag_reads row.
type historyRow struct {
	tagID, source, actor string
	// uid and tray are cleaned; "" is none.
	uid, tray string
	tagType   *string
	// records is an NDEF read's records as the body sent them, which Python checked.
	records []byte
	dumpKey string
	decoded []byte
	scan    scanFacts
}

// appendRead writes a history row in a transaction of its own (an NDEF read, after Python's).
func appendRead(ctx context.Context, pool *pgxpool.Pool, row historyRow) (string, error) {
	var id string
	err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		var err error
		id, err = insertRead(ctx, tx, row)
		return err
	})
	return id, err
}

// insertRead writes row and holds its dump and its photo in blob_refs, one hold per read.
func insertRead(ctx context.Context, tx pgx.Tx, row historyRow) (string, error) {
	text := func(s string) any {
		if s == "" {
			return nil
		}
		return s
	}
	var imageKey, imageType any
	if img := row.scan.image; img != nil {
		imageKey, imageType = img.Key, img.MediaType
	}
	var lat, lon, accuracy, at any
	if l := row.scan.location; l != nil {
		lat, lon, at = *l.Lat, *l.Lon, *l.At
		if l.AccuracyM != nil {
			accuracy = *l.AccuracyM
		}
	}
	var records any
	if len(row.records) > 0 && string(row.records) != "null" {
		records = string(row.records)
	}
	var decoded any
	if row.decoded != nil {
		decoded = string(row.decoded)
	}
	var id string
	// A device read makes one row: a second post of it answers the first row, and holds nothing.
	err := tx.QueryRow(ctx, `INSERT INTO filament_tag_reads
 (tag_id, source, actor, uid, tray_uuid, tag_type, records, dump_key, decoded, image_key, image_media_type,
  location_lat, location_lon, location_accuracy_m, location_at, device_read_id)
 VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb, $10, $11, $12, $13, $14, $15, $16::uuid)
 ON CONFLICT (device_read_id) WHERE device_read_id IS NOT NULL DO NOTHING RETURNING id::text`,
		row.tagID, row.source, row.actor, text(row.uid), text(row.tray), row.tagType, records,
		text(row.dumpKey), decoded, imageKey, imageType, lat, lon, accuracy, at, text(row.scan.deviceRead)).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		err = tx.QueryRow(ctx, "SELECT id::text FROM filament_tag_reads WHERE device_read_id = $1::uuid",
			row.scan.deviceRead).Scan(&id)
		return id, err
	}
	if err != nil {
		return "", err
	}
	for _, key := range []any{text(row.dumpKey), imageKey} {
		if key == nil {
			continue
		}
		if _, err := tx.Exec(ctx, `INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, $2, $3)
 ON CONFLICT DO NOTHING`, key, holderKind, id); err != nil {
			return "", err
		}
	}
	return id, nil
}

func lock(ctx context.Context, tx pgx.Tx, sql string, arg string) (*tagRow, error) {
	var row tagRow
	err := tx.QueryRow(ctx, sql, arg).Scan(&row.id, &row.tray)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &row, nil
}

// enqueue queues tagID for the Bambuddy tag push when it holds an open binding, as Python's
// filament/rfid_push.py enqueue_tag does: a bound tag that gains a tray UUID (set on it, or
// by a fold) changes what Bambuddy's link should say.
func enqueue(ctx context.Context, tx pgx.Tx, tagID string) error {
	_, err := tx.Exec(ctx, `INSERT INTO filament_bb_tag_pending (tag_id)
 SELECT $1::uuid WHERE EXISTS (SELECT 1 FROM filament_tag_bindings WHERE tag_id = $1::uuid AND unbound_at IS NULL)
 ON CONFLICT (tag_id) DO UPDATE SET queued_at = now(), attempts = 0, last_error = NULL`, tagID)
	return err
}

// fold moves a UID-only tag into the tag of its spool's tray ("Same spool as…", spec G3), as
// 20261126T1817Z_filament_tag_uid_canonical.sql merges a tag's two UID forms: its UIDs,
// bindings, reads, the ledger's copies of its id, its Bambuddy queue row and its newer
// last-seen fields move, and its note only when the tray's tag has none; then the row goes.
// The folded tag's kind is dropped: the tray's tag keeps its own (a tray UUID makes it a
// Bambu tag). A tag with backups is refused (backups are never
// changed), and so is a pair where both tags hold an open binding.
func fold(ctx context.Context, tx pgx.Tx, from, into string) error {
	var backups, bothBound bool
	if err := tx.QueryRow(ctx, `SELECT
 EXISTS (SELECT 1 FROM filament_tag_backups WHERE tag_id = $1),
 EXISTS (SELECT 1 FROM filament_tag_bindings WHERE tag_id = $1 AND unbound_at IS NULL)
 AND EXISTS (SELECT 1 FROM filament_tag_bindings WHERE tag_id = $2 AND unbound_at IS NULL)`,
		from, into).Scan(&backups, &bothBound); err != nil {
		return err
	}
	if backups {
		return problem(http.StatusConflict, "tag_has_backups",
			"this UID's tag has backups, which cannot move to its tray's tag")
	}
	if bothBound {
		return problem(http.StatusConflict, "tags_both_bound",
			"this UID's tag and its tray's tag are each bound to a spool; unbind one first")
	}
	for _, sql := range []string{
		"UPDATE filament_tag_bindings SET tag_id = $2 WHERE tag_id = $1",
		// filament/spools.py writes data.tag_id on both kinds; no foreign key guards it.
		`UPDATE filament_spool_events SET data = jsonb_set(data, '{tag_id}', to_jsonb($2::text))
 WHERE kind IN ('tag_bound', 'tag_unbound') AND data ->> 'tag_id' = $1`,
		`INSERT INTO filament_bb_tag_pending (tag_id)
 SELECT $2::uuid FROM filament_bb_tag_pending WHERE tag_id = $1 ON CONFLICT DO NOTHING`,
		"DELETE FROM filament_bb_tag_pending WHERE tag_id = $1",
		"UPDATE filament_tag_reads SET tag_id = $2 WHERE tag_id = $1",
		"UPDATE filament_tag_uids SET tag_id = $2 WHERE tag_id = $1",
		`UPDATE filament_tags AS t SET
 last_seen_at = CASE WHEN newer THEN f.last_seen_at ELSE t.last_seen_at END,
 last_seen_source = CASE WHEN newer THEN f.last_seen_source ELSE t.last_seen_source END,
 last_seen_records = CASE WHEN newer THEN f.last_seen_records ELSE t.last_seen_records END,
 note = COALESCE(t.note, f.note)
 FROM (SELECT g.*, g.last_seen_at IS NOT NULL AND (i.last_seen_at IS NULL OR g.last_seen_at > i.last_seen_at) AS newer
  FROM filament_tags g, filament_tags i WHERE g.id = $1 AND i.id = $2) AS f
 WHERE t.id = $2`,
		"DELETE FROM filament_tags WHERE id = $1",
	} {
		args := []any{from, into}
		if !strings.Contains(sql, "$2") {
			args = args[:1]
		}
		if _, err := tx.Exec(ctx, sql, args...); err != nil {
			return err
		}
	}
	return nil
}
