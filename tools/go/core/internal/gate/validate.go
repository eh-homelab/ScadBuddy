package gate

import (
	"fmt"
	"slices"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The validator: backend/scadbuddy/agent_data/gate_validate.py (removed in #7667) (a port of the agent's
// gate/validate.ts), check for check and in the same order.

const (
	questionsMax    = 4
	questionTextMax = 2_000
	answerMax       = 20_000
	// ResponseMax is the response cap: the largest valid answer at its worst JSON encoding.
	ResponseMax = questionsMax*(questionTextMax+answerMax)*6 + 1024
)

// Refused is a response the validator refuses, with its code (validate.ts REFUSALS).
type Refused struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (r *Refused) Error() string { return r.Message }

type principal struct{ kind, id string }

type question struct {
	text  string
	multi bool
}

// entry is what the workflow holds of a parked call.
type entry struct {
	id, kind, state string
	inputHash       *string
	requestedBy     *principal
	sessionOwner    *principal
	sessionCreator  *principal
	questions       []question
	hasQuestions    bool
	options         []string
	hasOptions      bool
	inRun           bool
}

type valid struct {
	decision string
	answers  []string
}

var allowed = map[string][]string{
	"approval": {"browser", "grant", "parent"},
	"answer":   {"browser"},
}

// jsWhitespace is what JavaScript's String.prototype.trim removes.
const jsWhitespace = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"

func jsTrim(s string) string { return strings.Trim(s, jsWhitespace) }

// ResponseBytes is the UTF-8 length of the response as JSON.stringify writes it.
func ResponseBytes(v any) int { return len(jsonv.Dumps(v)) }

func isAnswer(v any) bool {
	s, ok := v.(string)
	return ok && jsonv.UTF16Len(s) >= 1 && jsonv.UTF16Len(s) <= answerMax
}

func malformed(msg string) *Refused { return &Refused{"malformed", msg} }

func same(a *principal, b principal) bool { return a != nil && *a == b }

func validate(e *entry, rid string, response any, responder principal, role string) (*valid, error) {
	if e == nil || e.id != rid {
		return nil, &Refused{"stale", fmt.Sprintf("no pending input %s: it is stale or was never asked", rid)}
	}
	if e.state == "resolved" {
		return nil, &Refused{"resolved", rid + " is no longer waiting for a response"}
	}
	if e.state == "resolving" {
		return nil, &Refused{"resolving", rid + " is being resolved; it can no longer be answered"}
	}
	if ResponseBytes(response) > ResponseMax {
		return nil, &Refused{"too_large", fmt.Sprintf("a response may be at most %d bytes", ResponseMax)}
	}
	if !slices.Contains(allowed[e.kind], role) {
		msg := "outward actions need a human approval in the ScadBuddy UI, or another agent" +
			" with a per-token approval grant"
		if e.kind == "answer" {
			msg = "only the user in the ScadBuddy panel answers the agent's questions"
		}
		return nil, &Refused{"forbidden", msg}
	}
	if e.kind == "approval" && role == "grant" && e.inRun {
		return nil, &Refused{"forbidden",
			"a flow run's calls are decided by the person, never by an approval grant"}
	}
	if e.kind == "approval" && role == "parent" && !e.inRun {
		return nil, &Refused{"forbidden", "only a flow run's call goes to the session that started the run"}
	}
	if e.kind == "approval" && role != "browser" {
		if same(e.requestedBy, responder) || same(e.sessionOwner, responder) || same(e.sessionCreator, responder) {
			return nil, &Refused{"self", "an approval grant is for approving another agent's outward" +
				" actions, never your own; ask the user in the ScadBuddy UI"}
		}
	}
	body, ok := response.(jsonv.Object)
	kind, _ := body.Text("kind")
	if !ok || (kind != "approval" && kind != "answer") {
		return nil, malformed(`a response is {"kind": "approval", …} or {"kind": "answer", …}`)
	}
	if e.kind == "approval" {
		v, err := approval(e, rid, body, kind)
		if err == nil && role == "parent" && v.decision != "deny" {
			return nil, &Refused{"forbidden", "the session that started the run may deny the call" +
				" or pass it up, never approve it"}
		}
		return v, err
	}
	return answer(e, rid, body, kind)
}

func only(body jsonv.Object, names ...string) bool {
	for _, m := range body {
		if !slices.Contains(names, m.Key) {
			return false
		}
	}
	return true
}

func approval(e *entry, rid string, body jsonv.Object, kind string) (*valid, error) {
	if kind != "approval" {
		return nil, malformed(rid + ` is an approval: respond with {"kind": "approval", "decision": …}`)
	}
	if !only(body, "kind", "decision", "input_hash") {
		return nil, malformed(`an approval takes only "decision" and "input_hash"`)
	}
	decision, _ := body.Text("decision")
	if decision != "approve" && decision != "deny" {
		return nil, malformed(`"decision" must be "approve" or "deny"`)
	}
	if given, ok := body.Get("input_hash"); ok {
		s, isText := given.(string)
		if !isText || len(s) != 64 || strings.Trim(s, "0123456789abcdef") != "" {
			return nil, malformed(`"input_hash" must be 64 lowercase hex characters`)
		}
		if e.inputHash == nil || s != *e.inputHash {
			return nil, &Refused{"input_mismatch", rid + " is for a different input than the one" +
				" you were shown; the call needs a new approval"}
		}
	}
	return &valid{decision: decision}, nil
}

func answer(e *entry, rid string, body jsonv.Object, kind string) (*valid, error) {
	if kind != "answer" {
		return nil, malformed(rid + ` asks for an answer: respond with {"kind": "answer", …}`)
	}
	if !only(body, "kind", "answers", "choice", "text") {
		return nil, malformed(`an answer takes only "answers", "choice" or "text"`)
	}
	if e.hasOptions {
		choice, hasChoice := body.Get("choice")
		text, hasText := body.Get("text")
		if body.Has("answers") || hasChoice == hasText {
			return nil, malformed(rid + ` is an attention request: respond with exactly one of "choice" or "text"`)
		}
		if hasChoice {
			s, _ := choice.(string)
			if !isAnswer(choice) || !slices.Contains(e.options, s) {
				return nil, malformed(`"choice" must be one of ` + jsonv.Dumps(e.options) +
					`; use "text" for your own words`)
			}
			return &valid{answers: []string{s}}, nil
		}
		s, _ := text.(string)
		if !isAnswer(text) || jsTrim(s) == "" {
			return nil, malformed(fmt.Sprintf(`"text" must be 1 to %d characters, not only spaces`, answerMax))
		}
		return &valid{answers: []string{s}}, nil
	}
	given, ok := body.Get("answers")
	answers, isObject := given.(jsonv.Object)
	if !ok || !isObject || body.Has("choice") || body.Has("text") {
		return nil, malformed(rid + ` is a question: respond with "answers", one per question, keyed by its text`)
	}
	asked := make([]string, len(e.questions))
	all := len(answers) == len(e.questions)
	for i, q := range e.questions {
		asked[i] = q.text
		all = all && answers.Has(q.text)
	}
	if !all {
		return nil, malformed(`"answers" must answer exactly these questions: ` + jsonv.Dumps(asked))
	}
	var out []string
	for _, q := range e.questions {
		a, _ := answers.Get(q.text)
		if s, isText := a.(string); isText {
			if !isAnswer(s) {
				return nil, malformed(fmt.Sprintf("each answer must be 1 to %d characters", answerMax))
			}
			out = append(out, s)
			continue
		}
		picks, isList := a.([]any)
		good := isList && len(picks) > 0
		for _, p := range picks {
			good = good && isAnswer(p)
		}
		if !good {
			return nil, malformed(jsonv.Dumps(q.text) + " takes an answer: a string, or for a" +
				" multi-select a list of picks")
		}
		if !q.multi {
			return nil, malformed(jsonv.Dumps(q.text) + " takes one answer: send a string, not a list")
		}
		texts := make([]string, len(picks))
		for i, p := range picks {
			texts[i] = p.(string)
			if strings.Contains(texts[i], ", ") {
				return nil, malformed(`a pick must not contain ", ", which joins a multi-select's` +
					" picks; send the answer as one string")
			}
		}
		out = append(out, strings.Join(texts, ", "))
	}
	for _, a := range out {
		if jsonv.UTF16Len(a) > answerMax {
			return nil, malformed(fmt.Sprintf("each answer must be at most %d characters, a"+
				` multi-select's picks joined with ", "`, answerMax))
		}
	}
	for _, a := range out {
		if jsTrim(a) == "" {
			return nil, malformed("an answer must not be only spaces")
		}
	}
	if out == nil {
		out = []string{}
	}
	return &valid{answers: out}, nil
}

// ResponseHash is seal.ResponseHash of a response as JSON.stringify writes it.
func ResponseHash(v any) (string, error) { return seal.ResponseHash(jsonv.Dumps(v)) }
