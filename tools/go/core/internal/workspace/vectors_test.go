package workspace

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"testing"
	"testing/iotest"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The vectors are Python's own (backend/tests/test_workspace_seal_vectors.py): Go seals
// each file to the same bytes, opens Python's, and wraps and opens each file key alike.
const vectorsFile = "../../../../../backend/tests/fixtures/workspace-seal-vectors.json"

type fileVector struct {
	Size         int      `json:"size"`
	Seed         int      `json:"seed"`
	IVCounter    int      `json:"iv_counter"`
	SHA256       string   `json:"sha256"`
	FileKeyB64   string   `json:"file_key_b64"`
	Contexts     []string `json:"contexts"`
	SealedSize   int      `json:"sealed_size"`
	SealedSHA256 string   `json:"sealed_sha256"`
	SealedB64    string   `json:"sealed_b64"`
}

type wrapVector struct {
	Workspace  string `json:"workspace"`
	Path       string `json:"path"`
	File       int    `json:"file"`
	BlobKey    string `json:"blob_key"`
	Context    string `json:"context"`
	Under      string `json:"under"`
	FileKeyB64 string `json:"file_key_b64"`
	IVB64      string `json:"iv_b64"`
	WrappedB64 string `json:"wrapped_b64"`
}

type vectors struct {
	KEKB64       string `json:"kek_b64"`
	KEKID        string `json:"kek_id"`
	SegmentBytes int    `json:"segment_bytes"`
	Session      string `json:"session"`
	PayloadKey   struct {
		Subject      string `json:"subject"`
		Context      string `json:"context"`
		DEKB64       string `json:"dek_b64"`
		IVB64        string `json:"iv_b64"`
		DEKSealedB64 string `json:"dek_sealed_b64"`
	} `json:"payload_key"`
	Files []fileVector `json:"files"`
	Wraps []wrapVector `json:"wraps"`
}

func loadVectors(t *testing.T) vectors {
	t.Helper()
	raw, err := os.ReadFile(vectorsFile)
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func b64(t *testing.T, s string) []byte {
	t.Helper()
	out, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// counter is the vectors' IV source: byte i of the stream is (start + i) & 0xff.
type counter struct{ n int }

func (c *counter) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = byte(c.n + i)
	}
	c.n += len(p)
	return len(p), nil
}

func vectorPlaintext(size, seed int) []byte {
	out := make([]byte, size)
	for i := range out {
		out[i] = byte(i*7 + seed)
	}
	return out
}

// pythonSealed is file v sealed as Python sealed it: Go's sealing, which the test below
// holds to Python's bytes.
func pythonSealed(v fileVector) []byte {
	key, _ := base64.StdEncoding.DecodeString(v.FileKeyB64)
	return sealFile(key, vectorPlaintext(v.Size, v.Seed), v.SHA256, &counter{n: v.IVCounter})
}

func TestGoSealsAFileToPythonsBytesAndOpensThem(t *testing.T) {
	v := loadVectors(t)
	if v.SegmentBytes != segmentBytes {
		t.Fatalf("segment_bytes %d, Go's %d", v.SegmentBytes, segmentBytes)
	}
	for _, f := range v.Files {
		data := vectorPlaintext(f.Size, f.Seed)
		sum := sha256.Sum256(data)
		if hex.EncodeToString(sum[:]) != f.SHA256 {
			t.Fatalf("size %d: the plaintext is not the vector's", f.Size)
		}
		for i, want := range f.Contexts {
			if got := segmentContext(f.SHA256, i, i == len(f.Contexts)-1); got != want {
				t.Errorf("size %d segment %d: context %q, Python's %q", f.Size, i, got, want)
			}
		}
		sealed := pythonSealed(f)
		sealedSum := sha256.Sum256(sealed)
		if len(sealed) != f.SealedSize || hex.EncodeToString(sealedSum[:]) != f.SealedSHA256 {
			t.Errorf("size %d: Go sealed %d bytes that are not Python's %d", f.Size, len(sealed), f.SealedSize)
		}
		if f.SealedB64 != "" {
			sealed = b64(t, f.SealedB64)
		}
		key := b64(t, f.FileKeyB64)
		// In odd reads and in whole ones: segment edges never fall where a read does.
		for _, reader := range []io.Reader{iotest.HalfReader(bytes.NewReader(sealed)), bytes.NewReader(sealed)} {
			var got []byte
			emit := func(p []byte) error { got = append(got, p...); return nil }
			if err := openFile(key, reader, f.SHA256, emit); err != nil {
				t.Fatalf("size %d: %v", f.Size, err)
			}
			if !bytes.Equal(got, data) {
				t.Errorf("size %d: opened %d bytes that are not the file", f.Size, len(got))
			}
		}
		// Each segment opens in its own context only.
		for i, context := range f.Contexts {
			start := i * sealedSegment
			end := min(start+sealedSegment, len(sealed))
			if _, err := seal.OpenBytes(key, sealed[start:end], context); err != nil {
				t.Errorf("size %d segment %d does not open in %q", f.Size, i, context)
			}
		}
	}
}

func TestACutOrReorderedFileDoesNotOpen(t *testing.T) {
	v := loadVectors(t)
	f := v.Files[3] // one byte past a segment: two segments
	sealed, key := pythonSealed(f), b64(t, f.FileKeyB64)
	discard := func([]byte) error { return nil }
	if err := openFile(key, bytes.NewReader(sealed[:sealedSegment]), f.SHA256, discard); err == nil {
		t.Error("a file cut after its first segment opened")
	}
	if err := openFile(key, bytes.NewReader(sealed[:10]), f.SHA256, discard); err != errCutShort {
		t.Errorf("a file cut inside its overhead: %v", err)
	}
	swapped := append(append([]byte{}, sealed[sealedSegment:]...), sealed[:sealedSegment]...)
	if err := openFile(key, bytes.NewReader(swapped), f.SHA256, discard); err == nil {
		t.Error("a reordered file opened")
	}
}

func TestGoWrapsAFileKeyAsPythonDoes(t *testing.T) {
	v := loadVectors(t)
	kek, err := seal.FromBase64(v.KEKB64)
	if err != nil {
		t.Fatal(err)
	}
	if kek.ID != v.KEKID {
		t.Fatalf("kek id %s, Python's %s", kek.ID, v.KEKID)
	}
	pk := v.PayloadKey
	if seal.PayloadKeyContext(pk.Subject) != pk.Context || pk.Subject != "session-"+v.Session {
		t.Fatalf("payload key context %q, Python's %q", seal.PayloadKeyContext(pk.Subject), pk.Context)
	}
	dek := b64(t, pk.DEKB64)
	if got := seal.SealBytesIV(kek.Key(), dek, pk.Context, b64(t, pk.IVB64)); !bytes.Equal(got, b64(t, pk.DEKSealedB64)) {
		t.Error("the payload key is not sealed as Python seals it")
	}
	for _, w := range v.Wraps {
		if got := fileKeyContext(w.Workspace, w.Path, w.BlobKey); got != w.Context {
			t.Errorf("context %q, Python's %q", got, w.Context)
		}
		if w.BlobKey != "workspace-"+v.Files[w.File].SealedSHA256 {
			t.Errorf("blob key %s is not its file's", w.BlobKey)
		}
		key := dek
		if w.Under == "kek" {
			key = kek.Key()
		}
		fileKey := b64(t, w.FileKeyB64)
		if got := seal.SealBytesIV(key, fileKey, w.Context, b64(t, w.IVB64)); !bytes.Equal(got, b64(t, w.WrappedB64)) {
			t.Errorf("%s %s: the file key is not wrapped as Python wraps it", w.Workspace, w.Path)
		}
		opened, err := seal.OpenBytes(key, b64(t, w.WrappedB64), w.Context)
		if err != nil || !bytes.Equal(opened, fileKey) {
			t.Errorf("%s %s: Python's wrap does not open", w.Workspace, w.Path)
		}
	}
}
