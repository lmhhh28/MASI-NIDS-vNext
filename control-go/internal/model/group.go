package model

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
)

type RolloutGroupRequest struct {
	GroupID       string
	OrderedShards []string
	Template      RolloutRequest
}

type RolloutGroupOutcome struct {
	GroupID   string            `json:"group_id"`
	Status    string            `json:"status"`
	NextIndex int               `json:"next_index"`
	Shards    []GroupShardState `json:"shards"`
}

type GroupShardState struct {
	ShardIndex        int    `json:"shard_index"`
	ShardID           string `json:"shard_id"`
	OperationID       string `json:"operation_id"`
	Status            string `json:"status"`
	CurrentGeneration int64  `json:"current_generation,omitempty"`
	RouteEpoch        int64  `json:"route_epoch,omitempty"`
	ReasonCode        string `json:"reason_code"`
}

// CreateRolloutGroup freezes an ordered shard vector. It creates control facts
// only; no deployment/Edge call occurs in this transaction.
func (s *RolloutService) CreateRolloutGroup(ctx context.Context, req RolloutGroupRequest) (*RolloutGroupOutcome, error) {
	if req.GroupID == "" || len(req.GroupID) > 128 || len(req.OrderedShards) == 0 || len(req.OrderedShards) > 256 {
		return nil, errors.New("model: rollout group identity/shard count out of bounds")
	}
	seen := make(map[string]struct{}, len(req.OrderedShards))
	for _, shard := range req.OrderedShards {
		if shard == "" || len(shard) > 128 {
			return nil, errors.New("model: rollout group shard identity malformed")
		}
		if _, exists := seen[shard]; exists {
			return nil, errors.New("model: rollout group shard duplicated")
		}
		seen[shard] = struct{}{}
	}
	if req.Template.LogicalPoolID == "" || req.Template.TargetGeneration < 1 || req.Template.TargetRevisionID == "" ||
		req.Template.ModelControlIncarnationID == "" || req.Template.Scope == "" || req.Template.Kind != OpRollout {
		return nil, errors.New("model: rollout group template malformed")
	}
	requestDigest := rolloutGroupDigest(req)
	shardsJSON, _ := json.Marshal(req.OrderedShards)
	template := req.Template
	template.OperationID, template.ShardID, template.EdgeWorkloadRef = "", "", ""
	templateJSON, _ := json.Marshal(template)
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var eligible int
		if err := tx.QueryRow(ctx, `SELECT count(*) FROM shard_bindings WHERE shard_id=ANY($1)
			AND logical_pool_id=$2 AND model_control_incarnation_id=$3 AND scope=$4`, req.OrderedShards,
			req.Template.LogicalPoolID, req.Template.ModelControlIncarnationID, req.Template.Scope).Scan(&eligible); err != nil {
			return err
		}
		if eligible != len(req.OrderedShards) {
			return errors.New("model: rollout group shard vector drift/mismatch")
		}
		tag, err := tx.Exec(ctx, `INSERT INTO model_rollout_groups(group_id,request_digest,logical_pool_id,
			target_generation,target_revision_id,ordered_shards,rollout_template,status,next_index,model_control_incarnation_id,
			scope,actor_ref,reason_code,trace_id,created_at_unix_ms)
			VALUES($1,$2,$3,$4,$5,$6,$7,'planned',0,$8,$9,$10,'PLANNED',$11,$12)
			ON CONFLICT(group_id) DO NOTHING`, req.GroupID, requestDigest, req.Template.LogicalPoolID,
			req.Template.TargetGeneration, req.Template.TargetRevisionID, shardsJSON, templateJSON,
			req.Template.ModelControlIncarnationID, req.Template.Scope, req.Template.ActorRef,
			req.Template.TraceID, s.now().UnixMilli())
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			var existing string
			if err := tx.QueryRow(ctx, `SELECT request_digest FROM model_rollout_groups WHERE group_id=$1 FOR UPDATE`, req.GroupID).Scan(&existing); err != nil {
				return err
			}
			if existing != requestDigest {
				return errors.New("model: rollout group idempotency conflict")
			}
			return nil
		}
		for index, shard := range req.OrderedShards {
			operationID := groupOperationID(req.GroupID, shard)
			if _, err := tx.Exec(ctx, `INSERT INTO model_rollout_group_shards(group_id,shard_index,
				shard_id,operation_id,status,reason_code) VALUES($1,$2,$3,$4,'planned','PLANNED')`,
				req.GroupID, index, shard, operationID); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("model: create rollout group: %w", err)
	}
	return s.loadRolloutGroup(ctx, req.GroupID)
}

// AdvanceRolloutGroup executes shards strictly in the frozen order. The first
// non-applied child stops all later children; already-applied shards remain
// current and the durable group explicitly reports failed_mixed.
func (s *RolloutService) AdvanceRolloutGroup(ctx context.Context, groupID string) (*RolloutGroupOutcome, error) {
	var templateJSON []byte
	if err := s.pool.Pool.QueryRow(ctx, `SELECT rollout_template FROM model_rollout_groups WHERE group_id=$1`, groupID).Scan(&templateJSON); err != nil {
		return nil, err
	}
	var template RolloutRequest
	if err := json.Unmarshal(templateJSON, &template); err != nil {
		return nil, fmt.Errorf("model: parse rollout group template: %w", err)
	}
	for {
		group, err := s.loadRolloutGroup(ctx, groupID)
		if err != nil {
			return nil, err
		}
		if group.Status == "completed" || group.Status == "failed" || group.Status == "failed_mixed" {
			return group, nil
		}
		if group.NextIndex >= len(group.Shards) {
			return group, errors.New("model: rollout group cursor exceeds vector")
		}
		child := group.Shards[group.NextIndex]
		if child.Status != "planned" {
			return group, fmt.Errorf("model: rollout group child %s in unexpected state %s", child.ShardID, child.Status)
		}
		if _, err := s.pool.Pool.Exec(ctx, `UPDATE model_rollout_group_shards SET status='running',
			reason_code='RUNNING',updated_at=now() WHERE group_id=$1 AND shard_index=$2 AND status='planned'`,
			groupID, group.NextIndex); err != nil {
			return nil, err
		}
		if _, err := s.pool.Pool.Exec(ctx, `UPDATE model_rollout_groups SET status=CASE WHEN next_index=0
			THEN 'rolling' ELSE 'rolling_mixed' END,reason_code='ROLLING',updated_at=now()
			WHERE group_id=$1`, groupID); err != nil {
			return nil, err
		}
		req := template
		req.OperationID = child.OperationID
		req.ShardID = child.ShardID
		outcome, rolloutErr := s.Rollout(ctx, req)
		terminalStatus := "failed"
		reason := "ROLLOUT_ERROR"
		var generation, routeEpoch int64
		if outcome != nil {
			generation, routeEpoch, reason = outcome.NewGeneration, outcome.RouteEpoch, outcome.ReasonCode
			switch outcome.Status {
			case OpApplied:
				terminalStatus = "applied"
			case OpResumePending:
				terminalStatus = "resume_pending"
			}
		}
		if err := s.finishGroupChild(ctx, groupID, group.NextIndex, terminalStatus, reason, generation, routeEpoch); err != nil {
			return nil, err
		}
		if terminalStatus != "applied" {
			result, loadErr := s.loadRolloutGroup(ctx, groupID)
			if rolloutErr != nil {
				return result, rolloutErr
			}
			return result, loadErr
		}
	}
}

func (s *RolloutService) finishGroupChild(ctx context.Context, groupID string, index int, status, reason string, generation, routeEpoch int64) error {
	return s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE model_rollout_group_shards SET status=$1,current_generation=$2,
			route_epoch=$3,reason_code=$4,updated_at=now() WHERE group_id=$5 AND shard_index=$6 AND status='running'`,
			status, nullablePositive(generation), nullablePositive(routeEpoch), reason, groupID, index)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("model: rollout group child finalize CAS conflict")
		}
		if status == "applied" {
			var count int
			if err := tx.QueryRow(ctx, `SELECT count(*) FROM model_rollout_group_shards WHERE group_id=$1`, groupID).Scan(&count); err != nil {
				return err
			}
			next := index + 1
			groupStatus := "rolling_mixed"
			var finished any
			if next == count {
				groupStatus = "completed"
				finished = s.now().UnixMilli()
			}
			_, err = tx.Exec(ctx, `UPDATE model_rollout_groups SET next_index=$1,status=$2,
				reason_code=$3,finished_at_unix_ms=$4,updated_at=now() WHERE group_id=$5 AND next_index=$6`,
				next, groupStatus, stringsUpper(groupStatus), finished, groupID, index)
			return err
		}
		groupStatus := "failed"
		if index > 0 {
			groupStatus = "failed_mixed"
		}
		if _, err := tx.Exec(ctx, `UPDATE model_rollout_group_shards SET status='blocked',
			reason_code='BLOCKED_BY_PRIOR_FAILURE',updated_at=now() WHERE group_id=$1 AND shard_index>$2 AND status='planned'`,
			groupID, index); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE model_rollout_groups SET status=$1,reason_code=$2,
			finished_at_unix_ms=$3,updated_at=now() WHERE group_id=$4`, groupStatus, reason,
			s.now().UnixMilli(), groupID)
		return err
	})
}

func (s *RolloutService) loadRolloutGroup(ctx context.Context, groupID string) (*RolloutGroupOutcome, error) {
	var out RolloutGroupOutcome
	if err := s.pool.Pool.QueryRow(ctx, `SELECT group_id,status,next_index FROM model_rollout_groups WHERE group_id=$1`, groupID).
		Scan(&out.GroupID, &out.Status, &out.NextIndex); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, errors.New("model: rollout group not found")
		}
		return nil, err
	}
	rows, err := s.pool.Pool.Query(ctx, `SELECT shard_index,shard_id,operation_id,status,
		current_generation,route_epoch,reason_code FROM model_rollout_group_shards
		WHERE group_id=$1 ORDER BY shard_index`, groupID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var child GroupShardState
		var generation, routeEpoch *int64
		if err := rows.Scan(&child.ShardIndex, &child.ShardID, &child.OperationID, &child.Status,
			&generation, &routeEpoch, &child.ReasonCode); err != nil {
			return nil, err
		}
		if generation != nil {
			child.CurrentGeneration = *generation
		}
		if routeEpoch != nil {
			child.RouteEpoch = *routeEpoch
		}
		out.Shards = append(out.Shards, child)
	}
	return &out, rows.Err()
}

func rolloutGroupDigest(req RolloutGroupRequest) string {
	shards := append([]string(nil), req.OrderedShards...)
	// Preserve order in the digest and additionally bind the unordered set to
	// expose accidental membership drift independently of wave ordering.
	set := append([]string(nil), shards...)
	sort.Strings(set)
	template := req.Template
	template.OperationID, template.ShardID, template.EdgeWorkloadRef = "", "", ""
	templateJSON, _ := json.Marshal(template)
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%v|%v", req.GroupID, templateJSON, shards, set)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func groupOperationID(groupID, shardID string) string {
	sum := sha256.Sum256([]byte(groupID + "|" + shardID))
	return "mro-" + hex.EncodeToString(sum[:16])
}

func nullablePositive(value int64) any {
	if value < 1 {
		return nil
	}
	return value
}

func stringsUpper(value string) string {
	switch value {
	case "completed":
		return "COMPLETED"
	case "rolling_mixed":
		return "ROLLING_MIXED"
	default:
		return "ROLLING"
	}
}
