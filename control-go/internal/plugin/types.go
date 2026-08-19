// Package plugin implements the Plugin Manager for Go Control Core: catalog,
// immutable manifest revision, verification/qualification, activation/binding
// generation, drain/revoke/rollback, and audit.
//
// The Manager does NOT execute third-party code (the Runtime Host does that).
// Kind is closed: analysis-agent | read-only-tool | pure-transform. Unknown
// kind/major, wrong digest/publisher, capability expansion, revoked artifact,
// or unqualified runtime are rejected. Plugins NEVER write core schema, create
// Decision/Intent, or call Edge/P4 (ADR-0018, go-control-core-design §10.1).
package plugin

// Kind is the closed plugin kind enum.
type Kind string

const (
	KindAnalysisAgent Kind = "analysis-agent"
	KindReadOnlyTool  Kind = "read-only-tool"
	KindPureTransform Kind = "pure-transform"
)

// IsClosed reports whether the kind is in the closed enum.
func (k Kind) IsClosed() bool {
	switch k {
	case KindAnalysisAgent, KindReadOnlyTool, KindPureTransform:
		return true
	}
	return false
}

// ActivationState is the binding lifecycle state.
type ActivationState string

const (
	StateActive      ActivationState = "active"
	StateDrain       ActivationState = "drain"
	StateRevoked     ActivationState = "revoked"
	StateStale       ActivationState = "stale"
	StateUnqualified ActivationState = "unqualified"
)

// QualificationStatus is the manifest qualification outcome.
type QualificationStatus string

const (
	Qualified   QualificationStatus = "qualified"
	Unqualified QualificationStatus = "unqualified"
	Hold        QualificationStatus = "hold"
)

// RuntimeProfile is the runtime target (Wasm component or Host-managed gRPC).
type RuntimeProfile string

const (
	RuntimeWasmComponent RuntimeProfile = "wasm-component/v1"
	RuntimeGRPCService   RuntimeProfile = "grpc-service/v1"
)

// Manifest is an immutable manifest revision.
type Manifest struct {
	ManifestID         string         `json:"manifest_id"`
	ManifestRevision   int            `json:"manifest_revision"`
	ManifestDigest     string         `json:"manifest_digest"`
	PluginID           string         `json:"plugin_id"`
	Kind               Kind           `json:"kind"`
	Publisher          string         `json:"publisher"`
	Version            string         `json:"version"`
	Capabilities       []Capability   `json:"capabilities"`
	ResourceLimits     ResourceLimits `json:"resource_limits"`
	RuntimeProfile     RuntimeProfile `json:"runtime_profile"`
	WitDigest          string         `json:"wit_digest,omitempty"`
	ServiceProtoDigest string         `json:"service_proto_digest,omitempty"`
	SBOMDigest         string         `json:"sbom_digest"`
	ProvenanceDigest   string         `json:"provenance_digest"`
	SignatureStatus    string         `json:"signature_status"`
	Scope              string         `json:"scope"`
}

// Capability is a declared, bounded capability.
type Capability struct {
	CapabilityID   string `json:"capability_id"`
	CapabilityKind string `json:"capability_kind"`
	Declared       bool   `json:"declared"`
}

// ResourceLimits are the frozen per-binding resource caps.
type ResourceLimits struct {
	CPUMilli    int `json:"cpu_milli"`
	MemoryBytes int `json:"memory_bytes"`
	PIDCount    int `json:"pid_count"`
	FDCount     int `json:"fd_count"`
	DiskBytes   int `json:"disk_bytes"`
	DeadlineMS  int `json:"deadline_ms"`
	OutputBytes int `json:"output_bytes"`
	QueueDepth  int `json:"queue_depth"`
}

// Binding is an activation/binding generation.
type Binding struct {
	PluginID              string              `json:"plugin_id"`
	BindingGeneration     int                 `json:"binding_generation"`
	ManifestID            string              `json:"manifest_id"`
	ManifestRevision      int                 `json:"manifest_revision"`
	ManifestDigest        string              `json:"manifest_digest"`
	ConfigDigest          string              `json:"config_digest"`
	CapabilityDigest      string              `json:"capability_digest"`
	ResourceProfileDigest string              `json:"resource_profile_digest"`
	ActivationState       ActivationState     `json:"activation_state"`
	QualificationStatus   QualificationStatus `json:"qualification_status"`
	Scope                 string              `json:"scope"`
}
