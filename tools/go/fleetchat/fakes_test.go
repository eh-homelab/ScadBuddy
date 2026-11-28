package fleetchat

import (
	"context"
	"errors"
	"slices"
	"sort"
	"sync"
	"time"
)

// fakeStore keeps the fleet in maps, for the tests only.
type fakeStore struct {
	mu        sync.Mutex
	agents    map[string]AgentInfo           // session id -> agent
	members   map[string]map[string]struct{} // channel -> session ids
	messages  []Message
	delivered map[string][]int64 // session id -> seqs, ascending
	cursor    map[string]int64
	lastLimit int
	touches   int
	// existsAsked counts ChannelExists calls.
	existsAsked int
	virtual     map[string]string // virtual sender name -> reject message
}

const rotatorReject = `"rotator" is the login rotator, a virtual sender: it reads no messages.`

func newFakeStore() *fakeStore {
	return &fakeStore{
		agents:    map[string]AgentInfo{},
		members:   map[string]map[string]struct{}{},
		delivered: map[string][]int64{},
		cursor:    map[string]int64{},
		virtual:   map[string]string{"rotator": rotatorReject},
	}
}

func (f *fakeStore) Touch(_ context.Context, sessionID, handle string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.touches++
	f.agents[sessionID] = AgentInfo{Handle: handle, LastSeen: time.Now()}
	return nil
}

func (f *fakeStore) Agents(context.Context) ([]AgentInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []AgentInfo
	for _, a := range f.agents {
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Handle < out[j].Handle })
	return out, nil
}

func (f *fakeStore) Join(_ context.Context, sessionID, channel string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.members[channel] == nil {
		f.members[channel] = map[string]struct{}{}
	}
	f.members[channel][sessionID] = struct{}{}
	return nil
}

func (f *fakeStore) Leave(_ context.Context, sessionID, channel string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.members[channel]; !ok {
		return ErrUnknownChannel
	}
	if _, ok := f.members[channel][sessionID]; !ok {
		return ErrNotMember
	}
	delete(f.members[channel], sessionID)
	return nil
}

func (f *fakeStore) Channels(_ context.Context, sessionID string) ([]ChannelInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []ChannelInfo
	for name, m := range f.members {
		ci := ChannelInfo{Name: name}
		for sid := range m {
			ci.Members = append(ci.Members, f.agents[sid].Handle)
		}
		sort.Strings(ci.Members)
		for _, seq := range f.delivered[sessionID] {
			if seq > f.cursor[sessionID] && f.messages[seq-1].Channel == name {
				ci.Unread++
			}
		}
		out = append(out, ci)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

func (f *fakeStore) ChannelExists(_ context.Context, channel string) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.existsAsked++
	_, ok := f.members[channel]
	return ok, nil
}

func (f *fakeStore) Send(_ context.Context, sessionID string, t Target, text string) (int64, []string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var recipients []string
	if reject, ok := f.virtual[t.Agent]; ok {
		return 0, nil, VirtualSenderError(reject)
	}
	if t.Agent != "" {
		for sid, a := range f.agents {
			if a.Handle == t.Agent {
				recipients = []string{sid}
			}
		}
		if recipients == nil {
			return 0, nil, ErrUnknownAgent
		}
	} else {
		if _, ok := f.members[t.Channel]; !ok {
			return 0, nil, ErrUnknownChannel
		}
		if _, ok := f.members[t.Channel][sessionID]; !ok {
			return 0, nil, ErrNotMember
		}
		for sid := range f.members[t.Channel] {
			recipients = append(recipients, sid)
		}
		sort.Strings(recipients)
	}
	seq := int64(len(f.messages) + 1)
	f.messages = append(f.messages, Message{
		Seq: seq, From: f.agents[sessionID].Handle, Channel: t.Channel, To: t.Agent,
		Text: text, SentAt: time.Now(),
	})
	for _, sid := range recipients {
		f.delivered[sid] = append(f.delivered[sid], seq)
	}
	return seq, recipients, nil
}

func (f *fakeStore) Inbox(_ context.Context, sessionID string, limit int, peek bool) ([]Message, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.lastLimit = limit
	var out []Message
	more := false
	for _, seq := range f.delivered[sessionID] {
		if seq <= f.cursor[sessionID] {
			continue
		}
		if len(out) == limit {
			more = true
			break
		}
		out = append(out, f.messages[seq-1])
	}
	if !peek && len(out) > 0 {
		f.cursor[sessionID] = out[len(out)-1].Seq
	}
	return out, more, nil
}

func (f *fakeStore) Rewind(_ context.Context, sessionID string, seq int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if seq > f.cursor[sessionID] {
		return ErrBadSeq // as ScadBuddy's core: a rewind only moves the cursor back
	}
	f.cursor[sessionID] = seq
	return nil
}

// fakeBus records what is published and hands it to every subscriber.
type fakeBus struct {
	mu         sync.Mutex
	published  [][]string
	publishErr error
	subs       []func(Event)
	subscribed chan struct{}
}

func newFakeBus() *fakeBus { return &fakeBus{subscribed: make(chan struct{}, 16)} }

func (b *fakeBus) Publish(_ context.Context, sessionIDs []string) error {
	b.mu.Lock()
	b.published = append(b.published, slices.Clone(sessionIDs))
	err := b.publishErr
	b.mu.Unlock()
	if err != nil {
		return err
	}
	b.emit(Event{SessionIDs: sessionIDs})
	return nil
}

func (b *fakeBus) Subscribe(ctx context.Context, f func(Event)) error {
	b.mu.Lock()
	b.subs = append(b.subs, f)
	b.mu.Unlock()
	b.subscribed <- struct{}{}
	<-ctx.Done()
	return nil
}

func (b *fakeBus) emit(ev Event) {
	b.mu.Lock()
	subs := slices.Clone(b.subs)
	b.mu.Unlock()
	for _, f := range subs {
		f(ev)
	}
}

func (b *fakeBus) publishedCalls() [][]string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return slices.Clone(b.published)
}

var errBusDown = errors.New("bus down")
