// Package seal is the agent's envelope encryption for secrets at rest (#602):
// backend/scadbuddy/core/secrets.py, statement for statement, which is agent/src/secrets.ts in
// Python. The key file holds 32 random bytes, base64-encoded (openssl rand -base64 32). A
// secret is sealed with AES-256-GCM under a random data key, and the data key under the KEK,
// each as version(1) | IV(12) | tag(16) | ciphertext, with the AAD "v2|<context>" (and the
// context "dek:<context>" for the data key). The KEK's id is the first 16 hex characters of
// SHA-256 over it. v2 is written; v1 (the context alone as AAD) is still opened.
//
// No error, log record or String here carries a plaintext, a data key or the KEK.
package seal

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"syscall"
	"unicode/utf8"
)

const (
	// KEKBytes is the length of a KEK and of a data key.
	KEKBytes = 32
	ivBytes  = 12
	tagBytes = 16
	// V1 is the old format, still opened; Version is the one written.
	V1      = 0x01
	Version = 0x02
	// Variable names the KEK's file; PreviousVariable the one before it, during a rotation.
	Variable         = "SCADBUDDY_SECRET_KEY_FILE"
	PreviousVariable = "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE"
)

var base64Pattern = regexp.MustCompile(`^[A-Za-z0-9+/]+={0,2}$`)

// KeyError is a key file that is unreadable or not a key. It never carries the path or the
// contents.
type KeyError struct{ msg string }

func (e *KeyError) Error() string { return e.msg }

// Error is a failed open: another key, or altered bytes. It never carries plaintext.
type Error struct{ msg string }

func (e *Error) Error() string { return e.msg }

// KEK is a key-encryption key and its id.
type KEK struct {
	ID  string
	key []byte
}

// Key is the KEK's bytes, for a key derived from it.
func (k *KEK) Key() []byte { return k.key }

func (k *KEK) String() string { return "KEK(" + k.ID + ")" }

// Envelope is a sealed secret and its sealed data key.
type Envelope struct {
	SecretSealed []byte
	DEKSealed    []byte
	KEKID        string
}

func (e Envelope) String() string { return "Envelope(" + e.KEKID + ")" }

// FromBase64 is the KEK in text: 32 bytes, base64-encoded.
func FromBase64(text string) (*KEK, error) {
	// JS trim() also strips U+FEFF, so a BOM before the key loads in the agent.
	trimmed := pyStrip(strings.Trim(pyStrip(text), "\ufeff"))
	if !base64Pattern.MatchString(trimmed) || len(trimmed)%4 != 0 {
		return nil, &KeyError{Variable + " is not base64; it must hold 32 random bytes," +
			" base64-encoded (openssl rand -base64 32)"}
	}
	key, err := base64.StdEncoding.Strict().DecodeString(trimmed)
	if err != nil {
		return nil, &KeyError{Variable + " is not base64"}
	}
	if len(key) != KEKBytes {
		return nil, &KeyError{fmt.Sprintf("%s decodes to %d bytes, not %d (openssl rand -base64 32)",
			Variable, len(key), KEKBytes)}
	}
	return NewKEK(key), nil
}

// NewKEK is the KEK of 32 bytes.
func NewKEK(key []byte) *KEK {
	sum := sha256.Sum256(key)
	return &KEK{ID: hex.EncodeToString(sum[:])[:16], key: append([]byte(nil), key...)}
}

// Load reads the KEK in the file at path.
func Load(path string) (*KEK, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		errno := 0
		var e syscall.Errno
		if errors.As(err, &e) {
			errno = int(e)
		}
		return nil, &KeyError{fmt.Sprintf("%s cannot be read (errno %d)", Variable, errno)}
	}
	// As Node's readFile(..., 'utf8'): invalid bytes become U+FFFD, so a raw key file is
	// refused as "not base64" and none of its bytes reach the message.
	return FromBase64(strings.ToValidUTF8(string(raw), string(utf8.RuneError)))
}

// pyStrip is Python's str.strip(): it also takes the separators U+001C to U+001F.
func pyStrip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		switch r {
		case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0, 0x1680,
			0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
			return true
		}
		return r >= 0x2000 && r <= 0x200a
	})
}

func aad(version byte, context string) []byte {
	// v1 did not authenticate its version byte (secrets.ts `aadFor`).
	if version == V1 {
		return []byte(context)
	}
	return fmt.Appendf(nil, "v%d|%s", version, context)
}

func gcm(key []byte) cipher.AEAD {
	block, err := aes.NewCipher(key)
	if err != nil {
		panic(err) // Every key here is 32 bytes.
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		panic(err)
	}
	return aead
}

func sealWith(key, plaintext []byte, context string, random io.Reader) []byte {
	iv := make([]byte, ivBytes)
	if _, err := io.ReadFull(random, iv); err != nil {
		panic(err)
	}
	return sealIV(key, plaintext, context, iv)
}

func sealIV(key, plaintext []byte, context string, iv []byte) []byte {
	sealed := gcm(key).Seal(nil, iv, plaintext, aad(Version, context))
	// Go appends the tag; the format puts it before the ciphertext.
	ciphertext, tag := sealed[:len(sealed)-tagBytes], sealed[len(sealed)-tagBytes:]
	out := make([]byte, 0, 1+ivBytes+len(sealed))
	out = append(out, Version)
	out = append(out, iv...)
	out = append(out, tag...)
	return append(out, ciphertext...)
}

// OpenBytes is secrets.ts openBytes: what SealBytes (or the agent) sealed under key.
func OpenBytes(key, sealed []byte, context string) ([]byte, error) {
	if len(sealed) < 1+ivBytes+tagBytes || (sealed[0] != V1 && sealed[0] != Version) {
		return nil, &Error{"sealed value is malformed or of an unknown version"}
	}
	iv := sealed[1 : 1+ivBytes]
	tag := sealed[1+ivBytes : 1+ivBytes+tagBytes]
	ciphertext := sealed[1+ivBytes+tagBytes:]
	joined := append(append([]byte(nil), ciphertext...), tag...)
	plain, err := gcm(key).Open(nil, iv, joined, aad(sealed[0], context))
	if err != nil {
		return nil, &Error{"sealed value failed authentication (wrong key, or altered)"}
	}
	return plain, nil
}

// SealBytes is secrets.ts sealBytes, the current version: the payload keys and payloads.
func SealBytes(key, data []byte, context string) []byte {
	return sealWith(key, data, context, rand.Reader)
}

// SealBytesIV seals with a fixed IV, for the vectors only: a repeated IV under one key
// breaks GCM.
func SealBytesIV(key, data []byte, context string, iv []byte) []byte {
	return sealIV(key, data, context, iv)
}

// SealSecret seals plaintext under a new data key, and the data key under kek.
func SealSecret(kek *KEK, plaintext, context string) Envelope {
	return sealSecretWith(kek, plaintext, context, rand.Reader)
}

func sealSecretWith(kek *KEK, plaintext, context string, random io.Reader) Envelope {
	dek := make([]byte, KEKBytes)
	if _, err := io.ReadFull(random, dek); err != nil {
		panic(err)
	}
	return Envelope{
		SecretSealed: sealWith(dek, []byte(plaintext), context, random),
		DEKSealed:    sealWith(kek.key, dek, "dek:"+context, random),
		KEKID:        kek.ID,
	}
}

// OpenSecret opens what SealSecret sealed.
func OpenSecret(kek *KEK, e Envelope, context string) (string, error) {
	if e.KEKID != kek.ID {
		return "", &Error{fmt.Sprintf("secret was sealed with key %s, but %s holds key %s",
			e.KEKID, Variable, kek.ID)}
	}
	dek, err := OpenBytes(kek.key, e.DEKSealed, "dek:"+context)
	if err != nil {
		return "", err
	}
	plain, err := OpenBytes(dek, e.SecretSealed, context)
	if err != nil {
		return "", err
	}
	// Python's bytes.decode() refuses bytes that are no UTF-8.
	if !utf8.Valid(plain) {
		return "", &Error{"sealed value is not text"}
	}
	return string(plain), nil
}

// Keys are the KEK this process holds and, during a rotation, the one before it.
type Keys struct {
	Current  *KEK
	Previous *KEK
}

// LoadKeys reads SCADBUDDY_SECRET_KEY_FILE and SCADBUDDY_SECRET_KEY_PREVIOUS_FILE: nil when the
// first is not set. A file that is set and cannot be read stops the start.
func LoadKeys(env func(string) string) (*Keys, error) {
	path := env(Variable)
	if path == "" {
		return nil, nil
	}
	current, err := Load(path)
	if err != nil {
		return nil, err
	}
	keys := &Keys{Current: current}
	if previous := env(PreviousVariable); previous != "" {
		if keys.Previous, err = Load(previous); err != nil {
			return nil, &KeyError{strings.Replace(err.Error(), Variable, PreviousVariable, 1)}
		}
	}
	return keys, nil
}

// Opener is the KEK whose id is id, nil when this process holds none.
func (k *Keys) Opener(id string) *KEK {
	switch {
	case k.Current != nil && k.Current.ID == id:
		return k.Current
	case k.Previous != nil && k.Previous.ID == id:
		return k.Previous
	}
	return nil
}
