package fleetchat

import "context"

// Event says whose inboxes changed. Resync says that some events may have
// been lost (a reconnect, for example), so every watched inbox may have changed.
type Event struct {
	SessionIDs []string
	Resync     bool
}

// Bus carries inbox changes between fleet servers.
type Bus interface {
	// Publish announces new deliveries to the recipients' inboxes.
	Publish(ctx context.Context, sessionIDs []string) error
	// Subscribe calls f for each event until ctx ends, then returns. A Bus
	// that reconnects sends an Event with Resync after it does.
	Subscribe(ctx context.Context, f func(Event)) error
}
