// Package gate is the tool-call gate's validator (spec 2026-10-01 §6.6, "Responses"):
// backend/scadbuddy/agent_data/gate_validate.py, check for check and in the same order, which
// is agent/src/gate/validate.ts in Python. backend/tests/fixtures/pending-input-vectors.json
// pins the three together. It reads nothing but its arguments: no I/O, no clock.
//
// A response is JSON decoded with json.Decoder.UseNumber: objects as map[string]any, numbers
// as json.Number. Go's decoder turns a lone surrogate into U+FFFD; Python's encoder then
// refuses that response, so no valid response has one.
package gate

import (
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"slices"
	"strconv"
	"strings"
	"unicode/utf16"
)

// The agent's harness/questions.ts bounds, which the vectors file carries.
const (
	QuestionsMax    = 4
	QuestionTextMax = 2_000
	AnswerMax       = 20_000
	// ResponseMax is the classic route's response cap (plan 5b ruling 1): the largest valid
	// answer at its worst JSON encoding.
	ResponseMax = QuestionsMax*(QuestionTextMax+AnswerMax)*6 + 1024
)

// Principal is who asked, who owns a session, or who responds.
type Principal struct{ Kind, ID string }

// Question is one question of an answer entry.
type Question struct {
	Question    string
	MultiSelect bool
}

// Entry is what the workflow holds of a parked call.
type Entry struct {
	ID string
	// Kind is "approval" or "answer"; State "pending", "resolving" or "resolved".
	Kind, State    string
	InputHash      *string
	RequestedBy    *Principal
	SessionOwner   *Principal
	SessionCreator *Principal
	// Questions and Options are nil when the entry has none.
	Questions []Question
	Options   []string
	// InRun is a child session's call in a flow run's turn (#2482): the run's queue.
	InRun bool
}

// Request is a response to a parked call. Role is "browser", "grant", "owner", "parent" or
// "run_grant".
type Request struct {
	RequestID string
	Response  any
	Responder Principal
	Role      string
}

// Valid is a response the validator takes.
type Valid struct {
	// Decision is "approve" or "deny" for an approval, "" for an answer.
	Decision string
	Answers  []string
}

// Refused is a response the validator refuses. Code is "stale", "resolved", "resolving",
// "forbidden", "self", "malformed", "too_large" or "input_mismatch".
type Refused struct {
	Code    string
	Message string
}

func (r *Refused) Error() string { return r.Message }

var allowed = map[string][]string{
	"approval": {"browser", "grant", "parent"},
	"answer":   {"browser"},
}

// jsWhitespace is what JavaScript's String.prototype.trim removes (WhiteSpace and
// LineTerminator, ECMA-262).
const jsWhitespace = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006" +
	"\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"

func jsTrim(text string) string { return strings.Trim(text, jsWhitespace) }

// UTF16Length is a JavaScript string's length, which the agent's bounds count.
func UTF16Length(text string) int { return len(utf16.Encode([]rune(text))) }

// ResponseBytes is the UTF-8 length of the response as Python's json.dumps writes it, with
// ensure_ascii off and no spaces.
func ResponseBytes(response any) int { return len(dumps(response)) }

func isAnswer(v any) bool {
	s, ok := v.(string)
	return ok && UTF16Length(s) >= 1 && UTF16Length(s) <= AnswerMax
}

func malformed(message string) *Refused { return &Refused{"malformed", message} }

func same(a *Principal, b Principal) bool { return a != nil && *a == b }

// Validate is validate_respond: the response the entry takes, or a *Refused.
func Validate(entry *Entry, request Request) (*Valid, error) {
	rid := request.RequestID
	if entry == nil || entry.ID != rid {
		return nil, &Refused{"stale", fmt.Sprintf("no pending input %s: it is stale or was never asked", rid)}
	}
	if entry.State == "resolved" {
		return nil, &Refused{"resolved", rid + " is no longer waiting for a response"}
	}
	if entry.State == "resolving" {
		return nil, &Refused{"resolving", rid + " is being resolved; it can no longer be answered"}
	}
	if ResponseBytes(request.Response) > ResponseMax {
		return nil, &Refused{"too_large", fmt.Sprintf("a response may be at most %d bytes", ResponseMax)}
	}
	if !slices.Contains(allowed[entry.Kind], request.Role) {
		message := "outward actions need a human approval in the ScadBuddy UI, or another agent" +
			" with a per-token approval grant"
		if entry.Kind == "answer" {
			message = "only the user in the ScadBuddy panel answers the agent's questions"
		}
		return nil, &Refused{"forbidden", message}
	}
	if entry.Kind == "approval" && request.Role == "grant" && entry.InRun {
		return nil, &Refused{"forbidden",
			"a flow run's calls are decided by the person, never by an approval grant"}
	}
	if entry.Kind == "approval" && request.Role == "parent" && !entry.InRun {
		return nil, &Refused{"forbidden",
			"only a flow run's call goes to the session that started the run"}
	}
	if entry.Kind == "approval" && request.Role != "browser" {
		for _, o := range []*Principal{entry.RequestedBy, entry.SessionOwner, entry.SessionCreator} {
			if same(o, request.Responder) {
				return nil, &Refused{"self", "an approval grant is for approving another agent's" +
					" outward actions, never your own; ask the user in the ScadBuddy UI"}
			}
		}
	}
	body, ok := request.Response.(map[string]any)
	if !ok || (body["kind"] != "approval" && body["kind"] != "answer") {
		return nil, malformed(`a response is {"kind": "approval", …} or {"kind": "answer", …}`)
	}
	if entry.Kind == "approval" {
		valid, err := approval(entry, rid, body)
		if err != nil {
			return nil, err
		}
		if request.Role == "parent" && valid.Decision != "deny" {
			return nil, &Refused{"forbidden", "the session that started the run may deny the" +
				" call or pass it up, never approve it"}
		}
		return valid, nil
	}
	return answer(entry, rid, body)
}

// only says whether every member of body is among names.
func only(body map[string]any, names ...string) bool {
	for k := range body {
		if !slices.Contains(names, k) {
			return false
		}
	}
	return true
}

func approval(entry *Entry, rid string, body map[string]any) (*Valid, error) {
	if body["kind"] != "approval" {
		return nil, malformed(rid + ` is an approval: respond with {"kind": "approval", "decision": …}`)
	}
	if !only(body, "kind", "decision", "input_hash") {
		return nil, malformed(`an approval takes only "decision" and "input_hash"`)
	}
	decision := body["decision"]
	if decision != "approve" && decision != "deny" {
		return nil, malformed(`"decision" must be "approve" or "deny"`)
	}
	if given, ok := body["input_hash"]; ok {
		s, isText := given.(string)
		if !isText || len([]rune(s)) != 64 || strings.Trim(s, "0123456789abcdef") != "" {
			return nil, malformed(`"input_hash" must be 64 lowercase hex characters`)
		}
		if entry.InputHash == nil || s != *entry.InputHash {
			return nil, &Refused{"input_mismatch", rid + " is for a different input than the one" +
				" you were shown; the call needs a new approval"}
		}
	}
	return &Valid{Decision: decision.(string), Answers: []string{}}, nil
}

func answer(entry *Entry, rid string, body map[string]any) (*Valid, error) {
	if body["kind"] != "answer" {
		return nil, malformed(rid + ` asks for an answer: respond with {"kind": "answer", …}`)
	}
	if !only(body, "kind", "answers", "choice", "text") {
		return nil, malformed(`an answer takes only "answers", "choice" or "text"`)
	}
	_, hasAnswers := body["answers"]
	choice, hasChoice := body["choice"]
	text, hasText := body["text"]
	if entry.Options != nil {
		if hasAnswers || hasChoice == hasText {
			return nil, malformed(rid + ` is an attention request: respond with exactly one of "choice" or "text"`)
		}
		if hasChoice {
			c, _ := choice.(string)
			if !isAnswer(choice) || !slices.Contains(entry.Options, c) {
				return nil, malformed(`"choice" must be one of ` + dumps(toAny(entry.Options)) +
					`; use "text" for your own words`)
			}
			return &Valid{Answers: []string{c}}, nil
		}
		t, ok := text.(string)
		if !ok || !isAnswer(text) || jsTrim(t) == "" {
			return nil, malformed(fmt.Sprintf(`"text" must be 1 to %d characters, not only spaces`, AnswerMax))
		}
		return &Valid{Answers: []string{t}}, nil
	}
	asked := entry.Questions
	given, ok := body["answers"].(map[string]any)
	if !ok || hasChoice || hasText {
		return nil, malformed(rid + ` is a question: respond with "answers", one per question, keyed by its text`)
	}
	every := len(given) == len(asked)
	for _, q := range asked {
		_, in := given[q.Question]
		every = every && in
	}
	if !every {
		names := make([]any, len(asked))
		for i, q := range asked {
			names[i] = q.Question
		}
		return nil, malformed(`"answers" must answer exactly these questions: ` + dumps(names))
	}
	answers := []string{}
	for _, q := range asked {
		a := given[q.Question]
		if s, isText := a.(string); isText {
			if !isAnswer(s) {
				return nil, malformed(fmt.Sprintf("each answer must be 1 to %d characters", AnswerMax))
			}
			answers = append(answers, s)
			continue
		}
		picks, isList := a.([]any)
		allAnswers := isList && len(picks) > 0
		for _, p := range picks {
			allAnswers = allAnswers && isAnswer(p)
		}
		if !allAnswers {
			return nil, malformed(dumps(q.Question) + " takes an answer: a string, or for a" +
				" multi-select a list of picks")
		}
		if !q.MultiSelect {
			return nil, malformed(dumps(q.Question) + " takes one answer: send a string, not a list")
		}
		texts := make([]string, len(picks))
		for i, p := range picks {
			texts[i] = p.(string)
			if strings.Contains(texts[i], ", ") {
				return nil, malformed(`a pick must not contain ", ", which joins a multi-select's` +
					" picks; send the answer as one string")
			}
		}
		answers = append(answers, strings.Join(texts, ", "))
	}
	for _, a := range answers {
		if UTF16Length(a) > AnswerMax {
			return nil, malformed(fmt.Sprintf("each answer must be at most %d characters, a"+
				` multi-select's picks joined with ", "`, AnswerMax))
		}
	}
	for _, a := range answers {
		if jsTrim(a) == "" {
			return nil, malformed("an answer must not be only spaces")
		}
	}
	return &Valid{Answers: answers}, nil
}

func toAny(s []string) []any {
	out := make([]any, len(s))
	for i, v := range s {
		out[i] = v
	}
	return out
}

// dumps is Python's json.dumps(v, ensure_ascii=False, separators=(",", ":")).
func dumps(v any) string {
	var b strings.Builder
	write(&b, v)
	return b.String()
}

func write(b *strings.Builder, v any) {
	switch v := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(v))
	case json.Number:
		b.WriteString(pyNumber(string(v)))
	case string:
		quote(b, v)
	case []any:
		b.WriteByte('[')
		for i, item := range v {
			if i > 0 {
				b.WriteByte(',')
			}
			write(b, item)
		}
		b.WriteByte(']')
	case map[string]any:
		b.WriteByte('{')
		i := 0
		for k, item := range v {
			if i > 0 {
				b.WriteByte(',')
			}
			i++
			quote(b, k)
			b.WriteByte(':')
			write(b, item)
		}
		b.WriteByte('}')
	default:
		panic(fmt.Sprintf("%T is not JSON", v))
	}
}

func quote(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

// pyNumber is a JSON number as Python's json.loads, then json.dumps, writes it: an integer
// literal stays an exact int; any other number is a float, written as repr writes it.
func pyNumber(text string) string {
	if !strings.ContainsAny(text, ".eE") {
		n, ok := new(big.Int).SetString(text, 10)
		if ok {
			return n.String()
		}
	}
	x, _ := strconv.ParseFloat(text, 64)
	switch {
	case math.IsInf(x, 1):
		return "Infinity"
	case math.IsInf(x, -1):
		return "-Infinity"
	case x == 0:
		if math.Signbit(x) {
			return "-0.0"
		}
		return "0.0"
	}
	mantissa, exp, _ := strings.Cut(strconv.FormatFloat(x, 'e', -1, 64), "e")
	e, _ := strconv.Atoi(exp)
	if e < -4 || e >= 16 {
		sign := "+"
		if e < 0 {
			sign, e = "-", -e
		}
		return fmt.Sprintf("%se%s%02d", mantissa, sign, e)
	}
	out := strconv.FormatFloat(x, 'f', -1, 64)
	if !strings.Contains(out, ".") {
		out += ".0"
	}
	return out
}
