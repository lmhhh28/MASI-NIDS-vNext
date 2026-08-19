package plugin

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// BindingService manages plugin activation/binding generation, drain, revoke,
// and rollback. Activation is append-only (a new binding_generation per
// change); only one active binding per plugin; an active binding must be
// qualified. A revoked binding cannot re-activate without a new qualification.
// Rollback switches the active pointer to a still-qualified previous binding;
// it does NOT overwrite artifact/config/manifest/historical result/audit.
type BindingService struct {
	pool *db.Pool
}

func NewBindingService(pool *db.Pool) *BindingService {
	return &BindingService{pool: pool}
}

// Activate binds a qualified manifest revision as the active binding. An
// unqualified/hold manifest, a revoked plugin, or a capability expansion is
// rejected. The previous active binding (if any) is moved to drain.
func (s *BindingService) Activate(ctx context.Context, b Binding, actor security.Actor, traceID string) (*Binding, error) {
	if err := validateBinding(b); err != nil {
		return nil, err
	}
	if b.QualificationStatus != Qualified {
		return nil, fmt.Errorf("plugin: cannot activate unqualified/%q manifest (active requires qualified)", b.QualificationStatus)
	}
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		// Confirm the manifest is qualified (re-validate inside the txn).
		var qualStatus, manifestDigest, manifestScope string
		var qualifiedAt time.Time
		var capabilities, resourceLimits []byte
		err := tx.QueryRow(ctx, `
					SELECT q.qualification_status,m.manifest_digest,m.scope,m.capabilities,m.resource_limits,q.created_at
					FROM plugin_qualifications q JOIN plugin_manifests m
					  ON m.plugin_id=q.plugin_id AND m.manifest_id=q.manifest_id AND m.manifest_revision=q.manifest_revision
					WHERE q.plugin_id = $1 AND q.manifest_id = $2 AND q.manifest_revision = $3
					ORDER BY q.created_at DESC LIMIT 1`, b.PluginID, b.ManifestID, b.ManifestRevision).
			Scan(&qualStatus, &manifestDigest, &manifestScope, &capabilities, &resourceLimits, &qualifiedAt)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("plugin: no qualification for %s/%d", b.ManifestID, b.ManifestRevision)
			}
			return err
		}
		if qualStatus != string(Qualified) {
			return fmt.Errorf("plugin: manifest qualification is %s (not qualified)", qualStatus)
		}
		if manifestDigest != b.ManifestDigest || manifestScope != b.Scope {
			return errors.New("plugin: exact manifest digest/scope mismatch")
		}
		if digestBytes(capabilities) != b.CapabilityDigest || digestBytes(resourceLimits) != b.ResourceProfileDigest {
			return errors.New("plugin: capability/resource digest does not bind exact manifest")
		}
		var lastRevokedAt *time.Time
		if err := tx.QueryRow(ctx, `
				SELECT MAX(a.created_at)
				FROM plugin_audit_events a
				JOIN plugin_bindings prior
				  ON prior.plugin_id=a.plugin_id AND prior.binding_generation=a.binding_generation
				WHERE a.plugin_id=$1 AND a.action='binding.revoked'
				  AND prior.manifest_id=$2 AND prior.manifest_revision=$3
				  AND prior.manifest_digest=$4`,
			b.PluginID, b.ManifestID, b.ManifestRevision, b.ManifestDigest).Scan(&lastRevokedAt); err != nil {
			return err
		}
		if lastRevokedAt != nil && !qualifiedAt.After(*lastRevokedAt) {
			return errors.New("plugin: revoked manifest requires a newer qualification before reactivation")
		}
		var nextGeneration int
		if err := tx.QueryRow(ctx, `SELECT COALESCE(MAX(binding_generation),0)+1 FROM plugin_bindings WHERE plugin_id=$1`, b.PluginID).Scan(&nextGeneration); err != nil {
			return err
		}
		if b.BindingGeneration != nextGeneration {
			return errors.New("plugin: binding generation must be exact next generation")
		}
		// Drain any current active binding for this plugin (one active at a time).
		_, err = tx.Exec(ctx, `
			UPDATE plugin_bindings SET activation_state = 'drain'
			WHERE plugin_id = $1 AND activation_state = 'active'`, b.PluginID)
		if err != nil {
			return err
		}
		// Append the new active binding.
		_, err = tx.Exec(ctx, `
			INSERT INTO plugin_bindings (
				plugin_id, binding_generation, manifest_id, manifest_revision,
				manifest_digest, config_digest, capability_digest,
				resource_profile_digest, activation_state, qualification_status,
					actor_ref, trace_id, reason_code,scope)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active','qualified',$9,$10,'ACTIVE',$11)`,
			b.PluginID, b.BindingGeneration, b.ManifestID, b.ManifestRevision,
			b.ManifestDigest, b.ConfigDigest, b.CapabilityDigest,
			b.ResourceProfileDigest, actor.String(), traceID, b.Scope)
		if err != nil {
			return fmt.Errorf("plugin: activate binding (one active per plugin): %w", err)
		}
		return appendAudit(ctx, tx, b.PluginID, &b.BindingGeneration, "binding.activate", b.Scope, actor, "ACTIVE", traceID)
	})
	if err != nil {
		return nil, err
	}
	b.ActivationState = StateActive
	b.QualificationStatus = Qualified
	return &b, nil
}

// Drain marks the current active binding as draining (no new tasks; in-flight
// completes bounded). Does NOT revoke.
func (s *BindingService) Drain(ctx context.Context, pluginID string, actor security.Actor, traceID string) error {
	return s.setState(ctx, pluginID, StateDrain, "DRAIN", actor, traceID)
}

// Revoke irrevocably marks the binding revoked. A revoked binding cannot
// re-activate without a new qualification (rollback to a revoked binding is
// rejected).
func (s *BindingService) Revoke(ctx context.Context, pluginID string, actor security.Actor, traceID string) error {
	return s.setState(ctx, pluginID, StateRevoked, "REVOKED", actor, traceID)
}

// Rollback switches the active binding to a still-qualified, unrevoked previous
// binding generation. It does NOT overwrite artifact/config/manifest/historical
// result/audit (a new binding_generation row is appended pointing at the
// previous manifest digest).
func (s *BindingService) Rollback(ctx context.Context, pluginID string, previousBindingGeneration int, actor security.Actor, traceID string) error {
	if previousBindingGeneration < 1 {
		return errors.New("plugin: rollback requires a previous binding_generation >= 1")
	}
	return s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var prevManifestID, prevDigest, prevConfig, prevCap, prevResource, prevQual, prevScope string
		var prevManifestRevision int
		var prevState ActivationState
		err := tx.QueryRow(ctx, `
					SELECT b.manifest_id,b.manifest_revision,b.manifest_digest,b.config_digest,b.capability_digest,
					       b.resource_profile_digest,q.qualification_status,b.activation_state,b.scope
				FROM plugin_bindings b JOIN LATERAL(SELECT qualification_status,manifest_digest FROM plugin_qualifications q
				 WHERE q.plugin_id=b.plugin_id AND q.manifest_id=b.manifest_id AND q.manifest_revision=b.manifest_revision
				 ORDER BY q.created_at DESC LIMIT 1)q ON q.manifest_digest=b.manifest_digest
				WHERE b.plugin_id = $1 AND b.binding_generation = $2 FOR UPDATE`, pluginID, previousBindingGeneration).
			Scan(&prevManifestID, &prevManifestRevision, &prevDigest, &prevConfig, &prevCap, &prevResource, &prevQual, &prevState, &prevScope)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("plugin: previous binding %d not found", previousBindingGeneration)
			}
			return err
		}
		if prevState == StateRevoked {
			return errors.New("plugin: cannot rollback to a revoked binding")
		}
		if prevQual != string(Qualified) {
			return errors.New("plugin: cannot rollback to an unqualified binding")
		}
		// Drain current active.
		_, err = tx.Exec(ctx, `
			UPDATE plugin_bindings SET activation_state = 'drain'
			WHERE plugin_id = $1 AND activation_state = 'active'`, pluginID)
		if err != nil {
			return err
		}
		// Append a new binding_generation pointing at the exact previous manifest.
		var nextGen int
		err = tx.QueryRow(ctx, `
			SELECT COALESCE(MAX(binding_generation), 0) + 1 FROM plugin_bindings WHERE plugin_id = $1`,
			pluginID).Scan(&nextGen)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `
			INSERT INTO plugin_bindings (
				plugin_id, binding_generation, manifest_id, manifest_revision,
				manifest_digest, config_digest, capability_digest,
				resource_profile_digest, activation_state, qualification_status,
					actor_ref, trace_id, reason_code,scope)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active','qualified',$9,$10,'ROLLBACK_TO_EXACT_PREVIOUS',$11)`,
			pluginID, nextGen, prevManifestID, prevManifestRevision, prevDigest, prevConfig, prevCap, prevResource,
			actor.String(), traceID, prevScope)
		if err != nil {
			return fmt.Errorf("plugin: rollback append: %w", err)
		}
		return appendAudit(ctx, tx, pluginID, &nextGen, "binding.rollback", prevScope, actor, "ROLLBACK_TO_EXACT_PREVIOUS", traceID)
	})
}

func digestBytes(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func (s *BindingService) setState(ctx context.Context, pluginID string, state ActivationState, reason string, actor security.Actor, traceID string) error {
	return s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var generation int
		var scope, oldState string
		allowedStates := []string{string(StateActive)}
		if state == StateRevoked {
			allowedStates = append(allowedStates, string(StateDrain))
		}
		err := tx.QueryRow(ctx, `
			SELECT binding_generation,scope,activation_state
			FROM plugin_bindings
			WHERE plugin_id=$1 AND activation_state=ANY($2::text[])
			ORDER BY binding_generation DESC LIMIT 1 FOR UPDATE`, pluginID, allowedStates).
			Scan(&generation, &scope, &oldState)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("plugin: %s: no eligible binding for %s", reason, pluginID)
			}
			return fmt.Errorf("plugin: %s: %w", reason, err)
		}
		tag, err := tx.Exec(ctx, `
			UPDATE plugin_bindings SET activation_state=$1, reason_code=$2, actor_ref=$3, trace_id=$4
			WHERE plugin_id=$5 AND binding_generation=$6 AND activation_state=$7`,
			string(state), reason, actor.String(), traceID, pluginID, generation, oldState)
		if err != nil {
			return fmt.Errorf("plugin: %s: %w", reason, err)
		}
		if tag.RowsAffected() != 1 {
			return fmt.Errorf("plugin: %s: binding state changed concurrently", reason)
		}
		return appendAudit(ctx, tx, pluginID, &generation, "binding."+string(state), scope, actor, reason, traceID)
	})
}

func validateBinding(b Binding) error {
	if b.PluginID == "" || b.BindingGeneration < 1 {
		return errors.New("plugin: plugin_id and binding_generation >= 1 required")
	}
	if !digestRE.MatchString(b.ManifestDigest) {
		return errors.New("plugin: manifest_digest malformed")
	}
	if !digestRE.MatchString(b.ConfigDigest) || !digestRE.MatchString(b.CapabilityDigest) || !digestRE.MatchString(b.ResourceProfileDigest) {
		return errors.New("plugin: config/capability digest malformed")
	}
	if b.ManifestID == "" || b.ManifestRevision < 1 || b.Scope == "" {
		return errors.New("plugin: manifest identity/scope required")
	}
	return nil
}
