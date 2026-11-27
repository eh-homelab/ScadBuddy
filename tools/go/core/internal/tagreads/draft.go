package tagreads

import (
	"context"
	"errors"
	"math"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bambutag"
)

// brand is the maker of every Bambu MIFARE tag.
const brand = "Bambu Lab"

// nilScope is filament/catalog.py NIL: a subtype key for every material.
const nilScope = "00000000-0000-0000-0000-000000000000"

// keySQL is filament/catalog.py `key_sql` of $1.
const keySQL = `upper(regexp_replace(replace($1, '+', 'PLUS'), '[^[:alnum:]]', '', 'g'))`

// draft is a spool as a Bambu tag describes it, in SpoolCreate's shape: filament/tags.py
// `draft_of` for a Bambu read. A value SpoolCreate would refuse is left out.
type draft struct {
	MaterialID    string   `json:"material_id"`
	BrandID       *string  `json:"brand_id"`
	SubtypeID     *string  `json:"subtype_id"`
	Rgba          *string  `json:"rgba,omitempty"`
	DiameterMM    *float64 `json:"diameter_mm,omitempty"`
	NozzleTempMin *int     `json:"nozzle_temp_min,omitempty"`
	NozzleTempMax *int     `json:"nozzle_temp_max,omitempty"`
	DryingTempC   *int     `json:"drying_temp_c,omitempty"`
	DryingHours   *int     `json:"drying_hours,omitempty"`
	LabelWeightG  *int     `json:"label_weight_g,omitempty"`
}

// draftOf is the draft of tag, its material, brand and subtype found in the catalog by key;
// nil when no material is found.
func draftOf(ctx context.Context, pool *pgxpool.Pool, tag bambutag.Tag) (*draft, error) {
	if tag.FilamentType == nil || *tag.FilamentType == "" {
		return nil, nil
	}
	material, err := find(ctx, pool, `SELECT material_id::text FROM filament_material_names WHERE key = `+keySQL,
		*tag.FilamentType)
	if err != nil || material == nil {
		return nil, err
	}
	d := &draft{MaterialID: *material}
	if d.BrandID, err = find(ctx, pool, `SELECT brand_id::text FROM filament_brand_names WHERE key = `+keySQL, brand); err != nil {
		return nil, err
	}
	// "PLA Translucent" is the subtype Translucent of PLA.
	if tag.DetailedFilamentType != nil {
		sub := strings.TrimSpace(strings.TrimPrefix(*tag.DetailedFilamentType, *tag.FilamentType))
		if sub != "" {
			if d.SubtypeID, err = find(ctx, pool, `SELECT subtype_id::text FROM filament_subtype_names
 WHERE key = `+keySQL+` AND material_scope IN ($2, $3) ORDER BY material_scope = $3 LIMIT 1`,
				sub, *material, nilScope); err != nil {
				return nil, err
			}
		}
	}
	if tag.Color != nil {
		d.Rgba = tag.Color
	}
	d.DiameterMM = diameter(tag.FilamentDiameterMM)
	lo, hi := temp(tag.HotendMinTemperatureC), temp(tag.HotendMaxTemperatureC)
	if lo != nil && hi != nil && *lo > *hi {
		lo, hi = nil, nil
	}
	d.NozzleTempMin, d.NozzleTempMax = lo, hi
	d.DryingTempC = temp(tag.DryingTemperatureC)
	if v := tag.DryingTimeH; v != nil && *v > 0 && *v < 1000 {
		d.DryingHours = ptr(int(*v))
	}
	if v := tag.SpoolWeightG; v != nil && *v > 0 {
		d.LabelWeightG = ptr(int(*v))
	}
	return d, nil
}

// diameter is a diameter SpoolCreate takes (more than 0 and less than 100 mm), to 0.01 mm. It
// is rounded first and then checked, so a value that rounds to 0 or 100 is none.
func diameter[F ~float32 | ~float64](v *F) *float64 {
	if v == nil {
		return nil
	}
	mm := math.Round(float64(*v)*100) / 100
	if !(mm > 0 && mm < 100) {
		return nil
	}
	return &mm
}

// temp is a temperature SpoolCreate takes (0 to 1000 °C); a blank 0 is none.
func temp(v *uint16) *int {
	if v == nil || *v == 0 || *v > 1000 {
		return nil
	}
	return ptr(int(*v))
}

func find(ctx context.Context, pool *pgxpool.Pool, sql string, args ...any) (*string, error) {
	var id string
	err := pool.QueryRow(ctx, sql, args...).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &id, nil
}

func ptr[T any](v T) *T { return &v }
