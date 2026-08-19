package migrate

import "testing"

func TestMigrationBody(t *testing.T) {
	body, err := MigrationBody([]byte("-- x\nBEGIN;\nSELECT 1;\nCOMMIT;\n"))
	if err != nil || body == "" {
		t.Fatalf("valid envelope: %q %v", body, err)
	}
	if _, err := MigrationBody([]byte("SELECT 1;")); err == nil {
		t.Fatal("missing envelope must fail")
	}
	if _, err := MigrationBody([]byte("BEGIN;\nBEGIN;\nCOMMIT;")); err == nil {
		t.Fatal("nested envelope must fail")
	}
}
