package pluginstat

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// Service owns the canonical statistics facts: input freeze, the durable
// NON-EFFECT run ledger, artifact validation, and the bounded current/history
// projection. The dispatcher in this file is the ONLY advance mechanism for
// runs — bounded, CAS/fenced, derived solely from plugin_statistic_runs; it
// is never an effect-dispatcher claim source and never a second broker.
type Service struct {
	pool *db.Pool
	now  func() time.Time
}

type RunAuthorization struct {
	Scope           string
	DataClass       string
	SourceRead      bool
	StatisticsRun   bool
	TargetSetDigest string
}

type ScheduleAuthorization struct {
	Scope           string
	DataClass       string
	PlatformAdmin   bool
	CSRFVerified    bool
	StepUpFresh     bool
	TargetSetDigest string
	SourceRead      bool
	StatisticsRun   bool
}

type ClaimToken struct {
	RunID             string
	LeaseID           string
	ClaimGeneration   int64
	ResultFence       string
	BindingGeneration int64
	DefinitionID      string
	DefinitionDigest  string
	PluginID          string
	PluginRevision    string
	Scope             string
	ExpiresAtUnixMS   int64
	DeadlineMS        int
}

type OnDemandRequest struct {
	RunID             string
	DefinitionID      string
	DefinitionDigest  string
	IdempotencyKey    string
	Scope             string
	DataClass         string
	WindowStartUnixMS int64
	WindowEndUnixMS   int64
	TraceID           string
	ScheduleID        string
	TargetSetDigest   string
}

// StartOnDemand freezes only built-in host-owned PostgreSQL projections and
// appends the run in the same repeatable-read transaction. Browser-supplied rows,
// SQL, endpoints, expressions and external-source requests never enter this path.
func (s *Service) StartOnDemand(ctx context.Context, req OnDemandRequest, actor security.Actor, auth RunAuthorization) (string, error) {
	if !identityRE.MatchString(req.RunID) || !identityRE.MatchString(req.DefinitionID) || !identityRE.MatchString(req.IdempotencyKey) ||
		!digestRE.MatchString(req.DefinitionDigest) || !digestRE.MatchString(req.TargetSetDigest) || req.Scope == "" || req.DataClass == "" ||
		req.WindowStartUnixMS < 1 || req.WindowEndUnixMS <= req.WindowStartUnixMS || req.WindowEndUnixMS-req.WindowStartUnixMS > 24*60*60*1000 {
		return "", errors.New("pluginstat: malformed on-demand request")
	}
	if err := actor.Validate(); err != nil {
		return "", err
	}
	if !auth.SourceRead || !auth.StatisticsRun || auth.Scope != req.Scope || auth.DataClass != req.DataClass || auth.TargetSetDigest != req.TargetSetDigest {
		return "", errors.New("pluginstat: exact on-demand authorization required")
	}
	requestDigest := digestOf(fmt.Sprintf("ondemand|%s|%s|%s|%s|%s|%d|%d", req.DefinitionID, req.DefinitionDigest, req.Scope, req.ScheduleID, req.TargetSetDigest, req.WindowStartUnixMS, req.WindowEndUnixMS))
	resultID := ""
	err := s.pool.WithTx(ctx, []db.TxOption{db.RepeatableRead()}, func(tx *db.Tx) error {
		var bindingGen int64
		var pluginID, pluginRevision, configDigest string
		var deadlineMS int
		var projections, externalCaps []string
		if err := tx.QueryRow(ctx, `SELECT d.binding_generation,d.plugin_id,d.plugin_revision,b.config_digest,
			 d.deadline_ms,d.host_projection_refs,d.external_capability_ids
		 FROM plugin_statistics_definitions d JOIN plugin_bindings b ON b.plugin_id=d.plugin_id AND b.binding_generation=d.binding_generation
		 WHERE d.definition_id=$1 AND d.definition_digest=$2 AND d.scope=$3 AND d.data_class=$4
		 AND NOT d.revoked AND b.activation_state='active' AND b.qualification_status='qualified'`,
			req.DefinitionID, req.DefinitionDigest, req.Scope, req.DataClass).
			Scan(&bindingGen, &pluginID, &pluginRevision, &configDigest, &deadlineMS, &projections, &externalCaps); err != nil {
			return err
		}
		if req.ScheduleID != "" {
			var scheduleCurrent bool
			if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM plugin_statistic_schedules s
		 WHERE s.schedule_id=$1 AND s.definition_id=$2 AND s.definition_digest=$3 AND s.scope=$4 AND s.data_class=$5
		 AND s.target_set_digest=$6 AND NOT s.disabled AND s.schedule_revision=(SELECT max(x.schedule_revision) FROM plugin_statistic_schedules x WHERE x.schedule_id=$1))`,
				req.ScheduleID, req.DefinitionID, req.DefinitionDigest, req.Scope, req.DataClass, req.TargetSetDigest).Scan(&scheduleCurrent); err != nil {
				return err
			}
			if !scheduleCurrent {
				return errors.New("pluginstat: schedule revised/disabled before run freeze")
			}
		}
		var existingRun, existingRequest string
		err := tx.QueryRow(ctx, `SELECT run_id,request_digest FROM plugin_statistic_runs WHERE idempotency_key=$1`, req.IdempotencyKey).Scan(&existingRun, &existingRequest)
		if err == nil {
			if existingRequest != requestDigest {
				return ErrIdempotencyConflict
			}
			resultID = existingRun
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		rows := make([]map[string]any, 0, 256)
		for _, projection := range projections {
			projected, err := readHostProjection(ctx, tx, projection, req.Scope, req.WindowStartUnixMS, req.WindowEndUnixMS, 10000-len(rows))
			if err != nil {
				return err
			}
			rows = append(rows, projected...)
			if len(rows) > 10000 {
				return errors.New("pluginstat: frozen input row bound exceeded")
			}
		}
		sourceSequenceStart := uint64(0)
		sourceSequenceEnd := uint64(0)
		if len(rows) > 0 {
			sourceSequenceStart = 1
			sourceSequenceEnd = uint64(len(rows))
		}
		profileRefs := append([]string(nil), projections...)
		sortStrings(profileRefs)
		bundle := InputBundle{
			SchemaVersion: "masi-plugin-statistics/v1", RecordType: "input-bundle",
			RecordID: "bundle-" + shortDigest(requestDigest), RunID: req.RunID, RequestDigest: requestDigest,
			PluginID: pluginID, PluginRevision: pluginRevision, ConfigDigest: configDigest,
			BindingGeneration: bindingGen, DefinitionID: req.DefinitionID, DefinitionRevision: req.DefinitionID,
			DefinitionDigest: req.DefinitionDigest, SourceRevision: fmt.Sprintf("postgres-snapshot-%d", s.now().UnixMilli()),
			SourceProfileDigest: digestOf(strings.Join(profileRefs, "|") + "|" + req.DataClass),
			SourceGeneration:    1, SourceEpoch: "postgres-snapshot", SourceSequenceStart: sourceSequenceStart,
			SourceSequenceEnd: sourceSequenceEnd, Coverage: 1, Quality: string(QualityValid), Scope: req.Scope,
			DataClassRef: req.DataClass, WindowStartUnixMS: req.WindowStartUnixMS, WindowEndUnixMS: req.WindowEndUnixMS,
			AsOfUnixMS: req.WindowEndUnixMS, Rows: rows,
			ExternalSourceCapabilityRefs: append([]string{}, externalCaps...), BytesLimit: 2 * 1024 * 1024,
			CardinalityLimit: 10000, DeadlineMS: deadlineMS, ActorRef: "control-plugin-statistics",
			ReasonCode: "INPUT_FROZEN", TraceID: req.TraceID,
		}
		frozen := ComputeFrozenInputDigest(bundle)
		bundle.FrozenInputDigest = frozen
		bundle.BundleDigest = ComputeInputBundleDigest(bundle)
		bundleJSON, err := json.Marshal(bundle)
		if err != nil {
			return err
		}
		if len(bundleJSON) > 2*1024*1024 {
			return errors.New("pluginstat: frozen input bytes exceed 2 MiB")
		}
		runDigest := digestOf(requestDigest + "|" + frozen + fmt.Sprintf("|%d", bindingGen))
		tag, err := tx.Exec(ctx, `INSERT INTO plugin_statistic_runs(run_id,run_digest,request_digest,definition_id,definition_digest,schedule_id,
				 idempotency_key,status,binding_generation,frozen_input_digest,started_at_unix_ms,actor_ref,reason_code,trace_id,
				 scope,data_class,actor_issuer,actor_subject,target_set_digest) VALUES($1,$2,$3,$4,$5,$6,$7,'pending',$8,$9,$10,$11,'ON_DEMAND_STARTED',$12,$13,$14,$15,$16,$17)`,
			req.RunID, runDigest, requestDigest, req.DefinitionID, req.DefinitionDigest, nullable(req.ScheduleID), req.IdempotencyKey, bindingGen, frozen, s.now().UnixMilli(), actor.String(), req.TraceID, req.Scope, req.DataClass, actor.Issuer, actor.Subject, req.TargetSetDigest)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("pluginstat: on-demand insert failed")
		}
		if _, err := tx.Exec(ctx, `INSERT INTO plugin_statistic_input_bundles(run_id,definition_id,definition_digest,binding_generation,frozen_input_digest,bundle,bytes) VALUES($1,$2,$3,$4,$5,$6,$7)`, req.RunID, req.DefinitionID, req.DefinitionDigest, bindingGen, frozen, bundleJSON, len(bundleJSON)); err != nil {
			return err
		}
		resultID = req.RunID
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("pluginstat: start on-demand: %w", err)
	}
	return resultID, nil
}

func readHostProjection(ctx context.Context, tx *db.Tx, projection, scope string, startMS, endMS int64, limit int) ([]map[string]any, error) {
	if limit < 1 {
		return nil, errors.New("pluginstat: frozen input row bound exhausted")
	}
	rowsOut := []map[string]any{}
	switch projection {
	case "event-count-projection", "projection.events-current":
		rows, err := tx.Query(ctx, `SELECT quality,count(*) FROM events WHERE scope=$1 AND event_time >= $2 AND event_time < $3 GROUP BY quality ORDER BY quality LIMIT $4`, scope, time.UnixMilli(startMS), time.UnixMilli(endMS), limit)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var quality string
			var count int64
			if err := rows.Scan(&quality, &count); err != nil {
				return nil, err
			}
			rowsOut = append(rowsOut, map[string]any{"projection": projection, "quality": quality, "count": count})
		}
		return rowsOut, rows.Err()
	case "projection.incidents-current":
		rows, err := tx.Query(ctx, `SELECT severity,status,count(*) FROM incidents WHERE scope=$1 AND created_at >= $2 AND created_at < $3 GROUP BY severity,status ORDER BY severity,status LIMIT $4`, scope, time.UnixMilli(startMS), time.UnixMilli(endMS), limit)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var severity, status string
			var count int64
			if err := rows.Scan(&severity, &status, &count); err != nil {
				return nil, err
			}
			rowsOut = append(rowsOut, map[string]any{"projection": projection, "severity": severity, "status": status, "count": count})
		}
		return rowsOut, rows.Err()
	case "projection.targets-current":
		rows, err := tx.Query(ctx, `SELECT status,count(*) FROM targets WHERE scope=$1 GROUP BY status ORDER BY status LIMIT $2`, scope, limit)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var status string
			var count int64
			if err := rows.Scan(&status, &count); err != nil {
				return nil, err
			}
			rowsOut = append(rowsOut, map[string]any{"projection": projection, "status": status, "count": count})
		}
		return rowsOut, rows.Err()
	case "projection.rule-effectiveness-current":
		rows, err := tx.Query(ctx, `SELECT e.rule_id,ro.quality_status,ro.rate,ro.coverage,ro.outcome_status FROM rule_observations ro
		 JOIN rule_observation_epochs e ON e.epoch_id=ro.epoch_id JOIN effect_intents i ON i.effect_intent_id=e.effect_intent_id
		 JOIN effect_proposals p ON p.proposal_id=i.proposal_id WHERE p.scope=$1 AND ro.updated_at >= $2 AND ro.updated_at < $3
		 ORDER BY e.rule_id LIMIT $4`, scope, time.UnixMilli(startMS), time.UnixMilli(endMS), limit)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var rule, quality, outcome string
			var rate, coverage float64
			if err := rows.Scan(&rule, &quality, &rate, &coverage, &outcome); err != nil {
				return nil, err
			}
			rowsOut = append(rowsOut, map[string]any{"projection": projection, "rule_id": rule, "quality": quality, "rate": rate, "coverage": coverage, "outcome": outcome})
		}
		return rowsOut, rows.Err()
	default:
		return nil, fmt.Errorf("pluginstat: unknown host projection %s", projection)
	}
}

func shortDigest(d string) string {
	if len(d) >= 16 {
		return strings.TrimPrefix(d, "sha256:")[:16]
	}
	return d
}

func NewService(pool *db.Pool) *Service {
	return &Service{pool: pool, now: time.Now}
}

// InputBundle is the Go-frozen canonical input for one run.
type InputBundle struct {
	SchemaVersion                string           `json:"schema_version"`
	RecordType                   string           `json:"record_type"`
	RecordID                     string           `json:"record_id"`
	BundleDigest                 string           `json:"bundle_digest"`
	RunID                        string           `json:"run_id"`
	RequestDigest                string           `json:"request_digest"`
	PluginID                     string           `json:"plugin_id"`
	PluginRevision               string           `json:"plugin_revision"`
	ConfigDigest                 string           `json:"config_digest"`
	BindingGeneration            int64            `json:"binding_generation"`
	DefinitionID                 string           `json:"definition_id"`
	DefinitionRevision           string           `json:"definition_revision"`
	DefinitionDigest             string           `json:"definition_digest"`
	SourceRevision               string           `json:"source_revision"`
	SourceProfileDigest          string           `json:"source_profile_digest"`
	SourceGeneration             uint64           `json:"source_generation"`
	SourceEpoch                  string           `json:"source_epoch"`
	SourceSequenceStart          uint64           `json:"source_sequence_start"`
	SourceSequenceEnd            uint64           `json:"source_sequence_end"`
	Coverage                     float64          `json:"coverage"`
	Quality                      string           `json:"quality"`
	FrozenInputDigest            string           `json:"frozen_input_digest"`
	Scope                        string           `json:"scope"`
	DataClassRef                 string           `json:"data_class_ref"`
	WindowStartUnixMS            int64            `json:"window_start_unix_ms"`
	WindowEndUnixMS              int64            `json:"window_end_unix_ms"`
	AsOfUnixMS                   int64            `json:"as_of_unix_ms"`
	Rows                         []map[string]any `json:"rows"`
	ExternalSourceCapabilityRefs []string         `json:"external_source_capability_refs"`
	BytesLimit                   int              `json:"bytes_limit"`
	CardinalityLimit             int              `json:"cardinality_limit"`
	DeadlineMS                   int              `json:"deadline_ms"`
	ActorRef                     string           `json:"actor_ref"`
	ReasonCode                   string           `json:"reason_code"`
	TraceID                      string           `json:"trace_id"`
}

// ErrIdempotencyConflict is same key + different digest (STATISTICS-
// IDEMPOTENCY-SAME-KEY).
var ErrIdempotencyConflict = errors.New("pluginstat: idempotency key reused with different digest")

// FreezeInput validates the canonical input bundle for a run and returns the
// frozen digest. The frozen digest binds definition digest + window + rows so
// the artifact can always be traced back to the exact input. It is pure (no
// DB): durability comes from storing the digest on the run at StartRun.
func (s *Service) FreezeInput(ctx context.Context, b InputBundle) (string, error) {
	if b.SchemaVersion != "masi-plugin-statistics/v1" || b.RecordType != "input-bundle" ||
		!identityRE.MatchString(b.RecordID) || !identityRE.MatchString(b.RunID) ||
		!identityRE.MatchString(b.PluginID) || !identityRE.MatchString(b.PluginRevision) ||
		!identityRE.MatchString(b.DefinitionID) || !identityRE.MatchString(b.DefinitionRevision) ||
		!identityRE.MatchString(b.SourceEpoch) || !identityRE.MatchString(b.DataClassRef) ||
		!identityRE.MatchString(b.ActorRef) || !identityRE.MatchString(b.TraceID) ||
		!digestRE.MatchString(b.RequestDigest) || !digestRE.MatchString(b.ConfigDigest) ||
		!digestRE.MatchString(b.DefinitionDigest) || !digestRE.MatchString(b.SourceProfileDigest) ||
		b.BindingGeneration < 1 || b.SourceGeneration < 1 || b.Coverage < 0 || b.Coverage > 1 ||
		b.Scope == "" || b.SourceRevision == "" || len(b.SourceRevision) > 256 ||
		b.BytesLimit < 1 || b.BytesLimit > 2*1024*1024 || b.CardinalityLimit < 1 || b.CardinalityLimit > 10000 ||
		b.DeadlineMS < 1 || b.DeadlineMS > 10000 || b.AsOfUnixMS < b.WindowEndUnixMS ||
		b.ReasonCode != "INPUT_FROZEN" {
		return "", errors.New("pluginstat: bundle identity/digest malformed")
	}
	if len(b.Rows) > 10000 {
		return "", errors.New("pluginstat: input rows > 10000")
	}
	if len(b.ExternalSourceCapabilityRefs) > MaxExternalCapabilityRefs || hasDuplicates(b.ExternalSourceCapabilityRefs) {
		return "", errors.New("pluginstat: external-source capability refs outside bound")
	}
	for _, capabilityID := range b.ExternalSourceCapabilityRefs {
		if !identityRE.MatchString(capabilityID) || forbiddenValueRE.MatchString(capabilityID) {
			return "", errors.New("pluginstat: external-source capability identity malformed")
		}
	}
	for _, row := range b.Rows {
		if len(row) > 64 {
			return "", errors.New("pluginstat: input row fields >64")
		}
		for key, value := range row {
			if !identityRE.MatchString(key) {
				return "", errors.New("pluginstat: input field outside registry grammar")
			}
			switch v := value.(type) {
			case string:
				if len(v) > 4096 || forbiddenPayloadRE.MatchString(v) {
					return "", errors.New("pluginstat: input string unsafe/oversize")
				}
			case bool, int, int64, uint64, json.Number:
			case float64:
				if math.IsNaN(v) || math.IsInf(v, 0) {
					return "", errors.New("pluginstat: input NaN/Inf")
				}
			case nil:
			default:
				return "", fmt.Errorf("pluginstat: nested/unknown input scalar %T", value)
			}
		}
	}
	if b.WindowStartUnixMS < 1 || b.WindowEndUnixMS <= b.WindowStartUnixMS {
		return "", errors.New("pluginstat: window malformed")
	}
	if raw, err := json.Marshal(b); err != nil || len(raw) > 2*1024*1024 {
		return "", errors.New("pluginstat: input bundle bytes exceed 2 MiB")
	}
	frozen := ComputeFrozenInputDigest(b)
	if b.FrozenInputDigest != "" && b.FrozenInputDigest != frozen {
		return "", errors.New("pluginstat: caller frozen digest mismatch")
	}
	if b.BundleDigest != "" && b.BundleDigest != ComputeInputBundleDigest(b) {
		return "", errors.New("pluginstat: caller bundle digest mismatch")
	}
	return frozen, nil
}

// RegisterDefinition freezes a qualified manifest-owned definition after
// checking the host projection registry and any explicit read-only capability.
func (s *Service) RegisterDefinition(ctx context.Context, d Definition, manifestID string, manifestRevision int, scope, dataClass string) error {
	d = normalizeDefinitionSlices(d)
	if err := ValidateDefinition(d); err != nil {
		return err
	}
	if scope == "" || dataClass == "" || manifestID == "" || manifestRevision < 1 {
		return errors.New("pluginstat: manifest/scope/data class required")
	}
	approvedProjection := map[string]bool{
		"projection.rule-effectiveness-current": true,
		"projection.events-current":             true,
		"projection.incidents-current":          true,
		"projection.targets-current":            true,
		"event-count-projection":                true,
	}
	for _, ref := range d.HostProjectionRefs {
		if !approvedProjection[ref] {
			return fmt.Errorf("pluginstat: unknown host-owned projection %s", ref)
		}
	}
	return s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var kind, manifestDigest, qualificationID, qualificationDigest string
		var capsRaw []byte
		if err := tx.QueryRow(ctx, `SELECT m.kind,m.capabilities,m.manifest_digest,q.qualification_id,q.qualification_digest
			FROM plugin_manifests m JOIN plugin_bindings b
			 ON b.plugin_id=m.plugin_id AND b.manifest_id=m.manifest_id AND b.manifest_revision=m.manifest_revision
			 JOIN LATERAL(SELECT qualification_id,qualification_digest,manifest_digest,qualification_status
			   FROM plugin_qualifications q WHERE q.plugin_id=m.plugin_id AND q.manifest_id=m.manifest_id
			   AND q.manifest_revision=m.manifest_revision ORDER BY q.created_at DESC LIMIT 1) q
			 ON q.manifest_digest=m.manifest_digest
			 WHERE m.plugin_id=$1 AND m.manifest_id=$2 AND m.manifest_revision=$3
			 AND b.binding_generation=$4 AND b.activation_state='active' AND b.qualification_status='qualified'
			 AND b.manifest_digest=m.manifest_digest AND q.qualification_status='qualified'
			 AND m.scope=$5`, d.PluginID, manifestID, manifestRevision, d.BindingGeneration, scope).
			Scan(&kind, &capsRaw, &manifestDigest, &qualificationID, &qualificationDigest); err != nil {
			return err
		}
		if kind != string(d.PluginKind) {
			return errors.New("pluginstat: manifest kind mismatch")
		}
		if d.PluginRevision != fmt.Sprintf("%s:%d", manifestID, manifestRevision) {
			return errors.New("pluginstat: exact plugin revision mismatch")
		}
		var caps []struct {
			CapabilityID string `json:"capability_id"`
			Declared     bool   `json:"declared"`
		}
		if err := json.Unmarshal(capsRaw, &caps); err != nil {
			return err
		}
		allowedCaps := map[string]bool{}
		for _, c := range caps {
			if c.Declared {
				allowedCaps[c.CapabilityID] = true
			}
		}
		for _, capID := range d.ExternalSourceCapabilityRefs {
			if !allowedCaps[capID] {
				return fmt.Errorf("pluginstat: external capability %s not approved by manifest", capID)
			}
		}
		definitionJSON, err := json.Marshal(d)
		if err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `INSERT INTO plugin_statistics_definitions(
			 definition_id,definition_digest,definition,plugin_id,plugin_revision,manifest_id,manifest_revision,binding_generation,
			 producer_kind,host_projection_refs,external_capability_ids,display_hint,scope,data_class,deadline_ms)
			 VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) ON CONFLICT(definition_id) DO NOTHING`,
			d.DefinitionID, d.DefinitionDigest, definitionJSON, d.PluginID, d.PluginRevision, manifestID, manifestRevision, d.BindingGeneration,
			string(d.PluginKind), d.HostProjectionRefs, d.ExternalSourceCapabilityRefs, string(d.DisplayHint), scope, dataClass, d.DeadlineMS)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 1 {
			if _, err := tx.Exec(ctx, `UPDATE plugin_statistics_definitions SET manifest_digest=$1,
				qualification_id=$2,qualification_digest=$3 WHERE definition_id=$4`, manifestDigest,
				qualificationID, qualificationDigest, d.DefinitionID); err != nil {
				return err
			}
			return nil
		}
		var existing string
		if err := tx.QueryRow(ctx, `SELECT definition_digest FROM plugin_statistics_definitions WHERE definition_id=$1`, d.DefinitionID).Scan(&existing); err != nil {
			return err
		}
		if existing != d.DefinitionDigest {
			return errors.New("pluginstat: immutable definition identity conflict")
		}
		return nil
	})
}

// ComputeFrozenInputDigest is the pure canonical input digest.
func ComputeFrozenInputDigest(b InputBundle) string {
	externalCaps := append([]string(nil), b.ExternalSourceCapabilityRefs...)
	sortStrings(externalCaps)
	canonical := fmt.Sprintf(
		"%s|%s|%s|%s|%s|%d|%s|%s|%s|%s|%s|%s|%s|%d|%s|%d|%d|%016x|%s|%d|%d|%d|%d|%d|%d|%d",
		b.RunID, b.RequestDigest, b.PluginID, b.PluginRevision, b.ConfigDigest, b.BindingGeneration,
		b.DefinitionID, b.DefinitionRevision, b.DefinitionDigest, b.Scope, b.DataClassRef,
		b.SourceRevision, b.SourceProfileDigest, b.SourceGeneration, b.SourceEpoch,
		b.SourceSequenceStart, b.SourceSequenceEnd, math.Float64bits(b.Coverage), b.Quality,
		b.WindowStartUnixMS, b.WindowEndUnixMS, b.AsOfUnixMS, b.BytesLimit, b.CardinalityLimit,
		b.DeadlineMS, len(b.Rows),
	)
	if len(externalCaps) > 0 {
		canonical += "|external:[" + strings.Join(externalCaps, " ") + "]"
	}
	for _, row := range b.Rows {
		keys := make([]string, 0, len(row))
		for k := range row {
			keys = append(keys, k)
		}
		sortStrings(keys)
		for _, k := range keys {
			canonical += "|" + k + "=" + canonicalScalar(row[k])
		}
	}
	return digestOf(canonical)
}

// ComputeInputBundleDigest binds the exact public JSON bundle while excluding
// only its self-referential bundle_digest field.
func ComputeInputBundleDigest(b InputBundle) string {
	b.BundleDigest = ""
	return canonicalJSONDigest(b)
}

// StartRun creates a pending run in the durable ledger. Idempotency: the same
// (idempotency_key, run_digest) returns the original run; the same key with a
// different digest is a conflict.
func (s *Service) StartRun(ctx context.Context, runID, definitionID, definitionDigest, scheduleID, idempotencyKey string, bindingGeneration int64, bundle InputBundle, actor security.Actor, auth RunAuthorization, traceID string) (string, error) {
	if !identityRE.MatchString(runID) || !identityRE.MatchString(definitionID) {
		return "", errors.New("pluginstat: run/definition identity malformed")
	}
	if !identityRE.MatchString(idempotencyKey) {
		return "", errors.New("pluginstat: idempotency key malformed")
	}
	if bindingGeneration < 1 {
		return "", errors.New("pluginstat: binding_generation >= 1 required")
	}
	if !digestRE.MatchString(definitionDigest) || !digestRE.MatchString(auth.TargetSetDigest) || auth.Scope == "" || auth.DataClass == "" || !auth.SourceRead || !auth.StatisticsRun {
		return "", errors.New("pluginstat: exact definition and source-read/plugin.statistics.run authorization required")
	}
	if err := actor.Validate(); err != nil {
		return "", err
	}
	if bundle.DefinitionID != definitionID || bundle.DefinitionDigest != definitionDigest {
		return "", errors.New("pluginstat: input bundle definition mismatch")
	}
	frozenInputDigest, err := s.FreezeInput(ctx, bundle)
	if err != nil {
		return "", err
	}
	bundle.FrozenInputDigest = frozenInputDigest
	bundle.BundleDigest = ComputeInputBundleDigest(bundle)
	bundleJSON, err := json.Marshal(bundle)
	if err != nil || len(bundleJSON) > 2*1024*1024 {
		return "", errors.New("pluginstat: input bundle serialization/size invalid")
	}
	requestDigest := digestOf(fmt.Sprintf("run|%s|%s|%s|%s|%s|%d|%d", definitionID, definitionDigest, scheduleID, auth.Scope, auth.TargetSetDigest, bundle.WindowStartUnixMS, bundle.WindowEndUnixMS))
	runDigest := digestOf(requestDigest + "|" + frozenInputDigest + fmt.Sprintf("|%d", bindingGeneration))
	var resultID string
	err = s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var eligible bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(
		 SELECT 1 FROM plugin_statistics_definitions d JOIN plugin_bindings b
		   ON b.plugin_id=d.plugin_id AND b.binding_generation=d.binding_generation
		 WHERE d.definition_id=$1 AND d.definition_digest=$2 AND d.binding_generation=$3
		   AND d.scope=$4 AND d.data_class=$5 AND NOT d.revoked
		   AND b.activation_state='active' AND b.qualification_status='qualified')`,
			definitionID, definitionDigest, bindingGeneration, auth.Scope, auth.DataClass).Scan(&eligible); err != nil {
			return err
		}
		if !eligible {
			return errors.New("pluginstat: definition/binding revoked, stale, or out of scope")
		}
		if scheduleID != "" {
			var scheduleOK bool
			if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM plugin_statistic_schedules s
			 WHERE s.schedule_id=$1 AND s.definition_id=$2 AND s.definition_digest=$3
				   AND s.scope=$4 AND s.data_class=$5 AND s.target_set_digest=$6 AND NOT s.disabled
				   AND s.schedule_revision=(SELECT max(x.schedule_revision) FROM plugin_statistic_schedules x WHERE x.schedule_id=$1))`,
				scheduleID, definitionID, definitionDigest, auth.Scope, auth.DataClass, auth.TargetSetDigest).Scan(&scheduleOK); err != nil {
				return err
			}
			if !scheduleOK {
				return errors.New("pluginstat: schedule is not current/authorized")
			}
		}
		tag, err := tx.Exec(ctx, `INSERT INTO plugin_statistic_runs (
			 run_id,run_digest,request_digest,definition_id,definition_digest,schedule_id,idempotency_key,status,
			 binding_generation,frozen_input_digest,started_at_unix_ms,actor_ref,reason_code,trace_id,
				 scope,data_class,actor_issuer,actor_subject,target_set_digest)
				 VALUES($1,$2,$3,$4,$5,$6,$7,'pending',$8,$9,$10,$11,'STARTED',$12,$13,$14,$15,$16,$17)
			 ON CONFLICT(idempotency_key) DO NOTHING`, runID, runDigest, requestDigest, definitionID, definitionDigest, nullable(scheduleID),
			idempotencyKey, bindingGeneration, frozenInputDigest, s.now().UnixMilli(), actor.String(), traceID,
			auth.Scope, auth.DataClass, actor.Issuer, actor.Subject, auth.TargetSetDigest)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 1 {
			if _, err := tx.Exec(ctx, `INSERT INTO plugin_statistic_input_bundles(run_id,definition_id,definition_digest,binding_generation,frozen_input_digest,bundle,bytes) VALUES($1,$2,$3,$4,$5,$6,$7)`, runID, definitionID, definitionDigest, bindingGeneration, frozenInputDigest, bundleJSON, len(bundleJSON)); err != nil {
				return err
			}
			resultID = runID
			return nil
		}
		var existingDigest string
		if err := tx.QueryRow(ctx, `SELECT run_id,request_digest FROM plugin_statistic_runs WHERE idempotency_key=$1`, idempotencyKey).Scan(&resultID, &existingDigest); err != nil {
			return err
		}
		if existingDigest != requestDigest {
			return ErrIdempotencyConflict
		}
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("pluginstat: start run: %w", err)
	}
	return resultID, nil
}

// ClaimNextPending is the bounded dispatcher advance: it claims ONE pending,
// unclaimed, non-expired run with FOR UPDATE SKIP LOCKED and a lease/fence.
// The caller executes the plugin OUTSIDE any DB transaction and reports via
// FinishRun. This is a statistics dispatcher — NOT the effect dispatcher and
// NOT a claim on effect_intents.
type RunAuthorizer func(security.Actor, string, string, string) bool

func (s *Service) ClaimNextPending(ctx context.Context, owner string, leaseMS int64, authorize RunAuthorizer) (token *ClaimToken, ok bool, err error) {
	if leaseMS < 1000 || leaseMS > 600000 {
		return nil, false, errors.New("pluginstat: lease 1s..10min bounded")
	}
	if !identityRE.MatchString(owner) {
		return nil, false, errors.New("pluginstat: claim owner malformed")
	}
	if authorize == nil {
		return nil, false, errors.New("pluginstat: run authorizer unavailable")
	}
	nowMS := s.now().UnixMilli()
	nonce := make([]byte, 16)
	if _, err := rand.Read(nonce); err != nil {
		return nil, false, err
	}
	leaseID := owner + ":" + hex.EncodeToString(nonce)
	err = s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var runID, definitionID, definitionDigest, pluginID, pluginRevision, scope, dataClass, targetSetDigest, actorIssuer, actorSubject string
		var bindingGen, claimGen int64
		var deadlineMS int
		if err := tx.QueryRow(ctx, `
						SELECT r.run_id,r.definition_id,r.definition_digest,d.plugin_id,d.plugin_revision,
						       r.binding_generation,r.claim_generation,r.scope,d.deadline_ms,
					       r.data_class,r.target_set_digest,r.actor_issuer,r.actor_subject
				FROM plugin_statistic_runs r JOIN plugin_statistics_definitions d ON d.definition_id=r.definition_id
				JOIN plugin_bindings b ON b.plugin_id=d.plugin_id AND b.binding_generation=r.binding_generation
					WHERE r.status = 'pending' AND r.claim_generation < 3 AND r.definition_digest=d.definition_digest AND NOT d.revoked
				  AND b.activation_state='active' AND b.qualification_status='qualified'
				  AND (claim_expires_unix_ms IS NULL OR claim_expires_unix_ms < $1)
				ORDER BY started_at_unix_ms
						FOR UPDATE OF r SKIP LOCKED LIMIT 1`, nowMS).Scan(&runID, &definitionID, &definitionDigest,
			&pluginID, &pluginRevision, &bindingGen, &claimGen, &scope, &deadlineMS, &dataClass,
			&targetSetDigest, &actorIssuer, &actorSubject); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				ok = false
				return nil
			}
			return err
		}
		actor := security.Actor{Issuer: actorIssuer, Subject: actorSubject}
		if actor.Validate() != nil || !authorize(actor, scope, dataClass, targetSetDigest) {
			_, err := tx.Exec(ctx, `UPDATE plugin_statistic_runs SET status='fenced',finished_at_unix_ms=$1,reason_code='AUTHORIZATION_REVALIDATION_FAILED' WHERE run_id=$2 AND status='pending'`, nowMS, runID)
			ok = false
			return err
		}
		claimGen++
		fence := digestOf(fmt.Sprintf("%s|%s|%d|%s|%d", runID, leaseID, claimGen, definitionDigest, bindingGen))
		tag, err := tx.Exec(ctx, `
				UPDATE plugin_statistic_runs
				SET status = 'running', claim_owner = $1,
				    claim_expires_unix_ms = $2, claim_lease_id=$3,claim_generation=$4,
				    result_fence=$5, reason_code = 'CLAIMED'
				WHERE run_id = $6 AND status = 'pending'`,
			owner, nowMS+leaseMS, leaseID, claimGen, fence, runID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("pluginstat: claim CAS conflict")
		}
		token = &ClaimToken{RunID: runID, LeaseID: leaseID, ClaimGeneration: claimGen, ResultFence: fence,
			BindingGeneration: bindingGen, DefinitionID: definitionID, DefinitionDigest: definitionDigest,
			PluginID: pluginID, PluginRevision: pluginRevision,
			Scope: scope, ExpiresAtUnixMS: nowMS + leaseMS, DeadlineMS: deadlineMS}
		ok = true
		return nil
	})
	if err != nil {
		return nil, false, fmt.Errorf("pluginstat: claim: %w", err)
	}
	return token, ok, nil
}

type Executor interface {
	ExecuteStatistics(context.Context, ClaimToken, InputBundle) (Artifact, error)
}

func (s *Service) DispatchNext(ctx context.Context, owner string, executor Executor, authorize RunAuthorizer) (bool, error) {
	if executor == nil {
		return false, errors.New("pluginstat: executor unavailable")
	}
	token, ok, err := s.ClaimNextPending(ctx, owner, 30000, authorize)
	if err != nil || !ok {
		return ok, err
	}
	var raw []byte
	err = s.pool.Pool.QueryRow(ctx, `SELECT b.bundle FROM plugin_statistic_input_bundles b JOIN plugin_statistic_runs r ON r.run_id=b.run_id
	 WHERE r.run_id=$1 AND r.status='running' AND r.claim_lease_id=$2 AND r.claim_generation=$3 AND r.result_fence=$4
	 AND r.claim_expires_unix_ms>$5`, token.RunID, token.LeaseID, token.ClaimGeneration, token.ResultFence, s.now().UnixMilli()).Scan(&raw)
	if err != nil {
		_ = s.FinishRun(ctx, *token, RunFenced, Artifact{})
		return true, fmt.Errorf("pluginstat: load claimed input: %w", err)
	}
	var bundle InputBundle
	if err := json.Unmarshal(raw, &bundle); err != nil {
		_ = s.FinishRun(ctx, *token, RunFenced, Artifact{})
		return true, err
	}
	execCtx, cancel := context.WithTimeout(ctx, time.Duration(token.DeadlineMS)*time.Millisecond)
	defer cancel()
	artifact, execErr := executor.ExecuteStatistics(execCtx, *token, bundle)
	if execErr != nil {
		status := RunFailed
		if errors.Is(execErr, context.DeadlineExceeded) || errors.Is(execCtx.Err(), context.DeadlineExceeded) {
			status = RunExpired
		}
		if finishErr := s.FinishRun(ctx, *token, status, Artifact{}); finishErr != nil {
			return true, errors.Join(execErr, finishErr)
		}
		return true, fmt.Errorf("pluginstat: executor failed: %w", execErr)
	}
	return true, s.FinishRun(ctx, *token, RunSucceeded, artifact)
}

// ExpireStaleRunning CAS-moves runs whose lease expired back to pending with
// an audit reason (bounded, idempotent).
func (s *Service) ExpireStaleRunning(ctx context.Context) (int64, error) {
	tag, err := s.pool.Pool.Exec(ctx, `
			UPDATE plugin_statistic_runs
				SET status = CASE WHEN claim_generation >= 3 THEN 'fenced' ELSE 'pending' END,
				    finished_at_unix_ms = CASE WHEN claim_generation >= 3 THEN $1 ELSE NULL END,
				    reason_code = CASE WHEN claim_generation >= 3 THEN 'CLAIM_RETRY_EXHAUSTED' ELSE 'LEASE_EXPIRED_REQUEUE' END,
				    claim_owner = NULL, claim_expires_unix_ms = NULL, claim_lease_id=NULL,result_fence=NULL
		WHERE status = 'running' AND claim_expires_unix_ms < $1`,
		s.now().UnixMilli())
	if err != nil {
		return 0, fmt.Errorf("pluginstat: expire stale: %w", err)
	}
	return tag.RowsAffected(), nil
}

// SweepRetention applies the configured bounded tail to statistics history and
// terminal execution material. Current artifacts and any still-referenced run
// are retained. Each statement deletes at most limit rows in a short transaction.
func (s *Service) SweepRetention(ctx context.Context, now time.Time, retention time.Duration, limit int) (int64, error) {
	if s == nil || s.pool == nil || retention < 24*time.Hour || retention > 365*24*time.Hour || limit < 1 || limit > 1000 {
		return 0, errors.New("pluginstat: retention arguments invalid")
	}
	var deleted int64
	err := s.pool.Pool.QueryRow(ctx, `SELECT masi_sweep_plugin_statistics_retention($1,$2)`,
		now.Add(-retention), limit).Scan(&deleted)
	return deleted, err
}

// ReleaseOwnedClaimsOnShutdown returns this process's bounded pure statistics
// runs to pending under a new future claim generation. Any late result from the
// cancelled process is fenced by the cleared lease/result token.
func (s *Service) ReleaseOwnedClaimsOnShutdown(ctx context.Context, owner string) (int64, error) {
	if !identityRE.MatchString(owner) {
		return 0, errors.New("pluginstat: claim owner malformed")
	}
	tag, err := s.pool.Pool.Exec(ctx, `UPDATE plugin_statistic_runs SET status='pending',
		reason_code='PROCESS_DRAIN_REQUEUE',claim_owner=NULL,claim_expires_unix_ms=NULL,
		claim_lease_id=NULL,result_fence=NULL WHERE status='running' AND claim_owner=$1`, owner)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// FinishRun validates the artifact and finalizes the run: succeeded runs get
// the validated artifact + current/history projection under a SHORT txn; a
// late result from an OLD binding generation is recorded audit-only and never
// overwrites the newer current projection.
func (s *Service) FinishRun(ctx context.Context, token ClaimToken, status RunStatus, artifact Artifact) error {
	if token.RunID == "" || token.LeaseID == "" || token.ResultFence == "" || token.ClaimGeneration < 1 {
		return errors.New("pluginstat: complete requires exact claim token")
	}
	switch status {
	case RunSucceeded:
		if artifact.Status != RunSucceeded {
			return errors.New("pluginstat: succeeded completion requires artifact status=succeeded")
		}
		if artifact.ArtifactDigest != ComputeArtifactDigest(artifact) {
			return errors.New("pluginstat: artifact digest mismatch")
		}
		if err := ValidateArtifact(artifact); err != nil {
			// The artifact failed validation: the run fails, never partially
			// records a dangerous payload.
			return s.failRun(ctx, token, "ARTIFACT_VALIDATION_ERROR", err)
		}
		return s.commitSucceeded(ctx, token, artifact)
	case RunFailed, RunCancelled, RunExpired, RunFenced:
		return s.failRun(ctx, token, "RUN_"+string(status), nil)
	default:
		return fmt.Errorf("pluginstat: finish status must be terminal (got %s)", status)
	}
}

func (s *Service) commitSucceeded(ctx context.Context, token ClaimToken, artifact Artifact) error {
	artifactJSON, err := json.Marshal(artifact)
	if err != nil {
		return s.failRun(ctx, token, "ARTIFACT_MARSHAL_ERROR", err)
	}
	return s.pool.WithTx(ctx, []db.TxOption{db.ReadCommitted()}, func(tx *db.Tx) error {
		var bindingGen int64
		var defID, defDigest, scope, producerKind string
		var externalCapabilities []string
		if err := tx.QueryRow(ctx, `
					SELECT r.binding_generation,r.definition_id,r.definition_digest,r.scope,
					       d.producer_kind,d.external_capability_ids
					FROM plugin_statistic_runs r JOIN plugin_statistics_definitions d ON d.definition_id=r.definition_id
					WHERE r.run_id = $1 AND r.status='running'
					 AND claim_lease_id=$2 AND claim_generation=$3 AND result_fence=$4
					 AND claim_expires_unix_ms >= $5 FOR UPDATE`, token.RunID, token.LeaseID,
			token.ClaimGeneration, token.ResultFence, s.now().UnixMilli()).
			Scan(&bindingGen, &defID, &defDigest, &scope, &producerKind, &externalCapabilities); err != nil {
			return err
		}
		if defID != artifact.DefinitionID || defDigest != artifact.DefinitionDigest || token.DefinitionID != defID ||
			token.DefinitionDigest != defDigest || token.BindingGeneration != bindingGen || token.Scope != scope ||
			artifact.RunID != token.RunID || artifact.Provenance.RunID != token.RunID ||
			artifact.Provenance.DefinitionID != defID || artifact.Provenance.DefinitionDigest != defDigest ||
			artifact.Provenance.BindingGeneration != bindingGen || artifact.Provenance.PluginID != token.PluginID ||
			artifact.Provenance.PluginRevision != token.PluginRevision {
			return fmt.Errorf("pluginstat: artifact definition %s != run definition %s", artifact.DefinitionID, defID)
		}
		if err := validateExternalProvenance(PluginKind(producerKind), externalCapabilities, artifact.Provenance.ExternalSource); err != nil {
			return err
		}
		var stillEligible bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(
				SELECT 1 FROM plugin_statistics_definitions d JOIN plugin_bindings b
				 ON b.plugin_id=d.plugin_id AND b.binding_generation=d.binding_generation
				 WHERE d.definition_id=$1 AND d.definition_digest=$2 AND d.binding_generation=$3
				   AND d.scope=$4 AND NOT d.revoked AND b.activation_state='active'
				   AND b.qualification_status='qualified')`, defID, defDigest, bindingGen, scope).Scan(&stillEligible); err != nil {
			return err
		}
		if !stillEligible {
			tag, err := tx.Exec(ctx, `UPDATE plugin_statistic_runs SET status='fenced',
					finished_at_unix_ms=$1,reason_code='BINDING_REVOKED_BEFORE_COMMIT'
					WHERE run_id=$2 AND status='running' AND claim_lease_id=$3
					  AND claim_generation=$4 AND result_fence=$5`, s.now().UnixMilli(), token.RunID,
				token.LeaseID, token.ClaimGeneration, token.ResultFence)
			if err != nil {
				return err
			}
			if tag.RowsAffected() != 1 {
				return errors.New("pluginstat: revoke fence CAS conflict")
			}
			return nil
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO plugin_statistic_artifacts (
				artifact_id, artifact_digest, run_id, definition_id,
				definition_digest, status, quality, artifact, bytes,
					truncated_rows, truncated_series, actor_ref, trace_id,binding_generation,result_fence)
				VALUES ($1,$2,$3,$4,$5,'succeeded',$6,$7,$8,$9,$10,'pluginstat','',$11,$12)`,
			artifact.ArtifactID, artifact.ArtifactDigest, token.RunID, artifact.DefinitionID,
			artifact.DefinitionDigest, string(artifact.Quality), artifactJSON,
			len(artifactJSON), artifact.Truncation.TruncatedRows, artifact.Truncation.TruncatedSeries, bindingGen, token.ResultFence); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `
				UPDATE plugin_statistic_runs
				SET status = 'succeeded', finished_at_unix_ms = $1,
				    artifact_id = $2, reason_code = 'SUCCEEDED'
				WHERE run_id = $3 AND claim_lease_id=$4 AND claim_generation=$5 AND result_fence=$6`,
			s.now().UnixMilli(), artifact.ArtifactID, token.RunID, token.LeaseID, token.ClaimGeneration, token.ResultFence)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return errors.New("pluginstat: finish CAS conflict")
		}
		// History is always appended (bounded tail retention elsewhere).
		if _, err := tx.Exec(ctx, `
			INSERT INTO plugin_statistics_history (
					definition_id, binding_generation, artifact_id, run_id, quality,scope)
				VALUES ($1,$2,$3,$4,$5,$6)`,
			artifact.DefinitionID, bindingGen, artifact.ArtifactID, token.RunID, string(artifact.Quality), scope); err != nil {
			return err
		}
		// Current projection: an OLD-generation late result never overwrites
		// a newer generation's current (STATISTICS-OLD-GEN-LATE-NO-OVERWRITE).
		if _, err := tx.Exec(ctx, `
				INSERT INTO plugin_statistics_current (
					definition_id, binding_generation, artifact_id, run_id, quality,scope)
				SELECT $1,$2,$3,$4,$5,$6 WHERE EXISTS(
				 SELECT 1 FROM plugin_statistics_definitions d JOIN plugin_bindings b
				 ON b.plugin_id=d.plugin_id AND b.binding_generation=d.binding_generation
				 WHERE d.definition_id=$1 AND d.definition_digest=$7 AND d.binding_generation=$2
				 AND d.scope=$6 AND NOT d.revoked AND b.activation_state='active' AND b.qualification_status='qualified')
				ON CONFLICT (definition_id) DO UPDATE SET
				binding_generation = EXCLUDED.binding_generation,
				artifact_id = EXCLUDED.artifact_id,
				run_id = EXCLUDED.run_id, quality = EXCLUDED.quality,
				updated_at = now()
			WHERE plugin_statistics_current.binding_generation <= EXCLUDED.binding_generation`,
			artifact.DefinitionID, bindingGen, artifact.ArtifactID, token.RunID, string(artifact.Quality), scope, defDigest); err != nil {
			return err
		}
		return nil
	})
}

func (s *Service) failRun(ctx context.Context, token ClaimToken, reason string, cause error) error {
	tag, err := s.pool.Pool.Exec(ctx, `
		UPDATE plugin_statistic_runs
			SET status = CASE
			 WHEN $2='RUN_cancelled' THEN 'cancelled'
			 WHEN $2='RUN_expired' THEN 'expired'
			 WHEN $2='RUN_fenced' THEN 'fenced'
			 ELSE 'failed' END,
		    finished_at_unix_ms = $3, reason_code = $2
			WHERE run_id = $1 AND status='running' AND claim_lease_id=$4
			 AND claim_generation=$5 AND result_fence=$6 AND claim_expires_unix_ms >= $3`,
		token.RunID, reason, s.now().UnixMilli(), token.LeaseID, token.ClaimGeneration, token.ResultFence)
	if err != nil {
		return fmt.Errorf("pluginstat: fail run (%s): %w", reason, err)
	}
	if tag.RowsAffected() != 1 {
		return errors.New("pluginstat: fail CAS conflict")
	}
	if cause != nil {
		return fmt.Errorf("pluginstat: %s: %w", reason, cause)
	}
	return nil
}

// Schedule revision lifecycle (immutable append-only revisions).
func (s *Service) CreateScheduleRevision(ctx context.Context, scheduleID, definitionID, definitionDigest string, intervalSeconds int, disabled bool, actor security.Actor, auth ScheduleAuthorization, traceID string) (int64, error) {
	if !identityRE.MatchString(scheduleID) || !identityRE.MatchString(definitionID) {
		return 0, errors.New("pluginstat: schedule/definition identity malformed")
	}
	if !digestRE.MatchString(definitionDigest) {
		return 0, errors.New("pluginstat: definition digest malformed")
	}
	if intervalSeconds < 60 || intervalSeconds > 86400 {
		return 0, errors.New("pluginstat: interval_seconds 60..86400")
	}
	if err := actor.Validate(); err != nil {
		return 0, err
	}
	if auth.Scope == "" || auth.DataClass == "" || !digestRE.MatchString(auth.TargetSetDigest) || !auth.PlatformAdmin || !auth.CSRFVerified || !auth.StepUpFresh || !auth.SourceRead || !auth.StatisticsRun {
		return 0, errors.New("pluginstat: schedule mutation requires scoped platform-admin, CSRF, and fresh step-up")
	}
	var rev int64
	err := s.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,0))`, scheduleID); err != nil {
			return err
		}
		var definitionOK bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM plugin_statistics_definitions
		 WHERE definition_id=$1 AND definition_digest=$2 AND scope=$3 AND data_class=$4 AND NOT revoked)`,
			definitionID, definitionDigest, auth.Scope, auth.DataClass).Scan(&definitionOK); err != nil {
			return err
		}
		if !definitionOK {
			return errors.New("pluginstat: schedule definition stale/revoked/out of scope")
		}
		if err := tx.QueryRow(ctx, `
			SELECT COALESCE(MAX(schedule_revision), 0) + 1
			FROM plugin_statistic_schedules WHERE schedule_id = $1`, scheduleID).
			Scan(&rev); err != nil {
			return err
		}
		reason := "CREATED"
		if rev > 1 {
			reason = "REVISED"
		}
		if disabled {
			reason = "DISABLED"
		}
		_, err := tx.Exec(ctx, `
				INSERT INTO plugin_statistic_schedules (
					schedule_revision, schedule_id, definition_id, definition_digest,
					interval_seconds, disabled, actor_ref, trace_id, reason_code,
					scope,data_class,actor_issuer,actor_subject,target_set_digest)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`,
			rev, scheduleID, definitionID, definitionDigest, intervalSeconds,
			disabled, actor.String(), traceID, reason, auth.Scope, auth.DataClass, actor.Issuer, actor.Subject, auth.TargetSetDigest)
		return err
	})
	if err != nil {
		return 0, fmt.Errorf("pluginstat: create schedule revision: %w", err)
	}
	return rev, nil
}

type scheduledRun struct {
	scheduleID, definitionID, definitionDigest, scope, dataClass, targetSetDigest, issuer, subject string
	revision                                                                                       int64
	intervalSeconds                                                                                int
}

// MaterializeDueSchedules is the single bounded derivation from the append-only
// schedule ledger into plugin_statistic_runs. It has no independent queue.
func (s *Service) MaterializeDueSchedules(ctx context.Context, authorize func(security.Actor, string, string, string) bool) (int, error) {
	if authorize == nil {
		return 0, errors.New("pluginstat: schedule authorizer unavailable")
	}
	nowMS := s.now().UnixMilli()
	rows, err := s.pool.Pool.Query(ctx, `WITH latest AS(
	 SELECT DISTINCT ON(schedule_id) * FROM plugin_statistic_schedules ORDER BY schedule_id,schedule_revision DESC)
	 SELECT s.schedule_id,s.schedule_revision,s.definition_id,s.definition_digest,s.interval_seconds,s.scope,s.data_class,
	        s.target_set_digest,s.actor_issuer,s.actor_subject
	 FROM latest s LEFT JOIN plugin_statistic_runs r ON r.schedule_id=s.schedule_id
	 WHERE NOT s.disabled GROUP BY s.schedule_id,s.schedule_revision,s.definition_id,s.definition_digest,s.interval_seconds,
	  s.scope,s.data_class,s.target_set_digest,s.actor_issuer,s.actor_subject
	 HAVING COALESCE(max(r.started_at_unix_ms),0) <= $1-(s.interval_seconds::bigint*1000)
	 ORDER BY s.schedule_id LIMIT 4`, nowMS)
	if err != nil {
		return 0, err
	}
	due := []scheduledRun{}
	for rows.Next() {
		var item scheduledRun
		if err := rows.Scan(&item.scheduleID, &item.revision, &item.definitionID, &item.definitionDigest, &item.intervalSeconds, &item.scope, &item.dataClass, &item.targetSetDigest, &item.issuer, &item.subject); err != nil {
			rows.Close()
			return 0, err
		}
		due = append(due, item)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()
	created := 0
	for _, item := range due {
		actor := security.Actor{Issuer: item.issuer, Subject: item.subject}
		if actor.Validate() != nil || !authorize(actor, item.scope, item.dataClass, item.targetSetDigest) {
			continue
		}
		intervalMS := int64(item.intervalSeconds) * 1000
		end := (nowMS / intervalMS) * intervalMS
		start := end - intervalMS
		idempotency := fmt.Sprintf("sched:%s:%d:%d", item.scheduleID, item.revision, end)
		runID := "statrun-" + shortDigest(digestOf(idempotency))
		_, err := s.StartOnDemand(ctx, OnDemandRequest{RunID: runID, DefinitionID: item.definitionID, DefinitionDigest: item.definitionDigest, IdempotencyKey: idempotency, Scope: item.scope, DataClass: item.dataClass, TargetSetDigest: item.targetSetDigest, WindowStartUnixMS: start, WindowEndUnixMS: end, TraceID: "schedule:" + item.scheduleID, ScheduleID: item.scheduleID}, actor, RunAuthorization{Scope: item.scope, DataClass: item.dataClass, TargetSetDigest: item.targetSetDigest, SourceRead: true, StatisticsRun: true})
		if err != nil {
			return created, err
		}
		created++
	}
	return created, nil
}

func digestOf(s string) string {
	h := sha256.Sum256([]byte(s))
	return "sha256:" + hex.EncodeToString(h[:])
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

func canonicalScalar(v any) string {
	switch t := v.(type) {
	case string:
		return "s:" + t
	case bool:
		if t {
			return "b:1"
		}
		return "b:0"
	case float32:
		return fmt.Sprintf("f:%016x", math.Float64bits(float64(t)))
	case float64:
		return fmt.Sprintf("f:%016x", math.Float64bits(t))
	case int8:
		return fmt.Sprintf("i:%d", t)
	case int16:
		return fmt.Sprintf("i:%d", t)
	case int32:
		return fmt.Sprintf("i:%d", t)
	case int64:
		return fmt.Sprintf("i:%d", t)
	case int:
		return fmt.Sprintf("i:%d", t)
	case uint:
		return fmt.Sprintf("u:%d", t)
	case uint8:
		return fmt.Sprintf("u:%d", t)
	case uint16:
		return fmt.Sprintf("u:%d", t)
	case uint32:
		return fmt.Sprintf("u:%d", t)
	case uint64:
		return fmt.Sprintf("u:%d", t)
	case json.Number:
		if integer, err := t.Int64(); err == nil {
			return fmt.Sprintf("i:%d", integer)
		}
		if number, err := t.Float64(); err == nil {
			return fmt.Sprintf("f:%016x", math.Float64bits(number))
		}
	}
	return "invalid"
}
