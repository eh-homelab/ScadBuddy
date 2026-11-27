// Package blobclient stores bytes in the blob store through the shared CRUD area blobs
// (#7958, internal/blobstore): it asks for an upload, PUTs the bytes to the URL it gets
// with the headers it gets, and commits the upload. The bytes go as the PUT's body, never
// base64 (#2479), and the client holds no store credential.
package blobclient

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

// Path is the area's routes under core's URL.
const Path = "/api/v1/internal/crud/blobs"

// answerMax bounds an answer of the API read.
const answerMax = 64 << 10

// Ref is a stored blob: its content index key, sha256 and size.
type Ref struct {
	Key    string `json:"key"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// Error is a refused step. Its text never holds the upload's URL or headers, which carry
// a signature.
type Error struct {
	// Step is "upload", "put" or "commit".
	Step   string
	Status int
	// Reason is the problem's reason (kind_not_allowed, upload_mismatch, …), "" for none.
	Reason string
	Detail string
}

func (e *Error) Error() string {
	text := fmt.Sprintf("blob %s: HTTP %d", e.Step, e.Status)
	if e.Reason != "" {
		text += " " + e.Reason
	}
	if e.Detail != "" {
		text += ": " + e.Detail
	}
	return text
}

// Client reaches the area with a service credential whose scopes hold crud.blobs.
type Client struct {
	// BaseURL is core's URL, with no path.
	BaseURL string
	// Token is the service credential for each call.
	Token func() (string, error)
	// HTTP sends the calls to core.
	HTTP *http.Client
	// Store sends the PUT to the upload's URL, which is the blob store's, outside ScadBuddy.
	Store *http.Client
}

// New is a Client of the area at baseURL. Each call to core is bounded by callTimeout (2
// minutes) and the PUT to the store by 30 s. tel is the
// process's telemetry (nil for none): each request is a CLIENT span, which records the
// method, the status and the peer, never a URL, so the presigned query stays out. The calls
// to core carry the trace context; the PUT to the store carries none (httpx.Outside).
func New(baseURL string, token func() (string, error), tel *telemetry.Telemetry) *Client {
	base := strings.TrimRight(baseURL, "/")
	peer := ""
	if u, err := url.Parse(base); err == nil {
		peer = u.Hostname()
	}
	return &Client{
		BaseURL: base,
		Token:   token,
		HTTP:    &http.Client{Timeout: callTimeout, Transport: tel.Transport(http.DefaultTransport, peer)},
		Store:   &http.Client{Timeout: 30 * time.Second, Transport: tel.Transport(httpx.Outside(nil), "s3")},
	}
}

// callTimeout bounds each call to core. A commit may wait up to 10 s for a lock slot and
// 30 s for the object's lock (blobstore slotWait, stageLockTries × stageLockTimeout) before
// it answers 503 store_busy, then make its store requests; this leaves room for all of it,
// so the caller gets core's answer rather than its own timeout.
const callTimeout = 2 * time.Minute

type slot struct {
	UploadID string            `json:"upload_id"`
	URL      string            `json:"url"`
	Headers  map[string]string `json:"headers"`
}

// Upload stores body as a blob of kind with mediaType and answers its Ref.
func (c *Client) Upload(ctx context.Context, kind, mediaType string, body []byte) (Ref, error) {
	digest := sha256.Sum256(body)
	sum := hex.EncodeToString(digest[:])
	var s slot
	err := c.call(ctx, "upload", Path+"/uploads", map[string]any{
		"kind": kind, "size": len(body), "sha256": sum, "media_type": mediaType,
	}, &s)
	if err != nil {
		return Ref{}, err
	}
	if s.UploadID == "" || s.URL == "" {
		return Ref{}, &Error{Step: "upload", Status: http.StatusOK, Detail: "an answer without an upload"}
	}
	if err := c.put(ctx, s, body); err != nil {
		return Ref{}, err
	}
	var ref Ref
	if err := c.call(ctx, "commit", Path+"/uploads/"+url.PathEscape(s.UploadID)+"/commit", nil, &ref); err != nil {
		return Ref{}, err
	}
	if ref.SHA256 != sum || ref.Size != int64(len(body)) || ref.Key == "" {
		return Ref{}, &Error{Step: "commit", Status: http.StatusOK, Detail: "the commit named another blob"}
	}
	return ref, nil
}

// put sends body to the upload's URL with its headers.
func (c *Client) put(ctx context.Context, s slot, body []byte) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, s.URL, bytes.NewReader(body))
	if err != nil {
		return &Error{Step: "put", Detail: "the upload's URL is not one"}
	}
	for name, value := range s.Headers {
		req.Header.Set(name, value)
	}
	res, err := c.Store.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		// Not err itself: a url.Error quotes the URL, signature and all.
		return &Error{Step: "put", Detail: "the store did not answer"}
	}
	defer res.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, answerMax))
	if res.StatusCode/100 != 2 {
		return &Error{Step: "put", Status: res.StatusCode}
	}
	return nil
}

// call POSTs body as JSON (nil for none) to path and reads the answer into out.
func (c *Client) call(ctx context.Context, step, path string, body any, out any) error {
	token, err := c.Token()
	if err != nil {
		return fmt.Errorf("blob %s: %w", step, err)
	}
	reader := io.Reader(http.NoBody)
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := c.HTTP.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("blob %s: %w", step, err)
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, answerMax))
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("blob %s: %w", step, err)
	}
	if res.StatusCode != http.StatusOK {
		var problem struct {
			Reason string `json:"reason"`
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(raw, &problem)
		return &Error{Step: step, Status: res.StatusCode, Reason: problem.Reason, Detail: problem.Detail}
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return &Error{Step: step, Status: res.StatusCode, Detail: "an answer that is not JSON"}
	}
	return nil
}
