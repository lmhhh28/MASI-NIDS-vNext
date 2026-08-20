//! Deterministic first-release BMv2 IPv4 stateless firewall compiler/readback.

use std::collections::{BTreeMap, BTreeSet};

use prost::Message;

use crate::{
    EdgeError, EdgeResult,
    contract::{
        edge::{
            BaselineRule, CanonicalP4Entry, CompiledEffectPlan, EffectIntent, EffectKind,
            FirewallAction, OverlayRule,
        },
        p4::{
            Action, CounterData, CounterEntry, DirectCounterEntry, Entity, FieldMatch, Index,
            TableAction, TableEntry, Update, action, entity, field_match, table_action, update,
        },
    },
    digest,
};

/// Qualified P4 table IDs.
pub mod table_id {
    /// High-priority exact response overlay.
    pub const RESPONSE_OVERLAY: u32 = 33_554_689;
    /// Active baseline bank selector.
    pub const POLICY_SELECTOR: u32 = 33_554_690;
    /// Baseline bank 0.
    pub const BASELINE_0: u32 = 33_554_691;
    /// Baseline bank 1.
    pub const BASELINE_1: u32 = 33_554_692;
    /// Active telemetry bank selector.
    pub const TELEMETRY_SELECTOR: u32 = 33_554_694;
}

/// Qualified P4 action IDs.
pub mod action_id {
    /// Overlay permit and continue.
    pub const OVERLAY_PERMIT: u32 = 16_777_473;
    /// Overlay drop.
    pub const OVERLAY_DROP: u32 = 16_777_474;
    /// Baseline permit and continue.
    pub const BASELINE_PERMIT: u32 = 16_777_475;
    /// Baseline drop.
    pub const BASELINE_DROP: u32 = 16_777_476;
    /// Select baseline bank 0.
    pub const SELECT_POLICY_0: u32 = 16_777_477;
    /// Select baseline bank 1.
    pub const SELECT_POLICY_1: u32 = 16_777_478;
    /// Select telemetry bank 0.
    pub const SELECT_TELEMETRY_0: u32 = 16_777_479;
    /// Select telemetry bank 1.
    pub const SELECT_TELEMETRY_1: u32 = 16_777_480;
}

/// Qualified direct-counter IDs.
pub mod direct_counter_id {
    /// Overlay direct counter.
    pub const RESPONSE_OVERLAY: u32 = 318_767_361;
    /// Bank 0 direct counter.
    pub const BASELINE_0: u32 = 318_767_362;
    /// Bank 1 direct counter.
    pub const BASELINE_1: u32 = 318_767_363;
}

/// Qualified aggregate counter IDs.
pub mod counter_id {
    /// Overlay eligible traffic.
    pub const RESPONSE_ELIGIBLE: u32 = 301_990_145;
    /// Per-bank baseline eligible traffic.
    pub const BASELINE_ELIGIBLE: u32 = 301_990_146;
    /// Dual-bank telemetry cells.
    pub const TELEMETRY_CELL: u32 = 301_990_147;
    /// Per-bank telemetry aggregate.
    pub const TELEMETRY_BANK: u32 = 301_990_148;
    /// Per-bank telemetry classes.
    pub const TELEMETRY_CLASS: u32 = 301_990_149;
}

/// Compiled protobuf plan plus ready-to-send wire entities.
#[derive(Clone, Debug)]
pub struct CompiledPlan {
    /// Persisted/public plan representation.
    pub contract: CompiledEffectPlan,
    /// Canonical entries in contract order.
    pub entries: Vec<Entity>,
    /// Baseline default action, when applicable.
    pub default_entry: Option<Entity>,
    /// Selector flip, when applicable.
    pub selector_entry: Option<Entity>,
}

impl CompiledPlan {
    /// Restore exact wire entities from an effect journal record.
    pub fn from_contract(contract: CompiledEffectPlan) -> EdgeResult<Self> {
        let mut entries = Vec::with_capacity(contract.entries.len());
        for item in &contract.entries {
            let entity = Entity::decode(item.entity.as_slice()).map_err(|error| {
                EdgeError::WalCorrupt(format!("decode journal P4 entity: {error}"))
            })?;
            if canonical_entity_digest(&entity)? != item.canonical_entry_digest {
                return Err(EdgeError::WalCorrupt(
                    "journal P4 entity digest mismatch".into(),
                ));
            }
            entries.push(entity);
        }
        let default_entry = decode_optional_entity(&contract.default_entity)?;
        let selector_entry = decode_optional_entity(&contract.selector_entity)?;
        verify_plan_digest(&contract)?;
        Ok(Self {
            contract,
            entries,
            default_entry,
            selector_entry,
        })
    }

    /// Updates that install all compiled entries.
    #[must_use]
    pub fn insert_updates(&self) -> Vec<Update> {
        self.entries
            .iter()
            .cloned()
            .map(|entity| Update {
                r#type: update::Type::Insert as i32,
                entity: Some(entity),
            })
            .collect()
    }

    /// Exact key queries corresponding to compiled entries.
    pub fn key_queries(&self) -> EdgeResult<Vec<Entity>> {
        self.entries.iter().map(table_key_entity).collect()
    }
}

/// Verify the intent's stable contract fields and canonical effect digest.
pub fn validate_intent(intent: &EffectIntent) -> EdgeResult<()> {
    if intent.schema_version != "effect-intent-edge/v1" {
        return Err(EdgeError::UnknownMajor(intent.schema_version.clone()));
    }
    if intent.required_write_atomicity
        != crate::contract::edge::P4WriteAtomicity::ContinueOnError as i32
    {
        return Err(EdgeError::precondition(
            "ATOMICITY_UNSUPPORTED",
            "first-release target profile qualifies CONTINUE_ON_ERROR only",
        ));
    }
    for (field, value) in [
        ("effect_intent_id", intent.effect_intent_id.as_str()),
        ("operation_id", intent.operation_id.as_str()),
        ("target_id", intent.target_id.as_str()),
        ("actor_ref", intent.actor_ref.as_str()),
        ("reason_code", intent.reason_code.as_str()),
        ("trace_id", intent.trace_id.as_str()),
    ] {
        digest::validate_identity(value, field)?;
    }
    digest::validate_sha256(&intent.effect_digest, "effect_digest")?;
    digest::validate_sha256(&intent.authorization_digest, "authorization_digest")?;
    digest::validate_sha256(&intent.policy_revision_digest, "policy_revision_digest")?;
    let mut canonical = intent.clone();
    canonical.effect_digest.clear();
    let observed = digest::message_sha256(&canonical);
    if observed != intent.effect_digest {
        return Err(EdgeError::precondition(
            "EFFECT_DIGEST_CONFLICT",
            "effect digest does not match deterministic protobuf payload",
        ));
    }
    Ok(())
}

/// Compile after read-only preflight discovers the current active bank.
pub fn compile(
    intent: &EffectIntent,
    active_bank: u32,
    baseline_limit: usize,
    overlay_limit: usize,
) -> EdgeResult<CompiledPlan> {
    validate_intent(intent)?;
    if active_bank > 1 {
        return Err(EdgeError::precondition(
            "SELECTOR_MISMATCH",
            "active bank is outside [0,1]",
        ));
    }
    let kind = EffectKind::try_from(intent.kind)
        .map_err(|_| EdgeError::invalid("effect_kind", "unknown effect kind"))?;
    let inactive_bank = 1 - active_bank;
    let (entries, default_entry, selector_entry, direct_counter) = match kind {
        EffectKind::BaselineActivate => {
            if intent.baseline_rules.len() > baseline_limit || !intent.overlay_rules.is_empty() {
                return Err(EdgeError::exhausted(
                    "CAPACITY_EXCEEDED",
                    "baseline/overlay logical rule counts exceed the effect profile",
                ));
            }
            let entries = compile_baseline_rules(&intent.baseline_rules, inactive_bank)?;
            let default_entry = baseline_default(inactive_bank, intent.default_action)?;
            let selector_entry = policy_selector(inactive_bank)?;
            let direct_counter = if inactive_bank == 0 {
                direct_counter_id::BASELINE_0
            } else {
                direct_counter_id::BASELINE_1
            };
            (
                entries,
                Some(default_entry),
                Some(selector_entry),
                direct_counter,
            )
        }
        EffectKind::OverlayUpsert | EffectKind::OverlayDelete => {
            if intent.overlay_rules.is_empty()
                || intent.overlay_rules.len() > overlay_limit
                || !intent.baseline_rules.is_empty()
            {
                return Err(EdgeError::exhausted(
                    "CAPACITY_EXCEEDED",
                    "overlay logical rule count is empty or exceeds profile",
                ));
            }
            (
                compile_overlay_rules(&intent.overlay_rules)?,
                None,
                None,
                direct_counter_id::RESPONSE_OVERLAY,
            )
        }
        EffectKind::Unspecified => {
            return Err(EdgeError::invalid(
                "effect_kind",
                "unspecified effect kind is forbidden",
            ));
        }
        EffectKind::BoundedCaptureStart => {
            return Err(EdgeError::precondition(
                "BOUNDED_CAPTURE_PROFILE_UNSUPPORTED",
                "bounded capture is not qualified in this Edge target profile",
            ));
        }
    };
    let bank = if kind == EffectKind::BaselineActivate {
        inactive_bank
    } else {
        active_bank
    };
    let mut contract_entries = Vec::with_capacity(entries.len());
    for (index, entity) in entries.iter().enumerate() {
        let logical_rule_id = match kind {
            EffectKind::BaselineActivate => intent.baseline_rules[index].rule_id.clone(),
            EffectKind::OverlayUpsert | EffectKind::OverlayDelete => {
                intent.overlay_rules[index].rule_id.clone()
            }
            EffectKind::Unspecified => String::new(),
            EffectKind::BoundedCaptureStart => String::new(),
        };
        let table_id = table_entry(entity)?.table_id;
        let canonical_entry_digest = canonical_entity_digest(entity)?;
        contract_entries.push(CanonicalP4Entry {
            logical_rule_id,
            table_id,
            direct_counter_id: direct_counter,
            bank,
            entity: entity.encode_to_vec(),
            entity_id: canonical_entry_digest.clone(),
            match_priority_action_digest: canonical_entry_digest.clone(),
            canonical_entry_digest,
        });
    }
    let mut contract = CompiledEffectPlan {
        schema_version: "edge-compiled-effect/v1".into(),
        target_id: intent.target_id.clone(),
        effect_intent_id: intent.effect_intent_id.clone(),
        kind: intent.kind,
        active_bank,
        inactive_bank,
        entries: contract_entries,
        default_entity: default_entry
            .as_ref()
            .map_or_else(Vec::new, Message::encode_to_vec),
        selector_entity: selector_entry
            .as_ref()
            .map_or_else(Vec::new, Message::encode_to_vec),
        plan_digest: String::new(),
    };
    contract.plan_digest = plan_digest(&contract);
    Ok(CompiledPlan {
        contract,
        entries,
        default_entry,
        selector_entry,
    })
}

/// Decode selector readback into the active bank.
pub fn active_policy_bank(entities: &[Entity]) -> EdgeResult<u32> {
    if entities.len() != 1 {
        return Err(EdgeError::precondition(
            "SELECTOR_MISMATCH",
            "policy selector readback must contain exactly one entry",
        ));
    }
    let entry = table_entry(&entities[0])?;
    if entry.table_id != table_id::POLICY_SELECTOR {
        return Err(EdgeError::precondition(
            "SELECTOR_MISMATCH",
            "policy selector table ID mismatch",
        ));
    }
    match action_identifier(entry)? {
        action_id::SELECT_POLICY_0 => Ok(0),
        action_id::SELECT_POLICY_1 => Ok(1),
        _ => Err(EdgeError::precondition(
            "SELECTOR_MISMATCH",
            "policy selector action is not a qualified bank selector",
        )),
    }
}

/// Query for the single policy selector key.
#[must_use]
pub fn policy_selector_query() -> Entity {
    entity_from_table(TableEntry {
        table_id: table_id::POLICY_SELECTOR,
        r#match: vec![exact_match(1, &[0])],
        ..TableEntry::default()
    })
}

/// Query for the single telemetry selector key.
#[must_use]
pub fn telemetry_selector_query() -> Entity {
    entity_from_table(TableEntry {
        table_id: table_id::TELEMETRY_SELECTOR,
        r#match: vec![exact_match(1, &[0])],
        ..TableEntry::default()
    })
}

/// Exact telemetry selector entry for a new bank/epoch.
pub fn telemetry_selector(bank: u32, epoch: u32) -> EdgeResult<Entity> {
    let action_id = match bank {
        0 => action_id::SELECT_TELEMETRY_0,
        1 => action_id::SELECT_TELEMETRY_1,
        _ => return Err(EdgeError::invalid("telemetry_bank", "must be 0 or 1")),
    };
    Ok(entity_from_table(TableEntry {
        table_id: table_id::TELEMETRY_SELECTOR,
        r#match: vec![exact_match(1, &[0])],
        action: Some(TableAction {
            r#type: Some(table_action::Type::Action(Action {
                action_id,
                params: vec![action::Param {
                    param_id: 1,
                    value: epoch.to_be_bytes().to_vec(),
                }],
            })),
        }),
        ..TableEntry::default()
    }))
}

/// Decode the active telemetry bank and epoch.
pub fn active_telemetry_bank(entities: &[Entity]) -> EdgeResult<(u32, u32)> {
    if entities.len() != 1 {
        return Err(EdgeError::precondition(
            "TELEMETRY_SELECTOR_MISMATCH",
            "telemetry selector readback must contain one entry",
        ));
    }
    let entry = table_entry(&entities[0])?;
    let action = match entry
        .action
        .as_ref()
        .and_then(|action| action.r#type.as_ref())
    {
        Some(table_action::Type::Action(action)) => action,
        _ => {
            return Err(EdgeError::precondition(
                "TELEMETRY_SELECTOR_MISMATCH",
                "telemetry selector action missing",
            ));
        }
    };
    let bank = match action.action_id {
        action_id::SELECT_TELEMETRY_0 => 0,
        action_id::SELECT_TELEMETRY_1 => 1,
        _ => {
            return Err(EdgeError::precondition(
                "TELEMETRY_SELECTOR_MISMATCH",
                "unknown telemetry selector action",
            ));
        }
    };
    let epoch = action
        .params
        .iter()
        .find(|param| param.param_id == 1)
        .map(|param| bytes_to_u64(&param.value))
        .transpose()?
        .unwrap_or_default();
    let epoch = u32::try_from(epoch)
        .map_err(|_| EdgeError::precondition("TELEMETRY_SELECTOR_MISMATCH", "epoch overflow"))?;
    Ok((bank, epoch))
}

/// Query one indexed counter.
#[must_use]
pub fn counter_query(counter_id: u32, index: i64) -> Entity {
    Entity {
        entity: Some(entity::Entity::CounterEntry(CounterEntry {
            counter_id,
            index: Some(Index { index }),
            data: None,
        })),
    }
}

/// Reset one indexed counter to exact zero.
#[must_use]
pub fn counter_zero(counter_id: u32, index: i64) -> Entity {
    Entity {
        entity: Some(entity::Entity::CounterEntry(CounterEntry {
            counter_id,
            index: Some(Index { index }),
            data: Some(CounterData {
                byte_count: 0,
                packet_count: 0,
            }),
        })),
    }
}

/// Extract one counter value and reject negative target values.
pub fn counter_value(entity: &Entity) -> EdgeResult<(u64, u64)> {
    let data = match entity.entity.as_ref() {
        Some(entity::Entity::CounterEntry(entry)) => entry.data.as_ref(),
        Some(entity::Entity::DirectCounterEntry(entry)) => entry.data.as_ref(),
        _ => {
            return Err(EdgeError::invalid(
                "counter_entity",
                "expected counter entry",
            ));
        }
    }
    .ok_or_else(|| EdgeError::precondition("COUNTER_READBACK_MISSING", "counter data absent"))?;
    if data.packet_count < 0 || data.byte_count < 0 {
        return Err(EdgeError::precondition(
            "COUNTER_READBACK_INVALID",
            "counter value is negative",
        ));
    }
    Ok((data.packet_count as u64, data.byte_count as u64))
}

/// Direct-counter query bound to an exact table key.
pub fn direct_counter_query(entry: &Entity) -> EdgeResult<Entity> {
    Ok(Entity {
        entity: Some(entity::Entity::DirectCounterEntry(DirectCounterEntry {
            table_entry: Some(match table_key_entity(entry)?.entity {
                Some(entity::Entity::TableEntry(table)) => table,
                _ => {
                    return Err(EdgeError::invalid(
                        "direct_counter_entry",
                        "table key missing",
                    ));
                }
            }),
            data: None,
        })),
    })
}

/// Query all non-default entries in one table.
#[must_use]
pub fn table_query(table_id: u32) -> Entity {
    entity_from_table(TableEntry {
        table_id,
        ..TableEntry::default()
    })
}

/// Delete updates for all concrete entries returned by a table read.
pub fn delete_updates(entities: &[Entity]) -> EdgeResult<Vec<Update>> {
    entities
        .iter()
        .map(|entity| {
            Ok(Update {
                r#type: update::Type::Delete as i32,
                entity: Some(table_key_entity(entity)?),
            })
        })
        .collect()
}

/// Convert an entry to a deterministic MODIFY update.
#[must_use]
pub fn modify_update(entity: Entity) -> Update {
    Update {
        r#type: update::Type::Modify as i32,
        entity: Some(entity),
    }
}

/// Convert an entry to a deterministic INSERT update.
#[must_use]
pub fn insert_update(entity: Entity) -> Update {
    Update {
        r#type: update::Type::Insert as i32,
        entity: Some(entity),
    }
}

/// Convert a key to a deterministic DELETE update.
pub fn delete_update(entity: &Entity) -> EdgeResult<Update> {
    Ok(Update {
        r#type: update::Type::Delete as i32,
        entity: Some(table_key_entity(entity)?),
    })
}

/// Compare exact canonical expected and observed table entries.
pub fn exact_readback(expected: &[Entity], observed: &[Entity]) -> EdgeResult<String> {
    let expected_set = digest_set(expected)?;
    let observed_set = digest_set(observed)?;
    if expected_set != observed_set {
        return Err(EdgeError::precondition(
            "READBACK_MISMATCH",
            format!(
                "expected {} canonical entries, observed {}",
                expected_set.len(),
                observed_set.len()
            ),
        ));
    }
    let material = expected_set.into_iter().collect::<Vec<_>>().join("\n");
    Ok(digest::sha256(material.as_bytes()))
}

/// Exact digest of a single canonical table entry.
pub fn canonical_entity_digest(entity: &Entity) -> EdgeResult<String> {
    let canonical = canonical_table_entry(table_entry(entity)?.clone())?;
    Ok(digest::message_sha256(&canonical))
}

fn compile_baseline_rules(rules: &[BaselineRule], bank: u32) -> EdgeResult<Vec<Entity>> {
    let mut ordered = rules.to_vec();
    ordered.sort_by(|left, right| {
        right
            .priority
            .cmp(&left.priority)
            .then_with(|| left.rule_id.cmp(&right.rule_id))
            .then_with(|| left.rule_revision.cmp(&right.rule_revision))
    });
    let mut conflict_keys: BTreeMap<(i32, Vec<u8>), u32> = BTreeMap::new();
    let mut identities = BTreeSet::new();
    let mut result = Vec::with_capacity(ordered.len());
    for rule in &ordered {
        validate_rule_identity(
            &rule.rule_id,
            rule.rule_revision,
            &rule.canonical_rule_digest,
        )?;
        if !identities.insert((rule.rule_id.clone(), rule.rule_revision)) {
            return Err(EdgeError::precondition(
                "EQUAL_PRIORITY_CONFLICT",
                "duplicate logical rule identity",
            ));
        }
        if rule.priority <= 0 {
            return Err(EdgeError::invalid("priority", "must be positive"));
        }
        let source = rule
            .source
            .as_ref()
            .ok_or_else(|| EdgeError::invalid("source", "IPv4 prefix is required"))?;
        let destination = rule
            .destination
            .as_ref()
            .ok_or_else(|| EdgeError::invalid("destination", "IPv4 prefix is required"))?;
        let mut matches = Vec::with_capacity(8);
        push_optional_ternary(&mut matches, 1, rule.ingress_port.as_ref(), 9)?;
        push_prefix(&mut matches, 2, source.address, source.prefix_length)?;
        push_prefix(
            &mut matches,
            3,
            destination.address,
            destination.prefix_length,
        )?;
        push_optional_ternary(&mut matches, 4, rule.protocol.as_ref(), 8)?;
        push_optional_ternary(&mut matches, 5, rule.l4_present.as_ref(), 1)?;
        let l4_present = rule
            .l4_present
            .as_ref()
            .is_some_and(|value| value.present && value.value == 1);
        if (rule.source_port.as_ref().is_some_and(|value| value.present)
            || rule
                .destination_port
                .as_ref()
                .is_some_and(|value| value.present))
            && !l4_present
        {
            return Err(EdgeError::precondition(
                "UNSUPPORTED_L4_UNAVAILABLE",
                "port match requires l4_present=true",
            ));
        }
        push_optional_ternary(&mut matches, 6, rule.source_port.as_ref(), 16)?;
        push_optional_ternary(&mut matches, 7, rule.destination_port.as_ref(), 16)?;
        if rule
            .fragment_class
            .as_ref()
            .is_some_and(|value| value.present && value.value > 2)
        {
            return Err(EdgeError::precondition(
                "UNSUPPORTED_FRAGMENT",
                "invalid fragment class is not effect eligible",
            ));
        }
        push_optional_ternary(&mut matches, 8, rule.fragment_class.as_ref(), 2)?;
        matches.sort_by_key(|field| field.field_id);
        let action = baseline_action(rule.action)?;
        let table = if bank == 0 {
            table_id::BASELINE_0
        } else {
            table_id::BASELINE_1
        };
        let entry = TableEntry {
            table_id: table,
            r#match: matches,
            action: Some(table_action(action)),
            priority: rule.priority,
            ..TableEntry::default()
        };
        let mut match_only = entry.clone();
        match_only.action = None;
        let key = (rule.priority, match_only.encode_to_vec());
        if let Some(previous_action) = conflict_keys.insert(key, action)
            && previous_action != action
        {
            return Err(EdgeError::precondition(
                "EQUAL_PRIORITY_CONFLICT",
                "equal-priority exact match has conflicting actions",
            ));
        }
        result.push(entity_from_table(entry));
    }
    Ok(result)
}

fn compile_overlay_rules(rules: &[OverlayRule]) -> EdgeResult<Vec<Entity>> {
    let mut ordered = rules.to_vec();
    ordered.sort_by(|left, right| {
        left.rule_id
            .cmp(&right.rule_id)
            .then_with(|| left.rule_revision.cmp(&right.rule_revision))
    });
    let mut keys = BTreeSet::new();
    let mut result = Vec::with_capacity(ordered.len());
    for rule in &ordered {
        validate_rule_identity(
            &rule.rule_id,
            rule.rule_revision,
            &rule.canonical_rule_digest,
        )?;
        if !matches!(rule.protocol, 6 | 17)
            || rule.source_port > u16::MAX as u32
            || rule.destination_port > u16::MAX as u32
            || rule.expires_at_unix_ms <= 0
        {
            return Err(EdgeError::invalid(
                "overlay_rule",
                "requires TCP/UDP, exact u16 ports, and a positive expiry",
            ));
        }
        let matches = vec![
            exact_match(1, &[1]),
            exact_match(2, &[1]),
            exact_match(3, &rule.source_ipv4.to_be_bytes()),
            exact_match(4, &rule.destination_ipv4.to_be_bytes()),
            exact_match(5, &[rule.protocol as u8]),
            exact_match(6, &(rule.source_port as u16).to_be_bytes()),
            exact_match(7, &(rule.destination_port as u16).to_be_bytes()),
        ];
        let key = matches
            .iter()
            .flat_map(Message::encode_to_vec)
            .collect::<Vec<_>>();
        if !keys.insert(key) {
            return Err(EdgeError::precondition(
                "EQUAL_PRIORITY_CONFLICT",
                "duplicate exact overlay match",
            ));
        }
        result.push(entity_from_table(TableEntry {
            table_id: table_id::RESPONSE_OVERLAY,
            r#match: matches,
            action: Some(table_action(overlay_action(rule.action)?)),
            ..TableEntry::default()
        }));
    }
    Ok(result)
}

fn validate_rule_identity(rule_id: &str, revision: u64, rule_digest: &str) -> EdgeResult<()> {
    digest::validate_identity(rule_id, "rule_id")?;
    digest::validate_sha256(rule_digest, "canonical_rule_digest")?;
    if revision == 0 {
        return Err(EdgeError::invalid("rule_revision", "must be positive"));
    }
    Ok(())
}

fn baseline_default(bank: u32, action: i32) -> EdgeResult<Entity> {
    Ok(entity_from_table(TableEntry {
        table_id: if bank == 0 {
            table_id::BASELINE_0
        } else {
            table_id::BASELINE_1
        },
        action: Some(table_action(baseline_action(action)?)),
        is_default_action: true,
        ..TableEntry::default()
    }))
}

fn policy_selector(bank: u32) -> EdgeResult<Entity> {
    let action = match bank {
        0 => action_id::SELECT_POLICY_0,
        1 => action_id::SELECT_POLICY_1,
        _ => {
            return Err(EdgeError::invalid("bank", "must be 0 or 1"));
        }
    };
    Ok(entity_from_table(TableEntry {
        table_id: table_id::POLICY_SELECTOR,
        r#match: vec![exact_match(1, &[0])],
        action: Some(table_action(action)),
        ..TableEntry::default()
    }))
}

fn baseline_action(action: i32) -> EdgeResult<u32> {
    match FirewallAction::try_from(action) {
        Ok(FirewallAction::PermitAndContinue) => Ok(action_id::BASELINE_PERMIT),
        Ok(FirewallAction::Drop) => Ok(action_id::BASELINE_DROP),
        _ => Err(EdgeError::invalid("firewall_action", "unsupported action")),
    }
}

fn overlay_action(action: i32) -> EdgeResult<u32> {
    match FirewallAction::try_from(action) {
        Ok(FirewallAction::PermitAndContinue) => Ok(action_id::OVERLAY_PERMIT),
        Ok(FirewallAction::Drop) => Ok(action_id::OVERLAY_DROP),
        _ => Err(EdgeError::invalid("firewall_action", "unsupported action")),
    }
}

fn push_optional_ternary(
    fields: &mut Vec<FieldMatch>,
    field_id: u32,
    value: Option<&crate::contract::edge::OptionalUint32>,
    width: u32,
) -> EdgeResult<()> {
    let Some(value) = value else { return Ok(()) };
    if !value.present {
        return Ok(());
    }
    if width < 32 && value.value >= (1_u32 << width) {
        return Err(EdgeError::invalid(
            "match_value",
            "value exceeds field width",
        ));
    }
    let bytes = encode_width(u64::from(value.value), width)?;
    let mask = vec![0xff; bytes.len()];
    fields.push(ternary_match(field_id, &bytes, &mask));
    Ok(())
}

fn push_prefix(
    fields: &mut Vec<FieldMatch>,
    field_id: u32,
    address: u32,
    prefix_length: u32,
) -> EdgeResult<()> {
    if prefix_length > 32 {
        return Err(EdgeError::invalid("prefix_length", "must be in [0,32]"));
    }
    if prefix_length == 0 {
        return Ok(());
    }
    let mask = u32::MAX.checked_shl(32 - prefix_length).unwrap_or(0);
    let canonical = address & mask;
    if canonical != address {
        return Err(EdgeError::invalid(
            "ipv4_prefix.address",
            "host bits must be zero in canonical prefix",
        ));
    }
    fields.push(ternary_match(
        field_id,
        &canonical.to_be_bytes(),
        &mask.to_be_bytes(),
    ));
    Ok(())
}

fn encode_width(value: u64, width: u32) -> EdgeResult<Vec<u8>> {
    if width == 0 || width > 64 || (width < 64 && value >= (1_u64 << width)) {
        return Err(EdgeError::invalid("bit_width", "value does not fit field"));
    }
    let bytes = width.div_ceil(8) as usize;
    Ok(value.to_be_bytes()[8 - bytes..].to_vec())
}

fn bytes_to_u64(value: &[u8]) -> EdgeResult<u64> {
    if value.len() > 8 {
        return Err(EdgeError::precondition(
            "READBACK_MISMATCH",
            "integer bytes exceed 64 bits",
        ));
    }
    let mut bytes = [0_u8; 8];
    bytes[8 - value.len()..].copy_from_slice(value);
    Ok(u64::from_be_bytes(bytes))
}

fn exact_match(field_id: u32, value: &[u8]) -> FieldMatch {
    FieldMatch {
        field_id,
        field_match_type: Some(field_match::FieldMatchType::Exact(field_match::Exact {
            value: value.to_vec(),
        })),
    }
}

fn ternary_match(field_id: u32, value: &[u8], mask: &[u8]) -> FieldMatch {
    FieldMatch {
        field_id,
        field_match_type: Some(field_match::FieldMatchType::Ternary(field_match::Ternary {
            value: value.to_vec(),
            mask: mask.to_vec(),
        })),
    }
}

fn table_action(action_id: u32) -> TableAction {
    TableAction {
        r#type: Some(table_action::Type::Action(Action {
            action_id,
            params: Vec::new(),
        })),
    }
}

fn entity_from_table(entry: TableEntry) -> Entity {
    Entity {
        entity: Some(entity::Entity::TableEntry(entry)),
    }
}

fn table_entry(entity: &Entity) -> EdgeResult<&TableEntry> {
    match entity.entity.as_ref() {
        Some(entity::Entity::TableEntry(entry)) => Ok(entry),
        _ => Err(EdgeError::invalid("entity", "expected a table entry")),
    }
}

fn action_identifier(entry: &TableEntry) -> EdgeResult<u32> {
    match entry
        .action
        .as_ref()
        .and_then(|action| action.r#type.as_ref())
    {
        Some(table_action::Type::Action(action)) => Ok(action.action_id),
        _ => Err(EdgeError::precondition(
            "READBACK_MISMATCH",
            "table action is absent or indirect",
        )),
    }
}

fn table_key_entity(entity: &Entity) -> EdgeResult<Entity> {
    let entry = table_entry(entity)?;
    Ok(entity_from_table(TableEntry {
        table_id: entry.table_id,
        r#match: entry.r#match.clone(),
        priority: entry.priority,
        is_default_action: entry.is_default_action,
        ..TableEntry::default()
    }))
}

fn canonical_table_entry(mut entry: TableEntry) -> EdgeResult<TableEntry> {
    entry.r#match.sort_by_key(|field| field.field_id);
    let mut seen = BTreeSet::new();
    if entry
        .r#match
        .iter()
        .any(|field| !seen.insert(field.field_id))
    {
        return Err(EdgeError::precondition(
            "READBACK_MISMATCH",
            "duplicate match field ID",
        ));
    }
    if let Some(TableAction {
        r#type: Some(table_action::Type::Action(action)),
    }) = entry.action.as_mut()
    {
        action.params.sort_by_key(|param| param.param_id);
    }
    entry.controller_metadata = 0;
    entry.counter_data = None;
    entry.idle_timeout_ns = 0;
    entry.metadata.clear();
    entry.is_const = false;
    Ok(entry)
}

fn digest_set(entities: &[Entity]) -> EdgeResult<BTreeSet<String>> {
    entities.iter().map(canonical_entity_digest).collect()
}

fn plan_digest(contract: &CompiledEffectPlan) -> String {
    let mut canonical = contract.clone();
    canonical.plan_digest.clear();
    digest::message_sha256(&canonical)
}

fn verify_plan_digest(contract: &CompiledEffectPlan) -> EdgeResult<()> {
    digest::validate_sha256(&contract.plan_digest, "plan_digest")?;
    if plan_digest(contract) != contract.plan_digest {
        return Err(EdgeError::WalCorrupt(
            "compiled plan digest mismatch".into(),
        ));
    }
    Ok(())
}

fn decode_optional_entity(payload: &[u8]) -> EdgeResult<Option<Entity>> {
    if payload.is_empty() {
        return Ok(None);
    }
    Entity::decode(payload)
        .map(Some)
        .map_err(|error| EdgeError::WalCorrupt(format!("decode journal entity: {error}")))
}

#[cfg(test)]
mod tests {
    use crate::contract::edge::{Fence, Ipv4Prefix, OptionalUint32};

    use super::*;

    fn optional(value: Option<u32>) -> OptionalUint32 {
        OptionalUint32 {
            present: value.is_some(),
            value: value.unwrap_or_default(),
        }
    }

    fn intent() -> EffectIntent {
        let mut intent = EffectIntent {
            schema_version: "effect-intent-edge/v1".into(),
            effect_intent_id: "effect-1".into(),
            operation_id: "operation-1".into(),
            target_id: "target-1".into(),
            fence: Some(Fence {
                target_control_incarnation_id: "incarnation-1".into(),
                target_assignment_generation: 1,
                actor_runtime_epoch: "actor-1".into(),
                application_generation: 1,
                election_id_low: 10,
                ..Fence::default()
            }),
            authorization_digest: format!("sha256:{}", "b".repeat(64)),
            kind: EffectKind::BaselineActivate as i32,
            policy_revision_digest: format!("sha256:{}", "c".repeat(64)),
            default_action: FirewallAction::PermitAndContinue as i32,
            baseline_rules: vec![BaselineRule {
                rule_id: "rule-1".into(),
                rule_revision: 1,
                canonical_rule_digest: format!("sha256:{}", "d".repeat(64)),
                priority: 100,
                ingress_port: Some(optional(Some(1))),
                source: Some(Ipv4Prefix {
                    address: u32::from_be_bytes([192, 0, 2, 0]),
                    prefix_length: 24,
                }),
                destination: Some(Ipv4Prefix {
                    address: u32::from_be_bytes([198, 51, 100, 10]),
                    prefix_length: 32,
                }),
                protocol: Some(optional(Some(6))),
                l4_present: Some(optional(Some(1))),
                source_port: Some(optional(None)),
                destination_port: Some(optional(Some(22))),
                fragment_class: Some(optional(Some(0))),
                action: FirewallAction::Drop as i32,
            }],
            deadline_unix_ms: 2_000_000_000_000,
            actor_ref: "operator-1".into(),
            reason_code: "BASELINE_ACTIVATION".into(),
            trace_id: "trace-1".into(),
            required_write_atomicity: crate::contract::edge::P4WriteAtomicity::ContinueOnError
                as i32,
            ..EffectIntent::default()
        };
        let mut canonical = intent.clone();
        canonical.effect_digest.clear();
        intent.effect_digest = digest::message_sha256(&canonical);
        intent
    }

    #[test]
    fn compiler_is_deterministic_and_bank_scoped() -> Result<(), Box<dyn std::error::Error>> {
        let first = compile(&intent(), 0, 4096, 1024)?;
        let second = compile(&intent(), 0, 4096, 1024)?;
        assert_eq!(first.contract.plan_digest, second.contract.plan_digest);
        assert_eq!(1, first.contract.inactive_bank);
        assert_eq!(
            table_id::BASELINE_1,
            table_entry(&first.entries[0])?.table_id
        );
        assert_eq!(
            canonical_entity_digest(&first.entries[0])?,
            first.contract.entries[0].canonical_entry_digest
        );
        Ok(())
    }

    #[test]
    fn non_initial_fragment_with_port_is_rejected() {
        let mut value = intent();
        value.baseline_rules[0].fragment_class = Some(optional(Some(2)));
        // The profile never permits claiming L4 on a non-initial fragment.
        value.baseline_rules[0].l4_present = Some(optional(Some(0)));
        let mut canonical = value.clone();
        canonical.effect_digest.clear();
        value.effect_digest = digest::message_sha256(&canonical);
        assert!(compile(&value, 0, 4096, 1024).is_err());
    }

    #[test]
    fn unknown_effect_digest_conflict_is_rejected() {
        let mut value = intent();
        value.effect_digest = format!("sha256:{}", "0".repeat(64));
        assert!(compile(&value, 0, 4096, 1024).is_err());
    }
}
