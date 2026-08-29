//! Closed runtime registry for Wasm components and explicit Host-managed services.

pub mod service;
pub mod wasm;

use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};

use crate::contract::host::ExecuteRequest;
use crate::error::HostResult;

/// Reason why an in-flight execution was externally interrupted.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum InterruptReason {
    /// No interruption requested.
    Running = 0,
    /// Explicit caller/lifecycle cancellation.
    Cancelled = 1,
    /// Total deadline expired.
    Deadline = 2,
    /// Explicit epoch interruption used by the runtime guard/fault matrix.
    Epoch = 3,
}

/// Shared cancellation state for one invocation.
#[derive(Debug)]
pub struct InvocationControl {
    reason: AtomicU8,
    notify: tokio::sync::Notify,
}

impl InvocationControl {
    /// Create a running invocation control.
    #[must_use]
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            reason: AtomicU8::new(InterruptReason::Running as u8),
            notify: tokio::sync::Notify::new(),
        })
    }

    /// Request a first-writer-wins interruption.
    pub fn interrupt(&self, reason: InterruptReason) {
        let _ = self.reason.compare_exchange(
            InterruptReason::Running as u8,
            reason as u8,
            Ordering::AcqRel,
            Ordering::Acquire,
        );
        self.notify.notify_waiters();
    }

    /// Read the current reason.
    #[must_use]
    pub fn reason(&self) -> InterruptReason {
        match self.reason.load(Ordering::Acquire) {
            1 => InterruptReason::Cancelled,
            2 => InterruptReason::Deadline,
            3 => InterruptReason::Epoch,
            _ => InterruptReason::Running,
        }
    }

    /// Wait until an interrupt is requested.
    pub async fn cancelled(&self) {
        let notified = self.notify.notified();
        tokio::pin!(notified);
        loop {
            // Register before inspecting the atomic reason. This closes the
            // notify_waiters lost-wakeup window between the state check and
            // awaiting the notification.
            notified.as_mut().enable();
            if self.reason() != InterruptReason::Running {
                return;
            }
            notified.as_mut().await;
            notified.set(self.notify.notified());
        }
    }
}

/// One instantiated closed runtime profile.
#[derive(Clone)]
pub enum RuntimeInstance {
    /// Precompiled Component Model instance factory.
    Wasm(Arc<wasm::WasmRuntime>),
    /// Exact allowlisted already-deployed service.
    Service(Arc<service::ServiceRuntime>),
}

impl RuntimeInstance {
    /// Execute one bounded request.
    pub async fn execute(
        &self,
        request: &ExecuteRequest,
        control: Arc<InvocationControl>,
    ) -> HostResult<Vec<u8>> {
        match self {
            Self::Wasm(runtime) => {
                runtime
                    .execute(&request.input, request.deadline_ms, control)
                    .await
            }
            Self::Service(runtime) => runtime.execute(request, control).await,
        }
    }

    /// Ask the runtime to drain. Wasm instances are per-call and need no remote RPC.
    pub async fn drain(&self, deadline_ms: u32, trace_id: &str) -> HostResult<()> {
        match self {
            Self::Wasm(_) => Ok(()),
            Self::Service(runtime) => runtime.drain(deadline_ms, trace_id).await,
        }
    }

    /// Disable the runtime. Wasm contains no durable state.
    pub async fn disable(
        &self,
        revocation_digest: &str,
        deadline_ms: u32,
        trace_id: &str,
    ) -> HostResult<()> {
        match self {
            Self::Wasm(_) => Ok(()),
            Self::Service(runtime) => {
                runtime
                    .disable(revocation_digest, deadline_ms, trace_id)
                    .await
            }
        }
    }

    /// Interrupt a Wasm engine promptly after cancellation/deadline.
    pub fn interrupt_epoch(&self) {
        if let Self::Wasm(runtime) = self {
            runtime.increment_epoch();
        }
    }
}
