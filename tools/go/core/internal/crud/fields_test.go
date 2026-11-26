package crud

import "testing"

func TestPyUUIDReadsAsPydantic(t *testing.T) {
	const id = "0123abcd-0000-4000-8000-00000000beef"
	const simple = "0123abcd00004000800000000000beef"
	for in, want := range map[string]bool{
		id:                                     true,
		"0123ABCD-0000-4000-8000-00000000BEEF": true,
		simple:                                 true,
		"{" + id + "}":                         true,
		"urn:uuid:" + id:                       true,
		"urn:uuid:{" + id + "}":                false,
		"URN:UUID:" + id:                       false,
		"{" + simple + "}":                     false,
		"urn:uuid:" + simple:                   false,
	} {
		got, ok := PyUUID(in)
		if ok != want || ok && got != id {
			t.Errorf("PyUUID(%q) = %q, %v; want ok %v", in, got, ok, want)
		}
	}
}

func TestReplaceInvalidUTF8AsPython(t *testing.T) {
	for in, want := range map[string]string{
		"abc":          "abc",
		"a\xffb":       "a�b",
		"\xff\xfe":     "��",
		"\xe2\x82":     "�",
		"\xe2\x82x":    "�x",
		"\xed\xa0\x80": "���",
		"\xf0\x9f\x98": "�",
		"é\xc3":        "é�",
	} {
		if got := replaceInvalidUTF8(in); got != want {
			t.Errorf("replaceInvalidUTF8(%q) = %q; want %q", in, got, want)
		}
	}
}
