package labelprint

import (
	"bytes"
	"encoding/hex"
	"errors"
	"testing"
)

// The byte streams are the examples of RFC 8010 appendix A.
// https://www.rfc-editor.org/rfc/rfc8010.html#appendix-A
const (
	rfcPrintJobRequest  = "010100020000000101470012617474726962757465732d6368617273657400057574662d3848001b617474726962757465732d6e61747572616c2d6c616e67756167650005656e2d757345000b7072696e7465722d757269002c6970703a2f2f7072696e7465722e6578616d706c652e636f6d2f6970702f7072696e742f70696e65747265654200086a6f622d6e616d650006666f6f6261722200166970702d6174747269627574652d666964656c69747900010102210006636f706965730004000000144400057369646573001374776f2d73696465642d6c6f6e672d6564676503" + "2521504446"
	rfcPrintJobResponse = "010100000000000101470012617474726962757465732d6368617273657400057574662d3848001b617474726962757465732d6e61747572616c2d6c616e67756167650005656e2d757341000e7374617475732d6d657373616765000d7375636365737366756c2d6f6b022100066a6f622d69640004000000934500076a6f622d75726900276970703a2f2f7072696e7465722e6578616d706c652e636f6d2f6970702f7072696e742f3134372300096a6f622d73746174650004000000034400116a6f622d73746174652d726561736f6e73000a6a6f622d71756575656403"
	rfcMediaCol         = "010100020000000101470012617474726962757465732d6368617273657400057574662d3848001b617474726962757465732d6e61747572616c2d6c616e67756167650005656e2d757345000b7072696e7465722d757269002c6970703a2f2f7072696e7465722e6578616d706c652e636f6d2f6970702f7072696e742f70696e6574726565023400096d656469612d636f6c00004a0000000a6d656469612d73697a6534000000004a0000000b782d64696d656e73696f6e2100000004000054564a0000000b792d64696d656e73696f6e210000000400006d2437000000004a0000000b6d656469612d636f6c6f724200000004626c75653700000000032521504446"
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
		Data: []byte("%!PDF"),
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
	if got := m.Strings(TagJob, "media-col"); len(got) != 0 {
		t.Fatalf("media-col %v", got)
	}
	if string(m.Data) != "%!PDF" {
		t.Fatalf("data %q", m.Data)
	}
}

func TestDecodeRefusesShortAndOverLimit(t *testing.T) {
	full := mustHex(t, rfcPrintJobResponse)
	valuePast := append(append([]byte{}, full[:8]...), 0x01, 0x47, 0x00, 0x01, 'a', 0x00, 0x09, 'x')
	cases := map[string]struct {
		in    []byte
		limit int64
	}{
		"short":      {full[:7], 1 << 20},
		"value past": {valuePast, 1 << 20},
		"over limit": {full, int64(len(full) - 1)},
		"no end tag": {full[:len(full)-1], 1 << 20},
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
	if len(d.Groups(TagPrinter)) != 2 {
		t.Fatalf("groups %d", len(d.Groups(TagPrinter)))
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
