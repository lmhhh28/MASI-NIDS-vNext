package a2a

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

type peerRuntime struct {
	config config.A2APeer
	base   *url.URL
	http   *http.Client
}

type Client struct {
	pool  *db.Pool
	peers map[string]*peerRuntime // exact plugin_id -> peer
	now   func() time.Time
}

type activeBinding struct {
	PluginID          string
	BindingGeneration int
	ManifestDigest    string
	Scope             string
}

type a2aSendRequest struct {
	Message struct {
		MessageID string    `json:"messageId"`
		Role      string    `json:"role"`
		Parts     []a2aPart `json:"parts"`
		Metadata  any       `json:"metadata"`
	} `json:"message"`
	Configuration struct {
		AcceptedOutputModes []string `json:"acceptedOutputModes"`
	} `json:"configuration"`
	Metadata map[string]any `json:"metadata"`
}

type a2aPart struct {
	Text      *string         `json:"text,omitempty"`
	Raw       *string         `json:"raw,omitempty"`
	URL       *string         `json:"url,omitempty"`
	Data      json.RawMessage `json:"data,omitempty"`
	MediaType string          `json:"mediaType,omitempty"`
}

type a2aResponse struct {
	Task    *a2aTask    `json:"task,omitempty"`
	Message *a2aMessage `json:"message,omitempty"`
}

type a2aTask struct {
	ID        string         `json:"id"`
	ContextID string         `json:"contextId,omitempty"`
	Status    a2aTaskStatus  `json:"status"`
	Artifacts []a2aArtifact  `json:"artifacts,omitempty"`
	History   []a2aMessage   `json:"history,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

type a2aTaskStatus struct {
	State     string      `json:"state"`
	Message   *a2aMessage `json:"message,omitempty"`
	Timestamp string      `json:"timestamp,omitempty"`
}

type a2aMessage struct {
	MessageID string         `json:"messageId"`
	ContextID string         `json:"contextId,omitempty"`
	TaskID    string         `json:"taskId,omitempty"`
	Role      string         `json:"role"`
	Parts     []a2aPart      `json:"parts"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

type a2aArtifact struct {
	ArtifactID  string         `json:"artifactId"`
	Name        string         `json:"name,omitempty"`
	Description string         `json:"description,omitempty"`
	Parts       []a2aPart      `json:"parts"`
	Metadata    map[string]any `json:"metadata,omitempty"`
}

type manifestCapability struct {
	CapabilityID   string `json:"capability_id"`
	CapabilityKind string `json:"capability_kind"`
	Declared       bool   `json:"declared"`
}

func NewClient(pool *db.Pool, configs []config.A2APeer, production bool) (*Client, error) {
	client := &Client{pool: pool, peers: make(map[string]*peerRuntime), now: time.Now}
	for _, peer := range configs {
		if _, exists := client.peers[peer.PluginID]; exists {
			return nil, errors.New("a2a: duplicate peer plugin identity")
		}
		runtime, err := buildPeer(peer, production)
		if err != nil {
			return nil, fmt.Errorf("a2a: build peer %s: %w", peer.PeerID, err)
		}
		client.peers[peer.PluginID] = runtime
	}
	return client, nil
}

func buildPeer(peer config.A2APeer, production bool) (*peerRuntime, error) {
	base, err := url.Parse(peer.BaseURL)
	if err != nil || base.Hostname() == "" || base.User != nil || base.RawQuery != "" || base.Fragment != "" ||
		(base.Path != "" && base.Path != "/") || peer.MaxResponseBytes < 1024 || peer.MaxResponseBytes > 128*1024 {
		return nil, errors.New("static peer URL/response bound malformed")
	}
	allowed := make(map[string]bool, len(peer.AllowedIPs))
	for _, raw := range peer.AllowedIPs {
		ip := net.ParseIP(raw)
		if ip == nil || ip.IsUnspecified() || ip.IsMulticast() || (production && ip.IsLoopback()) {
			return nil, errors.New("static peer IP allowlist malformed")
		}
		allowed[ip.String()] = true
	}
	if len(allowed) == 0 || len(allowed) > 16 {
		return nil, errors.New("static peer IP allowlist bound invalid")
	}
	port := base.Port()
	if port == "" {
		if production {
			port = "443"
		} else {
			port = "80"
		}
	}
	dial := func(ctx context.Context, network, _ string) (net.Conn, error) {
		addresses := make([]string, 0, len(allowed))
		if literal := net.ParseIP(base.Hostname()); literal != nil {
			if allowed[literal.String()] {
				addresses = append(addresses, literal.String())
			}
		} else {
			resolved, err := net.DefaultResolver.LookupIPAddr(ctx, base.Hostname())
			if err != nil {
				return nil, err
			}
			for _, item := range resolved {
				if allowed[item.IP.String()] {
					addresses = append(addresses, item.IP.String())
				}
			}
		}
		if len(addresses) == 0 {
			return nil, errors.New("a2a: DNS result outside static IP allowlist")
		}
		sort.Strings(addresses)
		return (&net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}).DialContext(ctx, network, net.JoinHostPort(addresses[0], port))
	}
	transport := &http.Transport{Proxy: nil, DialContext: dial, DisableCompression: true,
		ForceAttemptHTTP2: true, MaxIdleConns: 16, MaxIdleConnsPerHost: 4, IdleConnTimeout: time.Minute,
		TLSHandshakeTimeout: 3 * time.Second, ResponseHeaderTimeout: 5 * time.Second}
	if production {
		if base.Scheme != "https" || peer.ServerName == "" {
			return nil, errors.New("production peer requires HTTPS/server name")
		}
		rootsPEM, err := os.ReadFile(peer.CAFile)
		if err != nil {
			return nil, err
		}
		roots := x509.NewCertPool()
		if !roots.AppendCertsFromPEM(rootsPEM) {
			return nil, errors.New("peer CA contains no certificate")
		}
		cert, err := tls.LoadX509KeyPair(peer.CertFile, peer.KeyFile)
		if err != nil {
			return nil, err
		}
		transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS13, RootCAs: roots,
			Certificates: []tls.Certificate{cert}, ServerName: peer.ServerName}
	} else if base.Scheme != "http" || (base.Hostname() != "localhost" &&
		(net.ParseIP(base.Hostname()) == nil || !net.ParseIP(base.Hostname()).IsLoopback())) {
		return nil, errors.New("test peer must be loopback HTTP")
	}
	return &peerRuntime{config: peer, base: base, http: &http.Client{Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("a2a: redirects disabled") }}}, nil
}

func (c *Client) Submit(ctx context.Context, input InputBundle, actor security.Actor) (*TaskProjection, error) {
	if c == nil || c.pool == nil {
		return nil, errors.New("a2a: client unavailable")
	}
	if err := c.freezeInput(ctx, &input); err != nil {
		return nil, err
	}
	if err := ValidateInput(&input, c.now()); err != nil {
		return nil, err
	}
	actorID, err := actorRef(actor)
	if err != nil {
		return nil, err
	}
	binding, err := c.loadActiveBinding(ctx, input.PluginID, input.BindingGeneration, input.Scope)
	if err != nil {
		return nil, err
	}
	peer := c.peers[input.PluginID]
	if peer == nil {
		return nil, errors.New("a2a: plugin has no statically allowlisted peer")
	}
	wire, raw, requestDigest, err := buildSendRequest(input)
	if err != nil {
		return nil, err
	}
	_ = wire
	inserted := false
	projection := TaskProjection{TaskID: input.TaskID, PluginID: input.PluginID,
		BindingGeneration: input.BindingGeneration, Status: "submitted", RequestDigest: requestDigest}
	err = c.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `INSERT INTO analysis_task_requests(task_id,plugin_id,binding_generation,
			manifest_digest,scope,input_digest,request_digest,peer_id,status,poll_count,deadline_unix_ms,
			actor_ref,trace_id,reason_code,created_at_unix_ms,updated_at_unix_ms,idempotency_key,a2a_version,input_bundle)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,'submitted',0,$9,$10,$11,'A2A_SUBMITTED',$12,$12,$13,'1.0',$14)
			ON CONFLICT(actor_ref,idempotency_key) DO NOTHING`, input.TaskID, binding.PluginID,
			binding.BindingGeneration, binding.ManifestDigest, binding.Scope, input.InputDigest, requestDigest,
			peer.config.PeerID, input.DeadlineUnixMS, actorID, input.TraceID, c.now().UnixMilli(), input.IdempotencyKey, inputRawForDB(input))
		if err != nil {
			return err
		}
		inserted = tag.RowsAffected() == 1
		if !inserted {
			return tx.QueryRow(ctx, `SELECT task_id,plugin_id,binding_generation,status,poll_count,request_digest,
				COALESCE(remote_task_id,''),COALESCE(remote_context_id,''),COALESCE(last_response_digest,'')
				FROM analysis_task_requests WHERE actor_ref=$1 AND idempotency_key=$2 FOR SHARE`, actorID, input.IdempotencyKey).
				Scan(&projection.TaskID, &projection.PluginID, &projection.BindingGeneration, &projection.Status,
					&projection.PollCount, &projection.RequestDigest, &projection.RemoteTaskID,
					&projection.RemoteContextID, &projection.ResponseDigest)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if !inserted {
		if projection.RequestDigest != requestDigest || projection.PluginID != input.PluginID ||
			projection.BindingGeneration != input.BindingGeneration || projection.TaskID != input.TaskID {
			return nil, errors.New("a2a: idempotency key reused with different task identity")
		}
		return &projection, nil
	}
	submitCtx, cancel := context.WithDeadline(ctx, time.UnixMilli(input.DeadlineUnixMS))
	defer cancel()
	response, responseRaw, err := c.do(submitCtx, peer, http.MethodPost, "/message:send", raw)
	if err != nil {
		_ = c.markAmbiguous(context.Background(), input.TaskID, requestDigest)
		return &TaskProjection{TaskID: input.TaskID, PluginID: input.PluginID, BindingGeneration: input.BindingGeneration,
			Status: "unknown", RequestDigest: requestDigest}, err
	}
	appliedProjection, err := c.applyTaskResponse(ctx, input, binding, response, responseRaw, false)
	if appliedProjection != nil {
		appliedProjection.RequestDigest = requestDigest
	}
	return appliedProjection, err
}

// freezeInput replaces caller hints with Go-read canonical fact digests. A
// supplied digest must already match; missing facts or scope drift fail closed.
func (c *Client) freezeInput(ctx context.Context, input *InputBundle) error {
	if input == nil || input.Scope == "" || len(input.EventRefs) > 128 || len(input.IncidentRefs) > 32 ||
		len(input.EvidenceRefs) > 128 || len(input.RuntimeRefs) > 64 {
		return errors.New("a2a: input reference envelope malformed")
	}
	type group struct {
		refs   []FactRef
		query  string
		direct bool
	}
	groups := []group{
		{refs: input.EventRefs, query: `SELECT e.event_id,jsonb_build_object('event_id',e.event_id,'target_id',e.target_id,
			'shard_id',e.shard_id,'event_time_unix_ms',(extract(epoch from e.event_time)*1000)::bigint,
			'input_digest',e.input_digest,'output_digest',e.output_digest,'model_revision_id',e.model_revision_id,
			'label_id',e.label_id,'confidence',e.confidence,'quality',e.quality,'trace_id',e.trace_id)
			FROM events e WHERE e.event_id=ANY($1) AND e.scope=$2`},
		{refs: input.IncidentRefs, query: `SELECT i.incident_id,jsonb_build_object('incident_id',i.incident_id,
			'severity',i.severity,'status',i.status,'first_event_id',i.first_event_id,'last_event_id',i.last_event_id,
			'trace_id',i.trace_id) FROM incidents i WHERE i.incident_id=ANY($1) AND i.scope=$2`},
		{refs: input.EvidenceRefs, query: `SELECT evidence_id,reference_digest FROM evidence_refs
			WHERE evidence_id=ANY($1) AND scope=$2`, direct: true},
		{refs: input.RuntimeRefs, query: `SELECT c.observation_id,jsonb_build_object('observation_id',c.observation_id,
			'target_id',c.target_id,'target_control_incarnation_id',c.target_control_incarnation_id,
			'assignment_generation',c.assignment_generation,'actor_runtime_epoch',c.actor_runtime_epoch,
			'application_generation',c.application_generation,'p4info_digest',c.p4info_digest,
			'pipeline_digest',c.pipeline_digest,'capacity_digest',c.capacity_digest,'freshness',c.freshness,
			'observed_at_unix_ms',c.observed_at_unix_ms,'expires_at_unix_ms',c.expires_at_unix_ms)
			FROM target_capability_observations c JOIN targets t ON t.target_id=c.target_id
			WHERE c.observation_id=ANY($1) AND t.scope=$2`},
	}
	outputs := []*[]FactRef{&input.EventRefs, &input.IncidentRefs, &input.EvidenceRefs, &input.RuntimeRefs}
	for index, item := range groups {
		if len(item.refs) == 0 {
			continue
		}
		ids := make([]string, 0, len(item.refs))
		hints := make(map[string]string, len(item.refs))
		seenIDs := make(map[string]bool, len(item.refs))
		for _, ref := range item.refs {
			if !validIdentity(ref.ID) || seenIDs[ref.ID] {
				return errors.New("a2a: fact identity malformed or duplicated")
			}
			seenIDs[ref.ID] = true
			hints[ref.ID] = ref.Digest
			ids = append(ids, ref.ID)
		}
		rows, err := c.pool.Pool.Query(ctx, item.query, ids, input.Scope)
		if err != nil {
			return err
		}
		frozen := make([]FactRef, 0, len(ids))
		for rows.Next() {
			var id string
			var raw []byte
			if err := rows.Scan(&id, &raw); err != nil {
				rows.Close()
				return err
			}
			canonicalDigest := string(raw)
			if !item.direct {
				canonicalDigest = digest(raw)
			}
			if hint := hints[id]; hint != "" && hint != canonicalDigest {
				rows.Close()
				return errors.New("a2a: caller fact digest differs from canonical fact")
			}
			frozen = append(frozen, FactRef{ID: id, Digest: canonicalDigest})
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		if len(frozen) != len(ids) {
			return errors.New("a2a: referenced fact missing or outside scope")
		}
		sortFactRefs(frozen)
		*outputs[index] = frozen
	}
	input.InputDigest = ""
	input.InputDigest = ComputeInputDigest(*input)
	return nil
}

func (c *Client) Poll(ctx context.Context, taskID string, actor security.Actor) (*TaskProjection, error) {
	if c == nil || c.pool == nil || !validIdentity(taskID) {
		return nil, errors.New("a2a: task/client unavailable")
	}
	actorID, err := actorRef(actor)
	if err != nil {
		return nil, err
	}
	var input InputBundle
	var binding activeBinding
	var remoteTask, remoteContext, peerID, status string
	var pollCount int
	var requestDigest string
	err = c.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var inputDigest, idempotency, traceID string
		var inputRaw []byte
		var deadline int64
		err := tx.QueryRow(ctx, `SELECT t.plugin_id,t.binding_generation,t.manifest_digest,t.scope,t.input_digest,
			t.remote_task_id,t.remote_context_id,t.peer_id,t.status,t.poll_count,t.request_digest,t.deadline_unix_ms,
			t.idempotency_key,t.trace_id,t.input_bundle FROM analysis_task_requests t
			WHERE t.task_id=$1 AND t.actor_ref=$2 FOR UPDATE`, taskID, actorID).
			Scan(&binding.PluginID, &binding.BindingGeneration, &binding.ManifestDigest, &binding.Scope, &inputDigest,
				&remoteTask, &remoteContext, &peerID, &status, &pollCount, &requestDigest, &deadline, &idempotency, &traceID, &inputRaw)
		if err != nil {
			return err
		}
		if remoteTask == "" || remoteContext == "" || pollCount >= 3 || deadline <= c.now().UnixMilli() ||
			(status != "submitted" && status != "working") {
			return errors.New("a2a: task not pollable within bounds")
		}
		if err := json.Unmarshal(inputRaw, &input); err != nil || input.TaskID != taskID || input.PluginID != binding.PluginID ||
			input.BindingGeneration != binding.BindingGeneration || input.Scope != binding.Scope || input.InputDigest != inputDigest ||
			input.DeadlineUnixMS != deadline || input.IdempotencyKey != idempotency || input.TraceID != traceID ||
			ComputeInputDigest(input) != input.InputDigest {
			return errors.New("a2a: persisted frozen input bundle invalid")
		}
		tag, err := tx.Exec(ctx, `UPDATE analysis_task_requests SET poll_count=poll_count+1,
			updated_at_unix_ms=$1,reason_code='A2A_POLLING' WHERE task_id=$2 AND poll_count=$3`,
			c.now().UnixMilli(), taskID, pollCount)
		if err != nil || tag.RowsAffected() != 1 {
			return errors.New("a2a: poll CAS conflict")
		}
		pollCount++
		return nil
	})
	if err != nil {
		return nil, err
	}
	current, err := c.loadActiveBinding(ctx, binding.PluginID, binding.BindingGeneration, binding.Scope)
	if err != nil || current.ManifestDigest != binding.ManifestDigest {
		_, _ = c.pool.Pool.Exec(ctx, `UPDATE analysis_task_requests SET status='fenced',reason_code='A2A_BINDING_FENCED',
			updated_at_unix_ms=$1 WHERE task_id=$2`, c.now().UnixMilli(), taskID)
		return nil, errors.New("a2a: binding generation revoked or drifted")
	}
	peer := c.peers[binding.PluginID]
	if peer == nil || peer.config.PeerID != peerID {
		return nil, errors.New("a2a: static peer identity drifted")
	}
	response, responseRaw, err := c.do(ctx, peer, http.MethodGet, "/tasks/"+url.PathEscape(remoteTask), nil)
	if err != nil {
		return &TaskProjection{TaskID: taskID, RemoteTaskID: remoteTask, RemoteContextID: remoteContext,
			PluginID: binding.PluginID, BindingGeneration: binding.BindingGeneration, Status: status,
			PollCount: pollCount, RequestDigest: requestDigest}, err
	}
	projection, err := c.applyTaskResponse(ctx, input, binding, response, responseRaw, true)
	if projection != nil {
		projection.PollCount = pollCount
	}
	return projection, err
}

func buildSendRequest(input InputBundle) (a2aSendRequest, []byte, string, error) {
	inputRaw, err := json.Marshal(input)
	if err != nil {
		return a2aSendRequest{}, nil, "", err
	}
	var wire a2aSendRequest
	wire.Message.MessageID = "msg-" + strings.TrimPrefix(digest([]byte(input.TaskID+":"+input.InputDigest)), "sha256:")[:32]
	wire.Message.Role = "ROLE_USER"
	wire.Message.Parts = []a2aPart{{Data: inputRaw, MediaType: "application/json"}}
	wire.Message.Metadata = map[string]any{"schemaVersion": InputSchema, "inputDigest": input.InputDigest,
		"pluginId": input.PluginID, "bindingGeneration": input.BindingGeneration, "deadlineUnixMs": input.DeadlineUnixMS}
	wire.Configuration.AcceptedOutputModes = []string{"application/json"}
	wire.Metadata = map[string]any{"traceId": input.TraceID, "noPush": true, "noStreaming": true, "delegationDepth": 0}
	raw, err := json.Marshal(wire)
	if err != nil || len(raw) > 64*1024 {
		return a2aSendRequest{}, nil, "", errors.New("a2a: send request exceeds 64 KiB")
	}
	return wire, raw, digest(raw), nil
}

func (c *Client) do(ctx context.Context, peer *peerRuntime, method, path string, body []byte) (*a2aResponse, []byte, error) {
	deadline := c.now().Add(30 * time.Second)
	if existing, ok := ctx.Deadline(); ok && existing.Before(deadline) {
		deadline = existing
	}
	callCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	u := *peer.base
	u.Path = path
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(callCtx, method, u.String(), reader)
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("A2A-Version", ProtocolVersion)
	req.Header.Set("Accept", "application/a2a+json")
	if body != nil {
		req.Header.Set("Content-Type", "application/a2a+json")
	}
	resp, err := peer.http.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	if resp.Header.Get("A2A-Version") != ProtocolVersion {
		return nil, nil, errors.New("a2a: response protocol version missing or unsupported")
	}
	if media := strings.ToLower(strings.TrimSpace(strings.Split(resp.Header.Get("Content-Type"), ";")[0])); media != "application/a2a+json" {
		return nil, nil, errors.New("a2a: response content type unsupported")
	}
	limited := io.LimitReader(resp.Body, int64(peer.config.MaxResponseBytes)+1)
	raw, err := io.ReadAll(limited)
	if err != nil || len(raw) == 0 || len(raw) > peer.config.MaxResponseBytes {
		return nil, nil, errors.New("a2a: response malformed or exceeds bound")
	}
	if resp.StatusCode != http.StatusOK {
		return nil, nil, fmt.Errorf("a2a: peer status %d", resp.StatusCode)
	}
	var decoded a2aResponse
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&decoded); err != nil || dec.Decode(&struct{}{}) != io.EOF || (decoded.Task == nil) == (decoded.Message == nil) {
		return nil, nil, errors.New("a2a: response must contain exactly one task or message")
	}
	if decoded.Task == nil {
		return nil, nil, errors.New("a2a: polling profile rejects direct Message response")
	}
	return &decoded, raw, nil
}

func (c *Client) applyTaskResponse(ctx context.Context, input InputBundle, binding activeBinding,
	response *a2aResponse, raw []byte, polling bool) (*TaskProjection, error) {
	if response == nil || response.Task == nil || !validIdentity(response.Task.ID) || !validIdentity(response.Task.ContextID) {
		return nil, errors.New("a2a: task identity malformed")
	}
	var storedRemote string
	if err := c.pool.Pool.QueryRow(ctx, `SELECT COALESCE(remote_task_id,'') FROM analysis_task_requests WHERE task_id=$1`, input.TaskID).Scan(&storedRemote); err != nil {
		return nil, err
	}
	if polling && storedRemote != response.Task.ID || !polling && storedRemote != "" && storedRemote != response.Task.ID {
		return nil, errors.New("a2a: remote task identity drift")
	}
	status, terminal, err := mapTaskStatus(response.Task.Status.State)
	if err != nil {
		status = "unknown"
	}
	artifacts := make([]Artifact, 0, len(response.Task.Artifacts))
	if response.Task.Status.State == "TASK_STATE_COMPLETED" {
		if len(response.Task.Artifacts) == 0 || len(response.Task.Artifacts) > 8 {
			return nil, errors.New("a2a: completed analysis task requires 1..8 bounded artifacts")
		}
		for _, wire := range response.Task.Artifacts {
			if !validIdentity(wire.ArtifactID) || len(wire.Parts) != 1 || wire.Parts[0].Text != nil ||
				wire.Parts[0].Raw != nil || wire.Parts[0].URL != nil || len(wire.Parts[0].Data) == 0 ||
				wire.Parts[0].MediaType != "application/json" {
				return nil, errors.New("a2a: artifact must contain one structured JSON data part")
			}
			var artifact Artifact
			dec := json.NewDecoder(bytes.NewReader(wire.Parts[0].Data))
			dec.DisallowUnknownFields()
			if err := dec.Decode(&artifact); err != nil || dec.Decode(&struct{}{}) != io.EOF || artifact.ArtifactID != wire.ArtifactID {
				return nil, errors.New("a2a: artifact body malformed or identity mismatch")
			}
			if err := ValidateArtifact(&artifact, input); err != nil {
				return nil, err
			}
			artifacts = append(artifacts, artifact)
		}
		status = aggregateArtifactOutcome(artifacts)
	}
	responseDigest := digest(raw)
	nowMS := c.now().UnixMilli()
	err = c.pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		var currentManifest string
		if err := tx.QueryRow(ctx, `SELECT b.manifest_digest FROM plugin_bindings b
			WHERE b.plugin_id=$1 AND b.binding_generation=$2 AND b.activation_state='active'
			  AND b.qualification_status='qualified' FOR SHARE`, binding.PluginID, binding.BindingGeneration).
			Scan(&currentManifest); err != nil || currentManifest != binding.ManifestDigest {
			return errors.New("a2a: binding fenced before response commit")
		}
		tag, err := tx.Exec(ctx, `UPDATE analysis_task_requests SET remote_task_id=$1,remote_context_id=$2,
			status=$3,last_response_digest=$4,response_bytes=$5,updated_at_unix_ms=$6,reason_code=$7
			WHERE task_id=$8 AND plugin_id=$9 AND binding_generation=$10 AND manifest_digest=$11
			  AND status IN ('submitted','working','unknown')`, response.Task.ID, response.Task.ContextID, status,
			responseDigest, len(raw), nowMS, "A2A_"+strings.ToUpper(status), input.TaskID, binding.PluginID,
			binding.BindingGeneration, binding.ManifestDigest)
		if err != nil || tag.RowsAffected() != 1 {
			return errors.New("a2a: task response CAS conflict")
		}
		for index, artifact := range artifacts {
			body, _ := json.Marshal(artifact)
			if _, err := tx.Exec(ctx, `INSERT INTO analysis_artifacts(artifact_id,task_id,plugin_id,binding_generation,
				scope,artifact_digest,media_type,body,body_bytes,non_executable,deployment_eligible,trace_id,
				created_at_unix_ms,artifact_sequence,input_digest,tool_trajectory_digest,analysis_outcome)
				VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,true,false,$10,$11,$12,$13,$14,$15)`, artifact.ArtifactID,
				input.TaskID, binding.PluginID, binding.BindingGeneration, binding.Scope, artifact.ArtifactDigest,
				artifact.MediaType, body, len(body), artifact.TraceID, nowMS, index, artifact.InputDigest,
				artifact.ToolTrajectoryDigest, artifact.AnalysisOutcome); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO evidence_refs(evidence_id,kind,reference_digest,source,trace_id,scope)
				VALUES($1,'agent-artifact',$2,$3,$4,$5)`, artifact.ArtifactID, artifact.ArtifactDigest,
				"a2a:"+binding.PluginID, artifact.TraceID, binding.Scope); err != nil {
				return err
			}
		}
		return nil
	})
	projection := &TaskProjection{TaskID: input.TaskID, RemoteTaskID: response.Task.ID,
		RemoteContextID: response.Task.ContextID, PluginID: binding.PluginID,
		BindingGeneration: binding.BindingGeneration, Status: status, RequestDigest: "", ResponseDigest: responseDigest}
	if err != nil {
		return projection, err
	}
	_ = terminal
	return projection, nil
}

func (c *Client) loadActiveBinding(ctx context.Context, pluginID string, generation int, scope string) (activeBinding, error) {
	var out activeBinding
	var capabilities []byte
	var kind string
	err := c.pool.Pool.QueryRow(ctx, `SELECT b.plugin_id,b.binding_generation,b.manifest_digest,b.scope,m.kind,m.capabilities
		FROM plugin_bindings b JOIN plugin_manifests m ON m.plugin_id=b.plugin_id AND m.manifest_id=b.manifest_id
		 AND m.manifest_revision=b.manifest_revision AND m.manifest_digest=b.manifest_digest
		WHERE b.plugin_id=$1 AND b.binding_generation=$2 AND b.scope=$3 AND b.activation_state='active'
		 AND b.qualification_status='qualified'`, pluginID, generation, scope).
		Scan(&out.PluginID, &out.BindingGeneration, &out.ManifestDigest, &out.Scope, &kind, &capabilities)
	if err != nil || kind != "analysis-agent" {
		return activeBinding{}, errors.New("a2a: active analysis binding unavailable")
	}
	var caps []manifestCapability
	if err := json.Unmarshal(capabilities, &caps); err != nil || len(caps) > 128 {
		return activeBinding{}, errors.New("a2a: manifest capabilities malformed")
	}
	allowed := false
	for _, cap := range caps {
		if cap.Declared && cap.CapabilityKind == "a2a-agent" && cap.CapabilityID == "analysis-a2a/v1" {
			allowed = true
		}
	}
	if !allowed {
		return activeBinding{}, errors.New("a2a: exact analysis-a2a/v1 capability not declared")
	}
	return out, nil
}

func (c *Client) markAmbiguous(ctx context.Context, taskID, requestDigest string) error {
	callCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	_, err := c.pool.Pool.Exec(callCtx, `UPDATE analysis_task_requests SET status='unknown',
		reason_code='A2A_SUBMIT_AMBIGUOUS',updated_at_unix_ms=$1
		WHERE task_id=$2 AND request_digest=$3 AND status='submitted'`, c.now().UnixMilli(), taskID, requestDigest)
	return err
}

func mapTaskStatus(state string) (string, bool, error) {
	switch state {
	case "TASK_STATE_SUBMITTED":
		return "submitted", false, nil
	case "TASK_STATE_WORKING":
		return "working", false, nil
	case "TASK_STATE_COMPLETED":
		return "succeeded", true, nil
	case "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED":
		return "failed", true, nil
	case "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED":
		return "limited", true, nil
	default:
		return "unknown", false, errors.New("unknown A2A task state")
	}
}

func aggregateArtifactOutcome(artifacts []Artifact) string {
	priority := map[string]int{"succeeded": 0, "limited": 1, "insufficient_evidence": 2, "failed": 3}
	selected := "succeeded"
	for _, artifact := range artifacts {
		if priority[artifact.AnalysisOutcome] > priority[selected] {
			selected = artifact.AnalysisOutcome
		}
	}
	return selected
}

func inputRawForDB(input InputBundle) string {
	raw, _ := json.Marshal(input)
	return string(raw)
}
