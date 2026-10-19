package api

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestListMapsFoldersAndFiles(t *testing.T) {
	f := newFake(t)
	f.folder(4, "Top", 0)
	f.folder(5, "Inner", 4)
	f.file(10, "a.3mf", 4, []byte("abc"), true)
	f.file(11, "notes.txt", 4, []byte("n"), false)
	f.file(12, "elsewhere.3mf", 0, nil, false)
	c := f.client(t)

	l, err := c.List(context.Background(), 4, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(l.Files) != 2 || l.Files[0].Name != "a.3mf" || l.Files[0].Size != 3 {
		t.Fatalf("%+v", l.Files)
	}
	a := l.Files[0]
	if a.ID != 10 || a.FolderID != 4 || !a.External || !a.Created.Equal(time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)) {
		t.Fatalf("%+v", a)
	}
	want := []Folder{{ID: 4, Name: "Top", ParentID: 0}, {ID: 5, Name: "Inner", ParentID: 4}}
	if len(l.Folders) != 2 || l.Folders[0] != want[0] || l.Folders[1] != want[1] {
		t.Fatalf("%+v", l.Folders)
	}

	l, err = c.List(context.Background(), 4, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(l.Files) != 1 {
		t.Fatalf("without all: %+v", l.Files)
	}

	top, err := c.List(context.Background(), 0, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(top.Files) != 1 || top.Files[0].ID != 12 || top.Files[0].FolderID != 0 {
		t.Fatalf("top level: %+v", top.Files)
	}
}

func TestA202IsFollowedToItsResult(t *testing.T) {
	f := newFake(t)
	f.slow["mkdir"] = true
	c := f.client(t)
	got, err := c.Mkdir(context.Background(), 0, "Parts")
	if err != nil {
		t.Fatal(err)
	}
	if got.Name != "Parts" || f.operationPolls < 2 {
		t.Fatalf("%+v polls=%d", got, f.operationPolls)
	}
}

func TestAFailedOperationCarriesItsStatus(t *testing.T) {
	f := newFake(t)
	f.file(10, "a.3mf", 0, nil, false)
	f.queued[10] = true
	f.slow["delete"] = true
	err := f.client(t).DeleteFiles(context.Background(), 10)
	if Errno(err) != syscall.EBUSY || !strings.Contains(err.Error(), "waiting to print") {
		t.Fatalf("errno %v from %v", Errno(err), err)
	}
}

func TestAnOperationThatOutlivesTheContextIsEIO(t *testing.T) {
	f := newFake(t)
	f.slow["mkdir"] = true
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_, err := f.client(t).Mkdir(ctx, 0, "Parts")
	if Errno(err) != syscall.EIO || !errors.Is(err, context.DeadlineExceeded) || !strings.Contains(err.Error(), "op1") {
		t.Fatalf("errno %v from %v", Errno(err), err)
	}
}

func TestAPollThatFailsIsRetried(t *testing.T) {
	f := newFake(t)
	f.slow["mkdir"] = true
	f.failPolls = 1
	got, err := f.client(t).Mkdir(context.Background(), 0, "Parts")
	if err != nil {
		t.Fatal(err)
	}
	if got.Name != "Parts" || f.operationPolls < 3 {
		t.Fatalf("%+v polls=%d", got, f.operationPolls)
	}
}

func TestALostOperationIsEIO(t *testing.T) {
	f := newFake(t)
	f.slow["mkdir"] = true
	f.lostOps = true
	_, err := f.client(t).Mkdir(context.Background(), 0, "Parts")
	if Errno(err) != syscall.EIO || !strings.Contains(err.Error(), "op1") {
		t.Fatalf("errno %v from %v", Errno(err), err)
	}
}

func TestAKeptFolderIsEBUSY(t *testing.T) {
	f := newFake(t)
	f.folder(4, "Top", 0)
	f.keep[4] = true
	err := f.client(t).DeleteFolder(context.Background(), 4)
	if Errno(err) != syscall.EBUSY {
		t.Fatalf("errno %v from %v", Errno(err), err)
	}
}

func TestAQueuedFileIsEBUSY(t *testing.T) {
	f := newFake(t)
	f.file(10, "a.3mf", 0, nil, false)
	f.queued[10] = true
	err := f.client(t).DeleteFiles(context.Background(), 10)
	if Errno(err) != syscall.EBUSY {
		t.Fatalf("errno %v from %v", Errno(err), err)
	}
}

func TestASkippedDeleteIsEBUSY(t *testing.T) {
	f := newFake(t)
	f.folder(4, "Top", 0)
	f.file(10, "a.3mf", 4, nil, false)
	f.skip[10] = "changed while deleting"
	c := f.client(t)
	for name, err := range map[string]error{
		"files":  c.DeleteFiles(context.Background(), 10),
		"folder": c.DeleteFolder(context.Background(), 4),
	} {
		if Errno(err) != syscall.EBUSY || !strings.Contains(err.Error(), "changed while deleting") {
			t.Errorf("%s: errno %v from %v", name, Errno(err), err)
		}
	}
}

func TestDeletesAndAMissingFolder(t *testing.T) {
	f := newFake(t)
	f.folder(4, "Top", 0)
	f.file(10, "a.3mf", 4, nil, false)
	f.file(11, "b.3mf", 0, nil, true)
	c := f.client(t)
	if err := c.DeleteFolder(context.Background(), 4); err != nil {
		t.Fatal(err)
	}
	if err := c.DeleteFiles(context.Background(), 11); err != nil {
		t.Fatal(err)
	}
	if len(f.files) != 0 || len(f.folders) != 0 {
		t.Fatalf("left %v %v", f.files, f.folders)
	}
	if err := c.DeleteFolder(context.Background(), 4); Errno(err) != syscall.ENOENT {
		t.Fatalf("errno %v from %v", Errno(err), err)
	}
}

func TestUpdatesSendOnlyTheFieldsGiven(t *testing.T) {
	f := newFake(t)
	f.folder(4, "Top", 0)
	f.file(10, "a.3mf", 4, nil, false)
	c := f.client(t)

	name := "b.3mf"
	got, err := c.UpdateFile(context.Background(), 10, &name, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got.Name != "b.3mf" || got.FolderID != 4 || len(f.lastBody) != 1 {
		t.Fatalf("%+v body=%v", got, f.lastBody)
	}

	top := 0
	got, err = c.UpdateFile(context.Background(), 10, nil, &top)
	if err != nil {
		t.Fatal(err)
	}
	if got.FolderID != 0 || len(f.lastBody) != 1 || f.lastBody["folder_id"] != float64(0) {
		t.Fatalf("%+v body=%v", got, f.lastBody)
	}

	f.slow["update_folder"] = true
	parts := "Parts"
	folder, err := c.UpdateFolder(context.Background(), 4, &parts, nil)
	if err != nil {
		t.Fatal(err)
	}
	if folder != (Folder{ID: 4, Name: "Parts"}) || len(f.lastBody) != 1 {
		t.Fatalf("%+v body=%v", folder, f.lastBody)
	}

	sub, err := c.Mkdir(context.Background(), 4, "Sub")
	if err != nil {
		t.Fatal(err)
	}
	if sub.ParentID != 4 {
		t.Fatalf("%+v", sub)
	}
	if _, err := c.UpdateFile(context.Background(), 99, &name, nil); Errno(err) != syscall.ENOENT {
		t.Fatalf("errno %v from %v", Errno(err), err)
	}
}

func TestErrnoMapping(t *testing.T) {
	cases := map[int]syscall.Errno{404: syscall.ENOENT, 409: syscall.EBUSY, 413: syscall.EFBIG, 401: syscall.EACCES, 403: syscall.EACCES, 500: syscall.EIO, 502: syscall.EIO}
	for status, want := range cases {
		if got := Errno(&Error{Status: status}); got != want {
			t.Errorf("%d: %v, want %v", status, got, want)
		}
	}
	if Errno(context.DeadlineExceeded) != syscall.EIO {
		t.Error("timeout is EIO")
	}
	if Errno(nil) != 0 {
		t.Error("nil is 0")
	}

	scope := errorFrom("delete", 409, []byte(`{"status":409,"detail":"The key needs the 'manage_library' scope","required_scope":"manage_library"}`))
	if Errno(scope) != syscall.EACCES || !strings.Contains(scope.Error(), "manage_library") {
		t.Errorf("scope: errno %v from %v", Errno(scope), scope)
	}
	busy := errorFrom("delete", 409, []byte(`{"status":409,"detail":"waiting to print"}`))
	if Errno(busy) != syscall.EBUSY || busy.Detail != "waiting to print" {
		t.Errorf("busy: %+v", busy)
	}
	invalid := errorFrom("list", 422, []byte(`{"detail":[{"msg":"bad"}]}`))
	if invalid.Detail != `[{"msg":"bad"}]` {
		t.Errorf("invalid: %+v", invalid)
	}
	// A refusal ScadBuddy relays (#4142): Bambuddy's own status, at the top or in a failed operation's extensions.
	relayed := errorFrom("upload", 502, []byte(`{"status":502,"detail":"Bambuddy answered 400 when asked to upload a:b","bambuddy_status":400}`))
	// A failed operation's `error`, as GET /operations/{id} carries it.
	inOperation := errorFrom("upload", 502, []byte(`{"type":"https://scadbuddy.dev/problems/bambuddy-unavailable","title":"Bad Gateway","status":502,"detail":"Bambuddy answered 400","extensions":{"bambuddy_status":400}}`))
	own := errorFrom("upload", 502, []byte(`{"status":502,"detail":"Bambuddy could not be reached"}`))
	if relayed.Upstream != 400 || inOperation.Upstream != 400 || own.Upstream != 0 || relayed.Status != 502 {
		t.Errorf("upstream: %+v %+v %+v", relayed, inOperation, own)
	}
}

func TestDownloadStreamsAndUploadSendsAKey(t *testing.T) {
	f := newFake(t)
	big := bytes.Repeat([]byte("x"), 5<<20)
	f.file(10, "big.3mf", 0, big, false)
	c := f.client(t)
	var got bytes.Buffer
	if err := c.Download(context.Background(), 10, &got); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got.Bytes(), big) {
		t.Fatal("download differs")
	}
	if err := c.Download(context.Background(), 99, &got); Errno(err) != syscall.ENOENT {
		t.Fatalf("errno %v from %v", Errno(err), err)
	}

	f.folder(4, "Top", 0)
	f.slow["upload"] = true
	up, err := c.Upload(context.Background(), 4, "new.3mf", "", bytes.NewReader([]byte("PK")))
	if err != nil {
		t.Fatal(err)
	}
	if len(f.lastKey) != 32 {
		t.Fatalf("Idempotency-Key on upload: %q", f.lastKey)
	}
	if _, err := c.Upload(context.Background(), 4, "again.3mf", "the-write's-key", bytes.NewReader([]byte("PK"))); err != nil || f.lastKey != "the-write's-key" {
		t.Fatalf("given key: %q %v", f.lastKey, err)
	}
	stored := f.files[up.ID]
	if up.Size != 2 || time.Since(up.Created) > time.Minute {
		t.Fatalf("size and created: %+v", up)
	}
	if up.Name != "new.3mf" || up.FolderID != 4 || stored == nil || string(stored.data) != "PK" || stored.folder != 4 {
		t.Fatalf("%+v stored=%+v", up, stored)
	}
}

func TestEveryWriteSendsItsOwnKey(t *testing.T) {
	f := newFake(t)
	c := f.client(t)
	if _, err := c.Mkdir(context.Background(), 0, "A"); err != nil {
		t.Fatal(err)
	}
	first := f.lastKey
	if _, err := c.Mkdir(context.Background(), 0, "B"); err != nil {
		t.Fatal(err)
	}
	if len(first) != 32 || f.lastKey == first {
		t.Fatalf("keys %q then %q", first, f.lastKey)
	}
}

// ScadBuddy's still-checking 503 (#4141) is followed by sending the same request under the
// same key again, for every write: the delete of a replaced file and an upload included.
func TestAStillCheckingAnswerIsFollowedForEveryWrite(t *testing.T) {
	for _, c := range []struct {
		kind string
		run  func(*Client) error
	}{
		{"upload", func(c *Client) error {
			_, err := c.Upload(context.Background(), 0, "a.3mf", "", bytes.NewReader(bytes.Repeat([]byte("ab"), 1<<16)))
			return err
		}},
		{"delete", func(c *Client) error { return c.DeleteFiles(context.Background(), 10) }},
		{"update_file", func(c *Client) error {
			name := "b.3mf"
			_, err := c.UpdateFile(context.Background(), 10, &name, nil)
			return err
		}},
		{"mkdir", func(c *Client) error { _, err := c.Mkdir(context.Background(), 0, "New"); return err }},
		{"update_folder", func(c *Client) error {
			name := "Moved"
			_, err := c.UpdateFolder(context.Background(), 4, &name, nil)
			return err
		}},
		{"delete_folder", func(c *Client) error { return c.DeleteFolder(context.Background(), 4) }},
	} {
		for _, mayHaveStarted := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s may_have_started=%v", c.kind, mayHaveStarted), func(t *testing.T) {
				f := newFake(t)
				f.folder(4, "Top", 0)
				f.file(10, "a.3mf", 0, []byte("x"), false)
				f.checking[c.kind] = 1
				f.mayHaveStarted = mayHaveStarted
				if err := c.run(f.client(t)); err != nil {
					t.Fatal(err)
				}
				if len(f.checkedKeys) != 1 || f.lastKey != f.checkedKeys[0] {
					t.Fatalf("keys: checked %v, then %q", f.checkedKeys, f.lastKey)
				}
				if c.kind == "upload" {
					uploaded := false
					for _, fl := range f.files {
						uploaded = uploaded || (fl.name == "a.3mf" && len(fl.data) == 2<<16)
					}
					if !uploaded {
						t.Fatal("the upload sent again did not carry the whole file")
					}
				}
			})
		}
	}
}

func TestAStillCheckingAnswerIsFollowedOnlyWithinTheContext(t *testing.T) {
	f := newFake(t)
	f.checking["mkdir"] = 1 << 20
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	if _, err := f.client(t).Mkdir(ctx, 0, "New"); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err %v, want the deadline", err)
	}
	// An upload whose reader cannot be read again is answered with the 503, not sent again.
	// Under the fake's lock: the mkdir the deadline cut may still be in its handler.
	f.mu.Lock()
	f.checking["upload"] = 1
	f.mu.Unlock()
	_, err := f.client(t).Upload(context.Background(), 0, "a.3mf", "", struct{ io.Reader }{strings.NewReader("abc")})
	var e *Error
	if !errors.As(err, &e) || e.Status != 503 {
		t.Fatalf("err %v, want the 503", err)
	}
}
