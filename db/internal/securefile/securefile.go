// Package securefile reads bounded configuration inputs without following
// symbolic links. Database credentials must enter through a secret file, never
// argv, logs, evidence, or an OCI layer.
package securefile

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const maxSecretBytes = 16 * 1024

// ReadSecret returns one trimmed secret value from a regular, non-symlink file.
func ReadSecret(path string) (string, error) {
	if path == "" {
		return "", errors.New("securefile: secret file path required")
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", fmt.Errorf("securefile: absolute path: %w", err)
	}
	info, err := os.Lstat(abs)
	if err != nil {
		return "", fmt.Errorf("securefile: lstat: %w", err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return "", errors.New("securefile: secret must be a regular non-symlink file")
	}
	if info.Size() < 1 || info.Size() > maxSecretBytes {
		return "", fmt.Errorf("securefile: secret bytes outside 1..%d", maxSecretBytes)
	}
	if info.Mode().Perm()&0o022 != 0 {
		return "", errors.New("securefile: group/world-writable secret rejected")
	}
	f, err := os.Open(abs)
	if err != nil {
		return "", fmt.Errorf("securefile: open: %w", err)
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, maxSecretBytes+1))
	if err != nil {
		return "", fmt.Errorf("securefile: read: %w", err)
	}
	value := strings.TrimSpace(string(raw))
	if value == "" || strings.ContainsAny(value, "\r\n\x00") {
		return "", errors.New("securefile: secret must be one non-empty line")
	}
	return value, nil
}
