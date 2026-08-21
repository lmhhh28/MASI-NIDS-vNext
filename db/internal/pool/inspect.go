// Package pool inspects the bounded PgBouncer administration database. It
// never returns auth_file contents, credentials, or per-client query text.
package pool

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

// Inspection is the secret-free transaction-pool readback used by the module
// gate. Expected values are deliberately fixed by the deployment profile.
type Inspection struct {
	TLS                bool              `json:"tls"`
	Version            string            `json:"version"`
	Settings           map[string]string `json:"settings"`
	ConfiguredDatabase bool              `json:"configured_database"`
	DatabaseHost       string            `json:"database_host"`
	DatabasePort       int               `json:"database_port"`
	PoolRows           int               `json:"pool_rows"`
}

var requiredSettings = map[string]string{
	"pool_mode":                 "transaction",
	"auth_type":                 "scram-sha-256",
	"client_tls_sslmode":        "verify-full",
	"server_tls_sslmode":        "verify-full",
	"max_client_conn":           "256",
	"default_pool_size":         "24",
	"max_db_connections":        "32",
	"max_user_connections":      "32",
	"query_timeout":             "35",
	"query_wait_timeout":        "10",
	"max_packet_size":           "16777216",
	"server_reset_query":        "DISCARD ALL",
	"server_check_query":        "SELECT 1",
	"application_name_add_host": "1",
}

// Inspect connects to PgBouncer's virtual "pgbouncer" database and checks the
// exact project profile. The supplied identity must be a dedicated pool admin,
// never the application identity.
func Inspect(ctx context.Context, dsn string, requireTLS bool) (*Inspection, error) {
	config, err := pgx.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("pool inspect: parse dsn: %w", err)
	}
	config.DefaultQueryExecMode = pgx.QueryExecModeSimpleProtocol
	conn, err := pgx.ConnectConfig(ctx, config)
	if err != nil {
		return nil, fmt.Errorf("pool inspect: connect: %w", err)
	}
	defer conn.Close(context.Background())
	out := &Inspection{Settings: map[string]string{}}
	if err := conn.QueryRow(ctx, `SHOW VERSION`).Scan(&out.Version); err != nil {
		return nil, fmt.Errorf("pool inspect: version: %w", err)
	}
	// PgBouncer exposes client TLS state in SHOW CLIENTS. The profile rejects
	// plaintext globally, so every observed client must report TLS.
	clientRows, err := conn.Query(ctx, `SHOW CLIENTS`)
	if err != nil {
		return nil, fmt.Errorf("pool inspect: clients: %w", err)
	}
	clientFields := clientRows.FieldDescriptions()
	clientCount := 0
	out.TLS = true
	for clientRows.Next() {
		clientCount++
		values, valueErr := clientRows.Values()
		if valueErr != nil || len(values) != len(clientFields) {
			clientRows.Close()
			return nil, errors.New("pool inspect: invalid SHOW CLIENTS row")
		}
		tlsText := ""
		for index, field := range clientFields {
			if string(field.Name) == "tls" {
				tlsText = fmt.Sprint(values[index])
			}
		}
		if tlsText == "" || tlsText == "no" || tlsText == "false" {
			out.TLS = false
		}
	}
	clientRows.Close()
	if clientCount == 0 {
		return nil, errors.New("pool inspect: current client missing")
	}
	if requireTLS && !out.TLS {
		return nil, errors.New("pool inspect: TLS required")
	}
	rows, err := conn.Query(ctx, `SHOW CONFIG`)
	if err != nil {
		return nil, fmt.Errorf("pool inspect: config: %w", err)
	}
	configFields := rows.FieldDescriptions()
	for rows.Next() {
		values, valueErr := rows.Values()
		if valueErr != nil || len(values) != len(configFields) {
			rows.Close()
			return nil, errors.New("pool inspect: invalid SHOW CONFIG row")
		}
		row := map[string]string{}
		for index, field := range configFields {
			if values[index] != nil {
				row[string(field.Name)] = fmt.Sprint(values[index])
			}
		}
		key, value := row["key"], row["value"]
		if _, required := requiredSettings[key]; required {
			out.Settings[key] = value
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	for key, expected := range requiredSettings {
		if out.Settings[key] != expected {
			return nil, fmt.Errorf("pool inspect: %s=%q expected %q", key, out.Settings[key], expected)
		}
	}
	rows, err = conn.Query(ctx, `SHOW DATABASES`)
	if err != nil {
		return nil, fmt.Errorf("pool inspect: databases: %w", err)
	}
	fields := rows.FieldDescriptions()
	for rows.Next() {
		values, valueErr := rows.Values()
		if valueErr != nil {
			rows.Close()
			return nil, valueErr
		}
		row := map[string]string{}
		for index, field := range fields {
			row[string(field.Name)] = fmt.Sprint(values[index])
		}
		if row["name"] == "masi_state_module_test" {
			out.ConfiguredDatabase = true
			out.DatabaseHost = row["host"]
			_, _ = fmt.Sscan(row["port"], &out.DatabasePort)
		}
	}
	rows.Close()
	if !out.ConfiguredDatabase || out.DatabaseHost != "postgres" || out.DatabasePort != 5432 {
		return nil, errors.New("pool inspect: exact database route missing")
	}
	rows, err = conn.Query(ctx, `SHOW POOLS`)
	if err != nil {
		return nil, fmt.Errorf("pool inspect: pools: %w", err)
	}
	for rows.Next() {
		out.PoolRows++
	}
	rows.Close()
	return out, nil
}
