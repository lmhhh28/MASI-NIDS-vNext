package main

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/api"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/event"
	"masi-nids/control-go/internal/firewall"
	"masi-nids/control-go/internal/governance"
	"masi-nids/control-go/internal/pluginstat"
	"masi-nids/control-go/internal/ruleobs"
	"masi-nids/control-go/internal/security"
)

// startMaintenance runs the bounded Go-owned derivations. It creates no second
// queue: effect work is selected only from effect_intents, and statistics work
// remains in plugin_statistic_runs.
func startMaintenance(ctx context.Context, logger *slog.Logger, pool *db.Pool,
	dispatcher *governance.Dispatcher, reconcile *governance.ReconcileService,
	overlays *firewall.OverlayService, stats *pluginstat.Service, statsExecutor pluginstat.Executor,
	ingest *event.IngestService, rules *ruleobs.Projector, mapping *security.RoleScopeMapping,
	eventRetention, pluginStatRetention, idempotencyRetention time.Duration,
	statisticsOwner string, invalidations api.InvalidationPublisher, markProgress func()) {
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		lastRetention := time.Time{}
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				stepCtx, cancel := context.WithTimeout(ctx, time.Second)
				if _, err := dispatcher.FenceExpiredClaims(stepCtx, 4); err != nil {
					logger.Warn("effect expired claim fence", "err", err)
				}
				if _, err := dispatcher.RetryPendingAcknowledgements(stepCtx, 4); err != nil {
					logger.Warn("effect acknowledgement retry", "err", err)
				}
				var intentID string
				err := pool.Pool.QueryRow(stepCtx, `SELECT effect_intent_id FROM effect_intents
						WHERE NOT is_fleet_parent AND claim_state='unclaimed'
						  AND gate_open AND not_before_unix_ms <= $1 AND deadline_unix_ms > $1
						ORDER BY created_at LIMIT 1`, now.UnixMilli()).Scan(&intentID)
				if err == nil {
					if _, err := dispatcher.Dispatch(stepCtx, intentID); err != nil {
						logger.Warn("effect dispatch", "intent_id", intentID, "err", err)
					}
					publishIntentInvalidation(ctx, pool, invalidations, intentID)
				} else if !errors.Is(err, pgx.ErrNoRows) {
					logger.Warn("effect select", "err", err)
				}
				var unknownID string
				err = pool.Pool.QueryRow(stepCtx, `SELECT effect_intent_id FROM effect_intents
					WHERE claim_state='unknown' ORDER BY created_at LIMIT 1`).Scan(&unknownID)
				if err == nil {
					if _, err := reconcile.ReconcileUnknown(stepCtx, unknownID); err != nil {
						logger.Warn("effect reconcile", "intent_id", unknownID, "err", err)
					}
					publishIntentInvalidation(ctx, pool, invalidations, unknownID)
				} else if !errors.Is(err, pgx.ErrNoRows) {
					logger.Warn("reconcile select", "err", err)
				}
				if _, err := overlays.ExpireOverlays(stepCtx); err != nil {
					logger.Warn("overlay expiry sweep", "err", err)
				}
				if _, err := stats.ExpireStaleRunning(stepCtx); err != nil {
					logger.Warn("statistics lease sweep", "err", err)
				}
				runAuthorize := func(actor security.Actor, scope, dataClass, targetSetDigest string) bool {
					for _, level := range []security.AuthzContextLevel{security.LevelAnalyst, security.LevelOperator, security.LevelScopedOperator, security.LevelPlatformAdmin} {
						source, e1 := mapping.AuthorizeScope(actor, level, scope, "source-read", targetSetDigest)
						_, e2 := mapping.AuthorizeScope(actor, level, scope, "plugin.statistics.run", targetSetDigest)
						if e1 == nil && e2 == nil && (source.DataClass == "" || source.DataClass == dataClass) {
							return true
						}
					}
					return false
				}
				if _, err := stats.MaterializeDueSchedules(stepCtx, runAuthorize); err != nil {
					logger.Warn("statistics schedule materialize", "err", err)
				}
				if statsExecutor != nil {
					if _, err := stats.DispatchNext(stepCtx, statisticsOwner, statsExecutor, runAuthorize); err != nil {
						logger.Warn("statistics dispatch", "err", err)
					}
				}
				if lastRetention.IsZero() || now.Sub(lastRetention) >= time.Hour {
					if _, _, err := rules.SweepRetention(stepCtx, now.UnixMilli()); err != nil {
						logger.Warn("rule retention sweep", "err", err)
					}
					if _, err := api.SweepMutationIdempotency(stepCtx, pool, now, idempotencyRetention, 1000); err != nil {
						logger.Warn("API idempotency retention sweep", "err", err)
					}
					if _, _, err := ingest.SweepRetention(stepCtx, now, eventRetention, 1000); err != nil {
						logger.Warn("event retention sweep", "err", err)
					}
					if _, err := stats.SweepRetention(stepCtx, now, pluginStatRetention, 1000); err != nil {
						logger.Warn("statistics retention sweep", "err", err)
					}
					lastRetention = now
				}
				if markProgress != nil {
					markProgress()
				}
				cancel()
			}
		}
	}()
}

func publishIntentInvalidation(ctx context.Context, pool *db.Pool, invalidations api.InvalidationPublisher, intentID string) {
	if pool == nil || invalidations == nil || intentID == "" {
		return
	}
	queryCtx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	var scope, fleetID, operationID, effectKind string
	if err := pool.QueryRow(queryCtx, `SELECT p.scope,COALESCE(i.fleet_operation_id,''),i.operation_id,i.effect_kind
		FROM effect_intents i JOIN effect_proposals p ON p.proposal_id=i.proposal_id
		WHERE i.effect_intent_id=$1`, intentID).Scan(&scope, &fleetID, &operationID, &effectKind); err != nil {
		return
	}
	_ = invalidations.PublishScoped(api.ResIntent, intentID, scope, time.Now().UnixMilli(), 0)
	if fleetID != "" {
		_ = invalidations.PublishScoped(api.ResFleetOp, fleetID, scope, time.Now().UnixMilli(), 0)
	}
	if effectKind == string(governance.KindFirewallBaselineActivate) ||
		effectKind == string(governance.KindFirewallRollback) || effectKind == string(governance.KindFirewallOverlay) {
		_ = invalidations.PublishScoped(api.ResFirewallBind, operationID, scope, time.Now().UnixMilli(), 0)
	}
	if effectKind == string(governance.KindBoundedCapture) {
		var captureID string
		if pool.QueryRow(queryCtx, `SELECT capture_id FROM bounded_capture_requests WHERE effect_intent_id=$1`, intentID).Scan(&captureID) == nil {
			_ = invalidations.PublishScoped(api.ResCapture, captureID, scope, time.Now().UnixMilli(), 0)
		}
	}
}
