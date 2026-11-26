package blobstore

import (
	"crypto/hkdf"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"strconv"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// An upload's id is a ticket, so no row stands for an upload not yet committed. Its format:
// urlsafe_b64(body) "." urlsafe_b64(HMAC-SHA256(body)), body being
// "v1\n<caller>\n<kind>\n<sha256>\n<size>\n<media type>\n<expiry, Unix seconds>", MACed
// under HKDF-SHA256 of the KEK with no salt and ticketInfo. It names the caller that asked:
// another caller's commit of it is a 404.
const (
	ticketInfo    = "scadbuddy:blobs:upload:v1"
	ticketVersion = "v1"
)

// ticket is one upload, as begin asked for it.
type ticket struct {
	Caller    string
	Kind      string
	SHA256    string
	Size      int64
	MediaType string
	// Expires is when the upload's URL stops being taken, whole seconds.
	Expires time.Time
}

var errTicket = errors.New("not an upload's id")

func ticketKey(kek *seal.KEK) []byte {
	key, err := hkdf.Key(sha256.New, kek.Key(), nil, ticketInfo, 32)
	if err != nil {
		panic(err)
	}
	return key
}

func ticketMAC(key []byte, body string) []byte {
	h := hmac.New(sha256.New, key)
	h.Write([]byte(body))
	return h.Sum(nil)
}

func (t ticket) body() string {
	return strings.Join([]string{
		ticketVersion, t.Caller, t.Kind, t.SHA256, strconv.FormatInt(t.Size, 10), t.MediaType,
		strconv.FormatInt(t.Expires.Unix(), 10),
	}, "\n")
}

// sign is the ticket's id under kek. No field holds a newline: begin checks each.
func (t ticket) sign(kek *seal.KEK) string {
	body := t.body()
	enc := base64.URLEncoding
	return enc.EncodeToString([]byte(body)) + "." + enc.EncodeToString(ticketMAC(ticketKey(kek), body))
}

// openTicket is the ticket id holds, MACed under the current KEK or, during a rotation, the
// one before it. Its expiry is not checked here.
func openTicket(keys *seal.Keys, id string) (ticket, error) {
	encodedBody, encodedMAC, ok := strings.Cut(id, ".")
	if !ok {
		return ticket{}, errTicket
	}
	enc := base64.URLEncoding
	body, err1 := enc.DecodeString(encodedBody)
	sum, err2 := enc.DecodeString(encodedMAC)
	// One spelling per ticket: a decoder that also took another would let two ids name it.
	if err1 != nil || err2 != nil || enc.EncodeToString(body) != encodedBody ||
		enc.EncodeToString(sum) != encodedMAC {
		return ticket{}, errTicket
	}
	authentic := false
	for _, kek := range []*seal.KEK{keys.Current, keys.Previous} {
		if kek != nil && hmac.Equal(sum, ticketMAC(ticketKey(kek), string(body))) {
			authentic = true
		}
	}
	if !authentic {
		return ticket{}, errTicket
	}
	fields := strings.Split(string(body), "\n")
	if len(fields) != 7 || fields[0] != ticketVersion {
		return ticket{}, errTicket
	}
	size, err1 := strconv.ParseInt(fields[4], 10, 64)
	expiry, err2 := strconv.ParseInt(fields[6], 10, 64)
	if err1 != nil || err2 != nil {
		return ticket{}, errTicket
	}
	return ticket{
		Caller: fields[1], Kind: fields[2], SHA256: fields[3], Size: size, MediaType: fields[5],
		Expires: time.Unix(expiry, 0).UTC(),
	}, nil
}
