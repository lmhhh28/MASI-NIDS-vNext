package firewall

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"sort"
	"strings"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

// RevisionService creates immutable normalized baseline revisions. A revision
// is the canonical input to the activation phase sequence; its digest binds the
// exact rules + default + target-set (ADR-0014).
type RevisionService struct {
	pool *db.Pool
}

func NewRevisionService(pool *db.Pool) *RevisionService {
	return &RevisionService{pool: pool}
}

// Create validates, computes the canonical revision_digest, and durably persists
// the revision. The revision is immutable; a later edit is a new revision.
func (s *RevisionService) Create(ctx context.Context, rev Revision) (*Revision, error) {
	if err := validateRevision(rev); err != nil {
		return nil, err
	}
	rev.Rules = canonicalRuleOrder(rev.Rules)
	rev.RevisionDigest = computeRevisionDigest(rev)
	rulesJSON, err := json.Marshal(rev.Rules)
	if err != nil {
		return nil, fmt.Errorf("firewall: marshal rules: %w", err)
	}
	tag, err := s.pool.Pool.Exec(ctx, `
		INSERT INTO firewall_revisions (revision_id, revision_digest, target_id,
		    default_action, rules, scope, actor_ref, reason_code)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING`,
		rev.RevisionID, rev.RevisionDigest, rev.TargetID,
		string(rev.DefaultAction), rulesJSON, rev.Scope, rev.ActorRef, "REVISION_CREATED")
	if err != nil {
		return nil, fmt.Errorf("firewall: create revision: %w", err)
	}
	if tag.RowsAffected() == 0 {
		var existingID, existingDigest, existingTarget, existingDefault, existingScope string
		var existingRules []byte
		if err := s.pool.Pool.QueryRow(ctx, `SELECT revision_id,revision_digest,target_id,default_action,rules,scope FROM firewall_revisions
				WHERE revision_id=$1 OR (target_id=$2 AND revision_digest=$3) LIMIT 1`,
			rev.RevisionID, rev.TargetID, rev.RevisionDigest).
			Scan(&existingID, &existingDigest, &existingTarget, &existingDefault, &existingRules, &existingScope); err != nil {
			return nil, fmt.Errorf("firewall: recall revision conflict: %w", err)
		}
		var persistedRules []Rule
		if err := json.Unmarshal(existingRules, &persistedRules); err != nil {
			return nil, fmt.Errorf("firewall: parse persisted revision: %w", err)
		}
		persisted := Revision{RevisionID: existingID, RevisionDigest: existingDigest, TargetID: existingTarget,
			DefaultAction: DefaultAction(existingDefault), Rules: persistedRules, Scope: existingScope}
		if existingID != rev.RevisionID || !sameRevisionBody(persisted, rev) {
			return nil, errors.New("firewall: immutable revision identity conflict")
		}
		// An older supported canonicalization may have included target identity.
		// Preserve its stored digest; never rewrite an applied immutable revision.
		rev.RevisionDigest = existingDigest
	}
	return &rev, nil
}

func sameRevisionBody(left, right Revision) bool {
	if left.RevisionID != right.RevisionID || left.TargetID != right.TargetID || left.DefaultAction != right.DefaultAction ||
		left.Scope != right.Scope || len(left.Rules) != len(right.Rules) {
		return false
	}
	leftJSON, _ := json.Marshal(canonicalRuleOrder(left.Rules))
	rightJSON, _ := json.Marshal(canonicalRuleOrder(right.Rules))
	return string(leftJSON) == string(rightJSON)
}

// validateRevision enforces the contract bounds (max 4096 rules, default_action
// enum, priority bounds, canonical rule digests present).
func validateRevision(rev Revision) error {
	if rev.RevisionID == "" || rev.TargetID == "" || rev.Scope == "" {
		return errors.New("firewall: revision_id, target_id, scope required")
	}
	if rev.DefaultAction != DefaultPermitAndContinue && rev.DefaultAction != DefaultDrop {
		return fmt.Errorf("firewall: bad default_action %q", rev.DefaultAction)
	}
	if len(rev.Rules) > 4096 {
		return fmt.Errorf("firewall: rules exceed 4096 (got %d)", len(rev.Rules))
	}
	seenIDs := make(map[string]struct{}, len(rev.Rules))
	rulesAtPriority := make(map[int][]Rule, len(rev.Rules))
	for i, r := range rev.Rules {
		if err := validateRule(r); err != nil {
			return fmt.Errorf("firewall: rule %d: %w", i, err)
		}
		if _, exists := seenIDs[r.RuleID]; exists {
			return fmt.Errorf("firewall: duplicate rule_id %s", r.RuleID)
		}
		seenIDs[r.RuleID] = struct{}{}
		for _, existing := range rulesAtPriority[r.Priority] {
			if rulesOverlap(existing, r) {
				return fmt.Errorf("firewall: same-priority overlapping rules %s and %s", existing.RuleID, r.RuleID)
			}
		}
		rulesAtPriority[r.Priority] = append(rulesAtPriority[r.Priority], r)
	}
	return nil
}

func rulesOverlap(left, right Rule) bool {
	leftSource, _ := netip.ParsePrefix(fmt.Sprintf("%s/%d", left.SourceIPv4.Address, left.SourceIPv4.PrefixLength))
	rightSource, _ := netip.ParsePrefix(fmt.Sprintf("%s/%d", right.SourceIPv4.Address, right.SourceIPv4.PrefixLength))
	leftDestination, _ := netip.ParsePrefix(fmt.Sprintf("%s/%d", left.DestinationIPv4.Address, left.DestinationIPv4.PrefixLength))
	rightDestination, _ := netip.ParsePrefix(fmt.Sprintf("%s/%d", right.DestinationIPv4.Address, right.DestinationIPv4.PrefixLength))
	return leftSource.Overlaps(rightSource) && leftDestination.Overlaps(rightDestination) &&
		optionalUintOverlaps(left.IngressPort, right.IngressPort) && optionalUintOverlaps(left.Protocol, right.Protocol) &&
		optionalBoolOverlaps(left.L4Present, right.L4Present) && optionalUintOverlaps(left.SourcePort, right.SourcePort) &&
		optionalUintOverlaps(left.DestinationPort, right.DestinationPort) &&
		(left.FragmentClass == FragmentWildcard || right.FragmentClass == FragmentWildcard || left.FragmentClass == right.FragmentClass)
}

func optionalUintOverlaps(left, right OptionalUint32) bool {
	return !left.Present || !right.Present || left.Value == right.Value
}

func optionalBoolOverlaps(left, right OptionalBool) bool {
	return !left.Present || !right.Present || left.Value == right.Value
}

func validateRule(r Rule) error {
	if r.RuleID == "" || len(r.RuleID) > 128 {
		return errors.New("rule_id length outside 1..128")
	}
	if r.RuleRevision < 1 {
		return fmt.Errorf("rule %s revision must be >=1", r.RuleID)
	}
	if r.Priority < 1 || r.Priority > 2147483647 {
		return fmt.Errorf("rule %s priority out of bounds", r.RuleID)
	}
	if r.Action != "permit-and-continue" && r.Action != "drop" {
		return fmt.Errorf("rule %s bad action %q", r.RuleID, r.Action)
	}
	if err := validateIPv4Prefix(r.SourceIPv4); err != nil {
		return fmt.Errorf("rule %s source prefix: %w", r.RuleID, err)
	}
	if err := validateIPv4Prefix(r.DestinationIPv4); err != nil {
		return fmt.Errorf("rule %s destination prefix: %w", r.RuleID, err)
	}
	if r.IngressPort.Present && r.IngressPort.Value > 510 {
		return fmt.Errorf("rule %s ingress_port exceeds 510", r.RuleID)
	}
	if r.Protocol.Present && r.Protocol.Value > 255 {
		return fmt.Errorf("rule %s protocol exceeds 255", r.RuleID)
	}
	if r.SourcePort.Present && r.SourcePort.Value > 65535 || r.DestinationPort.Present && r.DestinationPort.Value > 65535 {
		return fmt.Errorf("rule %s L4 port exceeds 65535", r.RuleID)
	}
	switch r.FragmentClass {
	case FragmentWildcard, FragmentNone, FragmentFirst, FragmentNonInitial:
	default:
		return fmt.Errorf("rule %s unknown fragment class %q", r.RuleID, r.FragmentClass)
	}
	if r.ActorRef == "" || len(r.ActorRef) > 128 || r.ReasonCode == "" || len(r.ReasonCode) > 64 {
		return fmt.Errorf("rule %s actor/reason identity required and bounded", r.RuleID)
	}
	if !security.ValidDigest(r.CanonicalRuleDigest) {
		return fmt.Errorf("rule %s missing canonical_rule_digest", r.RuleID)
	}
	if _, err := hex.DecodeString(strings.TrimPrefix(r.CanonicalRuleDigest, "sha256:")); err != nil {
		return fmt.Errorf("rule %s malformed canonical_rule_digest", r.RuleID)
	}
	if r.CanonicalRuleDigest != ComputeRuleDigest(r) {
		return fmt.Errorf("rule %s canonical digest mismatch", r.RuleID)
	}
	return nil
}

func validateIPv4Prefix(prefix IPv4Prefix) error {
	p, err := netip.ParsePrefix(fmt.Sprintf("%s/%d", prefix.Address, prefix.PrefixLength))
	if err != nil || !p.Addr().Is4() || p.Bits() < 0 || p.Bits() > 32 {
		return errors.New("malformed IPv4 prefix")
	}
	if p != p.Masked() {
		return errors.New("IPv4 prefix address is not network-normalized")
	}
	return nil
}

func ComputeRuleDigest(r Rule) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%d|%d|%t:%d|%s/%d|%s/%d|%t:%d|%t:%t|%t:%d|%t:%d|%s|%s|%t",
		r.RuleID, r.RuleRevision, r.Priority, r.IngressPort.Present, r.IngressPort.Value,
		r.SourceIPv4.Address, r.SourceIPv4.PrefixLength, r.DestinationIPv4.Address,
		r.DestinationIPv4.PrefixLength, r.Protocol.Present, r.Protocol.Value,
		r.L4Present.Present, r.L4Present.Value, r.SourcePort.Present, r.SourcePort.Value,
		r.DestinationPort.Present, r.DestinationPort.Value, r.FragmentClass, r.Action, r.Enabled)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

// computeRevisionDigest is the canonical, immutable digest over the normalized
// rules (sorted by priority for determinism) + default + scope. The normalized
// policy digest is fleet-portable; target identity is bound separately by the
// per-target revision row, assignment fence, and child effect digest.
func computeRevisionDigest(rev Revision) string {
	sorted := canonicalRuleOrder(rev.Rules)
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s", string(rev.DefaultAction), rev.Scope)
	for _, r := range sorted {
		fmt.Fprintf(h, "|%s|%d|%d|%s|%t|%s", r.RuleID, r.RuleRevision, r.Priority, r.Action, r.Enabled, r.CanonicalRuleDigest)
	}
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func canonicalRuleOrder(rules []Rule) []Rule {
	sorted := append([]Rule(nil), rules...)
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].Priority != sorted[j].Priority {
			return sorted[i].Priority < sorted[j].Priority
		}
		return sorted[i].RuleID < sorted[j].RuleID
	})
	return sorted
}

// StageIndex returns the index of a stage in the canonical order, or -1.
func StageIndex(s ActivationStage) int {
	for i, st := range ActivationStagesInOrder {
		if st == s {
			return i
		}
	}
	return -1
}

// ReachedStage reports whether stage `reached` has been completed (i.e. a later
// or equal stage is in completedStages). Used to enforce phase ordering.
func ReachedStage(completed []ActivationStage, reached ActivationStage) bool {
	target := StageIndex(reached)
	if target < 0 {
		return false
	}
	for _, c := range completed {
		if StageIndex(c) >= target {
			return true
		}
	}
	return false
}
