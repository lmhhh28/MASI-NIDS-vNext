//! Export a bounded real-Host/Wasm acceptance fixture for the Go dispatcher.

mod support;

use std::fs::Permissions;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

use support::{TestEnvironment, WASM_STATISTICS_PLUGIN_ID};

#[test]
fn export_statistics_fixture() -> Result<(), Box<dyn std::error::Error>> {
    let Some(output_raw) = std::env::var_os("MASI_PLUGIN_FIXTURE_EXPORT_DIR") else {
        return Ok(());
    };
    let definition_digest = std::env::var("MASI_PLUGIN_FIXTURE_DEFINITION_DIGEST")?;
    if !definition_digest.starts_with("sha256:") || definition_digest.len() != 71 {
        return Err("definition digest must be sha256:<64hex>".into());
    }
    let output = PathBuf::from(output_raw);
    if output.is_symlink() || !output.is_dir() || output.read_dir()?.next().is_some() {
        return Err("fixture export directory must be an empty regular directory".into());
    }

    let environment = TestEnvironment::new(false)?;
    let plugin_revision = "wasm-statistics-revision-1:1";
    let binding = environment.wasm_statistics_binding_custom(
        1,
        "active",
        plugin_revision,
        support::STATISTICS_DEFINITION_ID,
        &definition_digest,
    )?;

    let artifacts = output.join("artifacts");
    let tls = output.join("tls");
    std::fs::create_dir(&artifacts)?;
    std::fs::create_dir(&tls)?;
    let source_artifact = environment
        .config
        .artifact_cache_root
        .join(&binding.artifact_name);
    std::fs::copy(&source_artifact, artifacts.join(&binding.artifact_name))?;

    let server_certificate = tls.join("server.pem");
    let server_private_key = tls.join("server-key.pem");
    let ca = tls.join("ca.pem");
    let manager_certificate = tls.join("manager.pem");
    let manager_private_key = tls.join("manager-key.pem");
    std::fs::copy(
        &environment.config.server_tls.certificate_path,
        &server_certificate,
    )?;
    std::fs::copy(
        &environment.config.server_tls.private_key_path,
        &server_private_key,
    )?;
    std::fs::write(&ca, &environment.ca_pem)?;
    std::fs::write(&manager_certificate, &environment.manager_certificate_pem)?;
    std::fs::write(&manager_private_key, &environment.manager_private_key_pem)?;
    for private_key in [&server_private_key, &manager_private_key] {
        std::fs::set_permissions(private_key, Permissions::from_mode(0o600))?;
    }

    let mut host_config = environment.config.clone();
    host_config.artifact_cache_root = artifacts;
    host_config.server_tls.certificate_path = server_certificate;
    host_config.server_tls.private_key_path = server_private_key;
    host_config.server_tls.client_ca_path = ca.clone();
    let config_path = output.join("host-config.json");
    std::fs::write(&config_path, serde_json::to_vec_pretty(&host_config)?)?;
    let binding_path = output.join("binding.json");
    std::fs::write(&binding_path, serde_json::to_vec_pretty(&binding)?)?;
    let manifest_path = output.join("manifest.json");
    std::fs::write(&manifest_path, &binding.manifest_json)?;

    let runtime = serde_json::json!({
        "schema_version": "go-plugin-host-fixture/v1",
        "endpoint": format!("https://{}", host_config.listen_address),
        "server_name": "plugin-host.test",
        "health_endpoint": format!("http://{}", host_config.health_address),
        "config_path": config_path,
        "binding_path": binding_path,
        "manifest_path": manifest_path,
        "ca_path": ca,
        "manager_certificate_path": manager_certificate,
        "manager_private_key_path": manager_private_key,
        "plugin_id": WASM_STATISTICS_PLUGIN_ID,
        "plugin_revision": binding.plugin_revision,
        "binding_generation": binding.binding_generation,
        "config_digest": binding.config_digest,
        "capability_digest": binding.capability_digest,
        "resource_profile_digest": binding.resource_profile_digest,
        "definition_id": binding.statistics_definitions[0].definition_id,
        "definition_revision": binding.statistics_definitions[0].definition_revision,
        "definition_digest": binding.statistics_definitions[0].definition_digest,
        "scope": binding.scope,
        "artifact_digest": binding.artifact_digest,
        "envelope_digest": binding.envelope_digest
    });
    std::fs::write(
        output.join("runtime.json"),
        serde_json::to_vec_pretty(&runtime)?,
    )?;
    Ok(())
}
