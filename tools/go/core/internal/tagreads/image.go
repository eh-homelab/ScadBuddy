package tagreads

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"regexp"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// imageKind is the blob kind of a scan's photo (#8155 adds it to the blob store).
const imageKind = "scan_image"

var (
	imageKeyPattern  = regexp.MustCompile(`^scan_image-[0-9a-f]{64}$`)
	imageTypePattern = regexp.MustCompile(`^image/(jpeg|png|webp)$`)
)

// image is the photo taken with a scan (the owner, 2026-11-27): a committed scan_image blob.
type image struct {
	Key       string `json:"key"`
	MediaType string `json:"media_type"`
}

// imageOf is the read's photo, nil for none. Its key must name a scan_image blob the
// store has indexed: a page uploads the photo first and sends its key.
func (s *Reads) imageOf(ctx context.Context, fields map[string]json.RawMessage) (*image, *httpx.Problem) {
	raw, ok := fields["image"]
	if !ok || isNull(raw) {
		return nil, nil
	}
	var img image
	if err := json.Unmarshal(raw, &img); err != nil || !imageKeyPattern.MatchString(img.Key) ||
		!imageTypePattern.MatchString(img.MediaType) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "image"},
			Msg: "image is {key, media_type}: a scan_image blob's key and an image/jpeg, image/png or image/webp type"})
	}
	var found int
	err := s.Pool.QueryRow(ctx, "SELECT 1 FROM store_blobs WHERE key = $1 AND kind = $2", img.Key, imageKind).Scan(&found)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, problem(http.StatusUnprocessableEntity, "image_not_found", "no scan_image blob has this key; upload the photo first")
	}
	if err != nil {
		return nil, &httpx.Problem{Status: http.StatusInternalServerError, Detail: "the photo could not be checked"}
	}
	return &img, nil
}
