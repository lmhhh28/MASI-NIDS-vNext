package main

import "testing"

func TestMigrationBody(t *testing.T) {
	body, err := migrationBody([]byte("-- x\nBEGIN;\nSELECT 1;\nCOMMIT;\n"))
	if err != nil || body == "" {
		t.Fatalf("valid envelope: %q %v", body, err)
	}
	if _, err := migrationBody([]byte("SELECT 1;")); err == nil {
		t.Fatal("missing envelope must fail")
	}
	if _, err := migrationBody([]byte("BEGIN;\nBEGIN;\nCOMMIT;")); err == nil {
		t.Fatal("nested envelope must fail")
	}
}
