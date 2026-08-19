// Package boundary tests the Go Control Core module import boundary.
// A module may only depend on versioned public contracts (generated code),
// stdlib, and registered third-party dependencies — it must NOT import another
// module's internal source (AGENTS.md §"契约规则": 模块只能依赖版本化公开契约,
// 不得导入其他模块内部源码).
package boundary

import (
	"os/exec"
	"strings"
	"testing"
)

// forbiddenModuleDirs are other qualification modules whose internal source Go
// Control must never import. (edge-rs/infer-cpp/p4 are Rust/C++; they have no
// Go packages today, but the boundary is enforced defensively so a future Go
// helper in those dirs cannot be silently imported.)
var forbiddenModuleDirs = []string{
	"edge-rs/", "infer-cpp/", "p4/", "plugin-host-rs/", "analysis-py/",
	"web/", "ml-py/", "db/",
}

func TestNoCrossModuleInternalImports(t *testing.T) {
	out, err := exec.Command("go", "list", "-deps", "./...").CombinedOutput()
	if err != nil {
		t.Fatalf("go list -deps failed: %v\n%s", err, out)
	}
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		for _, dir := range forbiddenModuleDirs {
			if strings.Contains(line, dir) {
				t.Errorf("control-go imports forbidden cross-module path %q (matches %s)", line, dir)
			}
		}
	}
}

func TestModulePathIsControlGo(t *testing.T) {
	out, err := exec.Command("go", "list", "-m").CombinedOutput()
	if err != nil {
		t.Fatalf("go list -m failed: %v\n%s", err, out)
	}
	if strings.TrimSpace(string(out)) != "masi-nids/control-go" {
		t.Errorf("module path = %q, want masi-nids/control-go", strings.TrimSpace(string(out)))
	}
}
