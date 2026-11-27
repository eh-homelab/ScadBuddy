package fleetchat

import (
	"encoding/base64"
	"fmt"
	"strings"
)

// SegmentPrefix leads a URL path segment that carries a channel name as base64url (#8149).
//
// A host's store puts a channel in a path segment, such as ScadBuddy's
// /channels/{name}/members/{session_id}. A plain name (NameChannelPattern or
// LegacyChannelPattern) goes there as is. A GitHub reference holds "/" or "#", which a segment
// can carry only percent-escaped, so it goes as SegmentPrefix and the unpadded base64url
// (RFC 4648 §5) of its UTF-8 bytes. "~" is an RFC 3986 unreserved character, so no client
// escapes it, and it is in neither the base64url alphabet nor any channel name, so a segment is
// read one way only.
const SegmentPrefix = "~"

// ChannelSegment is the path segment for channel: channel itself when every byte is
// [a-z0-9.-], or else SegmentPrefix and its base64url.
func ChannelSegment(channel string) string {
	if channel != "" && strings.Trim(channel, "abcdefghijklmnopqrstuvwxyz0123456789.-") == "" {
		return channel
	}
	return SegmentPrefix + base64.RawURLEncoding.EncodeToString([]byte(channel))
}

// ParseChannelSegment is the channel a path segment names: the decoded name after
// SegmentPrefix, or else the segment itself. It is ErrBadChannel when the base64url is not in
// its one canonical unpadded form, or when the name is no channel's.
func ParseChannelSegment(segment string) (string, error) {
	channel := segment
	if enc, ok := strings.CutPrefix(segment, SegmentPrefix); ok {
		raw, err := base64.RawURLEncoding.Strict().DecodeString(enc)
		// The decoder skips "\r" and "\n"; only the canonical encoding is taken.
		if err != nil || base64.RawURLEncoding.EncodeToString(raw) != enc {
			return "", fmt.Errorf("%w: %q is not %q and unpadded base64url", ErrBadChannel, segment, SegmentPrefix)
		}
		channel = string(raw)
	}
	if err := checkChannel(channel); err != nil {
		return "", err
	}
	return channel, nil
}
