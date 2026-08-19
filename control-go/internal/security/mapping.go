package security

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
)

// LoadRoleScopeMapping loads the immutable deployment mapping and verifies its
// declared digest against the independently pinned configuration value. Mapping
// changes therefore require an explicit deployment/config revision.
func LoadRoleScopeMapping(path, pinnedDigest string) (*RoleScopeMapping, error) {
	if path == "" || !digestRE.MatchString(pinnedDigest) {
		return nil, fmt.Errorf("security: role mapping path/pinned digest required")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("security: read role mapping: %w", err)
	}
	if len(raw) > 1<<20 {
		return nil, fmt.Errorf("security: role mapping exceeds 1 MiB")
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	var mapping RoleScopeMapping
	if err := dec.Decode(&mapping); err != nil {
		return nil, fmt.Errorf("security: parse role mapping: %w", err)
	}
	if err := dec.Decode(&struct{}{}); err != io.EOF {
		return nil, fmt.Errorf("security: trailing role mapping content")
	}
	if err := mapping.Validate(); err != nil {
		return nil, err
	}
	if mapping.Digest != pinnedDigest {
		return nil, fmt.Errorf("security: role mapping digest %s != pinned %s", mapping.Digest, pinnedDigest)
	}
	computed, err := RoleMappingContentDigest(mapping)
	if err != nil {
		return nil, err
	}
	if computed != pinnedDigest {
		return nil, fmt.Errorf("security: role mapping content digest %s != pinned %s", computed, pinnedDigest)
	}
	return &mapping, nil
}

func RoleMappingContentDigest(mapping RoleScopeMapping) (string, error) {
	canonical := mapping
	canonical.Digest = ""
	content, err := json.Marshal(canonical)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(content)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}
