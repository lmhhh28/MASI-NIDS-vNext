#![allow(missing_docs)]

mod support;

use std::collections::BTreeSet;
use std::path::PathBuf;

use masi_plugin_host::contract::host::ExecuteRequest;
use masi_plugin_host::contract::service::HandshakeRequest;
use masi_plugin_host::statistics::{
    FrozenInputBundle, compute_frozen_input_digest, compute_input_bundle_digest,
};
use prost::Message as _;

#[test]
fn public_profiles_pin_current_contract_digests() -> Result<(), Box<dyn std::error::Error>> {
    let root = repository_root();
    let host_profile: serde_json::Value = serde_json::from_slice(&std::fs::read(
        root.join("contracts/profiles/v1/plugin-runtime-host.json"),
    )?)?;
    let service_profile: serde_json::Value = serde_json::from_slice(&std::fs::read(
        root.join("contracts/profiles/v1/grpc-service.json"),
    )?)?;
    let wasm_profile: serde_json::Value = serde_json::from_slice(&std::fs::read(
        root.join("contracts/profiles/v1/wasm-component.json"),
    )?)?;
    assert_eq!(
        host_profile["host_proto_digest"],
        masi_plugin_host::admission::sha256_bytes(&std::fs::read(
            root.join("contracts/plugin/host/v1/host.proto"),
        )?)
    );
    assert_eq!(
        service_profile["service_proto_digest"],
        masi_plugin_host::admission::sha256_bytes(&std::fs::read(
            root.join("contracts/plugin/service/v1/service.proto"),
        )?)
    );
    assert_eq!(
        wasm_profile["wit_digest"],
        masi_plugin_host::admission::sha256_bytes(&std::fs::read(
            root.join("contracts/plugin/wit/v1/pure-transform.wit"),
        )?)
    );
    assert_eq!(wasm_profile["imports"], serde_json::json!([]));
    assert_eq!(
        host_profile["direct_not_proxied_kinds"],
        serde_json::json!(["analysis-agent"])
    );
    Ok(())
}

#[test]
fn host_execute_protobuf_golden_is_exact() -> Result<(), Box<dyn std::error::Error>> {
    let fixture: serde_json::Value = serde_json::from_slice(&std::fs::read(
        repository_root().join("contracts/plugin/host/v1/golden/execute-request-v1.json"),
    )?)?;
    let message: ExecuteRequest = serde_json::from_value(fixture["message"].clone())?;
    assert_eq!(
        hex::encode(message.encode_to_vec()),
        fixture["protobuf_hex"]
            .as_str()
            .ok_or("golden protobuf_hex missing")?
    );
    Ok(())
}

#[test]
fn service_handshake_protobuf_golden_is_exact() -> Result<(), Box<dyn std::error::Error>> {
    let fixture: serde_json::Value = serde_json::from_slice(&std::fs::read(
        repository_root().join("contracts/plugin/service/v1/golden/handshake-v1.json"),
    )?)?;
    let message: HandshakeRequest = serde_json::from_value(fixture["message"].clone())?;
    assert_eq!(
        hex::encode(message.encode_to_vec()),
        fixture["protobuf_hex"]
            .as_str()
            .ok_or("golden protobuf_hex missing")?
    );
    Ok(())
}

#[test]
fn rust_manifest_surface_exactly_matches_strict_required_schema()
-> Result<(), Box<dyn std::error::Error>> {
    let environment = support::TestEnvironment::new(false)?;
    let binding = environment.wasm_binding(1, "staged")?;
    let manifest: serde_json::Value = serde_json::from_slice(&binding.manifest_json)?;
    let schema: serde_json::Value = serde_json::from_slice(&std::fs::read(
        repository_root().join("contracts/plugin/manifest/v1/schema.json"),
    )?)?;
    let manifest_fields: BTreeSet<_> = manifest
        .as_object()
        .ok_or("manifest is not an object")?
        .keys()
        .map(String::as_str)
        .collect();
    let required_fields: BTreeSet<_> = schema["required"]
        .as_array()
        .ok_or("manifest schema required missing")?
        .iter()
        .filter_map(serde_json::Value::as_str)
        .collect();
    assert_eq!(manifest_fields, required_fields);
    Ok(())
}

#[test]
fn statistics_input_bundle_golden_is_exact_and_schema_complete()
-> Result<(), Box<dyn std::error::Error>> {
    let raw = std::fs::read(
        repository_root().join("contracts/plugin/statistics/v1/golden/input-bundle-v1.json"),
    )?;
    let bundle: FrozenInputBundle = serde_json::from_slice(&raw)?;
    assert_eq!(
        compute_frozen_input_digest(&bundle)?,
        bundle.frozen_input_digest
    );
    assert_eq!(compute_input_bundle_digest(&bundle)?, bundle.bundle_digest);
    let document: serde_json::Value = serde_json::from_slice(&raw)?;
    let schema: serde_json::Value = serde_json::from_slice(&std::fs::read(
        repository_root().join("contracts/plugin/statistics/v1/schema.json"),
    )?)?;
    let actual: BTreeSet<_> = document
        .as_object()
        .ok_or("statistics golden is not an object")?
        .keys()
        .map(String::as_str)
        .collect();
    let required: BTreeSet<_> = schema["$defs"]["input_bundle_record"]["required"]
        .as_array()
        .ok_or("statistics input required list missing")?
        .iter()
        .filter_map(serde_json::Value::as_str)
        .collect();
    assert_eq!(actual, required);
    Ok(())
}

fn repository_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..")
}
