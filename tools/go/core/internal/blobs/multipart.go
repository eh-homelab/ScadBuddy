package blobs

import (
	"errors"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	// metaBytesMax is the largest meta part; framingBytesMax what multipart framing may add.
	metaBytesMax    = 48 * 1024
	framingBytesMax = 16 * 1024
	// MultipartBodyMax is the API's body gate for multipart (api/limits.py).
	MultipartBodyMax = 32 * 1024 * 1024
	// TextBodyMax is the API's body gate for JSON.
	TextBodyMax = 8 * 1024 * 1024
)

func unprocessable(detail string) error {
	return &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: detail}
}

func tooLarge(detail string) error {
	return &httpx.Problem{Status: http.StatusRequestEntityTooLarge, Detail: detail}
}

type part struct {
	name string
	file bool
	data []byte
}

// readMultipart is crud_agent_blobs.py (removed in #7667) `_multipart`: the meta part and up to parts byte
// parts, their sum at most bytesMax.
func readMultipart(r *http.Request, parts int, bytesMax int) (string, map[string][]byte, error) {
	_, params, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0]) != "multipart/form-data" {
		return "", nil, unprocessable("a write comes as multipart/form-data")
	}
	if r.ContentLength < 0 {
		return "", nil, &httpx.Problem{Status: http.StatusLengthRequired, Detail: "a write names its Content-Length"}
	}
	if r.ContentLength > int64(bytesMax+metaBytesMax+framingBytesMax) {
		return "", nil, tooLarge(fmt.Sprintf("a write is at most %d bytes", bytesMax))
	}
	all, err := parse(r.Body, params["boundary"], parts)
	if err != nil {
		var max *http.MaxBytesError
		if errors.As(err, &max) {
			return "", nil, err
		}
		return "", nil, unprocessable("the write's body was refused: " + err.Error())
	}
	meta, found := "", false
	for _, p := range all {
		if p.name == "meta" && !p.file {
			meta, found = string(p.data), true
			break
		}
	}
	if !found {
		return "", nil, unprocessable("a write has the part meta (JSON)")
	}
	files := map[string][]byte{}
	total := 0
	for _, p := range all {
		if p.name == "meta" {
			continue
		}
		if _, twice := files[p.name]; !p.file || twice {
			name := []rune(p.name)
			if len(name) > 40 {
				name = name[:40]
			}
			return "", nil, unprocessable(fmt.Sprintf("the part %s is not one file", string(name)))
		}
		total += len(p.data)
		if total > bytesMax {
			return "", nil, tooLarge(fmt.Sprintf("a write is at most %d bytes", bytesMax))
		}
		files[p.name] = p.data
	}
	return meta, files, nil
}

// parse reads every part, as Starlette's form(max_files=files, max_fields=1,
// max_part_size=metaBytesMax) does.
func parse(body io.Reader, boundary string, files int) ([]part, error) {
	if boundary == "" {
		return nil, errors.New("Missing boundary in multipart.")
	}
	reader := multipart.NewReader(body, boundary)
	var out []part
	fields, nfiles := 0, 0
	for {
		p, err := reader.NextPart()
		if errors.Is(err, io.EOF) {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		_, params, _ := mime.ParseMediaType(p.Header.Get("Content-Disposition"))
		_, file := params["filename"]
		item := part{name: params["name"], file: file}
		if file {
			if nfiles++; nfiles > files {
				return nil, fmt.Errorf("Too many files. Maximum number of files is %d.", files)
			}
			item.data, err = io.ReadAll(p)
		} else {
			if fields++; fields > 1 {
				return nil, errors.New("Too many fields. Maximum number of fields is 1.")
			}
			item.data, err = io.ReadAll(io.LimitReader(p, metaBytesMax+1))
			if err == nil && len(item.data) > metaBytesMax {
				return nil, fmt.Errorf("Part exceeded maximum size of %dKB.", metaBytesMax/1024)
			}
		}
		if err != nil {
			return nil, err
		}
		out = append(out, item)
	}
}
