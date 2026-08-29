// Package workload provides bounded, public-SQL qualification workloads for
// PostgreSQL State. It is test infrastructure, not a second business writer.
package workload

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Identity verifies the exact public boundary before any workload runs.
type Identity struct {
	Database         string `json:"database"`
	User             string `json:"user"`
	ServerVersionNum int    `json:"server_version_num"`
	TLS              bool   `json:"tls"`
}

// Open creates one bounded pool and rejects the wrong database, PostgreSQL
// major, identity, or TLS downgrade.
func Open(ctx context.Context, dsn, confirmedDatabase, expectedUser string, requireTLS bool, maxConnections int32) (*pgxpool.Pool, Identity, error) {
	if maxConnections < 1 || maxConnections > 32 {
		return nil, Identity{}, errors.New("workload: max connections outside 1..32")
	}
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, Identity{}, fmt.Errorf("workload: parse dsn: %w", err)
	}
	config.MaxConns = maxConnections
	config.MinConns = 1
	config.MaxConnLifetime = 2 * time.Minute
	config.MaxConnIdleTime = 30 * time.Second
	config.HealthCheckPeriod = 10 * time.Second
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, Identity{}, fmt.Errorf("workload: open pool: %w", err)
	}
	identity := Identity{}
	if err := pool.QueryRow(ctx, `SELECT current_database(),current_user,
		current_setting('server_version_num')::int,
		EXISTS(SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl)`).Scan(
		&identity.Database, &identity.User, &identity.ServerVersionNum, &identity.TLS); err != nil {
		pool.Close()
		return nil, Identity{}, fmt.Errorf("workload: identity: %w", err)
	}
	if identity.Database != confirmedDatabase || identity.User != expectedUser {
		pool.Close()
		return nil, Identity{}, errors.New("workload: database or identity mismatch")
	}
	if identity.ServerVersionNum < 180000 || identity.ServerVersionNum >= 190000 {
		pool.Close()
		return nil, Identity{}, errors.New("workload: PostgreSQL 18 required")
	}
	if requireTLS && !identity.TLS {
		pool.Close()
		return nil, Identity{}, errors.New("workload: TLS required")
	}
	return pool, identity, nil
}

// CapacityMeasurement is one rolled-back exact-schema capacity trial.
type CapacityMeasurement struct {
	RuleCount          int   `json:"rule_count"`
	RevisionJSONRules  int   `json:"revision_json_rules"`
	EpochRows          int   `json:"epoch_rows"`
	ObservationRows    int   `json:"observation_rows"`
	Rollup5mRows       int   `json:"rollup_5m_rows"`
	Rollup1hRows       int   `json:"rollup_1h_rows"`
	BuildMicroseconds  int64 `json:"build_microseconds"`
	CurrentQueryMicros int64 `json:"current_query_microseconds"`
	PageQueryMicros    int64 `json:"page_query_microseconds"`
	TrendQueryMicros   int64 `json:"trend_query_microseconds"`
	RolledBack         bool  `json:"rolled_back"`
}

// RunCapacity executes 0/128/1,024/4,096 trials against the actual target,
// governance, firewall and rule-observation tables. Each trial rolls back.
func RunCapacity(ctx context.Context, pool *pgxpool.Pool, counts []int) ([]CapacityMeasurement, error) {
	if len(counts) != 4 || counts[0] != 0 || counts[1] != 128 || counts[2] != 1024 || counts[3] != 4096 {
		return nil, errors.New("capacity: exact 0/128/1024/4096 matrix required")
	}
	measurements := make([]CapacityMeasurement, 0, len(counts))
	for _, count := range counts {
		measurement, err := capacityTrial(ctx, pool, count)
		if err != nil {
			return nil, fmt.Errorf("capacity %d: %w", count, err)
		}
		measurements = append(measurements, measurement)
	}
	return measurements, nil
}

func capacityTrial(ctx context.Context, pool *pgxpool.Pool, count int) (out CapacityMeasurement, resultErr error) {
	out.RuleCount = count
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return out, err
	}
	defer func() {
		if rollbackErr := tx.Rollback(context.Background()); rollbackErr == nil || errors.Is(rollbackErr, pgx.ErrTxClosed) {
			out.RolledBack = true
		} else if resultErr == nil {
			resultErr = rollbackErr
		}
	}()
	prefix := fmt.Sprintf("db-cap-%d-%d", count, time.Now().UnixNano())
	targetID := prefix + "-target"
	proposalID, decisionID, intentID := prefix+"-proposal", prefix+"-decision", prefix+"-intent"
	operationID, revisionID := prefix+"-operation", prefix+"-revision"
	digest := fmt.Sprintf("sha256:%064x", count+1)
	nowMS := time.Now().UnixMilli()
	buildStarted := time.Now()
	statements := []struct {
		query string
		args  []any
	}{
		{`INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,role,status,
			desired_profile_digest,credential_ref,scope,provenance,actor_ref,trace_id)
		  VALUES($1,'DB capacity target','dns:///db-capacity:9559',1,'db-capacity','active',
		  $2,'secret://module-gate','module-test','{"source":"module-gate"}',
		  'module-gate','module-capacity')`, []any{targetID, digest}},
		{`INSERT INTO effect_proposals(proposal_id,proposal_digest,actor_ref,scope,risk_level,
			effect_kind,target_set_digest,policy_digest,evidence_refs,expires_at_unix_ms,note,
			created_at_unix_ms,trace_id,reason_code)
		  VALUES($1,$2,'module-maker','module-test','R1','firewall-baseline',$3,$4,'[]',$5,
		  'bounded database capacity fixture',$6,'module-capacity','MODULE_CAPACITY')`,
			[]any{proposalID, prefix + "-proposal-digest", digest, digest, nowMS + 600000, nowMS}},
		{`INSERT INTO effect_decisions(decision_id,proposal_id,proposal_digest,actor_ref,risk_level,
			decision,authz_context,decision_digest,expires_at_unix_ms,created_at_unix_ms,trace_id,reason_code)
		  VALUES($1,$2,$3,'module-operator','R1','approve','{"module_gate":true}',$4,$5,$6,
		  'module-capacity','MODULE_CAPACITY')`,
			[]any{decisionID, proposalID, prefix + "-proposal-digest", prefix + "-decision-digest", nowMS + 600000, nowMS}},
		{`INSERT INTO effect_intents(effect_intent_id,operation_id,proposal_id,proposal_digest,
			decision_id,target_id,fence,effect_digest,authorization_digest,effect_kind,risk_level,
			deadline_unix_ms,claim_state,actor_ref,trace_id,reason_code,effect_payload)
		  VALUES($1,$2,$3,$4,$5,$6,'{"generation":1}',$7,$8,'firewall-baseline','R1',$9,
		  'unclaimed','module-gate','module-capacity','MODULE_CAPACITY','{"profile":"module"}')`,
			[]any{intentID, operationID, proposalID, prefix + "-proposal-digest", decisionID, targetID,
				prefix + "-effect-digest", prefix + "-decision-digest", nowMS + 600000}},
		{`INSERT INTO firewall_revisions(revision_id,revision_digest,target_id,default_action,rules,
			scope,actor_ref,reason_code)
		  SELECT $1,$2,$3,'permit-and-continue',COALESCE(jsonb_agg(jsonb_build_object(
			'rule_id',format('rule-%s',i),'priority',100000-i,'action','drop')),'[]'::jsonb),
			'module-test','module-gate','MODULE_CAPACITY' FROM generate_series(1,$4) AS g(i)`,
			[]any{revisionID, digest, targetID, count}},
		{`INSERT INTO rule_observation_epochs(epoch_id,effect_intent_id,operation_id,entity_id,
			rule_id,target_id,canonical_entry_digest,match_priority_action_digest,
			observation_epoch,reset_epoch,installation_readback)
		  SELECT $1||'-epoch-'||i,$2,$3,$4,$1||'-rule-'||i,$5,
			$1||'-entry-'||i,$1||'-match-'||i,1,1,'exact'
		  FROM generate_series(1,$6) AS g(i)`, []any{prefix, intentID, operationID, revisionID, targetID, count}},
		{`INSERT INTO rule_observations(epoch_id,sample_sequence,read_completed_at_unix_ms,
			packets,bytes,eligible_packets,direct_delta_packets,direct_delta_bytes,
			eligible_delta_packets,rate,coverage,quality_status,outcome_status,outcome_expected,outcome_actual)
		  SELECT $1||'-epoch-'||i,1,$2,i,i*64,i,i,i*64,i,1,1,'valid','observed','dropped','dropped'
		  FROM generate_series(1,$3) AS g(i)`, []any{prefix, nowMS, count}},
		{`INSERT INTO rule_rollups_5m(window_start_unix_ms,epoch_id,observation_epoch,reset_epoch,
			rule_id,direct_packets,direct_bytes,eligible_packets,sample_count,quality_status)
		  SELECT $2,$1||'-epoch-'||i,1,1,$1||'-rule-'||i,i,i*64,i,1,'valid'
		  FROM generate_series(1,$3) AS g(i)`, []any{prefix, nowMS - nowMS%300000, count}},
		{`INSERT INTO rule_rollups_1h(window_start_unix_ms,epoch_id,observation_epoch,reset_epoch,
			rule_id,direct_packets,direct_bytes,eligible_packets,sample_count,quality_status)
		  SELECT $2,$1||'-epoch-'||i,1,1,$1||'-rule-'||i,i,i*64,i,1,'valid'
		  FROM generate_series(1,$3) AS g(i)`, []any{prefix, nowMS - nowMS%3600000, count}},
	}
	for _, statement := range statements {
		if _, err := tx.Exec(ctx, statement.query, statement.args...); err != nil {
			return out, err
		}
	}
	out.BuildMicroseconds = time.Since(buildStarted).Microseconds()
	if err := tx.QueryRow(ctx, `SELECT jsonb_array_length(rules) FROM firewall_revisions WHERE revision_id=$1`, revisionID).Scan(&out.RevisionJSONRules); err != nil {
		return out, err
	}
	currentStarted := time.Now()
	if err := tx.QueryRow(ctx, `SELECT count(*) FROM rule_observation_epochs e
		JOIN rule_observations o USING(epoch_id) WHERE e.target_id=$1`, targetID).Scan(&out.ObservationRows); err != nil {
		return out, err
	}
	out.CurrentQueryMicros = time.Since(currentStarted).Microseconds()
	if err := tx.QueryRow(ctx, `SELECT count(*) FROM rule_observation_epochs WHERE target_id=$1`, targetID).Scan(&out.EpochRows); err != nil {
		return out, err
	}
	pageStarted := time.Now()
	var pageRows int
	if err := tx.QueryRow(ctx, `SELECT count(*) FROM (
		SELECT rule_id FROM rule_observation_epochs WHERE target_id=$1 ORDER BY rule_id LIMIT 200
	) page`, targetID).Scan(&pageRows); err != nil {
		return out, err
	}
	out.PageQueryMicros = time.Since(pageStarted).Microseconds()
	trendStarted := time.Now()
	if count > 0 {
		if err := tx.QueryRow(ctx, `SELECT count(*) FROM rule_rollups_5m
			WHERE rule_id=$1 AND window_start_unix_ms BETWEEN $2 AND $3`,
			prefix+"-rule-1", nowMS-86400000, nowMS).Scan(&out.Rollup5mRows); err != nil {
			return out, err
		}
	} else {
		out.Rollup5mRows = 0
	}
	out.TrendQueryMicros = time.Since(trendStarted).Microseconds()
	if err := tx.QueryRow(ctx, `SELECT count(*) FROM rule_rollups_1h r
		JOIN rule_observation_epochs e USING(epoch_id) WHERE e.target_id=$1`, targetID).Scan(&out.Rollup1hRows); err != nil {
		return out, err
	}
	if out.RevisionJSONRules != count || out.EpochRows != count || out.ObservationRows != count ||
		out.Rollup5mRows != min(count, 1) || out.Rollup1hRows != count || pageRows != min(count, 200) {
		return out, errors.New("capacity: row-count oracle mismatch")
	}
	return out, nil
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// SoakConfig fixes one bounded four-phase workload.
type SoakConfig struct {
	PhaseDuration time.Duration
	SampleEvery   time.Duration
	Warmup        time.Duration
	Formal        bool
}

type Phase struct {
	Name        string `json:"name"`
	TargetQPS   int    `json:"target_qps"`
	Concurrency int    `json:"concurrency"`
}

type SoakSample struct {
	Timestamp       string `json:"timestamp"`
	Phase           string `json:"phase"`
	Operations      uint64 `json:"operations"`
	Errors          uint64 `json:"errors"`
	AverageMicros   uint64 `json:"average_microseconds"`
	MaxMicros       uint64 `json:"max_microseconds"`
	P99UpperBoundMS int    `json:"p99_upper_bound_ms"`
	PoolTotal       int32  `json:"pool_total"`
	PoolAcquired    int32  `json:"pool_acquired"`
	PoolIdle        int32  `json:"pool_idle"`
}

type SoakResult struct {
	StartedAt        string       `json:"started_at"`
	FinishedAt       string       `json:"finished_at"`
	MonotonicSeconds float64      `json:"monotonic_seconds"`
	WarmupSeconds    float64      `json:"warmup_seconds"`
	QualifiedSeconds float64      `json:"qualified_seconds"`
	Formal           bool         `json:"formal"`
	PhaseDurationSec int          `json:"phase_duration_seconds"`
	Samples          []SoakSample `json:"samples"`
	Operations       uint64       `json:"operations"`
	Errors           uint64       `json:"errors"`
	CleanupRows      int64        `json:"cleanup_rows"`
	FirstErrors      []string     `json:"first_errors"`
}

var phases = []Phase{
	{Name: "steady", TargetQPS: 20, Concurrency: 4},
	{Name: "peak", TargetQPS: 80, Concurrency: 12},
	{Name: "saturation", TargetQPS: 160, Concurrency: 32},
	{Name: "recovery-or-activation", TargetQPS: 40, Concurrency: 8},
}

var latencyBounds = []time.Duration{
	time.Millisecond, 2 * time.Millisecond, 5 * time.Millisecond, 10 * time.Millisecond,
	25 * time.Millisecond, 50 * time.Millisecond, 100 * time.Millisecond, 250 * time.Millisecond,
	500 * time.Millisecond, time.Second, 3 * time.Second, 10 * time.Second,
}

type counters struct {
	operations atomic.Uint64
	errors     atomic.Uint64
	micros     atomic.Uint64
	maxMicros  atomic.Uint64
	buckets    []atomic.Uint64
	mu         sync.Mutex
	first      []string
}

func newCounters() *counters { return &counters{buckets: make([]atomic.Uint64, len(latencyBounds))} }

func (c *counters) record(elapsed time.Duration, err error) {
	c.operations.Add(1)
	micros := uint64(elapsed.Microseconds())
	c.micros.Add(micros)
	for {
		old := c.maxMicros.Load()
		if micros <= old || c.maxMicros.CompareAndSwap(old, micros) {
			break
		}
	}
	for index, bound := range latencyBounds {
		if elapsed <= bound {
			c.buckets[index].Add(1)
			break
		}
	}
	if err != nil {
		c.errors.Add(1)
		c.mu.Lock()
		if len(c.first) < 16 {
			c.first = append(c.first, err.Error())
		}
		c.mu.Unlock()
	}
}

type snapshot struct {
	ops, errs, micros, max uint64
	buckets                []uint64
}

func (c *counters) snapshot() snapshot {
	out := snapshot{ops: c.operations.Load(), errs: c.errors.Load(), micros: c.micros.Load(), max: c.maxMicros.Load(), buckets: make([]uint64, len(c.buckets))}
	for index := range c.buckets {
		out.buckets[index] = c.buckets[index].Load()
	}
	return out
}

func deltaSample(now time.Time, phase string, before, after snapshot, stats *pgxpool.Stat) SoakSample {
	ops := after.ops - before.ops
	micros := after.micros - before.micros
	average := uint64(0)
	if ops != 0 {
		average = micros / ops
	}
	threshold := (ops*99 + 99) / 100
	cumulative := uint64(0)
	p99 := int(latencyBounds[len(latencyBounds)-1] / time.Millisecond)
	for index := range after.buckets {
		cumulative += after.buckets[index] - before.buckets[index]
		if cumulative >= threshold {
			p99 = int(latencyBounds[index] / time.Millisecond)
			break
		}
	}
	return SoakSample{Timestamp: now.UTC().Format(time.RFC3339Nano), Phase: phase,
		Operations: ops, Errors: after.errs - before.errs, AverageMicros: average,
		MaxMicros: after.max, P99UpperBoundMS: p99, PoolTotal: stats.TotalConns(),
		PoolAcquired: stats.AcquiredConns(), PoolIdle: stats.IdleConns()}
}

// RunSoak executes four fixed phases. Formal mode rejects any duration other
// than four times 900 seconds; short runs are explicitly rehearsal only.
func RunSoak(ctx context.Context, pool *pgxpool.Pool, cfg SoakConfig, progress func(SoakSample)) (*SoakResult, error) {
	if cfg.Formal && cfg.PhaseDuration != 900*time.Second {
		return nil, errors.New("soak: formal phase duration must be exactly 900 seconds")
	}
	if cfg.Formal && cfg.Warmup != 60*time.Second {
		return nil, errors.New("soak: formal warmup must be exactly 60 seconds")
	}
	if cfg.Warmup < 0 || cfg.Warmup > 60*time.Second {
		return nil, errors.New("soak: warmup outside 0..60 seconds")
	}
	if cfg.PhaseDuration < time.Second || cfg.PhaseDuration > 900*time.Second {
		return nil, errors.New("soak: phase duration outside 1..900 seconds")
	}
	if cfg.SampleEvery < time.Second || cfg.SampleEvery > 30*time.Second {
		return nil, errors.New("soak: sample interval outside 1..30 seconds")
	}
	started := time.Now()
	result := &SoakResult{StartedAt: started.UTC().Format(time.RFC3339Nano), Formal: cfg.Formal,
		PhaseDurationSec: int(cfg.PhaseDuration.Seconds()), Samples: []SoakSample{}, FirstErrors: []string{}}
	all := newCounters()
	if cfg.Warmup > 0 {
		warmupStarted := time.Now()
		if err := runPhase(ctx, pool, Phase{Name: "warmup", TargetQPS: 20, Concurrency: 4},
			-1, SoakConfig{PhaseDuration: cfg.Warmup, SampleEvery: cfg.SampleEvery},
			all, &result.Samples, progress); err != nil {
			return result, err
		}
		result.WarmupSeconds = time.Since(warmupStarted).Seconds()
	}
	qualifiedStarted := time.Now()
	for phaseIndex, phase := range phases {
		if err := runPhase(ctx, pool, phase, phaseIndex, cfg, all, &result.Samples, progress); err != nil {
			return result, err
		}
	}
	finished := time.Now()
	result.QualifiedSeconds = finished.Sub(qualifiedStarted).Seconds()
	result.FinishedAt = finished.UTC().Format(time.RFC3339Nano)
	result.MonotonicSeconds = finished.Sub(started).Seconds()
	result.Operations = all.operations.Load()
	result.Errors = all.errors.Load()
	all.mu.Lock()
	result.FirstErrors = append(result.FirstErrors, all.first...)
	all.mu.Unlock()
	command, err := pool.Exec(ctx, `DELETE FROM api_mutation_idempotency
		WHERE actor_issuer='module-soak' AND actor_subject='postgresql-state'`)
	if err != nil {
		return result, fmt.Errorf("soak cleanup: %w", err)
	}
	result.CleanupRows = command.RowsAffected()
	if result.Errors != 0 {
		return result, fmt.Errorf("soak: %d operations failed", result.Errors)
	}
	if cfg.Formal && result.QualifiedSeconds < 3600 {
		return result, errors.New("soak: formal monotonic duration below 3600 seconds")
	}
	return result, nil
}

func runPhase(ctx context.Context, pool *pgxpool.Pool, phase Phase, phaseIndex int, cfg SoakConfig,
	all *counters, samples *[]SoakSample, progress func(SoakSample)) error {
	phaseCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	deadline := time.NewTimer(cfg.PhaseDuration)
	defer deadline.Stop()
	interval := time.Second / time.Duration(phase.TargetQPS)
	dispatch := time.NewTicker(interval)
	sampleTicker := time.NewTicker(cfg.SampleEvery)
	defer dispatch.Stop()
	defer sampleTicker.Stop()
	semaphore := make(chan struct{}, phase.Concurrency)
	var wait sync.WaitGroup
	previous := all.snapshot()
	sequence := atomic.Uint64{}
	for {
		select {
		case <-ctx.Done():
			cancel()
			wait.Wait()
			return ctx.Err()
		case <-deadline.C:
			// Stop dispatching first, then let the bounded in-flight set finish
			// against the still-live phase context. A planned phase boundary is
			// not classified as a database operation failure.
			wait.Wait()
			now := time.Now()
			current := all.snapshot()
			if current.ops != previous.ops {
				sample := deltaSample(now, phase.Name, previous, current, pool.Stat())
				*samples = append(*samples, sample)
				if progress != nil {
					progress(sample)
				}
			}
			return nil
		case <-sampleTicker.C:
			now := time.Now()
			current := all.snapshot()
			sample := deltaSample(now, phase.Name, previous, current, pool.Stat())
			previous = current
			*samples = append(*samples, sample)
			if len(*samples) > 400 {
				return errors.New("soak: sample bound exceeded")
			}
			if progress != nil {
				progress(sample)
			}
		case <-dispatch.C:
			select {
			case semaphore <- struct{}{}:
				wait.Add(1)
				seq := sequence.Add(1)
				go func() {
					defer wait.Done()
					defer func() { <-semaphore }()
					started := time.Now()
					opCtx, opCancel := context.WithTimeout(phaseCtx, 10*time.Second)
					err := soakOperation(opCtx, pool, phaseIndex, seq)
					opCancel()
					all.record(time.Since(started), err)
				}()
			default:
				all.record(0, errors.New("soak: bounded concurrency saturated"))
			}
		}
	}
}

func soakOperation(ctx context.Context, pool *pgxpool.Pool, phaseIndex int, sequence uint64) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	key := fmt.Sprintf("phase-%d-slot-%04d", phaseIndex, sequence%1024)
	digest := fmt.Sprintf("sha256:%064x", sequence)
	_, err = tx.Exec(ctx, `INSERT INTO api_mutation_idempotency(
		actor_issuer,actor_subject,idempotency_key,request_digest,status,response_status,
		response_body,completed_at)
	VALUES('module-soak','postgresql-state',$1,$2,'completed',200,'{}',clock_timestamp())
	ON CONFLICT(actor_issuer,actor_subject,idempotency_key) DO UPDATE SET
		request_digest=EXCLUDED.request_digest,status='completed',response_status=200,
		response_body='{}',completed_at=clock_timestamp()`, key, digest)
	if err != nil {
		return err
	}
	var version, chain string
	if err := tx.QueryRow(ctx, `SELECT value,checksum FROM masi_schema_meta WHERE key='version'`).Scan(&version, &chain); err != nil {
		return err
	}
	if version != "22" || len(chain) != 71 {
		return errors.New("soak: schema readback mismatch")
	}
	return tx.Commit(ctx)
}

// Phases returns a stable copy for evidence production.
func Phases() []Phase {
	return append([]Phase(nil), phases...)
}
