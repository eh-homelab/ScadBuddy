package unknowntrays

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Tray is one unknown tray in the list's JSON (UnknownSpoolTray).
type Tray struct {
	ID         string      `json:"id"`
	PrinterID  string      `json:"printer_id"`
	Printer    string      `json:"printer_name"`
	AmsID      int         `json:"ams_id"`
	TrayID     int         `json:"tray_id"`
	Slot       string      `json:"slot"`
	PlaceID    *string     `json:"place_id"`
	PlaceName  *string     `json:"place_name"`
	PlaceCode  *string     `json:"place_code"`
	Material   string      `json:"material"`
	ColourRGBA *string     `json:"colour_rgba"`
	RemainPct  *int        `json:"remain_pct"`
	FirstSeen  time.Time   `json:"first_seen_at"`
	SeenAt     time.Time   `json:"seen_at"`
	Candidates []Candidate `json:"candidates"`
}

// TagMade is a spool that capture made from an RFID tag (TagMadeSpool).
type TagMade struct {
	SpoolID     string    `json:"spool_id"`
	Number      int       `json:"number"`
	Code        string    `json:"code"`
	PrinterName string    `json:"printer_name"`
	Slot        string    `json:"slot"`
	MadeAt      time.Time `json:"made_at"`
}

// Listing is the list route's answer.
type Listing struct {
	Trays        []Tray    `json:"trays"`
	MadeFromTags []TagMade `json:"made_from_tags"`
}

const (
	// madeWindow and madeLimit bound made_from_tags.
	madeWindow = "24 hours"
	madeLimit  = 20
)

// List reads the rows, their candidates and the spools made from tags in one read-only
// snapshot.
func List(ctx context.Context, pool *pgxpool.Pool) (*Listing, error) {
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows, err := readRows(ctx, tx)
	if err != nil {
		return nil, err
	}
	out := &Listing{Trays: []Tray{}, MadeFromTags: []TagMade{}}
	if len(rows) > 0 {
		spools, err := readSpools(ctx, tx)
		if err != nil {
			return nil, err
		}
		keys, err := readKeys(ctx, tx)
		if err != nil {
			return nil, err
		}
		for _, r := range rows {
			t := Tray{
				ID: r.ID, PrinterID: r.PrinterID, Printer: r.Printer, AmsID: r.AmsID,
				TrayID: r.TrayID, Slot: TrayLabel(r.AmsID, r.TrayID), PlaceID: r.PlaceID,
				PlaceName: r.PlaceName, Material: r.Material, ColourRGBA: r.ColourRGBA,
				RemainPct: r.RemainPct, FirstSeen: r.FirstSeen.UTC(), SeenAt: r.SeenAt.UTC(),
				Candidates: Rank(r, spools, keys, CandidateLimit),
			}
			if r.PlaceNo != nil {
				code := PlaceCode(*r.PlaceNo)
				t.PlaceCode = &code
			}
			out.Trays = append(out.Trays, t)
		}
	}
	if out.MadeFromTags, err = readMade(ctx, tx); err != nil {
		return nil, err
	}
	return out, nil
}

func readRows(ctx context.Context, tx pgx.Tx) ([]Row, error) {
	res, err := tx.Query(ctx, `
		SELECT u.id::text, u.printer_id::text, p.name, u.ams_id, u.tray_id, u.material,
		       u.colour_rgba, u.remain_pct, u.first_seen_at, u.seen_at,
		       l.id::text, l.name, l.number
		FROM filament_unknown_trays u
		JOIN printers p ON p.id = u.printer_id
		LEFT JOIN filament_locations l ON l.printer_id = u.printer_id
		     AND l.ams_id = u.ams_id AND l.tray_id = u.tray_id AND l.archived_at IS NULL
		ORDER BY p.name, u.ams_id, u.tray_id`)
	if err != nil {
		return nil, err
	}
	defer res.Close()
	var rows []Row
	for res.Next() {
		var r Row
		if err := res.Scan(&r.ID, &r.PrinterID, &r.Printer, &r.AmsID, &r.TrayID, &r.Material,
			&r.ColourRGBA, &r.RemainPct, &r.FirstSeen, &r.SeenAt, &r.PlaceID, &r.PlaceName,
			&r.PlaceNo); err != nil {
			return nil, err
		}
		rows = append(rows, r)
	}
	return rows, res.Err()
}

// readSpools reads the unarchived spools with their effective material, colour and label
// weight (the spool's own, else its type's) and the stored grams_left.
func readSpools(ctx context.Context, tx pgx.Tx) ([]SpoolRow, error) {
	res, err := tx.Query(ctx, `
		SELECT s.id::text, s.number, coalesce(s.material_id, t.material_id)::text,
		       coalesce(m.name, ''), coalesce(s.colour_name, t.colour_name),
		       coalesce(s.rgba, t.rgba), coalesce(s.label_weight_g, t.label_weight_g),
		       s.grams_left, s.location_id::text, loc.name
		FROM filament_spools s
		LEFT JOIN filament_spool_types t ON t.id = s.type_id
		LEFT JOIN filament_materials m ON m.id = coalesce(s.material_id, t.material_id)
		LEFT JOIN filament_locations loc ON loc.id = s.location_id
		WHERE s.archived_at IS NULL
		ORDER BY s.number`)
	if err != nil {
		return nil, err
	}
	defer res.Close()
	var spools []SpoolRow
	for res.Next() {
		var s SpoolRow
		if err := res.Scan(&s.ID, &s.Number, &s.MaterialID, &s.Material, &s.ColourName, &s.RGBA,
			&s.LabelWeightG, &s.GramsLeft, &s.PlaceID, &s.PlaceName); err != nil {
			return nil, err
		}
		spools = append(spools, s)
	}
	return spools, res.Err()
}

// readKeys reads every material's stored catalog keys, its name's and its aliases', by id.
func readKeys(ctx context.Context, tx pgx.Tx) (map[string]map[string]bool, error) {
	res, err := tx.Query(ctx, `SELECT material_id::text, key FROM filament_material_names`)
	if err != nil {
		return nil, err
	}
	defer res.Close()
	keys := map[string]map[string]bool{}
	for res.Next() {
		var id, key string
		if err := res.Scan(&id, &key); err != nil {
			return nil, err
		}
		if keys[id] == nil {
			keys[id] = map[string]bool{}
		}
		keys[id][key] = true
	}
	return keys, res.Err()
}

// readMade reads the `created` events that capture booked for an unknown RFID tag, newest
// first. The window is counted on the database's clock.
func readMade(ctx context.Context, tx pgx.Tx) ([]TagMade, error) {
	res, err := tx.Query(ctx, `
		SELECT e.spool_id::text, s.number, e.at, coalesce(p.name, ''), e.ams_id, e.tray_id
		FROM filament_spool_events e
		JOIN filament_spools s ON s.id = e.spool_id
		LEFT JOIN printers p ON p.id = e.printer_id
		WHERE e.kind = 'created' AND e.data->>'by' = 'rfid_capture'
		  AND s.archived_at IS NULL
		  AND e.at > now() - $1::interval
		ORDER BY e.at DESC, e.seq DESC
		LIMIT $2`, madeWindow, madeLimit)
	if err != nil {
		return nil, err
	}
	defer res.Close()
	made := []TagMade{}
	for res.Next() {
		var m TagMade
		var ams, tray *int
		if err := res.Scan(&m.SpoolID, &m.Number, &m.MadeAt, &m.PrinterName, &ams, &tray); err != nil {
			return nil, err
		}
		m.MadeAt = m.MadeAt.UTC()
		m.Code = SpoolCode(m.Number)
		if ams != nil && tray != nil {
			m.Slot = TrayLabel(*ams, *tray)
		}
		made = append(made, m)
	}
	return made, res.Err()
}
