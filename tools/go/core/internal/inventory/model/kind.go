package model

import (
	"encoding/json"
	"time"
)

// StepConf is one layer's setting for one step: another handler, its parameters, or both
// (design §4.2).
type StepConf struct {
	Handler string         `json:"handler,omitempty"`
	Params  map[string]any `json:"params,omitempty"`
}

// Steps is a layer's settings, keyed by step.
type Steps map[string]StepConf

// Kind is one node of the kind tree (design §3).
type Kind struct {
	ID, Name, ParentID, Special, UOM string
	Class                            Class
	Steps                            Steps
	Schema                           json.RawMessage
	Saleable, Builtin                bool
	ArchivedAt                       *time.Time
}

// Chain is a kind and its ancestors, root first.
type Chain []Kind

// Class narrows the chain's classes root to leaf and checks the result. It also returns
// the chain's special type; a second one is ErrSecondSpecial.
func (ch Chain) Class() (Class, string, error) {
	var cls Class
	special := ""
	for _, k := range ch {
		var err error
		if cls, err = cls.Narrow(k.Class); err != nil {
			return Class{}, "", err
		}
		if k.Special != "" {
			if special != "" {
				return Class{}, "", ErrSecondSpecial
			}
			special = k.Special
		}
	}
	if err := cls.Check(); err != nil {
		return Class{}, "", err
	}
	return cls, special, nil
}
