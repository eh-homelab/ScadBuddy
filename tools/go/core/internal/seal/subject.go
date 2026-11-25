package seal

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"regexp"
	"slices"
)

// The subject payload codec of backend/scadbuddy/workflows/payload_codec.py: a durable
// subject's payloads are sealed under its own data key, a row of ai_payload_keys sealed under
// the KEK. agent/test/fixtures/payload-vectors.json pins it to the agent's.

const (
	// SubjectEncoding is a sealed payload's encoding; SubjectMetadata names its subject.
	SubjectEncoding = "binary/scadbuddy-subject"
	SubjectMetadata = "scadbuddy-subject"
	uuidRE          = `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`
)

var (
	subjectPattern = regexp.MustCompile(`^(session|flow)-` + uuidRE + `$`)
	childPattern   = regexp.MustCompile(`^((?:session|flow)-` + uuidRE + `)-[A-Za-z0-9_-]+$`)
	taskPattern    = regexp.MustCompile(`^bgtask-(` + uuidRE + `)-[0-9a-f]{8}$`)
)

// SubjectOf is the subject whose key seals a workflow's payloads, "" for none: a session or
// flow, its own child workflow, or a session's background task.
func SubjectOf(workflowID string) string {
	if subjectPattern.MatchString(workflowID) {
		return workflowID
	}
	if m := childPattern.FindStringSubmatch(workflowID); m != nil {
		return m[1]
	}
	if m := taskPattern.FindStringSubmatch(workflowID); m != nil {
		return "session-" + m[1]
	}
	return ""
}

// PayloadKeyContext is the context a subject's data key is sealed in under the KEK.
func PayloadKeyContext(subject string) string { return "dek:ai_payload_keys:" + subject }

// PayloadKeyLock is the advisory lock key a key's creation and forgetSubject take.
func PayloadKeyLock(subject string) string { return "scadbuddy:ai_payload_keys:" + subject }

// PayloadContext is the context a subject's payloads are sealed in.
func PayloadContext(subject string) string { return "payload:" + subject }

// Payload is a Temporal payload.
type Payload struct {
	Metadata map[string][]byte
	Data     []byte
}

// payloadBytes is the payload as the bytes that are sealed: sorted keys, as the agent's.
func payloadBytes(p Payload) []byte {
	b64 := base64.StdEncoding.EncodeToString
	keys := make([]string, 0, len(p.Metadata))
	for k := range p.Metadata {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	out := []byte(`{"d":` + quote(b64(p.Data)) + `,"m":{`)
	for i, k := range keys {
		if i > 0 {
			out = append(out, ',')
		}
		out = append(out, quote(k)+":"+quote(b64(p.Metadata[k]))...)
	}
	return append(out, "}}"...)
}

// quote is a string as Python's json.dumps writes it, ensure_ascii on.
func quote(s string) string {
	out := []byte{'"'}
	for _, u := range utf16Units(s) {
		switch {
		case u == '"':
			out = append(out, `\"`...)
		case u == '\\':
			out = append(out, `\\`...)
		case u == '\n':
			out = append(out, `\n`...)
		case u == '\r':
			out = append(out, `\r`...)
		case u == '\t':
			out = append(out, `\t`...)
		case u == '\b':
			out = append(out, `\b`...)
		case u == '\f':
			out = append(out, `\f`...)
		case u < 0x20 || u > 0x7e:
			out = append(out, []byte(`\u`+hex4(u))...)
		default:
			out = append(out, byte(u))
		}
	}
	return string(append(out, '"'))
}

func payloadFrom(data []byte) (Payload, error) {
	var parsed struct {
		D string            `json:"d"`
		M map[string]string `json:"m"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return Payload{}, err
	}
	p := Payload{Metadata: map[string][]byte{}}
	var err error
	if p.Data, err = base64.StdEncoding.DecodeString(parsed.D); err != nil {
		return Payload{}, err
	}
	for k, v := range parsed.M {
		if p.Metadata[k], err = base64.StdEncoding.DecodeString(v); err != nil {
			return Payload{}, err
		}
	}
	return p, nil
}

// DedupIV is the IV a large payload is sealed with: HMAC-SHA256 of its bytes under a key
// derived from the subject's, cut to 12 bytes. One IV is only ever used for one plaintext.
func DedupIV(dek []byte, subject string, plain []byte) []byte {
	ivKey := hmac.New(sha256.New, dek)
	ivKey.Write([]byte("payload-iv:" + subject))
	mac := hmac.New(sha256.New, ivKey.Sum(nil))
	mac.Write(plain)
	return mac.Sum(nil)[:ivBytes]
}

// SealPayload seals p under the subject's data key; iv is nil but for the vectors.
func SealPayload(dek []byte, subject string, p Payload, iv []byte) Payload {
	plain := payloadBytes(p)
	var data []byte
	if iv == nil {
		data = SealBytes(dek, plain, PayloadContext(subject))
	} else {
		data = SealBytesIV(dek, plain, PayloadContext(subject), iv)
	}
	return Payload{
		Metadata: map[string][]byte{
			"encoding": []byte(SubjectEncoding), SubjectMetadata: []byte(subject),
		},
		Data: data,
	}
}

// SealedSubject is the subject a sealed payload belongs to; "" for any other payload.
func SealedSubject(p Payload) string {
	if string(p.Metadata["encoding"]) != SubjectEncoding {
		return ""
	}
	return string(p.Metadata[SubjectMetadata])
}

// OpenPayload opens what SealPayload sealed.
func OpenPayload(dek []byte, subject string, p Payload) (Payload, error) {
	plain, err := OpenBytes(dek, p.Data, PayloadContext(subject))
	if err != nil {
		return Payload{}, err
	}
	return payloadFrom(plain)
}
