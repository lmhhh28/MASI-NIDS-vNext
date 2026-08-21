package migrate

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

const (
	maxMigrationFiles = 4096
	maxMigrationBytes = 4 * 1024 * 1024
)

// File is one immutable migration in a contiguous chain.
type File struct {
	Version  int    `json:"version"`
	Name     string `json:"name"`
	Checksum string `json:"checksum"`
	Body     string `json:"-"`
}

// LoadChain validates regular/no-symlink files, contiguous names, bounded
// sizes, and exactly one outer transaction envelope. The chain digest is over
// ordered (file name, file digest) pairs rather than filesystem metadata.
func LoadChain(directory string) ([]File, string, error) {
	root, err := filepath.Abs(directory)
	if err != nil {
		return nil, "", fmt.Errorf("migration chain: absolute directory: %w", err)
	}
	rootInfo, err := os.Lstat(root)
	if err != nil {
		return nil, "", fmt.Errorf("migration chain: lstat directory: %w", err)
	}
	if rootInfo.Mode()&os.ModeSymlink != 0 || !rootInfo.IsDir() {
		return nil, "", errors.New("migration chain: directory must be a non-symlink directory")
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, "", fmt.Errorf("migration chain: read directory: %w", err)
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".sql") {
			names = append(names, entry.Name())
		}
	}
	sort.Strings(names)
	if len(names) < 1 || len(names) > maxMigrationFiles {
		return nil, "", fmt.Errorf("migration chain: file count outside 1..%d", maxMigrationFiles)
	}
	h := sha256.New()
	files := make([]File, 0, len(names))
	for index, name := range names {
		if len(name) < 10 || name[4] != '_' || filepath.Base(name) != name {
			return nil, "", fmt.Errorf("migration chain: malformed name %q", name)
		}
		version, err := strconv.Atoi(name[:4])
		if err != nil || version != index+1 {
			return nil, "", fmt.Errorf("migration chain: expected %04d, got %q", index+1, name)
		}
		path := filepath.Join(root, name)
		info, err := os.Lstat(path)
		if err != nil {
			return nil, "", fmt.Errorf("migration chain: lstat %s: %w", name, err)
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
			return nil, "", fmt.Errorf("migration chain: %s is not a regular non-symlink file", name)
		}
		if info.Size() < 1 || info.Size() > maxMigrationBytes {
			return nil, "", fmt.Errorf("migration chain: %s bytes outside 1..%d", name, maxMigrationBytes)
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, "", fmt.Errorf("migration chain: read %s: %w", name, err)
		}
		sum := sha256.Sum256(raw)
		checksum := "sha256:" + hex.EncodeToString(sum[:])
		body, err := MigrationBody(raw)
		if err != nil {
			return nil, "", fmt.Errorf("migration chain: %s: %w", name, err)
		}
		_, _ = fmt.Fprintf(h, "%s\t%s\n", name, checksum)
		files = append(files, File{Version: version, Name: name, Checksum: checksum, Body: body})
	}
	return files, "sha256:" + hex.EncodeToString(h.Sum(nil)), nil
}

// MigrationBody strips the one required outer BEGIN/COMMIT envelope. The
// runner owns the transaction so the schema change and immutable history row
// either commit together or both disappear.
func MigrationBody(raw []byte) (string, error) {
	lines := strings.Split(string(raw), "\n")
	begin, commit := -1, -1
	for index, line := range lines {
		if strings.EqualFold(strings.TrimSpace(line), "BEGIN;") {
			begin = index
			break
		}
	}
	for index := len(lines) - 1; index >= 0; index-- {
		if strings.EqualFold(strings.TrimSpace(lines[index]), "COMMIT;") {
			commit = index
			break
		}
	}
	if begin < 0 || commit <= begin {
		return "", errors.New("exactly one outer BEGIN/COMMIT envelope is required")
	}
	for index, line := range lines {
		normalized := strings.ToUpper(strings.TrimSpace(line))
		if (normalized == "BEGIN;" && index != begin) || (normalized == "COMMIT;" && index != commit) {
			return "", errors.New("nested or additional transaction envelope rejected")
		}
	}
	lines[begin], lines[commit] = "", ""
	return strings.Join(lines, "\n"), nil
}

// IsTestDatabase is deliberately simple and stable: destructive test targets
// must include the literal word test in their exact database name.
func IsTestDatabase(name string) bool {
	return strings.Contains(strings.ToLower(name), "test")
}
