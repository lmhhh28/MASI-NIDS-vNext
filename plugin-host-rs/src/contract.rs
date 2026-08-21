//! Generated adapters for the public plugin contracts.

/// Manager-to-Host lifecycle and invocation contract.
pub mod host {
    #![allow(missing_docs)]
    tonic::include_proto!("masi.plugin.host.v1");
}

/// Host-to-Host-managed-service contract.
pub mod service {
    #![allow(missing_docs)]
    tonic::include_proto!("masi.plugin.service.v1");
}

/// Existing Go Plugin Statistics execution adapter contract.
pub mod control_adapter {
    #![allow(missing_docs)]
    tonic::include_proto!("masi.control.adapter.v1");
}
