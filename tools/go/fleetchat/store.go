// Package fleetchat is the agent fleet chat: direct messages and broadcast
// channels between Claude Code sessions, served as an MCP server. The host
// supplies the Store and the Bus; the library has no implementation of either.
package fleetchat

import (
	"context"
	"errors"
	"time"
)

// Message is one delivered message. Exactly one of Channel and To is set.
type Message struct {
	Seq     int64     `json:"seq"`
	From    string    `json:"from"`
	Channel string    `json:"channel,omitempty"`
	To      string    `json:"to,omitempty"`
	Text    string    `json:"text"`
	SentAt  time.Time `json:"sent_at"`
}

// AgentInfo is one agent of the fleet.
type AgentInfo struct {
	Handle   string    `json:"handle"`
	LastSeen time.Time `json:"last_seen"`
}

// ChannelInfo is one channel: its members' handles and the caller's unread count.
type ChannelInfo struct {
	Name    string   `json:"name"`
	Members []string `json:"members"`
	Unread  int      `json:"unread"`
}

// Target is where a message goes: a channel, or an agent by its handle.
// Exactly one is set.
type Target struct {
	Channel string
	Agent   string
}

// Store keeps the fleet. Every argument it gets is already validated.
type Store interface {
	// Touch records that the agent was seen now, creating it on first use.
	Touch(ctx context.Context, sessionID, handle string) error
	Agents(ctx context.Context) ([]AgentInfo, error)
	// Join adds the agent to the channel, creating the channel. A name valid only
	// before #8149 is never created: when no channel has it, Join returns
	// ErrBadChannel and changes nothing.
	Join(ctx context.Context, sessionID, channel string) error
	// Leave returns ErrUnknownChannel when no channel has the name, and
	// ErrNotMember when the agent is not in it.
	Leave(ctx context.Context, sessionID, channel string) error
	Channels(ctx context.Context, sessionID string) ([]ChannelInfo, error)
	// Send stores the message and delivers it: to the agent of a DM, or to
	// the channel's members at send time (sender included). It returns the
	// message's seq and the recipients' session ids. It returns
	// ErrUnknownAgent for a handle or name no agent has, a VirtualSenderError
	// for a virtual sender's name, ErrUnknownChannel for a
	// channel name no channel has, and ErrNotMember when the sender is not in
	// the channel.
	Send(ctx context.Context, sessionID string, t Target, text string) (int64, []string, error)
	// Inbox returns up to limit deliveries above the agent's cursor, oldest
	// first, and whether more are waiting. Unless peek, it moves the cursor
	// to the last one returned.
	Inbox(ctx context.Context, sessionID string, limit int, peek bool) ([]Message, bool, error)
	// Rewind sets the agent's cursor back to seq. A seq past the cursor is ErrBadSeq.
	Rewind(ctx context.Context, sessionID string, seq int64) error
}

var (
	ErrBadSession   = errors.New("session_id must be a canonical lower-case UUID")
	ErrBadChannel   = errors.New("channel must be, in lower case and at most 256 characters, an issue or PR reference (owner/repo#123), an Actions run reference (owner/repo/runs/123) or a name under Kubernetes' rules (a DNS-1123 subdomain: a-z, 0-9, - and ., each dot-separated part 1-63 characters that starts and ends with a letter or digit)")
	ErrBadTarget    = errors.New("give exactly one of channel or agent; agent is an 8-character lower-case hex handle or a name (1 to 40 of a-z, 0-9 and -, with at least one character that is not a hex digit, such as rotator)")
	ErrEmptyText    = errors.New("text is empty")
	ErrTooLong      = errors.New("text is longer than the largest message allowed")
	ErrBadSeq       = errors.New("seq must be 0 or more, and not past your cursor")
	ErrUnknownAgent = errors.New("no agent has that handle")
	ErrNotMember    = errors.New("not a member of that channel")
	// ErrUnknownChannel is a channel nobody has joined: fleet_join creates it.
	ErrUnknownChannel = errors.New("no channel has that name; fleet_join creates it")
	// ErrVirtualSender is a DM to a virtual sender: a reserved name that only posts, such
	// as "rotator" or "github". A Store returns it as VirtualSenderError, whose text is the
	// sender's own reject message.
	ErrVirtualSender = errors.New("that name is a virtual sender, which reads no messages")
)

// VirtualSenderError is ErrVirtualSender with the virtual sender's reject message as its
// text, so fleet_send returns that text as its tool error.
func VirtualSenderError(reject string) error {
	if reject == "" {
		return ErrVirtualSender
	}
	return virtualSenderError(reject)
}

type virtualSenderError string

func (e virtualSenderError) Error() string        { return string(e) }
func (e virtualSenderError) Is(target error) bool { return target == ErrVirtualSender }
