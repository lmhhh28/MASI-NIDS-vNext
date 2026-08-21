package securefile

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadSecret(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "dsn")
	if err := os.WriteFile(path, []byte("postgres://example/test\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	value, err := ReadSecret(path)
	if err != nil || value != "postgres://example/test" {
		t.Fatalf("value=%q err=%v", value, err)
	}
}

func TestReadSecretRejectsSymlinkAndWritableFile(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "target")
	if err := os.WriteFile(target, []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "link")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadSecret(link); err == nil {
		t.Fatal("symlink secret accepted")
	}
	if err := os.Chmod(target, 0o622); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadSecret(target); err == nil {
		t.Fatal("group/world-writable secret accepted")
	}
}
