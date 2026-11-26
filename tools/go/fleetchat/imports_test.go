package fleetchat

import (
	"os/exec"
	"strings"
	"testing"
)

// TestNoScadBuddyImports keeps the library host-neutral: WonderForge imports it
// too, so it may depend on nothing else from this repository.
func TestNoScadBuddyImports(t *testing.T) {
	out, err := exec.Command("go", "list", "-deps", "./...").CombinedOutput()
	if err != nil {
		t.Fatalf("go list -deps: %v\n%s", err, out)
	}
	const self = "github.com/eh-homelab/ScadBuddy/tools/go/fleetchat"
	for _, pkg := range strings.Fields(string(out)) {
		if strings.HasPrefix(pkg, "github.com/eh-homelab/ScadBuddy/") && pkg != self && !strings.HasPrefix(pkg, self+"/") {
			t.Errorf("fleetchat depends on %s", pkg)
		}
	}
}
