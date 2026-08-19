package plugin

import (
	"strings"
	"testing"
)

func TestKindIsClosed(t *testing.T) {
	for _, k := range []Kind{KindAnalysisAgent, KindReadOnlyTool, KindPureTransform} {
		if !k.IsClosed() {
			t.Errorf("kind %q must be closed", k)
		}
	}
	if Kind("unknown").IsClosed() {
		t.Fatal("unknown kind must not be closed")
	}
}

func goodManifest() Manifest {
	return Manifest{
		ManifestID: "man-1", ManifestRevision: 1, PluginID: "plug-1",
		Kind: KindReadOnlyTool, Publisher: "pub-1", Version: "1.0.0",
		Capabilities:   []Capability{{CapabilityID: "cap-1", CapabilityKind: "host-projection", Declared: true}},
		ResourceLimits: ResourceLimits{CPUMilli: 100, MemoryBytes: 1048576, PIDCount: 8, FDCount: 32, DeadlineMS: 5000, OutputBytes: 65536, QueueDepth: 100},
		RuntimeProfile: RuntimeWasmComponent, WitDigest: "sha256:" + strings.Repeat("a", 64),
		SBOMDigest:       "sha256:" + strings.Repeat("b", 64),
		ProvenanceDigest: "sha256:" + strings.Repeat("c", 64),
		SignatureStatus:  "signed",
		Scope:            "scope-1",
	}
}

func TestValidateManifestRejects(t *testing.T) {
	if err := validateManifest(goodManifest()); err != nil {
		t.Fatalf("good manifest rejected: %v", err)
	}
	bad := goodManifest()
	bad.Kind = "unknown-kind"
	if err := validateManifest(bad); err == nil {
		t.Fatal("unknown kind must be rejected")
	}
	bad2 := goodManifest()
	bad2.SignatureStatus = "rejected"
	if err := validateManifest(bad2); err == nil {
		t.Fatal("rejected signature must be rejected")
	}
	bad3 := goodManifest()
	bad3.WitDigest = ""
	if err := validateManifest(bad3); err == nil {
		t.Fatal("wasm-component without wit_digest must be rejected")
	}
	bad4 := goodManifest()
	bad4.RuntimeProfile = RuntimeGRPCService
	bad4.WitDigest = ""
	bad4.ServiceProtoDigest = ""
	if err := validateManifest(bad4); err == nil {
		t.Fatal("grpc-service without service_proto_digest must be rejected")
	}
	bad5 := goodManifest()
	bad5.SBOMDigest = "not-a-digest"
	if err := validateManifest(bad5); err == nil {
		t.Fatal("malformed sbom digest must be rejected")
	}
}

func TestComputeManifestDigestDeterministic(t *testing.T) {
	m := goodManifest()
	d1 := computeManifestDigest(m)
	d2 := computeManifestDigest(m)
	if d1 != d2 {
		t.Fatal("manifest digest not deterministic")
	}
	if !strings.HasPrefix(d1, "sha256:") {
		t.Fatalf("digest %q must be sha256-prefixed", d1)
	}
	changed := m
	changed.Version = "2.0.0"
	if computeManifestDigest(changed) == d1 {
		t.Fatal("manifest digest must change when version changes")
	}
}

func TestValidateBindingRejects(t *testing.T) {
	good := Binding{
		PluginID: "plug-1", BindingGeneration: 1,
		ManifestID: "man-1", ManifestRevision: 1, Scope: "scope-1",
		ManifestDigest:        "sha256:" + strings.Repeat("a", 64),
		ConfigDigest:          "sha256:" + strings.Repeat("b", 64),
		CapabilityDigest:      "sha256:" + strings.Repeat("c", 64),
		ResourceProfileDigest: "sha256:" + strings.Repeat("d", 64),
		QualificationStatus:   Qualified,
	}
	if err := validateBinding(good); err != nil {
		t.Fatalf("good binding rejected: %v", err)
	}
	bad := good
	bad.ManifestDigest = "not-a-digest"
	if err := validateBinding(bad); err == nil {
		t.Fatal("malformed manifest digest must be rejected")
	}
	bad2 := good
	bad2.BindingGeneration = 0
	if err := validateBinding(bad2); err == nil {
		t.Fatal("binding_generation 0 must be rejected")
	}
}
