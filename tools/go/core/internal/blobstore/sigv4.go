package blobstore

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

// S3 is where the S3 store's objects are, as backend/scadbuddy/library/settings_store.py
// S3Settings names it.
type S3 struct {
	Endpoint        string
	Region          string
	Bucket          string
	Prefix          string
	AccessKeyID     string
	SecretAccessKey string
}

func (s S3) String() string { return "S3(" + s.Endpoint + "/" + s.Bucket + ")" }

// objectURL is the object's URL, the bucket in the path. Python's store asks botocore for
// path addressing when store_s3_path_style is true and for "auto" when it is false
// (store/s3.py), and "auto" with an endpoint_url (always set here) is path addressing too,
// whatever the bucket: botocore 1.43.117, the backend's lock, presigned and sent HEAD to
// path-style URLs for the buckets "bucket" and "my.bucket" at a DO Spaces, an AWS and a
// MinIO endpoint (measured 2026-11-26 in the backend's venv). So both values are path style,
// and store_s3_path_style is not read.
func (s S3) objectURL(key string) (*url.URL, error) {
	u, err := url.Parse(strings.TrimRight(s.Endpoint, "/"))
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return nil, fmt.Errorf("store_s3_endpoint is not an http(s) URL")
	}
	u.Path = u.Path + "/" + s.Bucket + "/" + key
	return u, nil
}

// presign is the Signature V4 query-string presign of method on key, signed with headers
// (each lower-case name with its value; host is added), as botocore's
// generate_presigned_url signs it: the payload unsigned, the expiry in the query
// (https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html). The URL
// carries the signature: never log it.
func (s S3) presign(method, key string, headers map[string]string, expires time.Duration, now time.Time) (string, error) {
	u, err := s.objectURL(key)
	if err != nil {
		return "", err
	}
	return s.presignURL(method, u, headers, expires, now), nil
}

// presignURL is presign of the object at u.
func (s S3) presignURL(method string, u *url.URL, headers map[string]string, expires time.Duration, now time.Time) string {
	now = now.UTC()
	date := now.Format("20060102")
	stamp := now.Format("20060102T150405Z")
	scope := date + "/" + s.Region + "/s3/aws4_request"

	signed := map[string]string{"host": u.Host}
	for name, value := range headers {
		signed[strings.ToLower(name)] = strings.TrimSpace(value)
	}
	names := make([]string, 0, len(signed))
	for name := range signed {
		names = append(names, name)
	}
	sort.Strings(names)
	var canonicalHeaders strings.Builder
	for _, name := range names {
		canonicalHeaders.WriteString(name + ":" + signed[name] + "\n")
	}
	signedHeaders := strings.Join(names, ";")

	query := map[string]string{
		"X-Amz-Algorithm":     "AWS4-HMAC-SHA256",
		"X-Amz-Credential":    s.AccessKeyID + "/" + scope,
		"X-Amz-Date":          stamp,
		"X-Amz-Expires":       strconv.Itoa(int(expires / time.Second)),
		"X-Amz-SignedHeaders": signedHeaders,
	}
	canonicalQuery := encodeQuery(query)
	canonicalURI := encodePath(u.Path)
	request := strings.Join([]string{
		method, canonicalURI, canonicalQuery, canonicalHeaders.String(), signedHeaders, "UNSIGNED-PAYLOAD",
	}, "\n")
	digest := sha256.Sum256([]byte(request))
	toSign := "AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" + hex.EncodeToString(digest[:])
	signingKey := mac([]byte("AWS4"+s.SecretAccessKey), date)
	signingKey = mac(signingKey, s.Region)
	signingKey = mac(signingKey, "s3")
	signingKey = mac(signingKey, "aws4_request")
	signature := hex.EncodeToString(mac(signingKey, toSign))
	u.RawPath = canonicalURI
	u.RawQuery = canonicalQuery + "&X-Amz-Signature=" + signature
	return u.String()
}

func mac(key []byte, data string) []byte {
	h := hmac.New(sha256.New, key)
	h.Write([]byte(data))
	return h.Sum(nil)
}

// encodeQuery is the query sorted by name, each name and value URI-encoded.
func encodeQuery(query map[string]string) string {
	names := make([]string, 0, len(query))
	for name := range query {
		names = append(names, name)
	}
	sort.Strings(names)
	parts := make([]string, len(names))
	for i, name := range names {
		parts[i] = uriEncode(name, true) + "=" + uriEncode(query[name], true)
	}
	return strings.Join(parts, "&")
}

// encodePath is a path URI-encoded once, its slashes kept: S3 encodes a key once.
func encodePath(path string) string { return uriEncode(path, false) }

// uriEncode is SigV4's UriEncode: every byte but A-Z, a-z, 0-9, '-', '.', '_' and '~' as
// %XX, and '/' too unless it is a path.
func uriEncode(s string, slash bool) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case 'A' <= c && c <= 'Z', 'a' <= c && c <= 'z', '0' <= c && c <= '9',
			c == '-', c == '.', c == '_', c == '~':
			b.WriteByte(c)
		case c == '/' && !slash:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}
