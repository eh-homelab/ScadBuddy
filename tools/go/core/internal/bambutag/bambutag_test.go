package bambutag

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"testing"
)

var update = flag.Bool("update", false, "rewrite the golden JSON files in testdata")

// realUID is the owner's Bambu PLA Basic Brown spool. testdata/dc999be3.txt is its
// full read, taken 2026-11-26 on the owner's phone (adb logcat tag ScadBuddyNfc):
// "index:hex" pairs, one per data block.
var realUID = mustHex("DC999BE3")

// guideUID is the tag of the guide's examples/exampleDump.bin and
// exampleKeyfile.bin (RFID Tag Guide at GuideCommit).
var guideUID = mustHex("75886B1D")

func mustHex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		panic(err)
	}
	return b
}

func block(i int, h string) Block {
	b := Block{Index: i}
	copy(b.Data[:], mustHex(h))
	return b
}

func realBlocks(t *testing.T) []Block {
	t.Helper()
	raw, err := os.ReadFile("testdata/dc999be3.txt")
	if err != nil {
		t.Fatal(err)
	}
	var out []Block
	for _, pair := range strings.Fields(string(raw)) {
		i, h, ok := strings.Cut(pair, ":")
		n, err := strconv.Atoi(i)
		if !ok || err != nil || len(h) != 2*BlockSize {
			t.Fatalf("bad pair %q", pair)
		}
		out = append(out, block(n, h))
	}
	return out
}

// guideBlocks reads a 1 KiB dump and drops the sector trailers.
func guideBlocks(t *testing.T) []Block {
	t.Helper()
	raw, err := os.ReadFile("testdata/guide-exampleDump.bin")
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) != BlockCount*BlockSize {
		t.Fatalf("dump is %d bytes", len(raw))
	}
	var out []Block
	for i := range BlockCount {
		if IsTrailer(i) {
			continue
		}
		b := Block{Index: i}
		copy(b.Data[:], raw[i*BlockSize:])
		out = append(out, b)
	}
	return out
}

func keysHex(keys [SectorCount][6]byte) []string {
	out := make([]string, len(keys))
	for i, k := range keys {
		out[i] = fmt.Sprintf("%X", k)
	}
	return out
}

func TestDeriveKeys(t *testing.T) {
	// The output of the guide's deriveKeys.py DC999BE3, run with python3 -I and
	// pycryptodomex at GuideCommit. The app's NfcBridge.cs opened every sector of
	// this tag with the same algorithm, so these keys read the real tag.
	want := []string{
		"3F47702BFBDF", "D86DB085658A", "FFCCF3F52FDB", "3BD34705C329",
		"2CA4F4B17FA3", "6968A394AE1F", "4D0610CCED21", "FFC105845349",
		"E0F22506F826", "A042F03D259D", "10FF145E14D5", "BF3A7587D7D0",
		"4A1080CEB493", "AAE43D8E2E1B", "71675BA3B0D7", "F10AE691DE9F",
	}
	if got := keysHex(DeriveKeys(realUID)); strings.Join(got, " ") != strings.Join(want, " ") {
		t.Errorf("DeriveKeys(DC999BE3) = %v, want %v", got, want)
	}

	// The guide's exampleKeyfile.bin: 16 A keys, then 16 B keys of zero.
	keyfile, err := os.ReadFile("testdata/guide-exampleKeyfile.bin")
	if err != nil {
		t.Fatal(err)
	}
	keys := DeriveKeys(guideUID)
	for i, k := range keys {
		if !bytes.Equal(k[:], keyfile[i*6:i*6+6]) {
			t.Errorf("sector %d key A = %X, keyfile has %X", i, k, keyfile[i*6:i*6+6])
		}
		if b := keyfile[96+i*6 : 96+i*6+6]; !bytes.Equal(b, make([]byte, 6)) {
			t.Errorf("sector %d key B = %X, the guide says zero", i, b)
		}
	}

	// The guide's exampleDump.bin trailers hold the same A keys and the access
	// bits 87 87 87 69.
	dump, err := os.ReadFile("testdata/guide-exampleDump.bin")
	if err != nil {
		t.Fatal(err)
	}
	for s := range SectorCount {
		trailer := dump[(s*4+3)*BlockSize:][:BlockSize]
		if !bytes.Equal(trailer[:6], keys[s][:]) || !bytes.Equal(trailer[6:10], []byte{0x87, 0x87, 0x87, 0x69}) {
			t.Errorf("sector %d trailer %X does not hold key %X", s, trailer, keys[s])
		}
	}
}

func str(p *string) string {
	if p == nil {
		return "<absent>"
	}
	return *p
}

func num[T uint16 | float32 | float64](p *T) string {
	if p == nil {
		return "<absent>"
	}
	return fmt.Sprint(*p)
}

// fields flattens the typed fields for table checks.
func fields(t Tag) map[string]string {
	return map[string]string{
		"block0_uid":               str(t.Block0UID),
		"manufacturer_data":        str(t.ManufacturerData),
		"variant_id":               str(t.VariantID),
		"material_id":              str(t.MaterialID),
		"filament_type":            str(t.FilamentType),
		"detailed_filament_type":   str(t.DetailedFilamentType),
		"color":                    str(t.Color),
		"spool_weight_g":           num(t.SpoolWeightG),
		"filament_diameter_mm":     num(t.FilamentDiameterMM),
		"drying_temperature_c":     num(t.DryingTemperatureC),
		"drying_time_h":            num(t.DryingTimeH),
		"bed_temperature_type":     num(t.BedTemperatureType),
		"bed_temperature_c":        num(t.BedTemperatureC),
		"hotend_max_temperature_c": num(t.HotendMaxTemperatureC),
		"hotend_min_temperature_c": num(t.HotendMinTemperatureC),
		"x_cam_info":               str(t.XCamInfo),
		"nozzle_diameter_mm":       num(t.NozzleDiameterMM),
		"tray_uid":                 str(t.TrayUID),
		"spool_width_mm":           num(t.SpoolWidthMM),
		"production_date":          str(t.ProductionDate),
		"production_datetime":      str(t.ProductionDateTime),
		"short_production_date":    str(t.ShortProductionDate),
		"filament_length_m":        num(t.FilamentLengthM),
		"extra_color_format":       num(t.ExtraColorFormat),
		"extra_color_count":        num(t.ExtraColorCount),
		"second_color":             str(t.SecondColor),
	}
}

func TestDecodeDumps(t *testing.T) {
	for _, c := range []struct {
		name   string
		uid    []byte
		blocks func(*testing.T) []Block
		golden string
		want   map[string]string
	}{
		{
			// The values parse.py prints for this dump agree with each one.
			name: "the owner's PLA Basic Brown", uid: realUID, blocks: realBlocks,
			golden: "testdata/dc999be3.json",
			want: map[string]string{
				"block0_uid": "DC999BE3", "manufacturer_data": "3D080400054073123440CC90",
				"variant_id": "A00-N0", "material_id": "GFA00",
				"filament_type": "PLA", "detailed_filament_type": "PLA Basic",
				"color": "#9D432CFF", "spool_weight_g": "1000", "filament_diameter_mm": "1.75",
				"drying_temperature_c": "55", "drying_time_h": "8",
				"bed_temperature_type": "0", "bed_temperature_c": "0",
				"hotend_max_temperature_c": "230", "hotend_min_temperature_c": "190",
				"x_cam_info": "3421100EE803E803CDCC4C3F", "nozzle_diameter_mm": "0.2",
				"tray_uid": "D3B5D4BCA6044374A9B52E9C2DC9AC6A", "spool_width_mm": "66.25",
				"production_date": "2026_05_08_23_15", "production_datetime": "2026-05-08T23:15:00",
				"short_production_date": "26_05_08_23", "filament_length_m": "330",
				"extra_color_format": "2", "extra_color_count": "1", "second_color": "<absent>",
			},
		},
		{
			name: "the guide's exampleDump.bin", uid: guideUID, blocks: guideBlocks,
			golden: "testdata/guide-exampleDump.json",
			want: map[string]string{
				"block0_uid": "75886B1D", "manufacturer_data": "8B080400034339DB5B5E0A90",
				"variant_id": "A00-A0", "material_id": "GFA00",
				"filament_type": "PLA", "detailed_filament_type": "PLA Basic",
				"color": "#FF6A13FF", "spool_weight_g": "250", "filament_diameter_mm": "1.75",
				"drying_temperature_c": "55", "drying_time_h": "8",
				"bed_temperature_type": "1", "bed_temperature_c": "45",
				"hotend_max_temperature_c": "220", "hotend_min_temperature_c": "220",
				"x_cam_info": "8813100EE803E8039A99193F", "nozzle_diameter_mm": "0.2",
				"tray_uid": "D7AC3B89A16B47C4B061728044E1F2D5", "spool_width_mm": "66.25",
				"production_date": "2022_10_15_08_26", "production_datetime": "2022-10-15T08:26:00",
				"short_production_date": "6092202", "filament_length_m": "82",
				"extra_color_format": "0", "extra_color_count": "0", "second_color": "<absent>",
			},
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			tag, err := Decode(c.uid, c.blocks(t))
			if err != nil {
				t.Fatal(err)
			}
			got := fields(tag)
			for k, want := range c.want {
				if got[k] != want {
					t.Errorf("%s = %q, want %q", k, got[k], want)
				}
			}
			if len(got) != len(c.want) {
				t.Errorf("the table checks %d fields of %d", len(c.want), len(got))
			}
			if !tag.Complete || len(tag.MissingBlocks) != 0 || len(tag.Blocks) != 48 {
				t.Errorf("complete=%v missing=%v blocks=%d", tag.Complete, tag.MissingBlocks, len(tag.Blocks))
			}
			if tag.Signature.Verified || len(tag.Signature.Blocks) != 18 || tag.Signature.Blocks[0].Block != 40 {
				t.Errorf("signature = %+v", tag.Signature)
			}
			// Sectors 0-9: 30 data blocks, of which 14 are documented; the 16
			// Empty blocks are one range each.
			empty := 0
			for _, r := range tag.Unknown {
				if r.Kind == KindEmpty {
					empty++
				}
				if r.Block >= firstSignatureBlock || len(r.Hex) != 2*r.Length {
					t.Errorf("bad range %+v", r)
				}
			}
			if empty != 16 {
				t.Errorf("%d empty ranges, want 16", empty)
			}
			checkGolden(t, c.golden, tag)
		})
	}
}

// checkGolden pins the JSON a scan record stores. Run go test -update to
// rewrite it after a deliberate change (and bump DecoderVersion).
func checkGolden(t *testing.T, path string, tag Tag) {
	t.Helper()
	got, err := json.MarshalIndent(tag, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	if *update {
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Errorf("JSON differs from %s; run go test -update if the change is deliberate", path)
	}
}

func TestDecodePartial(t *testing.T) {
	var blocks []Block
	for _, b := range realBlocks(t) {
		if b.Index < 9*4 { // sectors 0-8
			blocks = append(blocks, b)
		}
	}
	tag, err := Decode(realUID, blocks)
	if err != nil {
		t.Fatal(err)
	}
	if tag.Complete {
		t.Error("complete with sectors 9-15 missing")
	}
	want := []int{36, 37, 38, 40, 41, 42, 44, 45, 46, 48, 49, 50, 52, 53, 54, 56, 57, 58, 60, 61, 62}
	if fmt.Sprint(tag.MissingBlocks) != fmt.Sprint(want) {
		t.Errorf("missing = %v, want %v", tag.MissingBlocks, want)
	}
	if len(tag.Signature.Blocks) != 0 || str(tag.MaterialID) != "GFA00" || num(tag.FilamentLengthM) != "330" {
		t.Errorf("decoded %+v", tag)
	}

	// Only sector 1: the fields of sectors 0, 2 and 3 are absent, not zero.
	tag, err = Decode(realUID, []Block{blocks[3], blocks[4], blocks[5]})
	if err != nil {
		t.Fatal(err)
	}
	got := fields(tag)
	for _, k := range []string{"block0_uid", "variant_id", "filament_type", "x_cam_info", "tray_uid", "spool_width_mm", "production_date", "filament_length_m", "extra_color_format"} {
		if got[k] != "<absent>" {
			t.Errorf("%s = %q with its block missing", k, got[k])
		}
	}
	if got["detailed_filament_type"] != "PLA Basic" || got["hotend_max_temperature_c"] != "230" {
		t.Errorf("sector 1 fields = %v", got)
	}
	raw, err := json.Marshal(tag)
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{`"tray_uid"`, `"variant_id"`, `"second_color"`} {
		if bytes.Contains(raw, []byte(k)) {
			t.Errorf("JSON names %s with its block missing: %s", k, raw)
		}
	}
	if !bytes.Contains(raw, []byte(`"missing_blocks":[0,1,2,8,`)) {
		t.Errorf("JSON missing_blocks: %s", raw)
	}
}

func TestDecodeRejects(t *testing.T) {
	zero := Block{Index: 1}
	for _, c := range []struct {
		name   string
		uid    []byte
		blocks []Block
		want   string
	}{
		{"an empty uid", nil, nil, "uid is 0 bytes"},
		{"a 7-byte uid", make([]byte, 7), nil, "uid is 7 bytes"},
		{"a negative index", realUID, []Block{{Index: -1}}, "block -1 is outside"},
		{"index 64", realUID, []Block{{Index: 64}}, "block 64 is outside"},
		{"trailer 3", realUID, []Block{{Index: 3}}, "block 3 is a sector trailer"},
		{"trailer 63", realUID, []Block{{Index: 63}}, "block 63 is a sector trailer"},
		{"a duplicate", realUID, []Block{zero, zero}, "block 1 appears twice"},
		{"another tag's block 0", guideUID, []Block{block(0, "dc999be33d080400054073123440cc90")}, "block 0 names uid DC999BE3, not 75886B1D"},
	} {
		if _, err := Decode(c.uid, c.blocks); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err = %v, want %q", c.name, err, c.want)
		}
	}
}

func TestDecodeBlock16(t *testing.T) {
	for _, c := range []struct {
		name, hex, second string
		ranges            string
	}{
		// Format 2, two colours, the second stored as ABGR: FF 33 22 11 is #112233FF.
		{"two colours", "02000200ff3322110000000000000000", "#112233FF", "16:8 "},
		{"one colour", "0200010000000000aa00000000000000", "<absent>", "16:4 16:8 "},
		{"empty format", "00000200ff332211aa00000000000000", "<absent>", "16:4 16:8 "},
	} {
		tag, err := Decode(realUID, []Block{block(16, c.hex)})
		if err != nil {
			t.Fatal(err)
		}
		ranges := ""
		for _, r := range tag.Unknown {
			ranges += fmt.Sprintf("%d:%d ", r.Block, r.Offset)
		}
		if str(tag.SecondColor) != c.second || ranges != c.ranges {
			t.Errorf("%s: second = %s, ranges %q, want %s, %q", c.name, str(tag.SecondColor), ranges, c.second, c.ranges)
		}
	}
}

func TestDecodeOddValues(t *testing.T) {
	// A NaN diameter, an infinite nozzle diameter, a date that does not exist,
	// non-UTF-8 text: the fields are absent or replaced, and the JSON still marshals.
	tag, err := Decode(realUID, []Block{
		block(2, "ff504c41000000000000000000000000"),
		block(5, "9d432cffe80300000000c07f00000000"),
		block(8, "000000000000000000000000000080ff"),
		block(12, "323032365f30325f33305f32335f3135"),
		block(13, "61006200000000000000000000000000"),
	})
	if err != nil {
		t.Fatal(err)
	}
	got := fields(tag)
	for k, want := range map[string]string{
		"filament_diameter_mm":  "<absent>",
		"nozzle_diameter_mm":    "<absent>",
		"production_date":       "2026_02_30_23_15",
		"production_datetime":   "<absent>",
		"filament_type":         "�PLA",
		"short_production_date": "a b",
		"spool_weight_g":        "1000",
	} {
		if got[k] != want {
			t.Errorf("%s = %q, want %q", k, got[k], want)
		}
	}
	if _, err := json.Marshal(tag); err != nil {
		t.Error(err)
	}
}

func TestProductionTimeTakesOnlyDigits(t *testing.T) {
	for in, want := range map[string]string{
		"2026_05_08_23_15":    "2026-05-08T23:15:00",
		"+2026_05_08_23_15":   "<absent>",
		"2026_+5_08_23_15":    "<absent>",
		"2026_5_08_23_15":     "<absent>",
		"2026_05_08_3_15":     "<absent>",
		"2026_05_08_23_15x":   "<absent>",
		"2026_05_08_23_15 ":   "<absent>",
		"2026_05_08_23_15_00": "<absent>",
		"2026_02_30_23_15":    "<absent>",
		"2026_05_08_24_00":    "<absent>",
	} {
		got := "<absent>"
		if p := productionTime(in); p != nil {
			got = *p
		}
		if got != want {
			t.Errorf("productionTime(%q) = %q, want %q", in, got, want)
		}
	}
}
