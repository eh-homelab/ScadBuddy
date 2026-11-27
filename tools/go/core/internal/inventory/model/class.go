// Package model is the inventory base model: the class a kind answers, the kind tree
// and the steps a kind, an item or a unit configures (design §3, §4.2).
package model

import (
	"errors"
	"fmt"
)

// Class answers what a thing is (design §3). "" is a key left open.
type Class struct {
	Nature, Role, Counting, Billing string
}

var (
	// ErrDurableLot: a durable thing is always counted by unit.
	ErrDurableLot = errors.New("a durable thing is counted by unit, not by lot")
	// ErrSecondSpecial: a chain of kinds names at most one special type.
	ErrSecondSpecial = errors.New("a kind sets a second special type")
	// ErrClassOpen: an item's kind has not answered every key its nature needs.
	ErrClassOpen = errors.New("the class is open")
)

// ClassChanged is a child kind that changes a key its parent set.
type ClassChanged struct {
	Key, From, To string
}

func (e *ClassChanged) Error() string {
	return fmt.Sprintf("a child kind changes %s from %s to %s", e.Key, e.From, e.To)
}

var values = map[string][]string{
	"nature":   {"physical", "service"},
	"role":     {"consumable", "durable"},
	"counting": {"lot", "unit"},
	"billing":  {"term", "hourly", "flat"},
}

// asked is the nature each key other than nature belongs to.
var asked = map[string]string{"role": "physical", "counting": "physical", "billing": "service"}

func (c Class) fields() []struct{ key, val string } {
	return []struct{ key, val string }{
		{"nature", c.Nature}, {"role", c.Role}, {"counting", c.Counting}, {"billing", c.Billing},
	}
}

func (c *Class) field(key string) *string {
	switch key {
	case "nature":
		return &c.Nature
	case "role":
		return &c.Role
	case "counting":
		return &c.Counting
	case "billing":
		return &c.Billing
	}
	return nil
}

// Get is the value of a class key by name ("" for an open key or an unknown name).
func (c Class) Get(key string) string {
	if p := c.field(key); p != nil {
		return *p
	}
	return ""
}

// Narrow sets the keys child sets. A child may answer a key c left open; it may never
// change one c set (*ClassChanged).
func (c Class) Narrow(child Class) (Class, error) {
	out := c
	for _, f := range child.fields() {
		if f.val == "" {
			continue
		}
		p := out.field(f.key)
		if *p != "" && *p != f.val {
			return c, &ClassChanged{Key: f.key, From: *p, To: f.val}
		}
		*p = f.val
	}
	return out, nil
}

// Matches says whether every key want sets has the same value in c.
func (c Class) Matches(want Class) bool {
	for _, f := range want.fields() {
		if f.val != "" && c.Get(f.key) != f.val {
			return false
		}
	}
	return true
}

// Complete says whether c answers every key its nature needs: a physical thing its role
// and counting, a service its billing.
func (c Class) Complete() bool {
	switch c.Nature {
	case "physical":
		return c.Role != "" && c.Counting != ""
	case "service":
		return c.Billing != ""
	}
	return false
}

// Check refuses a value out of range, a key the nature does not ask, and a durable lot.
func (c Class) Check() error {
	for _, f := range c.fields() {
		if f.val == "" {
			continue
		}
		ok := false
		for _, v := range values[f.key] {
			ok = ok || v == f.val
		}
		if !ok {
			return fmt.Errorf("%s %q is not one of %v", f.key, f.val, values[f.key])
		}
		if n := asked[f.key]; n != "" && c.Nature != "" && c.Nature != n {
			return fmt.Errorf("a %s thing has no %s", c.Nature, f.key)
		}
	}
	if c.Role == "durable" && c.Counting == "lot" {
		return ErrDurableLot
	}
	return nil
}
