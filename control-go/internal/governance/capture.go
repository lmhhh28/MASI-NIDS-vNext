package governance

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

const (
	boundedCaptureSchema  = "bounded-capture/v1"
	boundedCaptureAdapter = "bounded-flow-evidence/v1"
	boundedCaptureMode    = "flow-metadata-only"
)

// CaptureService owns immutable bounded-capture request facts. It never claims
// work: execution is exclusively represented by the linked effect_intent.
type CaptureService struct {
	pool *db.Pool
	now  func() time.Time
}

func NewCaptureService(pool *db.Pool) *CaptureService {
	return &CaptureService{pool: pool, now: time.Now}
}

// Register freezes one exact, metadata-only capture envelope before a Proposal
// can reference its digest. It has no external side effect.
func (s *CaptureService) Register(ctx context.Context, spec BoundedCaptureSpec, targetID, scope string,
	actor security.Actor, traceID string) (*BoundedCaptureSpec, error) {
	if s == nil || s.pool == nil || targetID == "" || len(targetID) > 128 || scope == "" || len(scope) > 256 ||
		traceID == "" || len(traceID) > 128 {
		return nil, errors.New("capture: target/scope/trace identity required")
	}
	if err := actor.Validate(); err != nil {
		return nil, err
	}
	if err := normalizeCaptureSpec(&spec); err != nil {
		return nil, err
	}
	nowMS := s.now().UnixMilli()
	if spec.ExpiresAtUnixMS <= nowMS+int64(spec.DurationMS) || spec.ExpiresAtUnixMS > s.now().Add(24*time.Hour).UnixMilli() {
		return nil, errors.New("capture: TTL must cover the capture and be at most 24 hours")
	}
	spec.CaptureDigest = ComputeCaptureDigest(spec)
	filterJSON, _ := json.Marshal(spec.Filter)
	specJSON, _ := json.Marshal(spec)
	tag, err := s.pool.Pool.Exec(ctx, `INSERT INTO bounded_capture_requests(
		capture_id,target_id,scope,capture_digest,capture_adapter_id,duration_ms,sample_limit,byte_limit,
		expires_at_unix_ms,max_concurrent_on_target,payload_mode,filter,spec,state,actor_ref,actor_issuer,
		actor_subject,trace_id,reason_code,created_at_unix_ms)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'planned',$14,$15,$16,$17,'CAPTURE_PLANNED',$18)
		ON CONFLICT(capture_id) DO NOTHING`, spec.CaptureID, targetID, scope, spec.CaptureDigest,
		spec.CaptureAdapterID, spec.DurationMS, spec.SampleLimit, spec.ByteLimit, spec.ExpiresAtUnixMS,
		spec.MaxConcurrentOnTarget, spec.PayloadMode, filterJSON, specJSON, actor.String(), actor.Issuer,
		actor.Subject, traceID, nowMS)
	if err != nil {
		return nil, fmt.Errorf("capture: register request: %w", err)
	}
	if tag.RowsAffected() == 0 {
		var raw []byte
		if err := s.pool.Pool.QueryRow(ctx, `SELECT spec FROM bounded_capture_requests
			WHERE capture_id=$1 AND target_id=$2 AND scope=$3 AND capture_digest=$4`,
			spec.CaptureID, targetID, scope, spec.CaptureDigest).Scan(&raw); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return nil, errors.New("capture: capture_id reused with a different immutable envelope")
			}
			return nil, err
		}
		var existing BoundedCaptureSpec
		if err := json.Unmarshal(raw, &existing); err != nil {
			return nil, err
		}
		return &existing, nil
	}
	return &spec, nil
}

func normalizeCaptureSpec(spec *BoundedCaptureSpec) error {
	if spec == nil {
		return errors.New("capture: spec required")
	}
	if spec.SchemaVersion == "" {
		spec.SchemaVersion = boundedCaptureSchema
	}
	if spec.CaptureAdapterID == "" {
		spec.CaptureAdapterID = boundedCaptureAdapter
	}
	if spec.PayloadMode == "" {
		spec.PayloadMode = boundedCaptureMode
	}
	if spec.SchemaVersion != boundedCaptureSchema || spec.CaptureAdapterID != boundedCaptureAdapter ||
		spec.PayloadMode != boundedCaptureMode || spec.CaptureID == "" || len(spec.CaptureID) > 128 ||
		spec.DurationMS < 1 || spec.DurationMS > 300_000 || spec.SampleLimit < 1 || spec.SampleLimit > 10_000 ||
		spec.ByteLimit < 1 || spec.ByteLimit > 16*1024*1024 || spec.MaxConcurrentOnTarget < 1 || spec.MaxConcurrentOnTarget > 4 {
		return errors.New("capture: schema/adapter/mode/identity/resource envelope malformed")
	}
	var err error
	if spec.Filter.SourceIPv4, err = normalizeOptionalIPv4Prefix(spec.Filter.SourceIPv4); err != nil {
		return fmt.Errorf("capture: source prefix: %w", err)
	}
	if spec.Filter.DestinationIPv4, err = normalizeOptionalIPv4Prefix(spec.Filter.DestinationIPv4); err != nil {
		return fmt.Errorf("capture: destination prefix: %w", err)
	}
	if spec.Filter.Protocol != nil && *spec.Filter.Protocol > 255 {
		return errors.New("capture: protocol outside uint8 range")
	}
	for _, port := range []*uint32{spec.Filter.SourcePort, spec.Filter.DestinationPort} {
		if port != nil && *port > 65535 {
			return errors.New("capture: port outside uint16 range")
		}
	}
	if (spec.Filter.SourcePort != nil || spec.Filter.DestinationPort != nil) &&
		(spec.Filter.Protocol == nil || (*spec.Filter.Protocol != 6 && *spec.Filter.Protocol != 17)) {
		return errors.New("capture: ports require exact TCP or UDP protocol")
	}
	return nil
}

func normalizeOptionalIPv4Prefix(raw string) (string, error) {
	if raw == "" {
		return "", nil
	}
	prefix, err := netip.ParsePrefix(raw)
	if err != nil || !prefix.Addr().Is4() || prefix != prefix.Masked() {
		return "", errors.New("must be a normalized IPv4 prefix")
	}
	return prefix.String(), nil
}

func ComputeCaptureDigest(spec BoundedCaptureSpec) string {
	spec.CaptureDigest = ""
	raw, _ := json.Marshal(spec)
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func ComputeCaptureReadbackDigest(readback BoundedCaptureReadback) string {
	raw, _ := json.Marshal(readback)
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// CaptureProjector updates only the derived request projection. It never
// claims, schedules, or creates work.
type CaptureProjector struct{}

func NewCaptureProjector() *CaptureProjector { return &CaptureProjector{} }

func (p *CaptureProjector) ProjectIntentState(ctx context.Context, tx *db.Tx, intentID string,
	state ClaimState, outcome AttemptStatus, reasonCode string) error {
	projected := ""
	switch {
	case outcome == AttemptApplied:
		projected = "applied"
	case outcome == AttemptHold:
		projected = "hold"
	case outcome == AttemptUnknown || outcome == AttemptReconciling:
		projected = "unknown"
	case state == ClaimClaimed:
		projected = "claimed"
	case state == ClaimExecuting:
		projected = "executing"
	}
	if projected == "" {
		return nil
	}
	_, err := tx.Exec(ctx, `UPDATE bounded_capture_requests SET state=$1,reason_code=$2
		WHERE effect_intent_id=$3`, projected, reasonCode, intentID)
	return err
}
