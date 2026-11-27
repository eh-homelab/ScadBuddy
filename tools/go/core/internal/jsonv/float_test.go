package jsonv

import "testing"

// The table is Python 3.12's json.dumps of each float.
func TestAFloatIsWrittenAsPythonWritesIt(t *testing.T) {
	for _, c := range []struct {
		f    float64
		want string
	}{
		{0, "0.0"}, {1, "1.0"}, {1.5, "1.5"}, {1e5, "100000.0"}, {1e6, "1000000.0"},
		{123456789, "123456789.0"}, {1e15, "1000000000000000.0"},
		{9999999999999998, "9999999999999998.0"}, {1e16, "1e+16"}, {1.5e16, "1.5e+16"},
		{1e-4, "0.0001"}, {1e-5, "1e-05"}, {0.1, "0.1"}, {1.0 / 3, "0.3333333333333333"},
		{-2.5e7, "-25000000.0"}, {1e300, "1e+300"}, {5e-324, "5e-324"},
	} {
		if got := Float(c.f); got != c.want {
			t.Errorf("Float(%v) = %q, want %q", c.f, got, c.want)
		}
	}
}
