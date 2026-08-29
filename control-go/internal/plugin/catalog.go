package plugin

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

type nonZeroDigestPattern struct{}

func (nonZeroDigestPattern) MatchString(value string) bool { return security.ValidDigest(value) }

var digestRE nonZeroDigestPattern

// CatalogService registers immutable manifest revisions and qualifies them.
// A manifest is immutable (append-only revision); a binding references the
// exact manifest digest, never a mutable alias (ADR-0018).
type CatalogService struct {
	pool *db.Pool
}

func NewCatalogService(pool *db.Pool) *CatalogService {
	return &CatalogService{pool: pool}
}

// Register validates and persists an immutable manifest revision. Unknown kind,
// wrong digest/publisher, or rejected signature are rejected before insert.
func (s *CatalogService) Register(ctx context.Context, m Manifest, actor security.Actor, traceID string) (*Manifest, error) {
	if err := validateManifest(m); err != nil {
		return nil, err
	}
	m.ManifestDigest = computeManifestDigest(m)
	capsJSON, _ := json.Marshal(m.Capabilities)
	limitsJSON, _ := json.Marshal(m.ResourceLimits)
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		_, err := tx.Exec(ctx, `
			INSERT INTO plugin_manifests (
				manifest_id, manifest_revision, manifest_digest, plugin_id, kind,
				publisher, version, capabilities, resource_limits, runtime_profile,
				wit_digest, service_proto_digest, sbom_digest, provenance_digest,
				signature_status, actor_ref, trace_id, scope)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)`,
			m.ManifestID, m.ManifestRevision, m.ManifestDigest, m.PluginID, string(m.Kind),
			m.Publisher, m.Version, capsJSON, limitsJSON, string(m.RuntimeProfile),
			nullable(m.WitDigest), nullable(m.ServiceProtoDigest), m.SBOMDigest, m.ProvenanceDigest,
			m.SignatureStatus, actor.String(), traceID, m.Scope)
		if err != nil {
			return err
		}
		return appendAudit(ctx, tx, m.PluginID, nil, "manifest.register", m.Scope, actor, "MANIFEST_REGISTERED", traceID)
	})
	if err != nil {
		return nil, fmt.Errorf("plugin: register manifest: %w", err)
	}
	return &m, nil
}

// Qualify records the qualification outcome for a manifest revision. An
// unqualified/hold manifest cannot be activated.
func (s *CatalogService) Qualify(ctx context.Context, pluginID, manifestID string, manifestRevision int, status QualificationStatus, actor security.Actor, traceID string) error {
	if status != Qualified && status != Unqualified && status != Hold {
		return fmt.Errorf("plugin: bad qualification status %q", status)
	}
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		// Load the manifest and scope in the same transaction as the immutable
		// qualification and audit facts.
		var manifestDigest, scope string
		err := tx.QueryRow(ctx, `
			SELECT manifest_digest,scope FROM plugin_manifests
			WHERE plugin_id=$1 AND manifest_id=$2 AND manifest_revision=$3
			FOR SHARE`, pluginID, manifestID, manifestRevision).Scan(&manifestDigest, &scope)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("plugin: manifest %s/%d not found", manifestID, manifestRevision)
			}
			return fmt.Errorf("plugin: load manifest: %w", err)
		}
		now := time.Now().UTC()
		qualID := "qual-" + shortID(fmt.Sprintf("%s|%s|%d", manifestDigest, status, now.UnixNano()))
		_, err = tx.Exec(ctx, `
			INSERT INTO plugin_qualifications (
				qualification_id, plugin_id, manifest_id, manifest_revision,
				manifest_digest, qualification_status, qualification_digest,
				qualified_at, actor_ref, trace_id, reason_code)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`,
			qualID, pluginID, manifestID, manifestRevision, manifestDigest, string(status),
			computeQualificationDigest(manifestDigest, status), now, actor.String(), traceID,
			reasonForQualification(status))
		if err != nil {
			return err
		}
		return appendAudit(ctx, tx, pluginID, nil, "manifest.qualify", scope, actor, reasonForQualification(status), traceID)
	})
	if err != nil {
		return fmt.Errorf("plugin: record qualification: %w", err)
	}
	return nil
}

func validateManifest(m Manifest) error {
	if !m.Kind.IsClosed() {
		return fmt.Errorf("plugin: unknown kind %q (closed: analysis-agent|read-only-tool|pure-transform)", m.Kind)
	}
	if m.ManifestID == "" || m.PluginID == "" || m.Publisher == "" {
		return errors.New("plugin: manifest_id, plugin_id, publisher required")
	}
	if m.ManifestRevision < 1 {
		return errors.New("plugin: manifest_revision must be >= 1")
	}
	if m.SignatureStatus != "signed" {
		return errors.New("plugin: only a verified signed manifest may be registered")
	}
	if !digestRE.MatchString(m.SBOMDigest) || !digestRE.MatchString(m.ProvenanceDigest) {
		return errors.New("plugin: sbom/provenance digest malformed")
	}
	if m.RuntimeProfile == RuntimeWasmComponent && m.WitDigest == "" {
		return errors.New("plugin: wasm-component runtime requires wit_digest")
	}
	if m.RuntimeProfile == RuntimeGRPCService && m.ServiceProtoDigest == "" {
		return errors.New("plugin: grpc-service runtime requires service_proto_digest")
	}
	if m.RuntimeProfile != RuntimeWasmComponent && m.RuntimeProfile != RuntimeGRPCService {
		return errors.New("plugin: unknown runtime profile")
	}
	if m.Scope == "" {
		return errors.New("plugin: scope required")
	}
	if len(m.Capabilities) > 128 {
		return errors.New("plugin: too many capabilities")
	}
	seenCaps := map[string]struct{}{}
	for _, capability := range m.Capabilities {
		if capability.CapabilityID == "" || capability.CapabilityKind == "" || !capability.Declared {
			return errors.New("plugin: capability must be named and explicitly declared")
		}
		if _, ok := seenCaps[capability.CapabilityID]; ok {
			return errors.New("plugin: duplicate capability id")
		}
		seenCaps[capability.CapabilityID] = struct{}{}
	}
	l := m.ResourceLimits
	if l.CPUMilli < 1 || l.MemoryBytes < 1 || l.PIDCount < 1 || l.FDCount < 1 || l.DiskBytes < 0 ||
		l.DeadlineMS < 1 || l.DeadlineMS > 600000 || l.OutputBytes < 1 || l.OutputBytes > 4*1024*1024 ||
		l.QueueDepth < 1 || l.QueueDepth > 1024 {
		return errors.New("plugin: resource limits invalid or unbounded")
	}
	return nil
}

func computeManifestDigest(m Manifest) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%d|%s|%s|%s|%s", m.ManifestID, m.ManifestRevision, m.PluginID,
		m.Kind, m.Publisher, m.Version)
	caps, _ := json.Marshal(m.Capabilities)
	h.Write(caps)
	limits, _ := json.Marshal(m.ResourceLimits)
	h.Write(limits)
	fmt.Fprintf(h, "|%s|%s|%s|%s|%s|%s|%s", m.RuntimeProfile, m.WitDigest, m.ServiceProtoDigest,
		m.SBOMDigest, m.ProvenanceDigest, m.SignatureStatus, m.Scope)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func computeQualificationDigest(manifestDigest string, status QualificationStatus) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s", manifestDigest, status)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func reasonForQualification(s QualificationStatus) string {
	switch s {
	case Qualified:
		return "QUALIFIED"
	case Unqualified:
		return "UNQUALIFIED"
	case Hold:
		return "HOLD"
	}
	return "UNKNOWN"
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func shortID(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:16])
}
