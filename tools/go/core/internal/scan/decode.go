package scan

import (
	"bytes"
	"context"
	"errors"
	"image"
	_ "image/jpeg" // The decoders image.Decode reads a photo with.
	_ "image/png"
	"log/slog"
	"math"
	"strings"
	"unicode/utf8"

	"github.com/makiuchi-d/gozxing"
	"github.com/makiuchi-d/gozxing/datamatrix"
	multiqr "github.com/makiuchi-d/gozxing/multi/qrcode"
	"github.com/makiuchi-d/gozxing/oned"
	_ "golang.org/x/image/webp"
)

const (
	// maxPixels bounds a photo before its pixels are decoded: a small file can claim a huge
	// image (a decompression bomb). 24 MP is a 6000 x 4000 phone photo; decoded it is
	// 96 MiB of RGBA.
	maxPixels = 24_000_000
	// maxSide bounds each side, so a 1 x 24 000 000 strip is refused too.
	maxSide = 12_000
	// maxCodes is the most codes one photo answers.
	maxCodes = 16
	// maxText is the most bytes of one code's text kept. A QR code holds at most 2953
	// bytes and a Data Matrix 1556, so a longer text is cut, never a real one.
	maxText = 4096
)

// The reasons a photo gives no codes, stored in code_scans.error.
const (
	errUnreadable = "image_unreadable"
	errTooLarge   = "image_too_large"
)

// Code is one code a photo holds: its format, as the browser's BarcodeDetector names it,
// and its text.
type Code struct {
	Format string `json:"format"`
	Text   string `json:"text"`
}

// formats are the codes read, by the names BarcodeDetector gives them.
var formats = map[gozxing.BarcodeFormat]string{
	gozxing.BarcodeFormat_QR_CODE:     "qr_code",
	gozxing.BarcodeFormat_DATA_MATRIX: "data_matrix",
	gozxing.BarcodeFormat_EAN_13:      "ean_13",
	gozxing.BarcodeFormat_EAN_8:       "ean_8",
	gozxing.BarcodeFormat_UPC_A:       "upc_a",
	gozxing.BarcodeFormat_UPC_E:       "upc_e",
	gozxing.BarcodeFormat_CODE_128:    "code_128",
}

// mediaTypes are the photo formats read, by image.DecodeConfig's name.
var mediaTypes = map[string]string{"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

// errHEIC is a HEIC, HEIF or AVIF photo: no decoder in pure Go reads one, so the page
// sends a JPEG instead (it draws the photo on a canvas and encodes that).
var errHEIC = errors.New("a HEIC, HEIF or AVIF photo is not read here: send a JPEG, PNG or WebP")

// errNotImage is a body that is no JPEG, PNG or WebP.
var errNotImage = errors.New("the body is not a JPEG, PNG or WebP image")

// isHEIC says whether body is an ISO-BMFF file of a HEIC, HEIF or AVIF brand.
func isHEIC(body []byte) bool {
	if len(body) < 12 || string(body[4:8]) != "ftyp" {
		return false
	}
	switch string(body[8:12]) {
	case "heic", "heix", "hevc", "hevx", "heim", "heis", "mif1", "msf1", "avif":
		return true
	}
	return false
}

// photo is what the header of an image says: its media type and size.
type photo struct {
	MediaType     string
	Width, Height int
}

// sniff reads body's header only. errHEIC and errNotImage are refusals of the upload.
func sniff(body []byte) (photo, error) {
	if isHEIC(body) {
		return photo{}, errHEIC
	}
	config, format, err := image.DecodeConfig(bytes.NewReader(body))
	if err != nil {
		return photo{}, errNotImage
	}
	mediaType, ok := mediaTypes[format]
	if !ok {
		return photo{}, errNotImage
	}
	return photo{MediaType: mediaType, Width: config.Width, Height: config.Height}, nil
}

// tooLarge says whether p's pixels are past the bounds.
func (p photo) tooLarge() bool {
	return p.Width <= 0 || p.Height <= 0 || p.Width > maxSide || p.Height > maxSide ||
		int64(p.Width)*int64(p.Height) > maxPixels
}

// decode is every code in body, or the reason there are none it could read ("" when the
// photo was read and holds none). The pixels are bounded before they are decoded.
func decode(ctx context.Context, body []byte) (photo, []Code, string) {
	p, err := sniff(body)
	if err != nil {
		return photo{MediaType: "application/octet-stream"}, nil, errUnreadable
	}
	if p.tooLarge() {
		return p, nil, errTooLarge
	}
	img, _, err := image.Decode(bytes.NewReader(body))
	if err != nil {
		return p, nil, errUnreadable
	}
	return p, read(ctx, img), ""
}

// read runs each reader over img once: every QR code (its multi reader), then one Data
// Matrix, one EAN or UPC and one Code 128. The 1D readers try the photo turned 90°
// (TRY_HARDER), and the QR and Data Matrix detectors find a code at any angle. A reader
// that panics on a strange photo reads nothing.
func read(ctx context.Context, img image.Image) []Code {
	bitmap, err := gozxing.NewBinaryBitmap(gozxing.NewHybridBinarizer(gozxing.NewLuminanceSourceFromImage(img)))
	if err != nil {
		return nil
	}
	hints := map[gozxing.DecodeHintType]any{gozxing.DecodeHintType_TRY_HARDER: true}
	var codes []Code
	seen := map[Code]bool{}
	// boxes are where the 2D codes found are: a 1D reader's row through one of them is no
	// barcode (an EAN's check digit passes about one row in ten).
	var boxes []image.Rectangle
	add := func(results ...*gozxing.Result) {
		for _, result := range results {
			if result == nil || len(codes) >= maxCodes {
				continue
			}
			format, ok := formats[result.GetBarcodeFormat()]
			if !ok {
				continue
			}
			c := Code{Format: format, Text: clean(result.GetText())}
			if c.Text == "" || seen[c] {
				continue
			}
			box, has := bounds(result.GetResultPoints())
			twoD := format == "qr_code" || format == "data_matrix"
			if !twoD && has && within(box, boxes) {
				continue
			}
			if twoD && has {
				boxes = append(boxes, box)
			}
			seen[c] = true
			codes = append(codes, c)
		}
	}
	guard := func(name string, run func()) {
		defer func() {
			if r := recover(); r != nil {
				slog.WarnContext(ctx, "a code reader failed on a photo", "reader", name)
			}
		}()
		run()
	}
	guard("qr_code", func() {
		results, _ := multiqr.NewQRCodeMultiReader().DecodeMultiple(bitmap, hints)
		add(results...)
	})
	guard("data_matrix", func() {
		result, _ := datamatrix.NewDataMatrixReader().Decode(bitmap, hints)
		add(result)
	})
	guard("upc_ean", func() {
		reader := oned.NewMultiFormatUPCEANReader(hints)
		if result, _ := reader.Decode(bitmap, hints); confirmed(bitmap, result, reader) {
			add(result)
		}
	})
	guard("code_128", func() {
		reader := oned.NewCode128Reader()
		if result, _ := reader.Decode(bitmap, hints); confirmed(bitmap, result, reader) {
			add(result)
		}
	})
	return codes
}

// confirmGaps are how far from a 1D result's row another row is read, in pixels.
var confirmGaps = []int{4, -4, 8, -8}

// confirmed says whether a 1D result reads the same on a row beside its own: a barcode's
// bars are tall, so a real one does, while a row of a photo's grain or of a 2D code that
// passed a check digit by chance (an EAN's passes about one row in ten) does not.
func confirmed(bitmap *gozxing.BinaryBitmap, result *gozxing.Result, reader gozxing.Reader) bool {
	if result == nil {
		return false
	}
	points := result.GetResultPoints()
	if len(points) < 2 {
		return false
	}
	first, last := points[0], points[len(points)-1]
	across := math.Abs(last.GetX()-first.GetX()) >= math.Abs(last.GetY()-first.GetY())
	w, h := bitmap.GetWidth(), bitmap.GetHeight()
	for _, gap := range confirmGaps {
		var line *gozxing.BinaryBitmap
		var err error
		if across {
			y := int((first.GetY()+last.GetY())/2) + gap
			if y < 0 || y >= h {
				continue
			}
			line, err = bitmap.Crop(0, y, w, 1)
		} else {
			// A barcode the reader found turned: read its column, turned back.
			x := int((first.GetX()+last.GetX())/2) + gap
			if x < 0 || x >= w {
				continue
			}
			if line, err = bitmap.Crop(x, 0, 1, h); err == nil {
				line, err = line.RotateCounterClockwise()
			}
		}
		if err != nil {
			continue
		}
		if again, err := reader.Decode(line, nil); err == nil && again.GetText() == result.GetText() {
			return true
		}
	}
	return false
}

// bounds is the box the points span, grown by a quarter of its size on each side (a 2D
// code's points are its finder patterns' centres, inside its edge).
func bounds(points []gozxing.ResultPoint) (image.Rectangle, bool) {
	if len(points) == 0 {
		return image.Rectangle{}, false
	}
	minX, minY, maxX, maxY := points[0].GetX(), points[0].GetY(), points[0].GetX(), points[0].GetY()
	for _, p := range points[1:] {
		minX, maxX = min(minX, p.GetX()), max(maxX, p.GetX())
		minY, maxY = min(minY, p.GetY()), max(maxY, p.GetY())
	}
	dx, dy := (maxX-minX)/4, (maxY-minY)/4
	return image.Rect(int(minX-dx), int(minY-dy), int(maxX+dx)+1, int(maxY+dy)+1), true
}

// within says whether box's centre is inside one of boxes.
func within(box image.Rectangle, boxes []image.Rectangle) bool {
	centre := image.Pt((box.Min.X+box.Max.X)/2, (box.Min.Y+box.Max.Y)/2)
	for _, b := range boxes {
		if centre.In(b) {
			return true
		}
	}
	return false
}

// clean is a code's text as it is kept: valid UTF-8 with no NUL (jsonb refuses U+0000), at
// most maxText bytes, cut at a character's start.
func clean(text string) string {
	text = strings.ReplaceAll(strings.ToValidUTF8(text, "�"), "\x00", "")
	if len(text) <= maxText {
		return text
	}
	cut := maxText
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	return text[:cut]
}
