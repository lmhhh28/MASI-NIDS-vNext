//! Generate Rust adapters from the repository's only protobuf sources.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let protoc = protoc_bin_vendored::protoc_bin_path()?;
    let mut core_config = tonic_prost_build::Config::new();
    core_config.protoc_executable(protoc.clone());

    let root = std::path::PathBuf::from("..").join("contracts");
    let edge = root.join("edge/v1/edge.proto");
    let inference = root.join("inference/v1/inference.proto");
    let p4 = root.join("p4runtime/v1/p4runtime.proto");

    let mut inference_config = tonic_prost_build::Config::new();
    inference_config.protoc_executable(protoc);
    tonic_prost_build::configure()
        .build_client(true)
        .build_server(true)
        .extern_path(".masi.edge.v1", "crate::contract::edge")
        .compile_with_config(inference_config, &[inference], std::slice::from_ref(&root))?;

    // Compile the owning Edge package last because prost also emits an empty
    // imported-package file while generating the separate inference service.
    tonic_prost_build::configure()
        .build_client(true)
        .build_server(true)
        .type_attribute(
            "masi.edge.v1",
            "#[derive(serde::Serialize, serde::Deserialize)]",
        )
        .type_attribute("masi.edge.v1", "#[serde(deny_unknown_fields)]")
        .compile_with_config(
            core_config,
            &[edge, p4],
            &[root, std::path::PathBuf::from("../contracts/p4runtime/v1")],
        )?;

    println!("cargo:rerun-if-changed=../contracts/edge/v1/edge.proto");
    println!("cargo:rerun-if-changed=../contracts/inference/v1/inference.proto");
    println!("cargo:rerun-if-changed=../contracts/p4runtime/v1/p4runtime.proto");
    println!("cargo:rerun-if-changed=../contracts/p4runtime/v1/google/rpc/status.proto");
    Ok(())
}
