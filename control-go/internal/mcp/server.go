// Package mcp implements the private, read-only masi-mcp-readonly/v1 server.
// The transport is a bounded JSON-only subset of MCP Streamable HTTP
// 2025-11-25: no prompts, sampling, elicitation, mutation, SSE, notifications
// from the server, arbitrary URI, or dynamic tool registration.
package mcp

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"masi-nids/control-go/internal/db"
)

const (
	ProtocolVersion     = "2025-11-25"
	EndpointProfile     = "masi-mcp-readonly/v1"
	maxRequestBytes     = 64 * 1024
	maxResultBytes      = 32 * 1024
	maxTotalResultBytes = 128 * 1024
	maxSessions         = 1024
	sessionTTL          = 30 * time.Minute
)

type Server struct {
	Pool              *db.Pool
	AllowedOrigin     string
	Production        bool
	AllowTestLoopback bool
	Now               func() time.Time

	mu       sync.Mutex
	sessions map[string]session
}

type session struct {
	PluginID          string
	BindingGeneration int
	Ready             bool
	ExpiresAt         time.Time
	Round             int
	Calls             int
	InFlight          int
	ResultBytes       int
}

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcResponse struct {
	JSONRPC string    `json:"jsonrpc"`
	ID      any       `json:"id"`
	Result  any       `json:"result,omitempty"`
	Error   *rpcError `json:"error,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type binding struct {
	PluginID          string
	BindingGeneration int
	ManifestDigest    string
	Scope             string
	Tools             map[string]bool
	Resources         map[string]bool
	ToolTimeout       time.Duration
	MaxResultBytes    int
}

type manifestCapability struct {
	CapabilityID   string `json:"capability_id"`
	CapabilityKind string `json:"capability_kind"`
	Declared       bool   `json:"declared"`
}

type toolDefinition struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

var fixedTools = map[string]toolDefinition{
	"masi.events.get": {
		Name: "masi.events.get", Description: "Read one canonical Event in the bound scope.",
		InputSchema: idSchema("event_id"),
	},
	"masi.incidents.get": {
		Name: "masi.incidents.get", Description: "Read one Incident projection in the bound scope.",
		InputSchema: idSchema("incident_id"),
	},
	"masi.evidence.get": {
		Name: "masi.evidence.get", Description: "Read one evidence reference in the bound scope.",
		InputSchema: idSchema("evidence_id"),
	},
	"masi.targets.get": {
		Name: "masi.targets.get", Description: "Read one managed target projection in the bound scope.",
		InputSchema: idSchema("target_id"),
	},
}

type resourceDefinition struct {
	URI         string `json:"uri"`
	Name        string `json:"name"`
	Description string `json:"description"`
	MimeType    string `json:"mimeType"`
}

var fixedResources = map[string]resourceDefinition{
	"masi.incidents.open":  {URI: "masi://incidents/open", Name: "Open incidents", Description: "At most 20 open incidents in the bound scope.", MimeType: "application/json"},
	"masi.evidence.recent": {URI: "masi://evidence/recent", Name: "Recent evidence", Description: "At most 20 recent evidence references in the bound scope.", MimeType: "application/json"},
}

func idSchema(name string) map[string]any {
	return map[string]any{"type": "object", "additionalProperties": false,
		"required": []string{name}, "properties": map[string]any{name: map[string]any{
			"type": "string", "minLength": 1, "maxLength": 128,
		}}}
}

func (s *Server) Handler(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		w.Header().Set("Allow", "POST, DELETE")
		http.Error(w, "MCP SSE transport disabled", http.StatusMethodNotAllowed)
		return
	}
	if r.Method == http.MethodDelete {
		s.deleteSession(w, r)
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST, DELETE")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.Pool == nil || s.Pool.Pool == nil || s.AllowedOrigin == "" {
		http.Error(w, "MCP unavailable", http.StatusServiceUnavailable)
		return
	}
	if r.Header.Get("Origin") != s.AllowedOrigin {
		http.Error(w, "invalid Origin", http.StatusForbidden)
		return
	}
	if r.Header.Get("MCP-Protocol-Version") != ProtocolVersion {
		http.Error(w, "unsupported MCP protocol version", http.StatusBadRequest)
		return
	}
	if media := strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0])); media != "application/json" {
		http.Error(w, "Content-Type must be application/json", http.StatusUnsupportedMediaType)
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxRequestBytes))
	if err != nil || len(raw) == 0 {
		http.Error(w, "MCP body malformed or oversized", http.StatusBadRequest)
		return
	}
	var req rpcRequest
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil || dec.Decode(&struct{}{}) != io.EOF || req.JSONRPC != "2.0" || req.Method == "" {
		s.writeRPC(w, nil, nil, &rpcError{Code: -32600, Message: "Invalid Request"})
		return
	}
	pluginID := r.Header.Get("X-MASI-Plugin-ID")
	generation, err := strconv.Atoi(r.Header.Get("X-MASI-Binding-Generation"))
	if err != nil || generation < 1 || !validIdentity(pluginID) || !s.authenticateTransport(r, pluginID) {
		http.Error(w, "MCP workload identity rejected", http.StatusUnauthorized)
		return
	}
	binding, err := s.loadBinding(r.Context(), pluginID, generation)
	if err != nil {
		http.Error(w, "MCP binding unavailable", http.StatusForbidden)
		return
	}
	requestDigest := digest(raw)
	traceID := r.Header.Get("X-MASI-Trace-ID")
	if !validIdentity(traceID) {
		traceID = "mcp-" + strings.TrimPrefix(requestDigest, "sha256:")[:24]
	}

	id, idErr := decodeID(req.ID)
	if idErr != nil && req.Method != "notifications/initialized" {
		s.respondAndAudit(w, binding, req.Method, "", requestDigest, traceID, id, nil,
			&rpcError{Code: -32600, Message: "request id required"}, "denied", "MCP_ID_INVALID")
		return
	}

	switch req.Method {
	case "initialize":
		if r.Header.Get("MCP-Session-Id") != "" {
			s.respondAndAudit(w, binding, req.Method, "", requestDigest, traceID, id, nil,
				&rpcError{Code: -32600, Message: "initialize must not reuse a session"}, "denied", "MCP_SESSION_REUSE")
			return
		}
		var params struct {
			ProtocolVersion string         `json:"protocolVersion"`
			Capabilities    map[string]any `json:"capabilities"`
			ClientInfo      struct {
				Name    string `json:"name"`
				Version string `json:"version"`
			} `json:"clientInfo"`
		}
		if strictParams(req.Params, 16*1024, &params) != nil || params.ProtocolVersion != ProtocolVersion ||
			params.ClientInfo.Name == "" || len(params.ClientInfo.Name) > 128 || params.ClientInfo.Version == "" || len(params.ClientInfo.Version) > 64 {
			s.respondAndAudit(w, binding, req.Method, "", requestDigest, traceID, id, nil,
				&rpcError{Code: -32602, Message: "unsupported initialization parameters"}, "denied", "MCP_INITIALIZE_REJECTED")
			return
		}
		sessionID, err := s.newSession(pluginID, generation)
		if err != nil {
			s.respondAndAudit(w, binding, req.Method, "", requestDigest, traceID, id, nil,
				&rpcError{Code: -32000, Message: "session capacity unavailable"}, "failed", "MCP_SESSION_CAPACITY")
			return
		}
		w.Header().Set("MCP-Session-Id", sessionID)
		s.respondAndAudit(w, binding, req.Method, "", requestDigest, traceID, id, map[string]any{
			"protocolVersion": ProtocolVersion,
			"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}, "resources": map[string]any{"listChanged": false, "subscribe": false}},
			"serverInfo":      map[string]string{"name": "masi-control-readonly", "version": "1.0.0"},
		}, nil, "allowed", "MCP_INITIALIZED")
	case "notifications/initialized":
		if len(req.ID) != 0 || !s.markSessionReady(r.Header.Get("MCP-Session-Id"), pluginID, generation) {
			http.Error(w, "invalid MCP session", http.StatusBadRequest)
			return
		}
		if err := s.audit(r.Context(), binding, req.Method, "", requestDigest, "", "allowed", "MCP_SESSION_READY", traceID); err != nil {
			s.mu.Lock()
			delete(s.sessions, r.Header.Get("MCP-Session-Id"))
			s.mu.Unlock()
			http.Error(w, "MCP audit unavailable", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusAccepted)
	case "tools/list":
		if !s.requireReadySession(r, pluginID, generation) {
			http.Error(w, "MCP session not ready", http.StatusBadRequest)
			return
		}
		tools := make([]toolDefinition, 0, len(fixedTools))
		for name, tool := range fixedTools {
			if binding.Tools[name] {
				tools = append(tools, tool)
			}
		}
		sort.Slice(tools, func(i, j int) bool { return tools[i].Name < tools[j].Name })
		s.respondAndAudit(w, binding, req.Method, "", requestDigest, traceID, id,
			map[string]any{"tools": tools}, nil, "allowed", "MCP_TOOLS_LISTED")
	case "tools/call":
		if !s.requireReadySession(r, pluginID, generation) {
			http.Error(w, "MCP session not ready", http.StatusBadRequest)
			return
		}
		var params struct {
			Name      string                     `json:"name"`
			Arguments map[string]json.RawMessage `json:"arguments"`
		}
		if strictParams(req.Params, 16*1024, &params) != nil || !binding.Tools[params.Name] {
			s.respondAndAudit(w, binding, req.Method, params.Name, requestDigest, traceID, id, nil,
				&rpcError{Code: -32602, Message: "tool not allowlisted"}, "denied", "MCP_TOOL_DENIED")
			return
		}
		round, err := strconv.Atoi(r.Header.Get("X-MASI-MCP-Round"))
		if err != nil || round < 1 {
			s.respondAndAudit(w, binding, req.Method, params.Name, requestDigest, traceID, id, nil,
				&rpcError{Code: -32602, Message: "MCP round malformed"}, "denied", "MCP_ROUND_MALFORMED")
			return
		}
		finish, err := s.beginReadOperation(r.Header.Get("MCP-Session-Id"), pluginID, generation, round)
		if err != nil {
			s.respondAndAudit(w, binding, req.Method, params.Name, requestDigest, traceID, id, nil,
				&rpcError{Code: -32000, Message: "MCP call budget exhausted"}, "denied", "MCP_BUDGET_EXHAUSTED")
			return
		}
		defer finish()
		toolCtx, toolCancel := context.WithTimeout(r.Context(), binding.ToolTimeout)
		defer toolCancel()
		result, err := s.callTool(toolCtx, binding, params.Name, params.Arguments)
		if err != nil {
			s.respondAndAudit(w, binding, req.Method, params.Name, requestDigest, traceID, id,
				map[string]any{"content": []any{map[string]string{"type": "text", "text": "read-only tool failed"}}, "isError": true},
				nil, "failed", "MCP_TOOL_FAILED")
			return
		}
		encoded, _ := json.Marshal(result)
		if len(encoded) > binding.MaxResultBytes || !s.consumeResultBytes(r.Header.Get("MCP-Session-Id"), len(encoded)) {
			s.respondAndAudit(w, binding, req.Method, params.Name, requestDigest, traceID, id,
				map[string]any{"content": []any{map[string]string{"type": "text", "text": "bounded result unavailable"}}, "isError": true},
				nil, "failed", "MCP_RESULT_BOUND")
			return
		}
		s.respondAndAudit(w, binding, req.Method, params.Name, requestDigest, traceID, id,
			map[string]any{"content": []any{map[string]string{"type": "text", "text": string(encoded)}}, "structuredContent": result},
			nil, "allowed", "MCP_TOOL_ALLOWED")
	case "resources/list":
		if !s.requireReadySession(r, pluginID, generation) {
			http.Error(w, "MCP session not ready", http.StatusBadRequest)
			return
		}
		resources := make([]resourceDefinition, 0, len(binding.Resources))
		for capability := range binding.Resources {
			resources = append(resources, fixedResources[capability])
		}
		sort.Slice(resources, func(i, j int) bool { return resources[i].URI < resources[j].URI })
		s.respondAndAudit(w, binding, req.Method, "", requestDigest, traceID, id,
			map[string]any{"resources": resources}, nil, "allowed", "MCP_RESOURCES_LISTED")
	case "resources/read":
		if !s.requireReadySession(r, pluginID, generation) {
			http.Error(w, "MCP session not ready", http.StatusBadRequest)
			return
		}
		var params struct {
			URI string `json:"uri"`
		}
		if strictParams(req.Params, 4096, &params) != nil {
			s.respondAndAudit(w, binding, req.Method, params.URI, requestDigest, traceID, id, nil,
				&rpcError{Code: -32602, Message: "resource URI malformed"}, "denied", "MCP_RESOURCE_DENIED")
			return
		}
		capability := ""
		for id, definition := range fixedResources {
			if definition.URI == params.URI && binding.Resources[id] {
				capability = id
			}
		}
		if capability == "" {
			s.respondAndAudit(w, binding, req.Method, params.URI, requestDigest, traceID, id, nil,
				&rpcError{Code: -32602, Message: "resource URI not allowlisted"}, "denied", "MCP_RESOURCE_DENIED")
			return
		}
		round, roundErr := strconv.Atoi(r.Header.Get("X-MASI-MCP-Round"))
		finish, budgetErr := s.beginReadOperation(r.Header.Get("MCP-Session-Id"), pluginID, generation, round)
		if roundErr != nil || budgetErr != nil {
			s.respondAndAudit(w, binding, req.Method, capability, requestDigest, traceID, id, nil,
				&rpcError{Code: -32000, Message: "MCP call budget exhausted"}, "denied", "MCP_BUDGET_EXHAUSTED")
			return
		}
		defer finish()
		resourceCtx, resourceCancel := context.WithTimeout(r.Context(), binding.ToolTimeout)
		defer resourceCancel()
		resource, readErr := s.readResource(resourceCtx, binding, capability)
		if readErr != nil || len(resource) > binding.MaxResultBytes || !s.consumeResultBytes(r.Header.Get("MCP-Session-Id"), len(resource)) {
			s.respondAndAudit(w, binding, req.Method, capability, requestDigest, traceID, id,
				map[string]any{"contents": []any{}, "isError": true}, nil, "failed", "MCP_RESOURCE_FAILED")
			return
		}
		s.respondAndAudit(w, binding, req.Method, capability, requestDigest, traceID, id,
			map[string]any{"contents": []any{map[string]any{"uri": params.URI, "mimeType": "application/json", "text": string(resource)}}},
			nil, "allowed", "MCP_RESOURCE_ALLOWED")
	default:
		s.respondAndAudit(w, binding, req.Method, "", requestDigest, traceID, id, nil,
			&rpcError{Code: -32601, Message: "Method not found"}, "denied", "MCP_METHOD_DENIED")
	}
}

func (s *Server) callTool(ctx context.Context, binding binding, name string, args map[string]json.RawMessage) (map[string]any, error) {
	if len(args) != 1 {
		return nil, errors.New("exactly one identity argument required")
	}
	var argName, query string
	switch name {
	case "masi.events.get":
		argName = "event_id"
		query = `SELECT jsonb_build_object('event_id',event_id,'target_id',target_id,'event_time',event_time,
		 'model_revision_id',model_revision_id,'label_id',label_id,'confidence',confidence,'quality',quality,
		 'trace_id',trace_id) FROM events WHERE event_id=$1 AND scope=$2`
	case "masi.incidents.get":
		argName = "incident_id"
		query = `SELECT jsonb_build_object('incident_id',incident_id,'severity',severity,'status',status,
		 'first_event_id',first_event_id,'last_event_id',last_event_id,'trace_id',trace_id)
		 FROM incidents WHERE incident_id=$1 AND scope=$2`
	case "masi.evidence.get":
		argName = "evidence_id"
		query = `SELECT jsonb_build_object('evidence_id',evidence_id,'kind',kind,'reference_digest',reference_digest,
		 'source',source,'trace_id',trace_id) FROM evidence_refs WHERE evidence_id=$1 AND scope=$2`
	case "masi.targets.get":
		argName = "target_id"
		query = `SELECT jsonb_build_object('target_id',target_id,'status',status,'device_id',device_id,'role',role,
		 'desired_profile_digest',desired_profile_digest) FROM targets WHERE target_id=$1 AND scope=$2`
	default:
		return nil, errors.New("unknown fixed tool")
	}
	var identity string
	if err := json.Unmarshal(args[argName], &identity); err != nil || !validIdentity(identity) {
		return nil, errors.New("tool identity malformed")
	}
	if len(args[argName]) == 0 {
		return nil, errors.New("tool argument missing")
	}
	var raw []byte
	if err := s.Pool.Pool.QueryRow(ctx, query, identity, binding.Scope).Scan(&raw); err != nil {
		return nil, err
	}
	if len(raw) > binding.MaxResultBytes {
		return nil, errors.New("tool result exceeds bound")
	}
	var result map[string]any
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func (s *Server) readResource(ctx context.Context, binding binding, capability string) ([]byte, error) {
	query := ""
	switch capability {
	case "masi.incidents.open":
		query = `SELECT COALESCE(jsonb_agg(x.body ORDER BY x.incident_id),'[]'::jsonb) FROM (
		 SELECT incident_id,jsonb_build_object('incident_id',incident_id,'severity',severity,'status',status,
		 'first_event_id',first_event_id,'last_event_id',last_event_id) body FROM incidents
		 WHERE scope=$1 AND status NOT IN ('closed','resolved') ORDER BY incident_id LIMIT 20) x`
	case "masi.evidence.recent":
		query = `SELECT COALESCE(jsonb_agg(x.body ORDER BY x.evidence_id),'[]'::jsonb) FROM (
		 SELECT evidence_id,jsonb_build_object('evidence_id',evidence_id,'kind',kind,'reference_digest',reference_digest,
		 'source',source) body FROM evidence_refs WHERE scope=$1 ORDER BY evidence_id DESC LIMIT 20) x`
	default:
		return nil, errors.New("unknown fixed resource")
	}
	var raw []byte
	if err := s.Pool.Pool.QueryRow(ctx, query, binding.Scope).Scan(&raw); err != nil {
		return nil, err
	}
	return raw, nil
}

func (s *Server) loadBinding(ctx context.Context, pluginID string, generation int) (binding, error) {
	var out binding
	var kind string
	var capabilities, resourceLimits []byte
	err := s.Pool.Pool.QueryRow(ctx, `SELECT b.plugin_id,b.binding_generation,b.manifest_digest,b.scope,m.kind,m.capabilities,m.resource_limits
		FROM plugin_bindings b JOIN plugin_manifests m ON m.plugin_id=b.plugin_id AND m.manifest_id=b.manifest_id
		 AND m.manifest_revision=b.manifest_revision AND m.manifest_digest=b.manifest_digest
		WHERE b.plugin_id=$1 AND b.binding_generation=$2 AND b.activation_state='active'
		 AND b.qualification_status='qualified'`, pluginID, generation).
		Scan(&out.PluginID, &out.BindingGeneration, &out.ManifestDigest, &out.Scope, &kind, &capabilities, &resourceLimits)
	if err != nil || kind != "analysis-agent" {
		return binding{}, errors.New("active analysis binding unavailable")
	}
	var caps []manifestCapability
	if err := json.Unmarshal(capabilities, &caps); err != nil || len(caps) > 128 {
		return binding{}, errors.New("manifest capabilities malformed")
	}
	out.Tools = map[string]bool{}
	out.Resources = map[string]bool{}
	var limits struct {
		DeadlineMS  int `json:"deadline_ms"`
		OutputBytes int `json:"output_bytes"`
	}
	if err := json.Unmarshal(resourceLimits, &limits); err != nil || limits.DeadlineMS < 1 || limits.OutputBytes < 1 {
		return binding{}, errors.New("manifest resource limits malformed")
	}
	out.ToolTimeout = 2 * time.Second
	if declared := time.Duration(limits.DeadlineMS) * time.Millisecond; declared < out.ToolTimeout {
		out.ToolTimeout = declared
	}
	out.MaxResultBytes = maxResultBytes
	if limits.OutputBytes < out.MaxResultBytes {
		out.MaxResultBytes = limits.OutputBytes
	}
	for _, cap := range caps {
		if !cap.Declared || !validIdentity(cap.CapabilityID) {
			continue
		}
		switch cap.CapabilityKind {
		case "mcp-tool":
			if _, supported := fixedTools[cap.CapabilityID]; !supported {
				return binding{}, fmt.Errorf("unknown MCP tool capability %s", cap.CapabilityID)
			}
			out.Tools[cap.CapabilityID] = true
		case "mcp-resource":
			if _, supported := fixedResources[cap.CapabilityID]; !supported {
				return binding{}, fmt.Errorf("unknown MCP resource capability %s", cap.CapabilityID)
			}
			out.Resources[cap.CapabilityID] = true
		default:
			if strings.HasPrefix(cap.CapabilityKind, "mcp-") {
				return binding{}, fmt.Errorf("unsupported MCP capability kind %s", cap.CapabilityKind)
			}
		}
	}
	return out, nil
}

func (s *Server) authenticateTransport(r *http.Request, pluginID string) bool {
	if s.Production {
		if r.TLS == nil || len(r.TLS.VerifiedChains) == 0 || len(r.TLS.PeerCertificates) != 1 {
			return false
		}
		return certificateHasExactSAN(r.TLS.PeerCertificates[0], pluginID)
	}
	if !s.AllowTestLoopback || r.Header.Get("X-MASI-Test-Plugin-ID") != pluginID {
		return false
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	return err == nil && net.ParseIP(host) != nil && net.ParseIP(host).IsLoopback()
}

func certificateHasExactSAN(cert *x509.Certificate, identity string) bool {
	if cert == nil {
		return false
	}
	matches := 0
	total := 0
	for _, name := range cert.DNSNames {
		total++
		if name == identity {
			matches++
		}
	}
	for _, uri := range cert.URIs {
		total++
		if uri.String() == identity {
			matches++
		}
	}
	return matches == 1 && total == 1
}

func (s *Server) newSession(pluginID string, generation int) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ensureSessions()
	s.pruneLocked()
	if len(s.sessions) >= maxSessions {
		return "", errors.New("session bound reached")
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	id := hex.EncodeToString(raw)
	s.sessions[id] = session{PluginID: pluginID, BindingGeneration: generation, ExpiresAt: s.now().Add(sessionTTL)}
	return id, nil
}

func (s *Server) markSessionReady(id, pluginID string, generation int) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ensureSessions()
	s.pruneLocked()
	item, ok := s.sessions[id]
	if !ok || item.PluginID != pluginID || item.BindingGeneration != generation {
		return false
	}
	item.Ready = true
	item.ExpiresAt = s.now().Add(sessionTTL)
	s.sessions[id] = item
	return true
}

func (s *Server) requireReadySession(r *http.Request, pluginID string, generation int) bool {
	id := r.Header.Get("MCP-Session-Id")
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ensureSessions()
	s.pruneLocked()
	item, ok := s.sessions[id]
	if !ok || !item.Ready || item.PluginID != pluginID || item.BindingGeneration != generation {
		return false
	}
	item.ExpiresAt = s.now().Add(sessionTTL)
	s.sessions[id] = item
	return true
}

func (s *Server) beginReadOperation(id, pluginID string, generation, round int) (func(), error) {
	if round < 1 || round > 2 {
		return func() {}, errors.New("round outside 1..2")
	}
	s.mu.Lock()
	s.ensureSessions()
	s.pruneLocked()
	item, ok := s.sessions[id]
	if !ok || !item.Ready || item.PluginID != pluginID || item.BindingGeneration != generation ||
		item.Calls >= 6 || item.InFlight >= 4 || round < item.Round || round > item.Round+1 {
		s.mu.Unlock()
		return func() {}, errors.New("session/call/parallel/round budget exhausted")
	}
	item.Round = round
	item.Calls++
	item.InFlight++
	item.ExpiresAt = s.now().Add(sessionTTL)
	s.sessions[id] = item
	s.mu.Unlock()
	return func() {
		s.mu.Lock()
		if current, exists := s.sessions[id]; exists && current.InFlight > 0 {
			current.InFlight--
			s.sessions[id] = current
		}
		s.mu.Unlock()
	}, nil
}

func (s *Server) consumeResultBytes(id string, amount int) bool {
	if amount < 0 {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.sessions[id]
	if !ok || item.ResultBytes+amount > maxTotalResultBytes {
		return false
	}
	item.ResultBytes += amount
	s.sessions[id] = item
	return true
}

func (s *Server) deleteSession(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Origin") != s.AllowedOrigin || r.Header.Get("MCP-Protocol-Version") != ProtocolVersion {
		http.Error(w, "invalid MCP transport", http.StatusBadRequest)
		return
	}
	pluginID := r.Header.Get("X-MASI-Plugin-ID")
	generation, err := strconv.Atoi(r.Header.Get("X-MASI-Binding-Generation"))
	if err != nil || generation < 1 || !s.authenticateTransport(r, pluginID) ||
		!s.requireReadySession(r, pluginID, generation) {
		http.Error(w, "invalid MCP workload/session", http.StatusUnauthorized)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ensureSessions()
	delete(s.sessions, r.Header.Get("MCP-Session-Id"))
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) ensureSessions() {
	if s.sessions == nil {
		s.sessions = make(map[string]session)
	}
}

func (s *Server) pruneLocked() {
	now := s.now()
	for id, item := range s.sessions {
		if !item.ExpiresAt.After(now) {
			delete(s.sessions, id)
		}
	}
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func (s *Server) respondAndAudit(w http.ResponseWriter, binding binding, method, capability, requestDigest, traceID string,
	id any, result any, rpcErr *rpcError, outcome, reason string) {
	response := rpcResponse{JSONRPC: "2.0", ID: id, Result: result, Error: rpcErr}
	raw, err := json.Marshal(response)
	if err != nil || len(raw) > maxResultBytes {
		response = rpcResponse{JSONRPC: "2.0", ID: id, Error: &rpcError{Code: -32000, Message: "bounded response unavailable"}}
		raw, _ = json.Marshal(response)
		outcome, reason = "failed", "MCP_RESPONSE_BOUND"
	}
	responseDigest := digest(raw)
	auditCtx, auditCancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer auditCancel()
	if err := s.audit(auditCtx, binding, method, capability, requestDigest, responseDigest, outcome, reason, traceID); err != nil {
		response = rpcResponse{JSONRPC: "2.0", ID: id, Error: &rpcError{Code: -32000, Message: "audit unavailable"}}
		raw, _ = json.Marshal(response)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

func (s *Server) audit(ctx context.Context, binding binding, method, capability, requestDigest, responseDigest,
	outcome, reason, traceID string) error {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return err
	}
	_, err := s.Pool.Pool.Exec(ctx, `INSERT INTO mcp_access_audit(audit_id,plugin_id,binding_generation,
		manifest_digest,scope,protocol_version,method,capability_id,request_digest,response_digest,outcome,
		reason_code,trace_id,created_at_unix_ms) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`,
		"mcp-audit-"+hex.EncodeToString(raw), binding.PluginID, binding.BindingGeneration, binding.ManifestDigest,
		binding.Scope, ProtocolVersion, method, nullable(capability), requestDigest, nullable(responseDigest), outcome,
		reason, traceID, s.now().UnixMilli())
	return err
}

func (s *Server) writeRPC(w http.ResponseWriter, id, result any, rpcErr *rpcError) {
	raw, _ := json.Marshal(rpcResponse{JSONRPC: "2.0", ID: id, Result: result, Error: rpcErr})
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

func decodeID(raw json.RawMessage) (any, error) {
	if len(raw) == 0 || string(raw) == "null" || len(raw) > 256 {
		return nil, errors.New("id missing")
	}
	var value any
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.UseNumber()
	if err := dec.Decode(&value); err != nil {
		return nil, err
	}
	switch value.(type) {
	case string, json.Number:
		return value, nil
	default:
		return nil, errors.New("id must be string or number")
	}
}

func strictParams(raw json.RawMessage, maximum int, dst any) error {
	if len(raw) == 0 || len(raw) > maximum {
		return errors.New("params missing or oversized")
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	if err := dec.Decode(&struct{}{}); err != io.EOF {
		return errors.New("trailing params")
	}
	return nil
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func validIdentity(value string) bool {
	if value == "" || len(value) > 128 {
		return false
	}
	for i, r := range value {
		if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') ||
			r == '.' || r == '_' || r == ':' || r == '-' {
			if i == 0 && !((r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')) {
				return false
			}
			continue
		}
		return false
	}
	return true
}

func nullable(value string) any {
	if value == "" {
		return nil
	}
	return value
}
