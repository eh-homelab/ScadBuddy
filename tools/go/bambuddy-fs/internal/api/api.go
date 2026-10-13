// Package api is the mount's view of ScadBuddy's library routes: the Kiota-generated
// client underneath, with 202 operations followed to their result, problems turned
// into *Error, and file contents streamed.
package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/scadbuddy-client/client"
	"github.com/eh-homelab/ScadBuddy/tools/go/scadbuddy-client/client/api"
	"github.com/eh-homelab/ScadBuddy/tools/go/scadbuddy-client/client/models"
	abs "github.com/microsoft/kiota-abstractions-go"
	"github.com/microsoft/kiota-abstractions-go/authentication"
	"github.com/microsoft/kiota-abstractions-go/serialization"
	kiotahttp "github.com/microsoft/kiota-http-go"
	jsonserialization "github.com/microsoft/kiota-serialization-json-go"
	"go.opentelemetry.io/otel/attribute"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/telemetry"
)

// Folder is one entry of the library's folder list, which is flat: ParentID 0 is
// the top level.
type Folder struct {
	ID       int
	Name     string
	ParentID int
}

// File is a library file. FolderID 0 is the top level.
type File struct {
	ID       int
	Name     string
	FolderID int
	Size     int64
	Created  time.Time
	External bool
}

// Listing is one folder's files and every folder in the library.
type Listing struct {
	Folders []Folder
	Files   []File
}

// Client calls ScadBuddy's /api/v1. JSON routes are built with the generated request
// builders and sent on the http.Client; file contents go over net/http directly.
type Client struct {
	base    string
	adapter *kiotahttp.NetHttpRequestAdapter
	kiota   *client.ApiClient
	http    *http.Client
}

const (
	firstPoll    = 250 * time.Millisecond
	lastPoll     = 2 * time.Second
	maxErrorBody = 64 << 10
)

// New takes ScadBuddy's API root, like http://host/api/v1. A nil httpClient is
// http.DefaultClient. Requests go through telemetry.Transport, on a copy of the client.
func New(baseURL string, httpClient *http.Client) (*Client, error) {
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	traced := *httpClient
	traced.Transport = telemetry.Transport(httpClient.Transport)
	httpClient = &traced
	base := strings.TrimRight(baseURL, "/")
	// The generated URL templates already start with /api/v1.
	origin, ok := strings.CutSuffix(base, "/api/v1")
	if !ok {
		return nil, fmt.Errorf("API URL %q does not end in /api/v1", baseURL)
	}
	adapter, err := kiotahttp.NewNetHttpRequestAdapterWithParseNodeFactoryAndSerializationWriterFactoryAndHttpClient(
		&authentication.AnonymousAuthenticationProvider{}, nil, nil, httpClient)
	if err != nil {
		return nil, err
	}
	adapter.SetBaseUrl(origin)
	return &Client{base: base, adapter: adapter, kiota: client.NewApiClient(adapter), http: httpClient}, nil
}

func (c *Client) library() *api.V1PrintLibraryRequestBuilder {
	return c.kiota.Api().V1().Print().Library()
}

// List returns a folder's files (folderID 0 is the top level) and the whole folder
// list. all lists every file type, not only printable ones.
func (c *Client) List(ctx context.Context, folderID int, all bool) (Listing, error) {
	query := api.V1PrintLibraryRequestBuilderGetQueryParameters{}
	if folderID != 0 {
		id := int32(folderID)
		query.Folder_id = &id
	}
	if all {
		query.All = &all
	}
	ctx = telemetry.WithCall(ctx, telemetry.Call{Op: "list_library", Route: "/api/v1/print/library", Attrs: folderAttr(folderID)})
	info, err := c.library().ToGetRequestInformation(ctx, &api.V1PrintLibraryRequestBuilderGetRequestConfiguration{QueryParameters: &query})
	if err != nil {
		return Listing{}, err
	}
	listing, err := read[models.LibraryListingable](ctx, c, "list", info, models.CreateLibraryListingFromDiscriminatorValue)
	if err != nil {
		return Listing{}, err
	}
	var out Listing
	for _, f := range listing.GetFolders() {
		out.Folders = append(out.Folders, Folder{ID: deref(f.GetId()), Name: derefString(f.GetName()), ParentID: deref(unionInt(f.GetParentId()))})
	}
	for _, f := range listing.GetFiles() {
		file := File{
			ID:       deref(f.GetId()),
			Name:     derefString(f.GetFilename()),
			FolderID: deref(unionInt(f.GetFolderId())),
			Size:     int64(deref(unionInt(f.GetFileSize()))),
			External: f.GetIsExternal() != nil && *f.GetIsExternal(),
		}
		if created := f.GetCreatedAt(); created != nil && created.GetDateTimeOffset() != nil {
			file.Created = *created.GetDateTimeOffset()
		}
		out.Files = append(out.Files, file)
	}
	return out, nil
}

// Download streams a file's bytes into w.
func (c *Client) Download(ctx context.Context, fileID int, w io.Writer) error {
	ctx = telemetry.WithCall(ctx, telemetry.Call{Op: "download", Route: "/api/v1/print/library/{file_id}/file", Transfer: telemetry.Download, Attrs: fileAttr(fileID)})
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("%s/print/library/%d/file", c.base, fileID), nil)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("download: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBody))
		return errorFrom("download", resp.StatusCode, body)
	}
	if _, err := io.Copy(w, resp.Body); err != nil {
		return fmt.Errorf("download: %w", err)
	}
	return nil
}

// Upload streams r into a new file called name in folderID (0 is the top level). The
// File it returns has the size of the bytes sent and Created set to now, since the
// route's answer carries neither.
func (c *Client) Upload(ctx context.Context, folderID int, name string, r io.Reader) (File, error) {
	pr, pw := io.Pipe()
	// Closing the reader on return fails the writer's next write, so it stops.
	defer pr.Close()
	form := multipart.NewWriter(pw)
	ctx = telemetry.WithCall(ctx, telemetry.Call{Op: "upload", Route: "/api/v1/print/library/files", Transfer: telemetry.Upload, Attrs: folderAttr(folderID)})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/print/library/files", pr)
	if err != nil {
		return File{}, err
	}
	req.Header.Set("Content-Type", form.FormDataContentType())
	req.Header.Set("Idempotency-Key", newKey())
	sent := make(chan int64, 1)
	go func() {
		n, err := writeForm(form, folderID, name, r)
		sent <- n
		pw.CloseWithError(err)
	}()
	resp, err := c.http.Do(req)
	if err != nil {
		return File{}, fmt.Errorf("upload: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return File{}, fmt.Errorf("upload: %w", err)
	}
	entry, err := answer[models.LibraryEntryUpdatedable](ctx, c, "upload", resp.StatusCode, body, models.CreateLibraryEntryUpdatedFromDiscriminatorValue)
	if err != nil {
		return File{}, err
	}
	// A success means the route read the whole form, so the writer has finished.
	file := updatedFile(entry)
	file.Size = <-sent
	file.Created = time.Now().UTC()
	return file, nil
}

// writeForm writes the upload's form and returns the number of file bytes in it.
func writeForm(form *multipart.Writer, folderID int, name string, r io.Reader) (int64, error) {
	part, err := form.CreateFormFile("file", name)
	if err != nil {
		return 0, err
	}
	n, err := io.Copy(part, r)
	if err != nil {
		return n, err
	}
	if folderID != 0 {
		if err := form.WriteField("folder_id", strconv.Itoa(folderID)); err != nil {
			return n, err
		}
	}
	return n, form.Close()
}

// UpdateFile renames and/or moves a file; a nil field is left as it is, and a
// folderID of 0 moves it to the top level. The route answers only the ID, name and
// folder, so the File's Size, Created and External are unknown and left zero: keep
// the ones the file had.
func (c *Client) UpdateFile(ctx context.Context, fileID int, name *string, folderID *int) (File, error) {
	body := models.NewLibraryFileUpdate()
	if name != nil {
		v := models.NewLibraryFileUpdate_LibraryFileUpdate_filename()
		v.SetString(name)
		body.SetFilename(v)
	}
	if folderID != nil {
		v := models.NewLibraryFileUpdate_LibraryFileUpdate_folder_id()
		v.SetInteger(int32Ptr(*folderID))
		body.SetFolderId(v)
	}
	ctx = telemetry.WithCall(ctx, telemetry.Call{Op: "update_file", Route: "/api/v1/print/library/{file_id}", Attrs: fileAttr(fileID)})
	info, err := c.library().ByFile_idInteger(int32(fileID)).ToPatchRequestInformation(ctx, body, nil)
	if err != nil {
		return File{}, err
	}
	entry, err := write[models.LibraryEntryUpdatedable](ctx, c, "update file", info, models.CreateLibraryEntryUpdatedFromDiscriminatorValue)
	if err != nil {
		return File{}, err
	}
	return updatedFile(entry), nil
}

// DeleteFiles deletes files: managed ones to Bambuddy's trash, external ones for
// good. A file the route skipped is a 409, so the caller never reports success for it.
func (c *Client) DeleteFiles(ctx context.Context, ids ...int) error {
	body := models.NewLibraryFilesRequest()
	fileIDs := make([]int32, len(ids))
	for i, id := range ids {
		fileIDs[i] = int32(id)
	}
	body.SetFileIds(fileIDs)
	var attrs []attribute.KeyValue
	if len(ids) == 1 {
		attrs = fileAttr(ids[0])
	}
	ctx = telemetry.WithCall(ctx, telemetry.Call{Op: "delete_files", Route: "/api/v1/print/library/delete", Attrs: attrs})
	info, err := c.library().DeletePath().ToPostRequestInformation(ctx, body, nil)
	if err != nil {
		return err
	}
	result, err := write[models.LibraryDeleteResultable](ctx, c, "delete", info, models.CreateLibraryDeleteResultFromDiscriminatorValue)
	if err != nil {
		return err
	}
	return skippedError("delete", result.GetSkipped())
}

// Mkdir creates a folder under parentID (0 is the top level).
func (c *Client) Mkdir(ctx context.Context, parentID int, name string) (Folder, error) {
	body := models.NewLibraryFolderCreate()
	body.SetName(&name)
	if parentID != 0 {
		v := models.NewLibraryFolderCreate_LibraryFolderCreate_parent_id()
		v.SetInteger(int32Ptr(parentID))
		body.SetParentId(v)
	}
	ctx = telemetry.WithCall(ctx, telemetry.Call{Op: "create_folder", Route: "/api/v1/print/library/folders", Attrs: folderAttr(parentID)})
	info, err := c.library().Folders().ToPostRequestInformation(ctx, body, nil)
	if err != nil {
		return Folder{}, err
	}
	folder, err := write[models.LibraryFolderable](ctx, c, "mkdir", info, models.CreateLibraryFolderFromDiscriminatorValue)
	if err != nil {
		return Folder{}, err
	}
	return libraryFolder(folder), nil
}

// UpdateFolder renames and/or moves a folder; a nil field is left as it is, and a
// parentID of 0 moves it to the top level.
func (c *Client) UpdateFolder(ctx context.Context, folderID int, name *string, parentID *int) (Folder, error) {
	body := models.NewLibraryFolderUpdate()
	if name != nil {
		v := models.NewLibraryFolderUpdate_LibraryFolderUpdate_name()
		v.SetString(name)
		body.SetName(v)
	}
	if parentID != nil {
		v := models.NewLibraryFolderUpdate_LibraryFolderUpdate_parent_id()
		v.SetInteger(int32Ptr(*parentID))
		body.SetParentId(v)
	}
	ctx = telemetry.WithCall(ctx, telemetry.Call{Op: "update_folder", Route: "/api/v1/print/library/folders/{folder_id}", Attrs: folderAttr(folderID)})
	info, err := c.library().Folders().ByFolder_idInteger(int32(folderID)).ToPatchRequestInformation(ctx, body, nil)
	if err != nil {
		return Folder{}, err
	}
	folder, err := write[models.LibraryFolderable](ctx, c, "update folder", info, models.CreateLibraryFolderFromDiscriminatorValue)
	if err != nil {
		return Folder{}, err
	}
	return libraryFolder(folder), nil
}

// DeleteFolder deletes a folder with everything in it (spec §5). Anything the route
// kept is a 409.
func (c *Client) DeleteFolder(ctx context.Context, folderID int) error {
	ctx = telemetry.WithCall(ctx, telemetry.Call{Op: "delete_folder", Route: "/api/v1/print/library/folders/{folder_id}/delete", Attrs: folderAttr(folderID)})
	info, err := c.library().Folders().ByFolder_idInteger(int32(folderID)).DeletePath().ToPostRequestInformation(ctx, nil)
	if err != nil {
		return err
	}
	result, err := write[models.LibraryFolderDeleteResultable](ctx, c, "rmdir", info, models.CreateLibraryFolderDeleteResultFromDiscriminatorValue)
	if err != nil {
		return err
	}
	if err := skippedError("rmdir", result.GetSkipped()); err != nil {
		return err
	}
	// The route can keep a folder without naming anything in `skipped`.
	if !slices.Contains(result.GetFolders(), int32(folderID)) {
		return &Error{Op: "rmdir", Status: http.StatusConflict, Detail: "the folder was kept"}
	}
	return nil
}

func skippedError(op string, skipped []models.SkippedLibraryFileable) error {
	if len(skipped) == 0 {
		return nil
	}
	reasons := make([]string, len(skipped))
	for i, s := range skipped {
		name := strconv.Itoa(deref(s.GetId()))
		if f := s.GetFilename(); f != nil && f.GetString() != nil {
			name = *f.GetString()
		}
		reasons[i] = name + ": " + derefString(s.GetReason())
	}
	return &Error{Op: op, Status: http.StatusConflict, Detail: strings.Join(reasons, "; ")}
}

// read sends a GET built by the generated client and decodes its 200 body.
func read[T any](ctx context.Context, c *Client, op string, info *abs.RequestInformation, factory serialization.ParsableFactory) (T, error) {
	var zero T
	status, body, err := c.send(ctx, op, info)
	if err != nil {
		return zero, err
	}
	if status != http.StatusOK {
		return zero, errorFrom(op, status, body)
	}
	return decode[T](op, body, factory)
}

// write sends a write built by the generated client with a fresh Idempotency-Key, and
// follows a 202 to its result.
func write[T any](ctx context.Context, c *Client, op string, info *abs.RequestInformation, factory serialization.ParsableFactory) (T, error) {
	var zero T
	info.Headers.Add("Idempotency-Key", newKey())
	status, body, err := c.send(ctx, op, info)
	if err != nil {
		return zero, err
	}
	return answer[T](ctx, c, op, status, body, factory)
}

// answer decodes a write's 200 body, or follows its 202 operation to the result.
func answer[T any](ctx context.Context, c *Client, op string, status int, body []byte, factory serialization.ParsableFactory) (T, error) {
	var zero T
	switch status {
	case http.StatusOK:
		return decode[T](op, body, factory)
	case http.StatusAccepted:
		operation, err := decode[models.Operationable](op, body, models.CreateOperationFromDiscriminatorValue)
		if err != nil {
			return zero, err
		}
		result, err := c.follow(ctx, op, derefString(operation.GetId()))
		if err != nil {
			return zero, err
		}
		return decode[T](op, result, factory)
	default:
		return zero, errorFrom(op, status, body)
	}
}

// follow polls GET /operations/{id} until the operation finishes, backing off from
// 250 ms to 2 s, and returns its result's JSON. The caller's context bounds it. A
// network error or a 5xx is retried, since the operation is still running; a 404 is
// an EIO, since it is the operation that is gone, not the file.
func (c *Client) follow(ctx context.Context, op, id string) ([]byte, error) {
	delay := firstPoll
	poll := telemetry.WithCall(ctx, telemetry.Call{Op: "poll_operation", Route: "/api/v1/operations/{operation_id}",
		Attrs: []attribute.KeyValue{telemetry.OperationID(id)}})
	for {
		info, err := c.kiota.Api().V1().Operations().ByOperation_id(id).ToGetRequestInformation(ctx, nil)
		if err != nil {
			return nil, err
		}
		status, body, err := c.send(poll, op, info)
		switch {
		case err != nil && ctx.Err() != nil:
			return nil, fmt.Errorf("%s: operation %s: %w", op, id, err)
		case err != nil || status >= http.StatusInternalServerError:
			if err := wait(ctx, op, id, delay); err != nil {
				return nil, err
			}
			delay = min(delay*2, lastPoll)
			continue
		case status == http.StatusNotFound:
			return nil, fmt.Errorf("%s: operation %s is gone: %s", op, id, errorFrom(op, status, body).Detail)
		case status != http.StatusOK:
			return nil, errorFrom(op, status, body)
		}
		operation, err := decode[models.Operationable](op, body, models.CreateOperationFromDiscriminatorValue)
		if err != nil {
			return nil, err
		}
		if s := operation.GetStatus(); s != nil && *s != models.RUNNING_OPERATION_STATUS {
			// The result is free-form in the schema; take it as JSON and decode it
			// with the route's own model.
			var raw struct {
				Result json.RawMessage `json:"result"`
				Error  json.RawMessage `json:"error"`
			}
			if err := json.Unmarshal(body, &raw); err != nil {
				return nil, fmt.Errorf("%s: operation %s: %w", op, id, err)
			}
			if *s == models.SUCCEEDED_OPERATION_STATUS {
				return raw.Result, nil
			}
			failed := http.StatusInternalServerError
			if e := operation.GetError(); e != nil && e.GetStatus() != nil {
				failed = int(*e.GetStatus())
			}
			return nil, errorFrom(op, failed, raw.Error)
		}
		if err := wait(ctx, op, id, delay); err != nil {
			return nil, err
		}
		delay = min(delay*2, lastPoll)
	}
}

func wait(ctx context.Context, op, id string, delay time.Duration) error {
	select {
	case <-ctx.Done():
		return fmt.Errorf("%s: operation %s: %w", op, id, ctx.Err())
	case <-time.After(delay):
		return nil
	}
}

// send turns the generated request into a net/http one and returns the answer's
// status and body.
func (c *Client) send(ctx context.Context, op string, info *abs.RequestInformation) (int, []byte, error) {
	info.PathParameters["baseurl"] = c.adapter.GetBaseUrl()
	native, err := c.adapter.ConvertToNativeRequest(ctx, info)
	if err != nil {
		return 0, nil, err
	}
	resp, err := c.http.Do(native.(*http.Request))
	if err != nil {
		return 0, nil, fmt.Errorf("%s: %w", op, err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, nil, fmt.Errorf("%s: %w", op, err)
	}
	return resp.StatusCode, body, nil
}

func decode[T any](op string, body []byte, factory serialization.ParsableFactory) (T, error) {
	var zero T
	node, err := jsonserialization.NewJsonParseNode(body)
	if err != nil {
		return zero, fmt.Errorf("%s: %w", op, err)
	}
	value, err := node.GetObjectValue(factory)
	if err != nil {
		return zero, fmt.Errorf("%s: %w", op, err)
	}
	out, ok := value.(T)
	if !ok {
		return zero, fmt.Errorf("%s: unexpected answer %T", op, value)
	}
	return out, nil
}

// newKey is a write's Idempotency-Key: 32 random hex digits.
func newKey() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func fileAttr(id int) []attribute.KeyValue { return []attribute.KeyValue{telemetry.FileID(id)} }

func folderAttr(id int) []attribute.KeyValue { return []attribute.KeyValue{telemetry.FolderID(id)} }

func updatedFile(e models.LibraryEntryUpdatedable) File {
	return File{ID: deref(e.GetId()), Name: derefString(e.GetFilename()), FolderID: deref(unionInt(e.GetFolderId()))}
}

func libraryFolder(f models.LibraryFolderable) Folder {
	return Folder{ID: deref(f.GetId()), Name: derefString(f.GetName()), ParentID: deref(unionInt(f.GetParentId()))}
}

// nullableInt is Kiota's shape for an `int | null` field.
type nullableInt interface{ GetInteger() *int32 }

func unionInt(u nullableInt) *int32 {
	if u == nil {
		return nil
	}
	return u.GetInteger()
}

func deref(v *int32) int {
	if v == nil {
		return 0
	}
	return int(*v)
}

func derefString(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}

func int32Ptr(v int) *int32 {
	i := int32(v)
	return &i
}
