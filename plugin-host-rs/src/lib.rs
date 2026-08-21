//! Independent, deny-by-default runtime for exact Manager-controlled plugin bindings.
//!
//! The crate owns only execution isolation. It does not own plugin catalog facts,
//! statistics schedules/runs/current, a durable queue, core PostgreSQL data, P4,
//! Edge, effect authorization, or the Analysis Agent business path.

pub mod admission;
pub mod config;
pub mod contract;
pub mod error;
pub mod health;
pub mod metrics;
pub mod runtime;
pub mod server;
pub mod state;
pub mod statistics;

pub use config::HostConfig;
pub use error::{HostError, ReasonCode};
pub use state::HostState;
