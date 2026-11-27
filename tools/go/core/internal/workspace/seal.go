package workspace

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"io"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// A file in the blob store is sealed as agent_data/workspace.py `seal_file` seals it (the
// agent's tests keep a copy, agent/test/support/sql/workspaceSealing.ts): under a file key
// of its own, in segments of segmentBytes, segment i in the context
// workspace-file:<sha256>:<i>:<more|end>. The row keeps the file key wrapped in the context
// workspace-file-key:<workspace>:<path>:<blob key>, under the session's payload data key
// or, for a principal's file, under the KEK (key_kek_id).
// backend/tests/fixtures/workspace-seal-vectors.json holds both sides to one format.

const (
	segmentBytes = 1024 * 1024
	// sealOverhead is what a seal adds to a segment: the version byte, the IV and the tag.
	sealOverhead  = 1 + 12 + 16
	sealedSegment = segmentBytes + sealOverhead
	noKEK         = "the API has no SCADBUDDY_SECRET_KEY_FILE"
)

func segmentContext(digest string, index int, last bool) string {
	end := "more"
	if last {
		end = "end"
	}
	return fmt.Sprintf("workspace-file:%s:%d:%s", digest, index, end)
}

func fileKeyContext(workspace, path, blobKey string) string {
	return "workspace-file-key:" + workspace + ":" + path + ":" + blobKey
}

// sealFile is data sealed under fileKey segment by segment, each IV read from random.
func sealFile(fileKey, data []byte, digest string, random io.Reader) []byte {
	out := make([]byte, 0, len(data)+(len(data)/segmentBytes+1)*sealOverhead)
	for offset, index := 0, 0; ; index++ {
		end := min(offset+segmentBytes, len(data))
		last := end >= len(data)
		iv := make([]byte, 12)
		if _, err := io.ReadFull(random, iv); err != nil {
			panic(err)
		}
		out = append(out, seal.SealBytesIV(fileKey, data[offset:end], segmentContext(digest, index, last), iv)...)
		if last {
			return out
		}
		offset = end
	}
}

// errCutShort is a sealed file that ends before its last segment's overhead.
var errCutShort = errors.New("a sealed workspace file is cut short")

// openFile hands each segment's plaintext of what sealFile (or Python, or the agent) sealed
// to emit as it arrives from sealed; a full segment with bytes after it is not the last. It
// fails with a *seal.Error (or errCutShort) when a segment does not open (another key, altered, reordered,
// cut short), with emit's error, or with sealed's.
func openFile(fileKey []byte, sealed io.Reader, digest string, emit func([]byte) error) error {
	held := make([]byte, 0, 2*sealedSegment)
	chunk := make([]byte, 64*1024)
	index := 0
	for {
		n, err := sealed.Read(chunk)
		held = append(held, chunk[:n]...)
		for len(held) > sealedSegment {
			plain, err := seal.OpenBytes(fileKey, held[:sealedSegment], segmentContext(digest, index, false))
			if err != nil {
				return err
			}
			if err := emit(plain); err != nil {
				return err
			}
			held = append(held[:0], held[sealedSegment:]...)
			index++
		}
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
	}
	if len(held) < sealOverhead {
		return errCutShort
	}
	tail, err := seal.OpenBytes(fileKey, held, segmentContext(digest, index, true))
	if err != nil {
		return err
	}
	if len(tail) > 0 {
		return emit(tail)
	}
	return nil
}

func newFileKey() []byte {
	key := make([]byte, seal.KEKBytes)
	if _, err := io.ReadFull(rand.Reader, key); err != nil {
		panic(err)
	}
	return key
}

// keyIn is payload_keys.py `key_in` in tx, with no audit row: the subject's data key, made
// first with create (only while its session stands, with standing); nil when it has none.
func keyIn(ctx context.Context, tx pgx.Tx, keys *seal.Keys, subject string, create, standing bool) ([]byte, error) {
	sealed, kekID, err := keyRow(ctx, tx, subject)
	if err != nil {
		return nil, err
	}
	if sealed == nil && create {
		made := seal.SealBytes(keys.Current.Key(), newFileKey(), seal.PayloadKeyContext(subject))
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, seal.PayloadKeyLock(subject)); err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)
			 SELECT $1, $2, $3
			 WHERE NOT EXISTS (SELECT 1 FROM ai_forgotten_subjects WHERE subject = $1)
			 AND (NOT $4 OR EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = substr($1, 9)))
			 ON CONFLICT (subject) DO NOTHING`, subject, made, keys.Current.ID, standing); err != nil {
			return nil, err
		}
		// Whoever inserted first, this is the key every encoder of the subject uses.
		if sealed, kekID, err = keyRow(ctx, tx, subject); err != nil {
			return nil, err
		}
	}
	if sealed == nil {
		return nil, nil
	}
	opener := keys.Opener(kekID)
	if opener == nil {
		return nil, refused("unavailable", fmt.Sprintf("the payload key of %s was sealed with key %s,"+
			" which the API does not hold", subject, kekID))
	}
	dek, err := seal.OpenBytes(opener.Key(), sealed, seal.PayloadKeyContext(subject))
	if err != nil {
		return nil, refused("unavailable", fmt.Sprintf("the payload key of %s does not open", subject))
	}
	return dek, nil
}

func keyRow(ctx context.Context, tx pgx.Tx, subject string) ([]byte, string, error) {
	var sealed []byte
	var kekID string
	err := tx.QueryRow(ctx, `SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = $1`,
		subject).Scan(&sealed, &kekID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, "", nil
	}
	return sealed, kekID, err
}

// sessionKey is workspace.py `_session_key`: the session's data key, in a transaction of
// its own; a key is made (create) only for a session that stands.
func sessionKey(ctx context.Context, pool *pgxpool.Pool, keys *seal.Keys, session string, create bool) ([]byte, error) {
	if keys == nil || keys.Current == nil {
		return nil, refused("unavailable", noKEK)
	}
	var dek []byte
	err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) (err error) {
		dek, err = keyIn(ctx, tx, keys, "session-"+session, create, true)
		return err
	})
	if err != nil {
		return nil, err
	}
	if dek == nil {
		return nil, refused("unavailable", fmt.Sprintf("session %s was deleted or forgotten", session))
	}
	return dek, nil
}

// wrapKey is fileKey wrapped for the row, and the KEK's id for a principal's file.
func wrapKey(ctx context.Context, pool *pgxpool.Pool, keys *seal.Keys, workspace, path, blobKey string,
	fileKey []byte) ([]byte, *string, error) {
	context := fileKeyContext(workspace, path, blobKey)
	session, isSession := sessionOf(workspace)
	if !isSession {
		if keys == nil || keys.Current == nil {
			return nil, nil, refused("unavailable", noKEK)
		}
		id := keys.Current.ID
		return seal.SealBytes(keys.Current.Key(), fileKey, context), &id, nil
	}
	dek, err := sessionKey(ctx, pool, keys, session, true)
	if err != nil {
		return nil, nil, err
	}
	return seal.SealBytes(dek, fileKey, context), nil, nil
}

// unwrapKey is the row's file key; unavailable when no key here opens it.
func unwrapKey(ctx context.Context, pool *pgxpool.Pool, keys *seal.Keys, workspace, path, blobKey string,
	sealed []byte, kekID *string) ([]byte, error) {
	context := fileKeyContext(workspace, path, blobKey)
	session, isSession := sessionOf(workspace)
	var key []byte
	if !isSession {
		var kek *seal.KEK
		if keys != nil && kekID != nil {
			kek = keys.Opener(*kekID)
		}
		if kek == nil {
			// Python writes the id it was given, None as None.
			shown := "None"
			if kekID != nil {
				shown = *kekID
			}
			return nil, refused("unavailable", fmt.Sprintf(
				"a workspace file's key was sealed with key %s, which the API does not hold", shown))
		}
		key = kek.Key()
	} else {
		dek, err := sessionKey(ctx, pool, keys, session, false)
		if err != nil {
			return nil, err
		}
		key = dek
	}
	fileKey, err := seal.OpenBytes(key, sealed, context)
	if err != nil {
		return nil, refused("unavailable", fmt.Sprintf("the key of %s does not open", path))
	}
	return fileKey, nil
}

func sessionOf(workspace string) (string, bool) {
	const prefix = "session:"
	if len(workspace) >= len(prefix) && workspace[:len(prefix)] == prefix {
		return workspace[len(prefix):], true
	}
	return "", false
}
