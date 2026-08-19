package plugin

import (
	"context"
	"errors"
	"fmt"
	"time"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// appendAudit records the exact OIDC subject for every successful plugin
// lifecycle mutation. It must be called in the same transaction as the fact it
// describes so a catalog/binding change can never commit without its audit row.
func appendAudit(
	ctx context.Context,
	tx *db.Tx,
	pluginID string,
	bindingGeneration *int,
	action, scope string,
	actor security.Actor,
	reasonCode, traceID string,
) error {
	if err := actor.Validate(); err != nil {
		return err
	}
	if pluginID == "" || action == "" || scope == "" || reasonCode == "" || traceID == "" {
		return errors.New("plugin: audit identity/action/scope/reason/trace required")
	}
	var generation any
	if bindingGeneration != nil {
		generation = *bindingGeneration
	}
	auditID := "audit-" + shortID(fmt.Sprintf("%s|%v|%s|%s|%s|%d", pluginID, generation, action, actor.String(), traceID, time.Now().UnixNano()))
	_, err := tx.Exec(ctx, `
		INSERT INTO plugin_audit_events (
			audit_id,plugin_id,binding_generation,action,scope,
			actor_ref,actor_issuer,actor_subject,reason_code,trace_id)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
		auditID, pluginID, generation, action, scope,
		actor.String(), actor.Issuer, actor.Subject, reasonCode, traceID)
	if err != nil {
		return fmt.Errorf("plugin: append audit: %w", err)
	}
	return nil
}
