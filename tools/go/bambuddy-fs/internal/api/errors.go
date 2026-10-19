package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"syscall"
)

// Error is a ScadBuddy answer that was not a success: its HTTP status (or a failed
// operation's) and the problem's detail. Op names the call, e.g. "mkdir".
type Error struct {
	Status int
	Detail string
	Op     string
	// Upstream is the status Bambuddy answered when ScadBuddy relays its refusal
	// (`bambuddy_status`, bambuddy/errors.py); 0 when the error is ScadBuddy's own.
	Upstream int
}

func (e *Error) Error() string {
	if e.Detail == "" {
		return fmt.Sprintf("%s: %d %s", e.Op, e.Status, http.StatusText(e.Status))
	}
	return fmt.Sprintf("%s: %d %s", e.Op, e.Status, e.Detail)
}

// Errno maps an error from this package to the errno FUSE answers with (spec §9).
func Errno(err error) syscall.Errno {
	if err == nil {
		return 0
	}
	// A call cut short by its operation's cancel: go-fuse cancels an operation's context
	// when the kernel interrupts it (FUSE_INTERRUPT, a signal to the caller). EINTR lets
	// the kernel restart a call whose signal handler asked for it, as Go's do; EIO
	// would fail it (TestLiveMount's `ls` on 2026-10-19, under Go's preemption signal).
	if errors.Is(err, context.Canceled) {
		return syscall.EINTR
	}
	var e *Error
	if errors.As(err, &e) {
		switch {
		case e.Status == 404:
			return syscall.ENOENT
		case e.Status == 409:
			return syscall.EBUSY
		case e.Status == 413:
			return syscall.EFBIG
		case e.Status == 401 || e.Status == 403:
			return syscall.EACCES
		}
	}
	return syscall.EIO
}

// errorFrom decodes a problem body. ScadBuddy answers a Bambuddy scope refusal with
// 409 and `required_scope` (bambuddy/errors.py), at the top level of a response or in
// a failed operation's `extensions`; that is a permission problem, so it becomes 403.
func errorFrom(op string, status int, body []byte) *Error {
	e := &Error{Op: op, Status: status}
	var problem struct {
		Detail         json.RawMessage `json:"detail"`
		RequiredScope  string          `json:"required_scope"`
		BambuddyStatus int             `json:"bambuddy_status"`
		Extensions     struct {
			RequiredScope  string `json:"required_scope"`
			BambuddyStatus int    `json:"bambuddy_status"`
		} `json:"extensions"`
	}
	if json.Unmarshal(body, &problem) != nil {
		e.Detail = strings.TrimSpace(string(body))
		return e
	}
	if json.Unmarshal(problem.Detail, &e.Detail) != nil {
		e.Detail = string(problem.Detail)
	}
	e.Upstream = problem.BambuddyStatus
	if e.Upstream == 0 {
		e.Upstream = problem.Extensions.BambuddyStatus
	}
	if status == http.StatusConflict && (problem.RequiredScope != "" || problem.Extensions.RequiredScope != "") {
		e.Status = http.StatusForbidden
	}
	return e
}
