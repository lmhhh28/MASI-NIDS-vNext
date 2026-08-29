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

func TestHistoryChainDigestAllowsOnlyAContainingKnownPrefix(t *testing.T) {
	files := []File{
		{Version: 1, Name: "0001_first.sql", Checksum: "sha256:" + strings.Repeat("1", 64)},
		{Version: 2, Name: "0002_second.sql", Checksum: "sha256:" + strings.Repeat("2", 64)},
		{Version: 3, Name: "0003_third.sql", Checksum: "sha256:" + strings.Repeat("3", 64)},
	}
	prefixes := ChainPrefixDigests(files)
	if len(prefixes) != 3 {
		t.Fatalf("unexpected prefix count: %d", len(prefixes))
	}
	if !validHistoryChainDigest(files, files[0].Name, prefixes[0]) ||
		!validHistoryChainDigest(files, files[0].Name, prefixes[2]) ||
		!validHistoryChainDigest(files, files[1].Name, prefixes[1]) {
		t.Fatal("legitimate historical batch digest rejected")
	}
	if validHistoryChainDigest(files, files[1].Name, prefixes[0]) ||
		validHistoryChainDigest(files, files[2].Name, "sha256:"+strings.Repeat("a", 64)) {
		t.Fatal("history digest not containing the row or not in the known chain accepted")
	}
}

func BenchmarkLoadChain(b *testing.B) {
	directory := filepath.Join("..", "..", "migrations")
	b.ReportAllocs()
	for b.Loop() {
		files, digest, err := LoadChain(directory)
		if err != nil || len(files) != 31 || digest == "" {
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
