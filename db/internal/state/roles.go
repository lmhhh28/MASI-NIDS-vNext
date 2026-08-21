package state

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

type RoleFact struct {
	Login       bool `json:"login"`
	Superuser   bool `json:"superuser"`
	CreateDB    bool `json:"create_db"`
	CreateRole  bool `json:"create_role"`
	Replication bool `json:"replication"`
	Inherit     bool `json:"inherit"`
	ConnLimit   int  `json:"connection_limit"`
}

type RuntimeRoleInspection struct {
	Database               string              `json:"database"`
	TLS                    bool                `json:"tls"`
	Roles                  map[string]RoleFact `json:"roles"`
	AppControlMember       bool                `json:"app_control_member"`
	AppBackupMember        bool                `json:"app_backup_member"`
	MonitoringMember       bool                `json:"monitoring_member"`
	MigrationPrivateMember bool                `json:"migration_private_member"`
	MigrationMonitorMember bool                `json:"migration_monitor_member"`
	PublicConnect          bool                `json:"public_connect"`
	PublicTemporary        bool                `json:"public_temporary"`
}

// InspectRuntimeRoles validates the post-migration sealed login/group matrix.
func InspectRuntimeRoles(ctx context.Context, dsn, confirmedDatabase string, requireTLS bool) (*RuntimeRoleInspection, error) {
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("role inspect: connect: %w", err)
	}
	defer conn.Close(context.Background())
	out := &RuntimeRoleInspection{Roles: map[string]RoleFact{}}
	if err := conn.QueryRow(ctx, `SELECT current_database(),
		EXISTS(SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl)`).Scan(&out.Database, &out.TLS); err != nil {
		return nil, err
	}
	if out.Database != confirmedDatabase {
		return nil, errors.New("role inspect: database mismatch")
	}
	if requireTLS && !out.TLS {
		return nil, errors.New("role inspect: TLS required")
	}
	expected := map[string]RoleFact{
		"masi_bootstrap":           {Login: false, Superuser: true, CreateDB: true, CreateRole: true, Replication: true, Inherit: true, ConnLimit: -1},
		"masi_migration_login":     {Login: true, ConnLimit: 4},
		"masi_app_login":           {Login: true, Inherit: true, ConnLimit: 64},
		"masi_replication_login":   {Login: true, Replication: true, ConnLimit: 4},
		"masi_monitoring_login":    {Login: true, Inherit: true, ConnLimit: 4},
		"masi_control_app":         {Inherit: true, ConnLimit: -1},
		"masi_control_readonly":    {Inherit: true, ConnLimit: -1},
		"masi_control_maintenance": {Inherit: true, ConnLimit: -1},
		"masi_analysis_private":    {Inherit: true, ConnLimit: -1},
		"masi_backup":              {Replication: true, Inherit: true, ConnLimit: -1},
		"masi_monitoring":          {Inherit: true, ConnLimit: -1},
	}
	for role, wanted := range expected {
		var fact RoleFact
		if err := conn.QueryRow(ctx, `SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,
			rolreplication,rolinherit,rolconnlimit FROM pg_roles WHERE rolname=$1`, role).Scan(
			&fact.Login, &fact.Superuser, &fact.CreateDB, &fact.CreateRole,
			&fact.Replication, &fact.Inherit, &fact.ConnLimit); err != nil {
			return nil, fmt.Errorf("role inspect: %s: %w", role, err)
		}
		out.Roles[role] = fact
		if fact != wanted {
			return nil, fmt.Errorf("role inspect: %s=%+v expected %+v", role, fact, wanted)
		}
	}
	if err := conn.QueryRow(ctx, `SELECT
		pg_has_role('masi_app_login','masi_control_app','MEMBER'),
		pg_has_role('masi_app_login','masi_backup','MEMBER'),
		pg_has_role('masi_monitoring_login','masi_monitoring','MEMBER'),
		pg_has_role('masi_migration_login','masi_analysis_private','MEMBER'),
		pg_has_role('masi_migration_login','pg_monitor','MEMBER')`).Scan(
		&out.AppControlMember, &out.AppBackupMember, &out.MonitoringMember,
		&out.MigrationPrivateMember, &out.MigrationMonitorMember); err != nil {
		return nil, err
	}
	if !out.AppControlMember || out.AppBackupMember || !out.MonitoringMember ||
		out.MigrationPrivateMember || out.MigrationMonitorMember {
		return nil, errors.New("role inspect: membership matrix mismatch")
	}
	if err := conn.QueryRow(ctx, `SELECT
		COALESCE(bool_or(grantee=0 AND privilege_type='CONNECT'),false),
		COALESCE(bool_or(grantee=0 AND privilege_type='TEMPORARY'),false)
	FROM pg_database d CROSS JOIN LATERAL aclexplode(
		COALESCE(d.datacl,acldefault('d',d.datdba)))
	WHERE d.datname=current_database()`).Scan(&out.PublicConnect, &out.PublicTemporary); err != nil {
		return nil, err
	}
	if out.PublicConnect || out.PublicTemporary {
		return nil, errors.New("role inspect: PUBLIC database privilege remains")
	}
	return out, nil
}
