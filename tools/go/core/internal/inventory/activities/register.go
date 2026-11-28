package activities

import (
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
)

// Register registers each activity of a by its name, inventory.<Name>, one by one, so a
// method added to Activities is never registered by accident.
func Register(w worker.ActivityRegistry, a *Activities) {
	for name, fn := range map[string]any{
		"inventory.KindCreate":   a.KindCreate,
		"inventory.KindUpdate":   a.KindUpdate,
		"inventory.KindArchive":  a.KindArchive,
		"inventory.KindRestore":  a.KindRestore,
		"inventory.ItemCreate":   a.ItemCreate,
		"inventory.ItemUpdate":   a.ItemUpdate,
		"inventory.ItemArchive":  a.ItemArchive,
		"inventory.ItemRestore":  a.ItemRestore,
		"inventory.ReceiveLot":   a.ReceiveLot,
		"inventory.ReceiveUnits": a.ReceiveUnits,
		"inventory.LotUpdate":    a.LotUpdate,
		"inventory.LotAdjust":    a.LotAdjust,
		"inventory.Take":         a.Take,
		"inventory.Count":        a.Count,
		"inventory.UnitUpdate":   a.UnitUpdate,
		"inventory.UnitStatus":   a.UnitStatus,
		"inventory.GiveBack":     a.GiveBack,
		"inventory.Recost":       a.Recost,
		"inventory.Repoint":      a.Repoint,
	} {
		w.RegisterActivityWithOptions(fn, activity.RegisterOptions{Name: name})
	}
}
