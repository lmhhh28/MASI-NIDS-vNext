package model

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

var (
	digestRE     = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	reasonCodeRE = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,63}$`)
)

// RevisionService registers immutable model revisions and append-only lifecycle
// events. model_revisions is the current qualification projection; its history
// is never inferred from an in-place status alone.
type RevisionService struct {
	pool *db.Pool
	now  func() time.Time
}

func NewRevisionService(pool *db.Pool) *RevisionService {
	return &RevisionService{pool: pool, now: time.Now}
}

func (s *RevisionService) RegisterRevision(ctx context.Context, revision ModelRevision) (*ModelRevision, error) {
	if err := validateRevision(revision); err != nil {
		return nil, err
	}
	if revision.QualificationStatus == Qualified && revision.QualifiedAtUnixMS == 0 {
		revision.QualifiedAtUnixMS = s.now().UnixMilli()
	}
	var canonical ModelRevision
	err := s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `
			INSERT INTO model_revisions (
			 model_revision_id,model_revision_digest,model_bundle_digest,feature_contract_digest,
			 label_contract_digest,output_adapter_digest,qualification_status,qualified_at_unix_ms,
			 reader_runtime_profile,actor_ref,trace_id,scope,actor_issuer,actor_subject)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
			ON CONFLICT DO NOTHING`, revision.ModelRevisionID, revision.ModelRevisionDigest,
			revision.ModelBundleDigest, revision.FeatureContractDigest, revision.LabelContractDigest,
			revision.OutputAdapterDigest, string(revision.QualificationStatus), revision.QualifiedAtUnixMS,
			revision.ReaderRuntimeProfile, revision.Actor.String(), revision.TraceID, revision.Scope,
			revision.Actor.Issuer, revision.Actor.Subject)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 1 {
			canonical = revision
			return insertModelRevisionEvent(ctx, tx, revision, "REGISTERED", "REVISION_REGISTERED",
				revision.Actor.String(), revision.Actor.Issuer, revision.Actor.Subject, s.now().UnixMilli())
		}
		var status string
		var qualifiedAt *int64
		err = tx.QueryRow(ctx, `SELECT model_revision_id,model_revision_digest,model_bundle_digest,
			feature_contract_digest,label_contract_digest,output_adapter_digest,qualification_status,
			qualified_at_unix_ms,reader_runtime_profile,actor_issuer,actor_subject,trace_id,scope
			FROM model_revisions WHERE model_revision_id=$1 OR model_revision_digest=$2 FOR UPDATE`,
			revision.ModelRevisionID, revision.ModelRevisionDigest).Scan(&canonical.ModelRevisionID,
			&canonical.ModelRevisionDigest, &canonical.ModelBundleDigest, &canonical.FeatureContractDigest,
			&canonical.LabelContractDigest, &canonical.OutputAdapterDigest, &status, &qualifiedAt,
			&canonical.ReaderRuntimeProfile, &canonical.Actor.Issuer, &canonical.Actor.Subject,
			&canonical.TraceID, &canonical.Scope)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return errors.New("model: registration conflict without canonical row")
			}
			return err
		}
		canonical.QualificationStatus = QualificationStatus(status)
		if qualifiedAt != nil {
			canonical.QualifiedAtUnixMS = *qualifiedAt
		}
		if !sameRevisionIdentity(canonical, revision) {
			return errors.New("model: immutable revision identity conflict")
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("model: register revision: %w", err)
	}
	return &canonical, nil
}

// Revoke appends the revocation fact and updates the current qualification
// projection in one short transaction. A duplicate revoke returns the canonical
// state without creating another event.
func (s *RevisionService) Revoke(ctx context.Context, modelRevisionID string, actor security.Actor, reasonCode, traceID string) error {
	if modelRevisionID == "" || !reasonCodeRE.MatchString(reasonCode) || traceID == "" {
		return errors.New("model: revoke identity/reason/trace required")
	}
	if err := actor.Validate(); err != nil {
		return fmt.Errorf("model: revoke actor: %w", err)
	}
	return s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		var revision ModelRevision
		var status string
		var qualifiedAt *int64
		if err := tx.QueryRow(ctx, `SELECT model_revision_id,model_revision_digest,model_bundle_digest,
			feature_contract_digest,label_contract_digest,output_adapter_digest,qualification_status,
			qualified_at_unix_ms,reader_runtime_profile,actor_issuer,actor_subject,trace_id,scope
			FROM model_revisions WHERE model_revision_id=$1 FOR UPDATE`, modelRevisionID).Scan(
			&revision.ModelRevisionID, &revision.ModelRevisionDigest, &revision.ModelBundleDigest,
			&revision.FeatureContractDigest, &revision.LabelContractDigest, &revision.OutputAdapterDigest,
			&status, &qualifiedAt, &revision.ReaderRuntimeProfile, &revision.Actor.Issuer, &revision.Actor.Subject, &revision.TraceID,
			&revision.Scope); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("model: revision %s not found", modelRevisionID)
			}
			return err
		}
		revision.QualificationStatus = QualificationStatus(status)
		if qualifiedAt != nil {
			revision.QualifiedAtUnixMS = *qualifiedAt
		}
		if revision.QualificationStatus == Revoked {
			return nil
		}
		revision.TraceID = traceID
		if err := insertModelRevisionEvent(ctx, tx, revision, "REVOKED", reasonCode,
			actor.String(), actor.Issuer, actor.Subject, s.now().UnixMilli()); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `UPDATE model_revisions SET qualification_status='revoked'
			WHERE model_revision_id=$1 AND qualification_status=$2`, modelRevisionID, status)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("model: revoke CAS conflict")
		}
		return nil
	})
}

func insertModelRevisionEvent(ctx context.Context, tx *db.Tx, revision ModelRevision, eventType, reasonCode, actorRef, actorIssuer, actorSubject string, occurredAtUnixMS int64) error {
	eventID := "mev-" + shortModelEventID(revision.ModelRevisionID+":"+eventType+":"+reasonCode+":"+actorRef)
	_, err := tx.Exec(ctx, `INSERT INTO model_revision_events(event_id,model_revision_id,
		model_revision_digest,event_type,previous_status,new_status,scope,actor_ref,actor_issuer,
		actor_subject,reason_code,trace_id,occurred_at_unix_ms)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, eventID,
		revision.ModelRevisionID, revision.ModelRevisionDigest, eventType, string(revision.QualificationStatus),
		eventStatus(eventType, revision.QualificationStatus), revision.Scope, actorRef, actorIssuer,
		actorSubject, reasonCode, revision.TraceID, occurredAtUnixMS)
	return err
}

func eventStatus(eventType string, current QualificationStatus) string {
	if eventType == "REVOKED" {
		return string(Revoked)
	}
	return string(current)
}

func sameRevisionIdentity(left, right ModelRevision) bool {
	return left.ModelRevisionID == right.ModelRevisionID &&
		left.ModelRevisionDigest == right.ModelRevisionDigest && left.ModelBundleDigest == right.ModelBundleDigest &&
		left.FeatureContractDigest == right.FeatureContractDigest && left.LabelContractDigest == right.LabelContractDigest &&
		left.OutputAdapterDigest == right.OutputAdapterDigest && left.QualificationStatus == right.QualificationStatus &&
		left.ReaderRuntimeProfile == right.ReaderRuntimeProfile && left.Scope == right.Scope
}

func shortModelEventID(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:16])
}

func validateRevision(revision ModelRevision) error {
	if revision.ModelRevisionID == "" || len(revision.ModelRevisionID) > 128 || revision.Scope == "" || len(revision.Scope) > 128 {
		return errors.New("model: model_revision_id/scope required and bounded")
	}
	for _, digest := range []string{revision.ModelRevisionDigest, revision.ModelBundleDigest,
		revision.FeatureContractDigest, revision.LabelContractDigest, revision.OutputAdapterDigest} {
		if !digestRE.MatchString(digest) {
			return fmt.Errorf("model: malformed digest %q", digest)
		}
	}
	switch revision.QualificationStatus {
	case Qualified, Unqualified, Hold, Revoked:
	default:
		return fmt.Errorf("model: bad qualification_status %q", revision.QualificationStatus)
	}
	if revision.ReaderRuntimeProfile == "" || len(revision.ReaderRuntimeProfile) > 128 {
		return errors.New("model: reader_runtime_profile required and bounded")
	}
	if err := revision.Actor.Validate(); err != nil {
		return fmt.Errorf("model: actor: %w", err)
	}
	if revision.TraceID == "" || len(revision.TraceID) > 128 {
		return errors.New("model: trace_id required and bounded")
	}
	return nil
}
