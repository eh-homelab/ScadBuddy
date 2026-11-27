package labelprint

import (
	"bytes"
	"encoding/hex"
	"errors"
	"math"
	"testing"
)

// The byte streams are the examples of RFC 8010 appendix A: A.1 (with the
// "%!PDF..." of its data row as the data), A.2 and A.7, copied from the
// octets column of each table.
// https://www.rfc-editor.org/rfc/rfc8010.html#appendix-A
const (
	rfcPrintJobRequest  = "010100020000000101470012617474726962757465732d6368617273657400057574662d3848001b617474726962757465732d6e61747572616c2d6c616e67756167650005656e2d757345000b7072696e7465722d757269002c6970703a2f2f7072696e7465722e6578616d706c652e636f6d2f6970702f7072696e742f70696e65747265654200086a6f622d6e616d650006666f6f6261722200166970702d6174747269627574652d666964656c69747900010102210006636f706965730004000000144400057369646573001374776f2d73696465642d6c6f6e672d656467650325215044462e2e2e"
	rfcPrintJobResponse = "010100000000000101470012617474726962757465732d6368617273657400057574662d3848001b617474726962757465732d6e61747572616c2d6c616e67756167650005656e2d757341000e7374617475732d6d657373616765000d7375636365737366756c2d6f6b022100066a6f622d69640004000000934500076a6f622d75726900306970703a2f2f7072696e7465722e6578616d706c652e636f6d2f6970702f7072696e742f70696e65747265652f3134372300096a6f622d737461746500040000000303"
	rfcMediaCol         = "010100050000000101470012617474726962757465732d6368617273657400057574662d3848001b617474726962757465732d6e61747572616c2d6c616e67756167650005656e2d757345000b7072696e7465722d757269002c6970703a2f2f7072696e7465722e6578616d706c652e636f6d2f6970702f7072696e742f70696e65747265653400096d656469612d636f6c00004a0000000a6d656469612d73697a6534000000004a0000000b782d64696d656e73696f6e2100000004000052084a0000000b792d64696d656e73696f6e21000000040000740437000000004a0000000a6d656469612d74797065440000000a73746174696f6e657279370000000003"
	printerURI          = "ipp://printer.example.com/ipp/print/pinetree"
)

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestEncodeMatchesRFC8010PrintJob(t *testing.T) {
	m := Message{
		Version:   [2]byte{1, 1},
		Code:      2,
		RequestID: 1,
		Attrs: []Attr{
			StrAttr(TagOperation, "attributes-charset", TagCharset, "utf-8"),
			StrAttr(TagOperation, "attributes-natural-language", TagLanguage, "en-us"),
			StrAttr(TagOperation, "printer-uri", TagURI, printerURI),
			StrAttr(TagOperation, "job-name", TagName, "foobar"),
			IntAttr(TagOperation, "ipp-attribute-fidelity", TagBoolean, 1),
			IntAttr(TagJob, "copies", TagInteger, 20),
			StrAttr(TagJob, "sides", TagKeyword, "two-sided-long-edge"),
		},
		Data: []byte("%!PDF..."),
	}
	var buf bytes.Buffer
	if err := m.Encode(&buf); err != nil {
		t.Fatal(err)
	}
	want := mustHex(t, rfcPrintJobRequest)
	if !bytes.Equal(buf.Bytes(), want) {
		t.Fatalf("encoded\n%x\nwant\n%x", buf.Bytes(), want)
	}
}

func TestDecodeRFC8010PrintJobResponse(t *testing.T) {
	m, err := Decode(bytes.NewReader(mustHex(t, rfcPrintJobResponse)), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if m.Code != 0 {
		t.Fatalf("status %d", m.Code)
	}
	if v, ok := m.Int(TagJob, "job-id"); !ok || v != 147 {
		t.Fatalf("job-id %d %v", v, ok)
	}
	if v, ok := m.Int(TagJob, "job-state"); !ok || v != 3 {
		t.Fatalf("job-state %d %v", v, ok)
	}
	if got := m.Strings(TagOperation, "status-message"); len(got) != 1 || got[0] != "successful-ok" {
		t.Fatalf("status-message %v", got)
	}
	if len(m.Data) != 0 {
		t.Fatalf("data %q", m.Data)
	}
}

func TestDecodeSkipsCollections(t *testing.T) {
	m, err := Decode(bytes.NewReader(mustHex(t, rfcMediaCol)), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	got := m.Strings(TagOperation, "printer-uri")
	if len(got) != 1 || got[0] != printerURI {
		t.Fatalf("printer-uri %v", got)
	}
	if got := m.Strings(TagOperation, "media-col"); len(got) != 0 {
		t.Fatalf("media-col %v", got)
	}
	if got := m.Strings(TagOperation, ""); len(got) != 0 {
		t.Fatalf("member values %v", got)
	}
	g := m.Groups(TagOperation)
	if len(g) != 1 || len(g[0]) != 3 {
		t.Fatalf("groups %+v", g)
	}
	for _, a := range g[0] {
		if a.Name == "media-col" || a.Name == "" {
			t.Fatalf("collection record in group: %+v", a)
		}
	}
}

// rec is one attribute record: value-tag, name-length, name, value-length, value.
func rec(tag Tag, name, value string) []byte {
	b := []byte{byte(tag), byte(len(name) >> 8), byte(len(name))}
	b = append(b, name...)
	b = append(b, byte(len(value)>>8), byte(len(value)))
	return append(b, value...)
}

func msg(parts ...[]byte) []byte {
	b := []byte{1, 1, 0, 0, 0, 0, 0, 1}
	for _, p := range parts {
		b = append(b, p...)
	}
	return b
}

func TestGroupsSkipsCollections(t *testing.T) {
	in := msg([]byte{byte(TagPrinter)},
		rec(TagName, "printer-name", "a"),
		rec(tagBegCollection, "media-col-default", ""),
		rec(tagMemberName, "", "media-type"),
		rec(TagKeyword, "", "labels"),
		rec(tagEndCollection, "", ""),
		rec(TagURI, "printer-uri-supported", "ipp://x/printers/a"),
		[]byte{byte(TagPrinter)},
		rec(TagName, "printer-name", "b"),
		[]byte{byte(TagEnd)})
	m, err := Decode(bytes.NewReader(in), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	g := m.Groups(TagPrinter)
	if len(g) != 2 || len(g[0]) != 2 || len(g[1]) != 1 {
		t.Fatalf("groups %+v", g)
	}
	if g[0][0].Name != "printer-name" || g[0][1].Name != "printer-uri-supported" {
		t.Fatalf("group 0 %+v", g[0])
	}
}

func TestDecodeAcceptsMixedValueTags(t *testing.T) {
	// RFC 8010 section 3.1.5: each additional value has its own value-tag, so
	// 1setOf (keyword | name) is valid.
	in := msg([]byte{byte(TagPrinter)},
		rec(TagKeyword, "job-sheets-default", "none"),
		rec(TagName, "", "my-banner"),
		[]byte{byte(TagEnd)})
	m, err := Decode(bytes.NewReader(in), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if got := m.Strings(TagPrinter, "job-sheets-default"); len(got) != 2 || got[0] != "none" || got[1] != "my-banner" {
		t.Fatalf("values %q", got)
	}
	var buf bytes.Buffer
	if err := m.Encode(&buf); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(buf.Bytes(), in) {
		t.Fatalf("encoded\n%x\nwant\n%x", buf.Bytes(), in)
	}
}

func TestStringsOnlyCharacterStrings(t *testing.T) {
	in := msg([]byte{byte(TagPrinter)},
		rec(0x30, "printer-current-time", "\x07\xea\x0b\x1b\x0c\x00\x00\x00+\x00\x00"),
		rec(0x32, "printer-resolution-default", "\x00\x00\x01\x2c\x00\x00\x01\x2c\x03"),
		rec(0x33, "copies-supported", "\x00\x00\x00\x01\x00\x00\x00\x09"),
		rec(0x13, "printer-info", ""),
		rec(0x12, "printer-location", ""),
		rec(0x10, "printer-geo-location", ""),
		rec(0x36, "printer-make-and-model", "\x00\x02en\x00\x03ZPL"),
		rec(TagText, "printer-info", "shelf"),
		[]byte{byte(TagEnd)})
	m, err := Decode(bytes.NewReader(in), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"printer-current-time", "printer-resolution-default", "copies-supported", "printer-location", "printer-geo-location"} {
		if got := m.Strings(TagPrinter, name); len(got) != 0 {
			t.Errorf("%s: %q", name, got)
		}
	}
	if got := m.Strings(TagPrinter, "printer-info"); len(got) != 1 || got[0] != "shelf" {
		t.Errorf("printer-info %q", got)
	}
	if got := m.Strings(TagPrinter, "printer-make-and-model"); len(got) != 1 || got[0] != "ZPL" {
		t.Errorf("printer-make-and-model %q", got)
	}
}

func TestIntOnlyIntegerBooleanEnum(t *testing.T) {
	in := msg([]byte{byte(TagPrinter)},
		rec(TagText, "a", "abcd"),
		rec(TagKeyword, "b", "x"),
		rec(TagEnum, "c", "\x00\x00\x00\x03"),
		rec(TagBoolean, "d", "\x01"),
		[]byte{byte(TagEnd)})
	m, err := Decode(bytes.NewReader(in), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"a", "b"} {
		if v, ok := m.Int(TagPrinter, name); ok {
			t.Errorf("%s: %d", name, v)
		}
	}
	if v, ok := m.Int(TagPrinter, "c"); !ok || v != 3 {
		t.Errorf("c: %d %v", v, ok)
	}
	if v, ok := m.Int(TagPrinter, "d"); !ok || v != 1 {
		t.Errorf("d: %d %v", v, ok)
	}
}

func TestDecodeRefusesUnbalancedCollections(t *testing.T) {
	open := rec(tagBegCollection, "media-col", "")
	cases := map[string][]byte{
		"open at end":       msg([]byte{byte(TagOperation)}, open, []byte{byte(TagEnd)}),
		"open at delimiter": msg([]byte{byte(TagOperation)}, open, []byte{byte(TagJob)}, rec(TagName, "job-name", "x"), []byte{byte(TagEnd)}),
		"stray end":         msg([]byte{byte(TagOperation)}, rec(tagEndCollection, "", ""), []byte{byte(TagEnd)}),
	}
	for name, in := range cases {
		if _, err := Decode(bytes.NewReader(in), 1<<20); !errors.Is(err, ErrNotIPP) {
			t.Errorf("%s: err %v", name, err)
		}
	}
}

func TestDecodeMaxLimit(t *testing.T) {
	if _, err := Decode(bytes.NewReader(mustHex(t, rfcPrintJobResponse)), math.MaxInt64); err != nil {
		t.Fatal(err)
	}
}

func TestDecodeRefusesShortAndOverLimit(t *testing.T) {
	full := mustHex(t, rfcPrintJobResponse)
	valuePast := append(append([]byte{}, full[:8]...), 0x01, 0x47, 0x00, 0x01, 'a', 0x00, 0x09, 'x')
	cases := map[string]struct {
		in    []byte
		limit int64
	}{
		"short":             {full[:7], 1 << 20},
		"value past":        {valuePast, 1 << 20},
		"over limit":        {full, int64(len(full) - 1)},
		"no end tag":        {full[:len(full)-1], 1 << 20},
		"short name length": {append(append([]byte{}, full[:8]...), 0x01, 0x47, 0x00), 1 << 20},
		"value length 0xFFFF": {append(append([]byte{}, full[:8]...),
			0x01, 0x47, 0x00, 0x01, 'a', 0xFF, 0xFF, 'u', 't', 'f', 0x03), 1 << 20},
	}
	for name, c := range cases {
		if _, err := Decode(bytes.NewReader(c.in), c.limit); !errors.Is(err, ErrNotIPP) {
			t.Errorf("%s: err %v", name, err)
		}
	}
	if _, err := Decode(bytes.NewReader(full), int64(len(full))); err != nil {
		t.Errorf("at limit: %v", err)
	}
}

func TestGroupsSplitsPrinters(t *testing.T) {
	m := Message{Version: [2]byte{1, 1}, Attrs: []Attr{
		StrAttr(TagPrinter, "printer-name", TagName, "a"),
		StrAttr(TagPrinter, "printer-uri-supported", TagURI, "ipp://x/printers/a", "ipp://y/printers/a"),
		{Group: TagPrinter},
		StrAttr(TagPrinter, "printer-name", TagName, "b"),
	}}
	var buf bytes.Buffer
	if err := m.Encode(&buf); err != nil {
		t.Fatal(err)
	}
	d, err := Decode(&buf, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	g := d.Groups(TagPrinter)
	if len(g) != 2 {
		t.Fatalf("groups %d", len(g))
	}
	if len(g[0]) != 2 || len(g[1]) != 1 {
		t.Fatalf("group sizes %d %d", len(g[0]), len(g[1]))
	}
	if string(g[0][0].Values[0]) != "a" || string(g[1][0].Values[0]) != "b" {
		t.Fatalf("groups %+v", g)
	}
	if got := g[0][1].Values; len(got) != 2 {
		t.Fatalf("values %q", got)
	}
}
