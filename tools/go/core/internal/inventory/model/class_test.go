package model

import (
	"errors"
	"testing"
)

func TestAChildMayAnswerWhatItsParentLeftOpen(t *testing.T) {
	got, err := Class{Nature: "physical", Role: "consumable"}.Narrow(Class{Counting: "unit"})
	if err != nil {
		t.Fatal(err)
	}
	want := Class{Nature: "physical", Role: "consumable", Counting: "unit"}
	if got != want {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestAChildMayNotChangeAnAnswer(t *testing.T) {
	_, err := Class{Nature: "physical", Role: "consumable"}.Narrow(Class{Role: "durable"})
	var changed *ClassChanged
	if !errors.As(err, &changed) {
		t.Fatalf("got %v, want *ClassChanged", err)
	}
	if *changed != (ClassChanged{Key: "role", From: "consumable", To: "durable"}) {
		t.Fatalf("got %+v", *changed)
	}
}

func TestADurableIsCountedByUnit(t *testing.T) {
	err := Class{Nature: "physical", Role: "durable", Counting: "lot"}.Check()
	if !errors.Is(err, ErrDurableLot) {
		t.Fatalf("got %v, want ErrDurableLot", err)
	}
}

func TestCheckRefusesAKeyItsNatureDoesNotAsk(t *testing.T) {
	for _, c := range []Class{
		{Nature: "service", Role: "durable"},
		{Nature: "service", Counting: "unit"},
		{Nature: "physical", Billing: "term"},
		{Nature: "solid"},
		{Nature: "service", Billing: "monthly"},
	} {
		if err := c.Check(); err == nil {
			t.Errorf("%+v: Check passed", c)
		}
	}
}

func TestComplete(t *testing.T) {
	for c, want := range map[Class]bool{
		{Nature: "physical", Role: "consumable"}:                  false,
		{Nature: "physical", Role: "consumable", Counting: "lot"}: true,
		{Nature: "service"}:                  false,
		{Nature: "service", Billing: "flat"}: true,
		{}:                                   false,
	} {
		if got := c.Complete(); got != want {
			t.Errorf("%+v: Complete() = %v, want %v", c, got, want)
		}
	}
}

func TestAChainSetsOneSpecialType(t *testing.T) {
	ch := Chain{
		{ID: "consumable", Class: Class{Nature: "physical", Role: "consumable"}},
		{ID: "filament", Class: Class{Counting: "unit"}, Special: "filament_spool"},
		{ID: "pla"},
	}
	cls, special, err := ch.Class()
	if err != nil {
		t.Fatal(err)
	}
	if special != "filament_spool" || cls != (Class{Nature: "physical", Role: "consumable", Counting: "unit"}) {
		t.Fatalf("got %+v %q", cls, special)
	}
	ch = append(ch, Kind{ID: "odd", Special: "printer"})
	if _, _, err := ch.Class(); !errors.Is(err, ErrSecondSpecial) {
		t.Fatalf("got %v, want ErrSecondSpecial", err)
	}
}
