//! MASI-NIDS-vNext Rust Edge Agent.
//!
//! The crate owns the sole production P4Runtime session per assigned target,
//! source/window/result durability, central-inference routing, and fenced P4
//! effects. It never owns PostgreSQL facts or a local inference fallback.

pub mod actor;
pub mod config;
pub mod contract;
pub mod digest;
pub mod endpoint;
pub mod error;
pub mod firewall;
pub mod inference;
pub mod p4runtime;
pub mod server;
pub mod supervisor;
pub mod wal;
pub mod window;

pub use error::{EdgeError, EdgeResult};

/// Package hierarchy required by the upstream P4Runtime google.rpc reference.
pub mod google {
    /// Google RPC package.
    #[allow(missing_docs)]
    pub mod rpc {
        tonic::include_proto!("google.rpc");
    }
}
