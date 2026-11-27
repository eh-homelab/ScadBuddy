// Package spoolref finds the spool a caller names by id or by a typed code (G17, #7143).
// A later caller adds a tag lookup beside Find without changing Find's users (#7167).
package spoolref

import (
	"context"
	"errors"
	"regexp"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
)

var (
	// ErrNotFound is an id that no spool has.
	ErrNotFound = errors.New("no spool has that id")
	// ErrCodeUnknown is a code that does not parse (codes/parse.py parse_code), names no
	// spool (a prefix other than S) or names a spool that does not exist.
	ErrCodeUnknown = errors.New("no spool has that code")
	// ErrArchived is a spool that is archived.
	ErrArchived = errors.New("the spool is archived")
)

// maxNumber is PostgreSQL's integer maximum.
const maxNumber = 1<<31 - 1

var codePattern = regexp.MustCompile(`^([A-Z])-?([0-9]{1,10})$`)

// Ref names a spool by id or by code. The id wins when both are set.
type Ref struct {
	ID   string
	Code string
}

// SpoolRow is the spool Find found.
type SpoolRow struct {
	ID     string
	Number int
}

// Querier is a pool or a transaction.
type Querier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Find is the live spool ref names.
func Find(ctx context.Context, q Querier, ref Ref) (SpoolRow, error) {
	var (
		row      SpoolRow
		archived bool
		err      error
	)
	const columns = `SELECT id::text, number, archived_at IS NOT NULL FROM filament_spools`
	if ref.ID != "" {
		err = q.QueryRow(ctx, columns+` WHERE id::text = $1`, strings.ToLower(strings.TrimSpace(ref.ID))).
			Scan(&row.ID, &row.Number, &archived)
		if errors.Is(err, pgx.ErrNoRows) {
			return SpoolRow{}, ErrNotFound
		}
	} else {
		found := codePattern.FindStringSubmatch(strings.ToUpper(strings.TrimSpace(ref.Code)))
		if found == nil || found[1] != "S" {
			return SpoolRow{}, ErrCodeUnknown
		}
		number, _ := strconv.Atoi(found[2])
		if number < 1 || number > maxNumber {
			return SpoolRow{}, ErrCodeUnknown
		}
		err = q.QueryRow(ctx, columns+` WHERE number = $1`, number).Scan(&row.ID, &row.Number, &archived)
		if errors.Is(err, pgx.ErrNoRows) {
			return SpoolRow{}, ErrCodeUnknown
		}
	}
	if err != nil {
		return SpoolRow{}, err
	}
	if archived {
		return SpoolRow{}, ErrArchived
	}
	return row, nil
}
