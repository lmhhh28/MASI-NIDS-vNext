//! Generate Rust clients and servers from the repository's public protobuf sources.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let protoc = protoc_bin_vendored::protoc_bin_path()?;
    let root = std::path::PathBuf::from("..").join("contracts");
    let sources = [
        root.join("plugin/host/v1/host.proto"),
        root.join("plugin/service/v1/service.proto"),
        root.join("control-adapter/v1/control_adapter.proto"),
    ];
    let mut config = tonic_prost_build::Config::new();
    config.protoc_executable(protoc);
    config.type_attribute(
        ".masi.plugin.host.v1",
        "#[derive(serde::Serialize, serde::Deserialize)] #[serde(deny_unknown_fields)]",
    );
    config.type_attribute(
        ".masi.control.adapter.v1",
        "#[derive(serde::Serialize, serde::Deserialize)] #[serde(deny_unknown_fields)]",
    );
    config.type_attribute(
        ".masi.plugin.service.v1",
        "#[derive(serde::Serialize, serde::Deserialize)] #[serde(deny_unknown_fields)]",
    );
    tonic_prost_build::configure()
        .build_client(true)
        .build_server(true)
        .compile_with_config(config, &sources, std::slice::from_ref(&root))?;

    for source in sources {
        println!("cargo:rerun-if-changed={}", source.display());
    }
    println!("cargo:rerun-if-changed=../contracts/plugin/wit/v1/pure-transform.wit");
    Ok(())
}
