// Package bambutag decodes the data of a Bambu Lab MIFARE Classic 1K filament tag
// and derives the tag's sector keys from its UID.
//
// The source of truth is the Bambu Research Group's RFID Tag Guide at commit
// a26cf83d7b296b71c558f8c332d464f4e5b9c314:
// https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/a26cf83d7b296b71c558f8c332d464f4e5b9c314/BambuLabRfid.md
// (the block layout),
// https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/a26cf83d7b296b71c558f8c332d464f4e5b9c314/parse.py
// (the reference parser) and
// https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/a26cf83d7b296b71c558f8c332d464f4e5b9c314/deriveKeys.py
// (the key derivation).
//
// Decode returns every field the guide documents as a typed value, in the guide's
// units. All numbers are little-endian. Every byte range the guide marks unknown,
// leaves undocumented ("__") or lists as Empty comes back as hex in Tag.Unknown.
// Sectors 10-15 hold an RSA-2048 signature. Decode returns them as hex and marks
// them unverified, because Bambu's public key is not known. Tag.Blocks keeps every
// block it got, so a later DecoderVersion can decode a stored scan again.
//
// Where BambuLabRfid.md and parse.py disagree, the decoder follows parse.py:
// BambuLabRfid.md gives the filament diameter in block 5 a length of 8, but
// parse.py reads a 4-byte float at bytes 8-11 and expects bytes 12-15 to be blank.
//
// The package is pure: no I/O and no SQL. It never writes, clones or emulates a
// tag; Bambu tags are never written.
package bambutag

import (
	"crypto/hkdf"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"math"
	"slices"
	"strings"
	"time"
)

// DecoderVersion changes whenever Decode's output changes for the same input.
const DecoderVersion = 1

// GuideCommit is the RFID Tag Guide commit that this decoder implements.
const GuideCommit = "a26cf83d7b296b71c558f8c332d464f4e5b9c314"

// MIFARE Classic 1K geometry.
const (
	BlockSize       = 16
	BlockCount      = 64
	SectorCount     = 16
	blocksPerSector = 4
)

// The guide's key derivation inputs (deriveKeys.py).
var (
	keySalt = []byte{0x9a, 0x75, 0x9c, 0xf2, 0xc4, 0xf7, 0xca, 0xff, 0x22, 0x2c, 0xb9, 0x76, 0x9b, 0x41, 0xbc, 0x96}
	keyInfo = "RFID-A\x00"
)

// DeriveKeys returns the 16 sector A keys of a Bambu tag: HKDF-SHA256 with the
// UID as the input key, the guide's salt and the info "RFID-A\0", 96 bytes cut
// into 16 keys of 6 bytes. Key i opens sector i. The B keys are not derived; the
// guide says they are always zero.
func DeriveKeys(uid []byte) [SectorCount][6]byte {
	out, err := hkdf.Key(sha256.New, uid, keySalt, keyInfo, SectorCount*6)
	if err != nil {
		// hkdf.Key fails only when the length is above 255 hash blocks.
		panic(err)
	}
	var keys [SectorCount][6]byte
	for i := range keys {
		copy(keys[i][:], out[i*6:])
	}
	return keys
}

// Block is one 16-byte data block, by its absolute index (0-63).
type Block struct {
	Index int
	Data  [BlockSize]byte
}

// Tag is a decoded tag. Its JSON is what a scan record stores, so the keys are
// stable: a change to them, or to a value's meaning, bumps DecoderVersion.
//
// A field whose block was not read is absent from the JSON (a nil pointer), never
// zero. A field whose block was read but is blank is present with its zero value.
type Tag struct {
	// DecoderVersion is the DecoderVersion that made this value.
	DecoderVersion int `json:"decoder_version"`
	// GuideCommit is the RFID Tag Guide commit that the decoder implements.
	GuideCommit string `json:"guide_commit"`
	// UID is the UID that was passed to Decode, upper-case hex.
	UID string `json:"uid"`
	// Complete is true when every data block of all 16 sectors is present.
	Complete bool `json:"complete"`
	// MissingBlocks lists the data blocks (trailers excluded) that were not read.
	MissingBlocks []int `json:"missing_blocks"`

	// Block 0, set by the tag manufacturer.

	// Block0UID is block 0 bytes 0-3, the tag UID, upper-case hex.
	Block0UID *string `json:"block0_uid,omitempty"`
	// ManufacturerData is block 0 bytes 4-15, upper-case hex.
	ManufacturerData *string `json:"manufacturer_data,omitempty"`

	// Block 1, the tray info index.

	// VariantID is the material variant ID, for example "A00-N0".
	VariantID *string `json:"variant_id,omitempty"`
	// MaterialID is the material ID, for example "GFA00".
	MaterialID *string `json:"material_id,omitempty"`

	// FilamentType is block 2, for example "PLA".
	FilamentType *string `json:"filament_type,omitempty"`
	// DetailedFilamentType is block 4, for example "PLA Basic".
	DetailedFilamentType *string `json:"detailed_filament_type,omitempty"`

	// Block 5.

	// Color is the filament colour as "#RRGGBBAA".
	Color *string `json:"color,omitempty"`
	// SpoolWeightG is the spool (filament) weight in grams.
	SpoolWeightG *uint16 `json:"spool_weight_g,omitempty"`
	// FilamentDiameterMM is the filament diameter in millimetres. It is absent
	// when the bytes are not a finite float; the raw bytes stay in Blocks.
	FilamentDiameterMM *float32 `json:"filament_diameter_mm,omitempty"`

	// Block 6.

	// DryingTemperatureC is the drying temperature in °C.
	DryingTemperatureC *uint16 `json:"drying_temperature_c,omitempty"`
	// DryingTimeH is the drying time in hours.
	DryingTimeH *uint16 `json:"drying_time_h,omitempty"`
	// BedTemperatureType is the bed temperature type; the guide does not know
	// what the values mean.
	BedTemperatureType *uint16 `json:"bed_temperature_type,omitempty"`
	// BedTemperatureC is the bed temperature in °C.
	BedTemperatureC *uint16 `json:"bed_temperature_c,omitempty"`
	// HotendMaxTemperatureC is the hotend's maximum temperature in °C.
	HotendMaxTemperatureC *uint16 `json:"hotend_max_temperature_c,omitempty"`
	// HotendMinTemperatureC is the hotend's minimum temperature in °C.
	HotendMinTemperatureC *uint16 `json:"hotend_min_temperature_c,omitempty"`

	// Block 8.

	// XCamInfo is block 8 bytes 0-11, upper-case hex. The guide names the field
	// "X Cam info" and does not document its structure.
	XCamInfo *string `json:"x_cam_info,omitempty"`
	// NozzleDiameterMM is the nozzle diameter in millimetres (the guide marks it
	// "...?"). It is absent when the bytes are not a finite float.
	NozzleDiameterMM *float32 `json:"nozzle_diameter_mm,omitempty"`

	// TrayUID is block 9, the tray UID, upper-case hex.
	TrayUID *string `json:"tray_uid,omitempty"`

	// SpoolWidthMM is block 10 bytes 4-5, stored as mm*100, in millimetres.
	SpoolWidthMM *float64 `json:"spool_width_mm,omitempty"`

	// Block 12.

	// ProductionDate is block 12 as text, "<year>_<month>_<day>_<hour>_<minute>".
	ProductionDate *string `json:"production_date,omitempty"`
	// ProductionDateTime is ProductionDate as "2006-01-02T15:04:05", with no time
	// zone because the guide names none. It is absent when ProductionDate is not
	// a valid date.
	ProductionDateTime *string `json:"production_datetime,omitempty"`

	// ShortProductionDate is block 13 as text (the guide: "Short Production
	// Date/Time...?").
	ShortProductionDate *string `json:"short_production_date,omitempty"`

	// FilamentLengthM is block 14 bytes 4-5, the filament length in metres (the
	// guide marks it "...?").
	FilamentLengthM *uint16 `json:"filament_length_m,omitempty"`

	// Block 16, the extra colour info.

	// ExtraColorFormat is the format identifier: 0 is empty, 2 is colour info.
	ExtraColorFormat *uint16 `json:"extra_color_format,omitempty"`
	// ExtraColorCount is the colour count.
	ExtraColorCount *uint16 `json:"extra_color_count,omitempty"`
	// SecondColor is the second colour as "#RRGGBBAA" (stored reversed, as ABGR).
	// It is set only when ExtraColorFormat is 2 and ExtraColorCount is 2 or more;
	// otherwise its bytes are in Unknown.
	SecondColor *string `json:"second_color,omitempty"`

	// Unknown lists every byte range of sectors 0-9 that the guide marks unknown,
	// leaves undocumented or lists as Empty, for each block that was read.
	Unknown []Range `json:"unknown"`
	// Signature is sectors 10-15.
	Signature Signature `json:"signature"`
	// Blocks is every block that was passed in, by index.
	Blocks []RawBlock `json:"blocks"`
}

// The kinds of a Range.
const (
	// KindUnknown is a field the guide names but whose meaning it does not know.
	KindUnknown = "unknown"
	// KindUndocumented is "__" bytes inside a documented block.
	KindUndocumented = "undocumented"
	// KindEmpty is a block the guide lists as Empty.
	KindEmpty = "empty"
)

// Range is a byte range of one block that the decoder does not interpret.
type Range struct {
	Block  int    `json:"block"`
	Offset int    `json:"offset"`
	Length int    `json:"length"`
	Kind   string `json:"kind"`
	Hex    string `json:"hex"`
}

// Signature is the RSA-2048 signature in sectors 10-15. Verified is always false:
// Bambu's public key is not known.
type Signature struct {
	Verified bool       `json:"verified"`
	Blocks   []RawBlock `json:"blocks"`
}

// RawBlock is one block as upper-case hex.
type RawBlock struct {
	Block int    `json:"block"`
	Hex   string `json:"hex"`
}

// The data blocks that the guide maps field by field. Every other data block of
// sectors 0-9 is a block the guide lists as Empty.
var documented = map[int][]span{
	0:  nil,
	1:  nil,
	2:  nil,
	4:  nil,
	5:  {{6, 2, KindUndocumented}, {12, 4, KindUndocumented}},
	6:  {{12, 4, KindUndocumented}},
	8:  nil,
	9:  nil,
	10: {{0, 4, KindUndocumented}, {6, 10, KindUndocumented}},
	12: nil,
	13: nil,
	14: {{0, 4, KindUndocumented}, {6, 10, KindUndocumented}},
	16: {{8, 8, KindUndocumented}},
	17: {{0, 2, KindUnknown}, {2, 14, KindUndocumented}},
}

type span struct {
	offset, length int
	kind           string
}

// firstSignatureBlock is block 0 of sector 10.
const firstSignatureBlock = 10 * blocksPerSector

// IsTrailer reports whether a block is a sector trailer (keys and access bits).
func IsTrailer(index int) bool { return index%blocksPerSector == blocksPerSector-1 }

// Decode decodes the data blocks of a tag with this UID. It accepts a partial
// read: a field whose block is missing is absent. It returns an error when the
// UID is not 4 bytes, a block index is outside 0-63 or repeated, a block is a
// sector trailer, or block 0 names another UID.
func Decode(uid []byte, blocks []Block) (Tag, error) {
	if len(uid) != 4 {
		return Tag{}, fmt.Errorf("bambutag: uid is %d bytes, want 4", len(uid))
	}
	byIndex := map[int][]byte{}
	for _, b := range blocks {
		switch {
		case b.Index < 0 || b.Index >= BlockCount:
			return Tag{}, fmt.Errorf("bambutag: block %d is outside 0-%d", b.Index, BlockCount-1)
		case IsTrailer(b.Index):
			return Tag{}, fmt.Errorf("bambutag: block %d is a sector trailer, not data", b.Index)
		}
		if _, dup := byIndex[b.Index]; dup {
			return Tag{}, fmt.Errorf("bambutag: block %d appears twice", b.Index)
		}
		byIndex[b.Index] = b.Data[:]
	}
	if b0, ok := byIndex[0]; ok && string(b0[:4]) != string(uid) {
		return Tag{}, fmt.Errorf("bambutag: block 0 names uid %X, not %X", b0[:4], uid)
	}

	t := Tag{
		DecoderVersion: DecoderVersion,
		GuideCommit:    GuideCommit,
		UID:            hexOf(uid),
		MissingBlocks:  []int{},
		Unknown:        []Range{},
		Signature:      Signature{Blocks: []RawBlock{}},
		Blocks:         []RawBlock{},
	}
	for i := range BlockCount {
		if IsTrailer(i) {
			continue
		}
		data, ok := byIndex[i]
		if !ok {
			t.MissingBlocks = append(t.MissingBlocks, i)
			continue
		}
		raw := RawBlock{Block: i, Hex: hexOf(data)}
		t.Blocks = append(t.Blocks, raw)
		if i >= firstSignatureBlock {
			t.Signature.Blocks = append(t.Signature.Blocks, raw)
			continue
		}
		spans, known := documented[i]
		if !known {
			spans = []span{{0, BlockSize, KindEmpty}}
		}
		if i == 16 && !t.decodeBlock16(data) {
			spans = append([]span{{4, 4, KindUndocumented}}, spans...)
		}
		for _, s := range spans {
			t.Unknown = append(t.Unknown, Range{
				Block: i, Offset: s.offset, Length: s.length, Kind: s.kind,
				Hex: hexOf(data[s.offset : s.offset+s.length]),
			})
		}
	}
	t.Complete = len(t.MissingBlocks) == 0

	if b, ok := byIndex[0]; ok {
		t.Block0UID = ptr(hexOf(b[0:4]))
		t.ManufacturerData = ptr(hexOf(b[4:16]))
	}
	if b, ok := byIndex[1]; ok {
		t.VariantID = ptr(text(b[0:8]))
		t.MaterialID = ptr(text(b[8:16]))
	}
	if b, ok := byIndex[2]; ok {
		t.FilamentType = ptr(text(b))
	}
	if b, ok := byIndex[4]; ok {
		t.DetailedFilamentType = ptr(text(b))
	}
	if b, ok := byIndex[5]; ok {
		t.Color = ptr("#" + hexOf(b[0:4]))
		t.SpoolWeightG = ptr(u16(b[4:6]))
		t.FilamentDiameterMM = f32(b[8:12])
	}
	if b, ok := byIndex[6]; ok {
		t.DryingTemperatureC = ptr(u16(b[0:2]))
		t.DryingTimeH = ptr(u16(b[2:4]))
		t.BedTemperatureType = ptr(u16(b[4:6]))
		t.BedTemperatureC = ptr(u16(b[6:8]))
		t.HotendMaxTemperatureC = ptr(u16(b[8:10]))
		t.HotendMinTemperatureC = ptr(u16(b[10:12]))
	}
	if b, ok := byIndex[8]; ok {
		t.XCamInfo = ptr(hexOf(b[0:12]))
		t.NozzleDiameterMM = f32(b[12:16])
	}
	if b, ok := byIndex[9]; ok {
		t.TrayUID = ptr(hexOf(b))
	}
	if b, ok := byIndex[10]; ok {
		t.SpoolWidthMM = ptr(float64(u16(b[4:6])) / 100)
	}
	if b, ok := byIndex[12]; ok {
		t.ProductionDate = ptr(text(b))
		t.ProductionDateTime = productionTime(*t.ProductionDate)
	}
	if b, ok := byIndex[13]; ok {
		t.ShortProductionDate = ptr(text(b))
	}
	if b, ok := byIndex[14]; ok {
		t.FilamentLengthM = ptr(u16(b[4:6]))
	}
	return t, nil
}

// decodeBlock16 sets the extra colour fields and reports whether bytes 4-7 were
// decoded as the second colour.
func (t *Tag) decodeBlock16(b []byte) bool {
	format, count := u16(b[0:2]), u16(b[2:4])
	t.ExtraColorFormat, t.ExtraColorCount = &format, &count
	if format != 2 || count < 2 {
		return false
	}
	abgr := slices.Clone(b[4:8])
	slices.Reverse(abgr)
	t.SecondColor = ptr("#" + hexOf(abgr))
	return true
}

// text decodes a string field as parse.py does: NUL bytes become spaces, and the
// ends are trimmed. A byte that is not valid UTF-8 becomes U+FFFD; the raw bytes
// stay in Tag.Blocks.
func text(b []byte) string {
	s := strings.ReplaceAll(string(b), "\x00", " ")
	return strings.ToValidUTF8(strings.TrimSpace(s), "�")
}

func productionTime(s string) *string {
	// time.Parse takes only the digits the layout names: no sign, no padding, no extra text,
	// and no day the calendar lacks.
	at, err := time.Parse("2006_01_02_15_04", s)
	if err != nil {
		return nil
	}
	return ptr(at.Format("2006-01-02T15:04:05"))
}

func f32(b []byte) *float32 {
	v := math.Float32frombits(binary.LittleEndian.Uint32(b))
	if math.IsNaN(float64(v)) || math.IsInf(float64(v), 0) {
		return nil
	}
	return &v
}

func u16(b []byte) uint16 { return binary.LittleEndian.Uint16(b) }

func hexOf(b []byte) string { return strings.ToUpper(hex.EncodeToString(b)) }

func ptr[T any](v T) *T { return &v }
