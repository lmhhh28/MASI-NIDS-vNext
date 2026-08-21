package migrate

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadChainDeterministic(t *testing.T) {
	root := t.TempDir()
	write := func(name, body string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(root, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("0001_first.sql", "BEGIN;\nCREATE TABLE first(id int);\nCOMMIT;\n")
	write("0002_second.sql", "BEGIN;\nCREATE TABLE second(id int);\nCOMMIT;\n")
	files, digest1, err := LoadChain(root)
	if err != nil {
		t.Fatal(err)
	}
	_, digest2, err := LoadChain(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 2 || files[0].Version != 1 || files[1].Version != 2 {
		t.Fatalf("unexpected chain: %+v", files)
	}
	if digest1 != digest2 || !strings.HasPrefix(digest1, "sha256:") {
		t.Fatalf("non-deterministic digest %q %q", digest1, digest2)
	}
}

func BenchmarkLoadChain(b *testing.B) {
	directory := filepath.Join("..", "..", "migrations")
	b.ReportAllocs()
	for b.Loop() {
		files, digest, err := LoadChain(directory)
		if err != nil || len(files) != 29 || digest == "" {
			b.Fatalf("LoadChain: files=%d digest=%q err=%v", len(files), digest, err)
		}
	}
}

func TestLoadChainRejectsGapSymlinkAndExtraTransaction(t *testing.T) {
	t.Run("gap", func(t *testing.T) {
		root := t.TempDir()
		if err := os.WriteFile(filepath.Join(root, "0002_gap.sql"), []byte("BEGIN;\nSELECT 1;\nCOMMIT;\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, _, err := LoadChain(root); err == nil {
			t.Fatal("gap accepted")
		}
	})
	t.Run("symlink", func(t *testing.T) {
		root := t.TempDir()
		target := filepath.Join(root, "target")
		if err := os.WriteFile(target, []byte("BEGIN;\nSELECT 1;\nCOMMIT;\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(target, filepath.Join(root, "0001_link.sql")); err != nil {
			t.Fatal(err)
		}
		if _, _, err := LoadChain(root); err == nil {
			t.Fatal("symlink accepted")
		}
	})
	t.Run("nested", func(t *testing.T) {
		if _, err := MigrationBody([]byte("BEGIN;\nBEGIN;\nSELECT 1;\nCOMMIT;\nCOMMIT;\n")); err == nil {
			t.Fatal("nested envelope accepted")
		}
	})
}

func TestIsTestDatabase(t *testing.T) {
	for _, name := range []string{"masi_state_test", "TEST_restore_1", "a-test-b"} {
		if !IsTestDatabase(name) {
			t.Fatalf("test database rejected: %s", name)
		}
	}
	for _, name := range []string{"masi_state", "production", ""} {
		if IsTestDatabase(name) {
			t.Fatalf("non-test database accepted: %s", name)
		}
	}
}
