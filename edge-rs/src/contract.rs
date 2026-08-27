//! Generated public wire contracts.

/// MASI Edge public API.
#[allow(missing_docs)]
pub mod edge {
    tonic::include_proto!("masi.edge.v1");
}

/// Central Inference public service boundary.
#[allow(missing_docs)]
pub mod inference {
    tonic::include_proto!("masi.inference.v1");
}

/// P4Runtime 1.4.1-compatible wire subset.
#[allow(missing_docs)]
pub mod p4 {
    tonic::include_proto!("p4.v1");
}

/// Canonical P4Info 1.4.1 schema used for semantic pipeline identity.
#[allow(missing_docs)]
pub mod p4info {
    tonic::include_proto!("p4.config.v1");
}

/// Google RPC status used by P4Runtime arbitration.
#[allow(missing_docs)]
pub mod google_rpc {
    tonic::include_proto!("google.rpc");
}
