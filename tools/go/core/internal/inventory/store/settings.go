package store

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
)

// ShopCurrency is the shop's currency, the setting orders_currency (core/settings.py): the
// settings row when it holds a code, else fallback (SCADBUDDY_ORDERS_CURRENCY, or USD as
// the setting's default). The API writes the row; Go only reads it.
func ShopCurrency(ctx context.Context, q Querier, fallback string) (string, error) {
	var code *string
	err := q.QueryRow(ctx, `SELECT value #>> '{}' FROM settings
		WHERE name = 'orders_currency' AND jsonb_typeof(value) = 'string'`).Scan(&code)
	if errors.Is(err, pgx.ErrNoRows) || err == nil && (code == nil || *code == "") {
		return fallback, nil
	}
	if err != nil {
		return "", err
	}
	return *code, nil
}
