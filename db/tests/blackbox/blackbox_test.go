package blackbox

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"masi-nids/postgresql-state/internal/migrate"
	"masi-nids/postgresql-state/internal/securefile"
	"masi-nids/postgresql-state/internal/state"
)

const digestA = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

var databaseSequence atomic.Uint64

func requireAdminDSN(t *testing.T) string {
	t.Helper()
	var dsn string
	if path := os.Getenv("MASI_DB_TEST_ADMIN_DSN_FILE"); path != "" {
		var err error
		dsn, err = securefile.ReadSecret(path)
		if err != nil {
			t.Fatalf("read MASI_DB_TEST_ADMIN_DSN_FILE: %v", err)
		}
	} else {
		dsn = os.Getenv("MASI_DB_TEST_ADMIN_DSN")
	}
	if dsn == "" {
		if os.Getenv("MASI_DB_BLACKBOX_REQUIRED") == "1" {
			t.Fatal("MASI_DB_TEST_ADMIN_DSN_FILE required")
		}
		t.Skip("set MASI_DB_TEST_ADMIN_DSN_FILE for real PostgreSQL 18 blackbox tests")
	}
	return dsn
}

func requireTLS() bool { return os.Getenv("MASI_DB_TEST_REQUIRE_TLS") != "0" }

func repoMigrations(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", "migrations"))
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func newTestDatabase(t *testing.T, label string) (string, string) {
	t.Helper()
	adminConfig, err := pgx.ParseConfig(requireAdminDSN(t))
	if err != nil {
		t.Fatal(err)
	}
	adminConfig.Database = "postgres"
	admin, err := pgx.ConnectConfig(context.Background(), adminConfig)
	if err != nil {
		t.Fatalf("connect admin database: %v", err)
	}
	defer admin.Close(context.Background())
	name := fmt.Sprintf("masi_state_%s_test_%d", label, databaseSequence.Add(1))
	if !migrate.IsTestDatabase(name) || len(name) > 63 {
		t.Fatalf("unsafe generated database name %q", name)
	}
	identifier := pgx.Identifier{name}.Sanitize()
	if _, err := admin.Exec(context.Background(), "CREATE DATABASE "+identifier); err != nil {
		t.Fatalf("create exact test database %s: %v", name, err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		cleanup, err := pgx.ConnectConfig(ctx, adminConfig)
		if err != nil {
			t.Errorf("cleanup connect: %v", err)
			return
		}
		defer cleanup.Close(context.Background())
		_, _ = cleanup.Exec(ctx, `SELECT pg_terminate_backend(pid) FROM pg_stat_activity
			WHERE datname=$1 AND pid<>pg_backend_pid()`, name)
		if _, err := cleanup.Exec(ctx, "DROP DATABASE "+identifier); err != nil {
			t.Errorf("drop exact test database %s: %v", name, err)
		}
	})
	parsed, err := url.Parse(requireAdminDSN(t))
	if err != nil || parsed.Scheme == "" {
		t.Fatalf("blackbox admin DSN must use a PostgreSQL URL: %v", err)
	}
	parsed.Path = "/" + name
	return name, parsed.String()
}

func migrateDatabase(t *testing.T, name, dsn, directory string) *migrate.Result {
	t.Helper()
	return migrateDatabaseVersion(t, name, dsn, directory, "22")
}

func migrateDatabaseVersion(t *testing.T, name, dsn, directory, expectedVersion string) *migrate.Result {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	result, err := migrate.Run(ctx, migrate.Config{
		DSN: dsn, Directory: directory, ConfirmedDatabase: name,
		RequireTestDatabase: true, RequireTLS: requireTLS(), HistoryTable: migrate.ProductionHistory,
		ExpectedSchemaVersion: expectedVersion, SourceRevision: "db-blackbox-test",
		LockTimeout: 5 * time.Second, StatementTimeout: 2 * time.Minute,
	})
	if err != nil {
		t.Fatalf("migrate %s: %v", name, err)
	}
	return result
}

func connect(t *testing.T, dsn string) *pgx.Conn {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close(context.Background()) })
	return conn
}

func copyChain(t *testing.T, count int) string {
	t.Helper()
	source := repoMigrations(t)
	entries, err := os.ReadDir(source)
	if err != nil {
		t.Fatal(err)
	}
	target := t.TempDir()
	copied := 0
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".sql") {
			continue
		}
		if copied >= count {
			break
		}
		raw, err := os.ReadFile(filepath.Join(source, entry.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(target, entry.Name()), raw, 0o600); err != nil {
			t.Fatal(err)
		}
		copied++
	}
	if copied != count {
		t.Fatalf("copied %d migrations, expected %d", copied, count)
	}
	return target
}

func TestFreshRepeatAndCatalogReadback(t *testing.T) {
	name, dsn := newTestDatabase(t, "fresh")
	first := migrateDatabase(t, name, dsn, repoMigrations(t))
	if len(first.Applied) != 31 || len(first.AlreadyApplied) != 0 {
		t.Fatalf("fresh result applied=%d existing=%d", len(first.Applied), len(first.AlreadyApplied))
	}
	second := migrateDatabase(t, name, dsn, repoMigrations(t))
	if len(second.Applied) != 0 || len(second.AlreadyApplied) != 31 || first.ChainDigest != second.ChainDigest {
		t.Fatalf("repeat result: %+v", second)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	inspection, err := state.Inspect(ctx, dsn, name, migrate.ProductionHistory, requireTLS())
	if err != nil {
		t.Fatal(err)
	}
	if inspection.ServerVersionNum < 180000 || inspection.ServerVersionNum >= 190000 ||
		inspection.SchemaVersion != "22" || inspection.MigrationRows != 31 ||
		inspection.SchemaChainDigest != first.ChainDigest {
		t.Fatalf("inspection mismatch: %+v", inspection)
	}
	conn := connect(t, dsn)
	var unsafeDefaults, validatedConstraints int
	if err := conn.QueryRow(context.Background(), `
SELECT
  count(*) FILTER (WHERE column_default IS NOT NULL),
  (SELECT count(*) FROM pg_constraint
   WHERE conname IN ('firewall_activation_expected_cas_nonzero_v22',
                     'fleet_operation_digest_nonzero_v22',
                     'analysis_artifact_digest_nonzero_v22') AND convalidated)
FROM information_schema.columns
WHERE (table_name,column_name) IN (
  ('firewall_activations','expected_cas_digest'),
  ('fleet_operations','operation_digest'),
  ('fleet_operations','completed_vector_digest'),
  ('analysis_artifacts','input_digest'),
  ('analysis_artifacts','tool_trajectory_digest'))`).Scan(&unsafeDefaults, &validatedConstraints); err != nil {
		t.Fatal(err)
	}
	if unsafeDefaults != 0 || validatedConstraints != 3 {
		t.Fatalf("zero-digest hardening missing defaults=%d constraints=%d", unsafeDefaults, validatedConstraints)
	}
}

func TestPreviousToCurrentChecksumDriftAndInterruptedRetry(t *testing.T) {
	t.Run("previous-to-current", func(t *testing.T) {
		name, dsn := newTestDatabase(t, "previous")
		migrateDatabaseVersion(t, name, dsn, copyChain(t, 30), "21")
		result := migrateDatabase(t, name, dsn, repoMigrations(t))
		if len(result.Applied) != 1 || result.Applied[0] != "0031_reject_zero_digest_defaults.sql" {
			t.Fatalf("upgrade did not apply only 0031: %+v", result.Applied)
		}
	})

	t.Run("checksum-drift", func(t *testing.T) {
		name, dsn := newTestDatabase(t, "drift")
		migrateDatabase(t, name, dsn, repoMigrations(t))
		drifted := copyChain(t, 31)
		path := filepath.Join(drifted, "0001_core_event.sql")
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(raw, []byte("\n-- forbidden drift\n")...), 0o600); err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		_, err = migrate.Run(ctx, migrate.Config{DSN: dsn, Directory: drifted,
			ConfirmedDatabase: name, RequireTestDatabase: true, RequireTLS: requireTLS(),
			HistoryTable: migrate.ProductionHistory, ExpectedSchemaVersion: "22",
			SourceRevision: "drift-negative"})
		if err == nil || !strings.Contains(err.Error(), "checksum drift") {
			t.Fatalf("checksum drift not rejected: %v", err)
		}
	})

	t.Run("legacy-history-backfill", func(t *testing.T) {
		name, dsn := newTestDatabase(t, "legacy_history")
		first := migrateDatabase(t, name, dsn, repoMigrations(t))
		conn := connect(t, dsn)
		if _, err := conn.Exec(context.Background(), `
CREATE TABLE legacy_migration_history AS
SELECT name,checksum,applied_at FROM masi_migration_history;
DROP TABLE masi_migration_history;
ALTER TABLE legacy_migration_history RENAME TO masi_migration_history`); err != nil {
			t.Fatal(err)
		}
		second := migrateDatabase(t, name, dsn, repoMigrations(t))
		if len(second.AlreadyApplied) != 31 || second.ChainDigest != first.ChainDigest {
			t.Fatalf("legacy history upgrade mismatch: %+v", second)
		}
		var invalid int
		if err := conn.QueryRow(context.Background(), `SELECT count(*) FROM masi_migration_history
WHERE chain_digest<>$1 OR chain_digest=$2`, first.ChainDigest,
			"sha256:"+strings.Repeat("0", 64)).Scan(&invalid); err != nil {
			t.Fatal(err)
		}
		if invalid != 0 {
			t.Fatalf("legacy history retained %d invalid chain digests", invalid)
		}
	})

	t.Run("unknown-nonzero-history-chain-digest", func(t *testing.T) {
		name, dsn := newTestDatabase(t, "history_digest_drift")
		migrateDatabase(t, name, dsn, repoMigrations(t))
		conn := connect(t, dsn)
		if _, err := conn.Exec(
			context.Background(),
			`UPDATE masi_migration_history SET chain_digest=$1 WHERE name='0001_core_event.sql'`,
			"sha256:"+strings.Repeat("a", 64),
		); err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		_, err := migrate.Run(ctx, migrate.Config{
			DSN: dsn, Directory: repoMigrations(t), ConfirmedDatabase: name,
			RequireTestDatabase: true, RequireTLS: requireTLS(),
			HistoryTable: migrate.ProductionHistory, ExpectedSchemaVersion: "22",
			SourceRevision: "history-digest-drift-negative",
		})
		if err == nil || !strings.Contains(err.Error(), "unknown history chain digest") {
			t.Fatalf("unknown nonzero history chain digest not rejected: %v", err)
		}
	})

	t.Run("interrupted-atomic-retry", func(t *testing.T) {
		name, dsn := newTestDatabase(t, "interrupted")
		broken := copyChain(t, 30)
		body := "BEGIN;\nCREATE TABLE should_not_commit(id INTEGER);\nSELECT 1/0;\nCOMMIT;\n"
		if err := os.WriteFile(filepath.Join(broken, "0031_injected_failure.sql"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		_, err := migrate.Run(ctx, migrate.Config{DSN: dsn, Directory: broken,
			ConfirmedDatabase: name, RequireTestDatabase: true, RequireTLS: requireTLS(),
			HistoryTable: migrate.ProductionHistory, ExpectedSchemaVersion: "22",
			SourceRevision: "interruption-negative"})
		cancel()
		if err == nil || !strings.Contains(err.Error(), "0031_injected_failure.sql") {
			t.Fatalf("injected failure not reported: %v", err)
		}
		conn := connect(t, dsn)
		var tableExists, historyExists bool
		if err := conn.QueryRow(context.Background(), `SELECT to_regclass('public.should_not_commit') IS NOT NULL,
				EXISTS(SELECT 1 FROM masi_migration_history WHERE name='0031_injected_failure.sql')`).
			Scan(&tableExists, &historyExists); err != nil {
			t.Fatal(err)
		}
		if tableExists || historyExists {
			t.Fatalf("failed migration leaked table=%v history=%v", tableExists, historyExists)
		}
		result := migrateDatabase(t, name, dsn, repoMigrations(t))
		if len(result.AlreadyApplied) != 30 {
			t.Fatalf("canonical retry failed: %+v", result)
		}
	})
}

func TestPartitionMaintenanceDrainsDefaultRows(t *testing.T) {
	name, dsn := newTestDatabase(t, "partition_drain")
	migrateDatabase(t, name, dsn, repoMigrations(t))
	conn := connect(t, dsn)
	ctx := context.Background()
	exec := func(query string, args ...any) {
		t.Helper()
		if _, err := conn.Exec(ctx, query, args...); err != nil {
			t.Fatalf("seed partition drain: %v\n%s", err, query)
		}
	}

	anchor := time.Now().UTC().AddDate(4, 7, 0)
	anchor = time.Date(anchor.Year(), anchor.Month(), 1, 0, 0, 0, 0, time.UTC)
	futureTime := anchor.Add(12 * time.Hour)
	futureMS := futureTime.UnixMilli()

	exec(`INSERT INTO event_identities(event_id,event_idempotency_key,input_digest,output_digest,
		event_time,committed_at_unix_ms) VALUES('drain-event','drain-event-key',$1,$1,$2,$3)`,
		digestA, futureTime, futureMS)
	exec(`INSERT INTO events(event_id,event_idempotency_key,input_digest,output_digest,
		canonical_event_id,model_control_incarnation_id,shard_id,route_epoch,logical_pool_id,
		pool_generation,binding_generation,source_window_identity,quality,commit_status,event_time,
		committed_at_unix_ms,ingest_batch_digest,trace_id,reason_code,scope)
		VALUES('drain-event','drain-event-key',$1,$1,'drain-event','model-inc','shard',1,'pool',
		1,1,'{}','valid','committed',$2,$3,$1,'trace','COMMITTED','scope-drain')`,
		digestA, futureTime, futureMS)
	exec(`INSERT INTO rule_observation_epochs(epoch_id,effect_intent_id,operation_id,entity_id,
		rule_id,target_id,canonical_entry_digest,match_priority_action_digest,observation_epoch,
		reset_epoch,installation_readback) VALUES('drain-epoch','intent','operation','entity',
		'rule','target',$1,$1,1,1,'exact')`, digestA)
	for _, table := range []string{"rule_rollups_5m", "rule_rollups_1h"} {
		exec(fmt.Sprintf(`INSERT INTO %s(window_start_unix_ms,epoch_id,observation_epoch,
			reset_epoch,rule_id,direct_packets,direct_bytes,eligible_packets,sample_count,quality_status)
			VALUES($1,'drain-epoch',1,1,'rule',0,0,0,1,'valid')`, table), futureMS)
	}

	exec(`INSERT INTO plugin_manifests(manifest_id,manifest_revision,manifest_digest,plugin_id,kind,
		publisher,version,capabilities,resource_limits,runtime_profile,sbom_digest,provenance_digest,
		signature_status,actor_ref,trace_id,scope) VALUES('drain-manifest',1,$1,'drain-plugin',
		'pure-transform','publisher','1.0.0','[]','{}','wasm-component/v1',$1,$1,'signed',
		'actor','trace','scope-drain')`, digestA)
	exec(`INSERT INTO plugin_qualifications(qualification_id,plugin_id,manifest_id,manifest_revision,
		manifest_digest,qualification_status,qualification_digest,qualified_at,actor_ref,trace_id,
		reason_code) VALUES('drain-qualification','drain-plugin','drain-manifest',1,$1,'qualified',
		$1,clock_timestamp(),'actor','trace','QUALIFIED')`, digestA)
	exec(`INSERT INTO plugin_bindings(plugin_id,binding_generation,manifest_id,manifest_revision,
		manifest_digest,config_digest,capability_digest,resource_profile_digest,activation_state,
		qualification_status,actor_ref,trace_id,reason_code,scope) VALUES('drain-plugin',1,
		'drain-manifest',1,$1,$1,$1,$1,'active','qualified','actor','trace','ACTIVE','scope-drain')`,
		digestA)
	exec(`INSERT INTO plugin_statistics_definitions(definition_id,definition_digest,definition,
		plugin_id,plugin_revision,manifest_id,manifest_revision,binding_generation,producer_kind,
		host_projection_refs,display_hint,scope,data_class,deadline_ms,manifest_digest,
		qualification_id,qualification_digest) VALUES('drain-definition',$1,'{}','drain-plugin',
		'1.0.0','drain-manifest',1,1,'pure-transform',ARRAY['events/v1'],'table','scope-drain',
		'internal',1000,$1,'drain-qualification',$1)`, digestA)
	exec(`INSERT INTO plugin_statistic_runs(run_id,run_digest,request_digest,definition_id,
		definition_digest,idempotency_key,status,binding_generation,frozen_input_digest,
		started_at_unix_ms,actor_ref,reason_code,trace_id,scope,data_class,actor_issuer,
		actor_subject,target_set_digest) VALUES('drain-run',$1,$1,'drain-definition',$1,
		'drain-idempotency','succeeded',1,$1,$2,'actor','SUCCEEDED','trace','scope-drain',
		'internal','issuer','subject',$1)`, digestA, futureMS)
	exec(`INSERT INTO plugin_statistic_artifacts(artifact_id,artifact_digest,run_id,definition_id,
		definition_digest,status,quality,artifact,bytes,actor_ref,trace_id,binding_generation,
		result_fence) VALUES('drain-artifact',$1,'drain-run','drain-definition',$1,'succeeded',
		'valid','{}',2,'actor','trace',1,'drain-fence')`, digestA)
	exec(`INSERT INTO plugin_statistics_history(definition_id,binding_generation,artifact_id,run_id,
		quality,scope,recorded_at) VALUES('drain-definition',1,'drain-artifact','drain-run','valid',
		'scope-drain',$1)`, futureTime)

	var created, repeated int
	if err := conn.QueryRow(ctx, `SELECT masi_ensure_time_partitions($1,2)`, anchor).Scan(&created); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueryRow(ctx, `SELECT masi_ensure_time_partitions($1,2)`, anchor).Scan(&repeated); err != nil {
		t.Fatal(err)
	}
	if created != 12 || repeated != 0 {
		t.Fatalf("partition maintenance created=%d repeated=%d", created, repeated)
	}

	suffix := anchor.Format("200601")
	wantRelations := []string{
		"events_" + suffix,
		"rule_rollups_5m_" + suffix,
		"rule_rollups_1h_" + suffix,
		"plugin_statistics_history_" + suffix,
	}
	queries := []string{
		`SELECT tableoid::regclass::text FROM events WHERE event_id='drain-event'`,
		`SELECT tableoid::regclass::text FROM rule_rollups_5m WHERE epoch_id='drain-epoch'`,
		`SELECT tableoid::regclass::text FROM rule_rollups_1h WHERE epoch_id='drain-epoch'`,
		`SELECT tableoid::regclass::text FROM plugin_statistics_history WHERE run_id='drain-run'`,
	}
	for index, query := range queries {
		var relation string
		if err := conn.QueryRow(ctx, query).Scan(&relation); err != nil {
			t.Fatal(err)
		}
		if relation != wantRelations[index] {
			t.Fatalf("row remained outside explicit partition: got=%s want=%s", relation, wantRelations[index])
		}
	}
}

func TestExactRelationsCASAndDenyRoles(t *testing.T) {
	name, dsn := newTestDatabase(t, "constraints")
	migrateDatabase(t, name, dsn, repoMigrations(t))
	conn := connect(t, dsn)
	ctx := context.Background()
	exec := func(query string, args ...any) {
		t.Helper()
		if _, err := conn.Exec(ctx, query, args...); err != nil {
			t.Fatalf("seed: %v\n%s", err, query)
		}
	}
	expectCode := func(code, query string, args ...any) {
		t.Helper()
		_, err := conn.Exec(ctx, query, args...)
		var pgErr *pgconn.PgError
		if !errors.As(err, &pgErr) || pgErr.Code != code {
			t.Fatalf("wanted SQLSTATE %s, got %v for %s", code, err, query)
		}
	}

	exec(`INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,role,status,
		desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
		VALUES('target-a','A','https://target-a.test:9559',1,'primary','active',$1,'cred:a','scope-a','actor','trace-a'),
		      ('target-b','B','https://target-b.test:9559',2,'primary','active',$1,'cred:b','scope-b','actor','trace-b')`, digestA)
	exec(`INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,edge_workload_ref,
		lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
		actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES('target-a',1,'target-inc-a','edge-a','lease-a',1000,301000,10,19,'epoch-a',1,
		'actor','trace-a','issuer','subject')`)
	expectCode("23514", `INSERT INTO target_assignments(target_id,assignment_generation,incarnation_id,
		edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,election_floor,election_ceiling,
		actor_runtime_epoch,application_generation,actor_ref,trace_id,actor_issuer,actor_subject)
		VALUES('target-a',2,'target-inc-a','edge-b','lease-b',302000,602000,19,29,'epoch-b',2,
		'actor','trace-b','issuer','subject')`)

	exec(`INSERT INTO firewall_revisions(revision_id,revision_digest,target_id,default_action,rules,scope,
		actor_ref,reason_code) VALUES('fw-a',$1,'target-a','drop','[]','scope-a','actor','CREATED')`, digestA)
	expectCode("23503", `INSERT INTO firewall_bindings(target_id,current_revision_id,active_bank,
		selector_state,cas_digest) VALUES('target-b','fw-a',0,'stable',$1)`, digestA)

	exec(`INSERT INTO model_revisions(model_revision_id,model_revision_digest,model_bundle_digest,
		feature_contract_digest,label_contract_digest,output_adapter_digest,qualification_status,
		reader_runtime_profile,actor_ref,trace_id,scope)
		VALUES('model-a',$1,$1,$1,$1,$1,'qualified','model-runtime-central-cpu/v1','actor','trace','scope-a'),
		      ('model-b',$2,$2,$2,$2,$2,'qualified','model-runtime-central-cpu/v1','actor','trace','scope-a')`,
		digestA, "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
	exec(`INSERT INTO model_control_incarnations(incarnation_id,source,rotated_at_unix_ms,actor_ref,trace_id)
		VALUES('model-inc-a','initial',1,'actor','trace')`)
	exec(`UPDATE model_control_state SET active_incarnation_id='model-inc-a',writer_enabled=false`)
	exec(`INSERT INTO logical_pools(logical_pool_id,current_generation,availability_profile,runtime_profile,
		actor_ref,trace_id) VALUES('pool-a',1,'availability-single/v1','model-runtime-central-cpu/v1','actor','trace')`)
	exec(`INSERT INTO pool_generations(logical_pool_id,pool_generation,model_revision_id,
		startup_envelope_digest,pool_observation_digest,binding_digest,status,min_ready_replicas,
		capacity_qualified,model_control_incarnation_id)
		VALUES('pool-a',1,'model-a',$1,$1,$1,'active',1,true,'model-inc-a')`, digestA)
	expectCode("23503", `INSERT INTO shard_bindings(shard_id,logical_pool_id,
		model_control_incarnation_id,current_generation,current_binding_generation,current_revision_id,
		route_epoch,resume_state,cas_digest,scope)
		VALUES('shard-a','pool-a','model-inc-a',1,1,'model-b',1,'current',$1,'scope-a')`, digestA)
	expectCode("23514", `UPDATE model_control_state SET active_incarnation_id=NULL,writer_enabled=true`)

	exec(`INSERT INTO plugin_manifests(manifest_id,manifest_revision,manifest_digest,plugin_id,kind,
		publisher,version,capabilities,resource_limits,runtime_profile,sbom_digest,provenance_digest,
		signature_status,actor_ref,trace_id,scope)
		VALUES('manifest-a',1,$1,'plugin-a','pure-transform','publisher','1.0.0','[]','{}',
		'wasm-component/v1',$1,$1,'signed','actor','trace','scope-a')`, digestA)
	expectCode("23503", `INSERT INTO plugin_bindings(plugin_id,binding_generation,manifest_id,
		manifest_revision,manifest_digest,config_digest,capability_digest,resource_profile_digest,
		activation_state,qualification_status,actor_ref,trace_id,reason_code,scope)
		VALUES('plugin-a',1,'manifest-a',1,$1,$2,$2,$2,'active','qualified','actor','trace','ACTIVE','scope-a')`,
		"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", digestA)

	var appCanUpdateFirewall, appCanMutateHistory, analysisCanReadEvents bool
	if err := conn.QueryRow(ctx, `SELECT
		has_table_privilege('masi_control_app','firewall_revisions','UPDATE'),
		has_table_privilege('masi_control_app','masi_migration_history','UPDATE'),
		has_table_privilege('masi_analysis_private','events','SELECT')`).
		Scan(&appCanUpdateFirewall, &appCanMutateHistory, &analysisCanReadEvents); err != nil {
		t.Fatal(err)
	}
	if appCanUpdateFirewall || appCanMutateHistory || analysisCanReadEvents {
		t.Fatalf("deny matrix failed app_fw=%v app_history=%v analysis_events=%v",
			appCanUpdateFirewall, appCanMutateHistory, analysisCanReadEvents)
	}
}

func TestPartitionRetentionTimeoutAndRecoveryRotation(t *testing.T) {
	name, dsn := newTestDatabase(t, "recovery")
	result := migrateDatabase(t, name, dsn, repoMigrations(t))
	conn := connect(t, dsn)
	ctx := context.Background()

	var created, repeated int
	anchor := time.Now().UTC().AddDate(4, 7, 0)
	anchor = time.Date(anchor.Year(), anchor.Month(), 1, 0, 0, 0, 0, time.UTC)
	if err := conn.QueryRow(ctx, `SELECT masi_ensure_time_partitions($1,2)`, anchor).Scan(&created); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueryRow(ctx, `SELECT masi_ensure_time_partitions($1,2)`, anchor).Scan(&repeated); err != nil {
		t.Fatal(err)
	}
	if created != 12 || repeated != 0 {
		t.Fatalf("partition maintenance created=%d repeated=%d", created, repeated)
	}

	oldTime := time.Now().UTC().Add(-48 * time.Hour).Truncate(time.Millisecond)
	oldMS := oldTime.UnixMilli()
	if _, err := conn.Exec(ctx, `INSERT INTO event_identities(event_id,event_idempotency_key,input_digest,
		output_digest,event_time,committed_at_unix_ms) VALUES('retained-event','retained-key',$1,$1,$2,$3)`,
		digestA, oldTime, oldMS); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Exec(ctx, `INSERT INTO events(event_id,event_idempotency_key,input_digest,output_digest,
		canonical_event_id,model_control_incarnation_id,shard_id,route_epoch,logical_pool_id,pool_generation,
		binding_generation,source_window_identity,quality,commit_status,event_time,committed_at_unix_ms,
		ingest_batch_digest,trace_id,reason_code,scope)
		VALUES('retained-event','retained-key',$1,$1,'retained-event','model-inc','shard',1,'pool',1,1,
		'{}','valid','committed',$2,$3,$1,'trace','COMMITTED','scope-retention')`, digestA, oldTime, oldMS); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Exec(ctx, `INSERT INTO retention_holds(hold_id,fact_domain,scope,reason_code,
		starts_at,actor_ref,trace_id) VALUES('hold-event','events','scope-retention','LEGAL_HOLD',
		clock_timestamp()-interval '1 day','auditor','trace-hold')`); err != nil {
		t.Fatal(err)
	}
	var eventsDeleted, identitiesDeleted int64
	if err := conn.QueryRow(ctx, `SELECT events_deleted,identities_deleted
		FROM masi_sweep_event_retention(clock_timestamp()-interval '1 day',100)`).
		Scan(&eventsDeleted, &identitiesDeleted); err != nil {
		t.Fatal(err)
	}
	if eventsDeleted != 0 || identitiesDeleted != 0 {
		t.Fatalf("legal hold did not pin event: %d/%d", eventsDeleted, identitiesDeleted)
	}
	if _, err := conn.Exec(ctx, `DELETE FROM retention_holds WHERE hold_id='hold-event'`); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueryRow(ctx, `SELECT events_deleted,identities_deleted
		FROM masi_sweep_event_retention(clock_timestamp()-interval '1 day',100)`).
		Scan(&eventsDeleted, &identitiesDeleted); err != nil {
		t.Fatal(err)
	}
	if eventsDeleted != 1 || identitiesDeleted != 1 {
		t.Fatalf("bounded retention mismatch: %d/%d", eventsDeleted, identitiesDeleted)
	}

	if _, err := conn.Exec(ctx, `SET statement_timeout='50ms'; SELECT pg_sleep(1)`); err == nil {
		t.Fatal("statement timeout did not cancel work")
	} else {
		var pgErr *pgconn.PgError
		if !errors.As(err, &pgErr) || pgErr.Code != "57014" {
			t.Fatalf("unexpected statement timeout error: %v", err)
		}
	}
	if _, err := conn.Exec(ctx, `RESET statement_timeout`); err != nil {
		t.Fatal(err)
	}

	if _, err := conn.Exec(ctx, `INSERT INTO target_control_incarnations(incarnation_id,source,
		rotated_at_unix_ms,actor_ref,trace_id) VALUES('target-old','initial',1,'actor','trace')`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Exec(ctx, `UPDATE target_control_state SET active_incarnation_id='target-old',
		writer_enabled=false`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Exec(ctx, `INSERT INTO model_control_incarnations(incarnation_id,source,
		rotated_at_unix_ms,actor_ref,trace_id) VALUES('model-old','initial',1,'actor','trace')`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Exec(ctx, `UPDATE model_control_state SET active_incarnation_id='model-old',
		writer_enabled=false`); err != nil {
		t.Fatal(err)
	}
	targetID, modelID, err := state.RotateRecoveredIncarnations(ctx, dsn, name, requireTLS(), state.Rotation{
		NewTargetIncarnation: "target-new", NewModelIncarnation: "model-new", Source: "pitr",
		BackupDigest: digestA, SchemaDigest: result.ChainDigest, SourceTimeline: 1,
		RestoredTimeline: 2, RecoveryID: "recovery-test-1", Actor: "restore-job", TraceID: "trace-restore"})
	if err != nil {
		t.Fatal(err)
	}
	if targetID != "target-new" || modelID != "model-new" {
		t.Fatalf("rotation IDs %s/%s", targetID, modelID)
	}
	var targetWriter, modelWriter, isolated bool
	var effectCount, statisticsRunCount int
	if err := conn.QueryRow(ctx, `SELECT t.writer_enabled,m.writer_enabled,r.isolated,
		(SELECT count(*) FROM effect_intents),(SELECT count(*) FROM plugin_statistic_runs)
		FROM target_control_state t CROSS JOIN model_control_state m
		JOIN database_recovery_events r ON r.recovery_id='recovery-test-1'
		WHERE t.singleton AND m.singleton`).
		Scan(&targetWriter, &modelWriter, &isolated, &effectCount, &statisticsRunCount); err != nil {
		t.Fatal(err)
	}
	if targetWriter || modelWriter || !isolated || effectCount != 0 || statisticsRunCount != 0 {
		t.Fatalf("restore opened side effects: target=%v model=%v isolated=%v effects=%d stats=%d",
			targetWriter, modelWriter, isolated, effectCount, statisticsRunCount)
	}
}

func TestConcurrentAssignmentCASAndLockTimeout(t *testing.T) {
	name, dsn := newTestDatabase(t, "concurrency")
	migrateDatabase(t, name, dsn, repoMigrations(t))
	seed := connect(t, dsn)
	ctx := context.Background()
	if _, err := seed.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,
		role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
		VALUES('cas-target','CAS','https://cas-target.test:9559',99,'primary','active',$1,
		'cred','scope','actor','trace')`, digestA); err != nil {
		t.Fatal(err)
	}
	if _, err := seed.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,
		incarnation_id,edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,
		election_floor,election_ceiling,actor_runtime_epoch,application_generation,actor_ref,
		trace_id,actor_issuer,actor_subject) VALUES('cas-target',1,'inc','edge','lease-1',1000,
		301000,1,10,'epoch-1',1,'actor','trace','issuer','subject')`); err != nil {
		t.Fatal(err)
	}

	start := make(chan struct{})
	results := make(chan error, 2)
	var wg sync.WaitGroup
	for index := 0; index < 2; index++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			conn, err := pgx.Connect(ctx, dsn)
			if err != nil {
				results <- err
				return
			}
			defer conn.Close(context.Background())
			<-start
			_, err = conn.Exec(ctx, `INSERT INTO target_assignments(target_id,assignment_generation,
				incarnation_id,edge_workload_ref,lease_id,issued_at_unix_ms,expires_at_unix_ms,
				election_floor,election_ceiling,actor_runtime_epoch,application_generation,actor_ref,
				trace_id,actor_issuer,actor_subject) VALUES('cas-target',2,'inc',$1,$2,302000,
				602000,11,20,$3,2,'actor',$4,'issuer','subject')`,
				fmt.Sprintf("edge-%d", index), fmt.Sprintf("lease-%d", index),
				fmt.Sprintf("epoch-%d", index), fmt.Sprintf("trace-%d", index))
			results <- err
		}(index)
	}
	close(start)
	wg.Wait()
	close(results)
	successes, conflicts := 0, 0
	for err := range results {
		if err == nil {
			successes++
			continue
		}
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && (pgErr.Code == "23505" || pgErr.Code == "23514") {
			conflicts++
			continue
		}
		t.Fatalf("unexpected CAS error: %v", err)
	}
	if successes != 1 || conflicts != 1 {
		t.Fatalf("CAS outcomes successes=%d conflicts=%d", successes, conflicts)
	}

	if _, err := seed.Exec(ctx, `INSERT INTO targets(target_id,display_name,p4runtime_endpoint,device_id,
		role,status,desired_profile_digest,credential_ref,scope,actor_ref,trace_id)
		VALUES('deadlock-a','Deadlock A','https://deadlock-a.test:9559',100,'primary','active',$1,
		'cred-a','scope','actor','trace'),
		('deadlock-b','Deadlock B','https://deadlock-b.test:9559',101,'primary','active',$1,
		'cred-b','scope','actor','trace')`, digestA); err != nil {
		t.Fatal(err)
	}
	deadlockA := connect(t, dsn)
	deadlockB := connect(t, dsn)
	deadlockCtx, cancelDeadlock := context.WithTimeout(ctx, 5*time.Second)
	defer cancelDeadlock()
	txA, err := deadlockA.Begin(deadlockCtx)
	if err != nil {
		t.Fatal(err)
	}
	txB, err := deadlockB.Begin(deadlockCtx)
	if err != nil {
		_ = txA.Rollback(context.Background())
		t.Fatal(err)
	}
	if _, err := txA.Exec(deadlockCtx, `UPDATE targets SET display_name='tx-a'
		WHERE target_id='deadlock-a'`); err != nil {
		t.Fatal(err)
	}
	if _, err := txB.Exec(deadlockCtx, `UPDATE targets SET display_name='tx-b'
		WHERE target_id='deadlock-b'`); err != nil {
		t.Fatal(err)
	}
	deadlockStart := make(chan struct{})
	deadlockResults := make(chan error, 2)
	updateOther := func(tx pgx.Tx, target string) {
		<-deadlockStart
		_, updateErr := tx.Exec(deadlockCtx, `UPDATE targets SET display_name=display_name||'-other'
			WHERE target_id=$1`, target)
		if updateErr != nil {
			_ = tx.Rollback(context.Background())
			deadlockResults <- updateErr
			return
		}
		deadlockResults <- tx.Commit(deadlockCtx)
	}
	go updateOther(txA, "deadlock-b")
	go updateOther(txB, "deadlock-a")
	close(deadlockStart)
	deadlockSuccesses, deadlockVictims := 0, 0
	for range 2 {
		deadlockErr := <-deadlockResults
		if deadlockErr == nil {
			deadlockSuccesses++
			continue
		}
		var pgErr *pgconn.PgError
		if errors.As(deadlockErr, &pgErr) && pgErr.Code == "40P01" {
			deadlockVictims++
			continue
		}
		t.Fatalf("unexpected deadlock result: %v", deadlockErr)
	}
	if deadlockSuccesses != 1 || deadlockVictims != 1 {
		t.Fatalf("deadlock outcomes successes=%d victims=%d", deadlockSuccesses, deadlockVictims)
	}
	var survivingRows int
	if err := seed.QueryRow(ctx, `SELECT count(*) FROM targets
		WHERE target_id IN ('deadlock-a','deadlock-b')`).Scan(&survivingRows); err != nil {
		t.Fatal(err)
	}
	if survivingRows != 2 {
		t.Fatalf("deadlock recovery lost rows: %d", survivingRows)
	}

	locker := connect(t, dsn)
	waiter := connect(t, dsn)
	tx, err := locker.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(context.Background())
	if _, err := tx.Exec(ctx, `UPDATE targets SET display_name='locked' WHERE target_id='cas-target'`); err != nil {
		t.Fatal(err)
	}
	if _, err := waiter.Exec(ctx, `SET lock_timeout='100ms'; UPDATE targets SET display_name='waiter'
		WHERE target_id='cas-target'`); err == nil {
		t.Fatal("lock timeout did not bound blocked statement")
	} else {
		var pgErr *pgconn.PgError
		if !errors.As(err, &pgErr) || pgErr.Code != "55P03" {
			t.Fatalf("unexpected lock timeout: %v", err)
		}
	}
}
