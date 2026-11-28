package activities

import (
	"slices"
	"testing"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
)

// names are the 19 activities of K1 (plan Task 12), by name.
var names = []string{
	"inventory.KindCreate", "inventory.KindUpdate", "inventory.KindArchive", "inventory.KindRestore",
	"inventory.ItemCreate", "inventory.ItemUpdate", "inventory.ItemArchive", "inventory.ItemRestore",
	"inventory.ReceiveLot", "inventory.ReceiveUnits", "inventory.LotUpdate", "inventory.LotAdjust",
	"inventory.Take", "inventory.Count", "inventory.UnitUpdate", "inventory.UnitStatus",
	"inventory.GiveBack", "inventory.Recost", "inventory.Repoint",
}

// registry records what Register registers.
type registry struct{ names []string }

func (r *registry) RegisterActivity(any) {}
func (r *registry) RegisterActivityWithOptions(_ any, o activity.RegisterOptions) {
	r.names = append(r.names, o.Name)
}
func (r *registry) RegisterDynamicActivity(any, activity.DynamicRegisterOptions) {}

func TestEveryActivityIsRegisteredByName(t *testing.T) {
	// A real worker over a lazy client: Register must not panic (the SDK panics on a
	// method whose shape it cannot run).
	c, err := client.NewLazyClient(client.Options{HostPort: "127.0.0.1:1"})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	Register(worker.New(c, "inventory", worker.Options{}), &Activities{})

	r := &registry{}
	Register(r, &Activities{})
	got := slices.Clone(r.names)
	want := slices.Clone(names)
	slices.Sort(got)
	slices.Sort(want)
	if !slices.Equal(got, want) {
		t.Fatalf("registered %v\nwant %v", got, want)
	}
}
