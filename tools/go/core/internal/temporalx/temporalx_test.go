package temporalx

import (
	"strings"
	"testing"
)

func TestTheConfigNeedsAnAddressAndHasItsDefaults(t *testing.T) {
	if _, err := FromEnv(func(string) string { return "" }); err == nil || !strings.Contains(err.Error(), "SCADBUDDY_TEMPORAL_ADDRESS") {
		t.Fatalf("got %v", err)
	}
	c, err := FromEnv(func(k string) string { return map[string]string{"SCADBUDDY_TEMPORAL_ADDRESS": "temporal:7233"}[k] })
	if err != nil || c != (Config{Address: "temporal:7233", Namespace: "scadbuddy", InventoryQueue: "inventory"}) {
		t.Fatalf("%+v %v", c, err)
	}
	c, err = FromEnv(func(k string) string {
		return map[string]string{"SCADBUDDY_TEMPORAL_ADDRESS": "t:1", "SCADBUDDY_TEMPORAL_NAMESPACE": "ns",
			"SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY": "q"}[k]
	})
	if err != nil || c != (Config{Address: "t:1", Namespace: "ns", InventoryQueue: "q"}) {
		t.Fatalf("%+v %v", c, err)
	}
	if _, err := Client(c, nil); err != nil {
		t.Fatal(err)
	}
}
