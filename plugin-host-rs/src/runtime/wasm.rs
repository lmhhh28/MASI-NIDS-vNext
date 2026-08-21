//! Deny-by-construction WASI 0.2 Component Model pure-transform execution.

#![allow(missing_docs)]

use std::sync::Arc;
use std::time::Duration;

use wasmtime::component::{Component, Linker};
use wasmtime::{Config, Engine, Store, StoreLimits, StoreLimitsBuilder, Trap, UpdateDeadline};

use crate::config::Limits;
use crate::error::{HostError, HostResult, ReasonCode};
use crate::runtime::{InterruptReason, InvocationControl};

wasmtime::component::bindgen!({
    path: "../contracts/plugin/wit/v1/pure-transform.wit",
    world: "pure-transform",
});

struct StoreData {
    limits: StoreLimits,
}

/// Precompiled exact Wasm component and immutable execution limits.
pub struct WasmRuntime {
    engine: Engine,
    component: Component,
    limits: Limits,
}

impl WasmRuntime {
    /// Compile and validate one Component Model artifact.
    pub fn compile(bytes: &[u8], limits: &Limits) -> HostResult<Self> {
        let mut config = Config::new();
        config.wasm_component_model(true);
        config.consume_fuel(true);
        config.epoch_interruption(true);
        config.max_wasm_stack(limits.wasm_stack_bytes);
        let engine = Engine::new(&config).map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("Wasmtime engine: {error}"),
            )
        })?;
        let component = Component::new(&engine, bytes).map_err(|error| {
            HostError::new(
                ReasonCode::InvalidArgument,
                format!("component validation: {error:#}"),
            )
        })?;
        let component_type = component.component_type();
        if component_type.imports(&engine).len() != 0 {
            return Err(HostError::new(
                ReasonCode::CapabilityDenied,
                "qualified pure-transform component must not import any host capability",
            ));
        }
        let exports: Vec<_> = component_type
            .exports(&engine)
            .map(|(name, _)| name.to_owned())
            .collect();
        if exports != ["transform"] {
            return Err(HostError::new(
                ReasonCode::UnknownRuntimeProfile,
                "component exports do not exactly match the qualified pure-transform world",
            ));
        }
        Ok(Self {
            engine,
            component,
            limits: limits.clone(),
        })
    }

    /// Execute a single synchronous pure transform under fuel, resource and epoch guards.
    pub async fn execute(
        &self,
        input: &[u8],
        deadline_ms: u32,
        control: Arc<InvocationControl>,
    ) -> HostResult<Vec<u8>> {
        if input.len() > self.limits.max_input_bytes {
            return Err(HostError::new(
                ReasonCode::ResourceExhausted,
                "Wasm input exceeds profile",
            ));
        }
        let deadline = u64::from(deadline_ms).min(self.limits.max_deadline_ms);
        if deadline == 0 {
            return Err(HostError::new(
                ReasonCode::DeadlineExceeded,
                "zero remaining deadline",
            ));
        }
        let engine = self.engine.clone();
        let component = self.component.clone();
        let input = input.to_vec();
        let limits = self.limits.clone();
        let execution_control = control.clone();

        let timer_engine = self.engine.clone();
        let timer_control = control.clone();
        let timer = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(deadline)).await;
            timer_control.interrupt(InterruptReason::Deadline);
            timer_engine.increment_epoch();
        });

        let task = tokio::task::spawn_blocking(move || {
            let deadline_at = std::time::Instant::now() + Duration::from_millis(deadline);
            let store_limits = StoreLimitsBuilder::new()
                .memory_size(limits.wasm_linear_memory_bytes)
                .table_elements(limits.wasm_table_elements)
                .instances(limits.wasm_instances)
                .memories(limits.wasm_memories)
                .tables(limits.wasm_tables)
                .build();
            let mut store = Store::new(
                &engine,
                StoreData {
                    limits: store_limits,
                },
            );
            store.limiter(|data| &mut data.limits);
            store.set_fuel(limits.wasm_fuel).map_err(|error| {
                HostError::new(ReasonCode::Internal, format!("set Wasm fuel: {error}"))
            })?;
            let callback_control = execution_control.clone();
            store.epoch_deadline_callback(move |_| {
                Ok(
                    if callback_control.reason() != InterruptReason::Running
                        || std::time::Instant::now() >= deadline_at
                    {
                        UpdateDeadline::Interrupt
                    } else {
                        UpdateDeadline::Continue(1)
                    },
                )
            });
            store.set_epoch_deadline(1);
            let linker = Linker::<StoreData>::new(&engine);
            let instance = PureTransform::instantiate(&mut store, &component, &linker)
                .map_err(|error| map_wasm_error(error, execution_control.reason()))?;
            let output = instance
                .call_transform(&mut store, &input)
                .map_err(|error| map_wasm_error(error, execution_control.reason()))?
                .map_err(|message| HostError::new(ReasonCode::PluginTrap, message))?;
            if output.len() > limits.max_output_bytes {
                return Err(HostError::new(
                    ReasonCode::ResourceExhausted,
                    "Wasm output exceeds profile",
                ));
            }
            Ok(output)
        });

        let result = task.await.map_err(|error| {
            HostError::new(ReasonCode::Internal, format!("Wasm worker join: {error}"))
        })?;
        timer.abort();
        result
    }

    /// Increment the engine epoch so an interrupted call traps promptly.
    pub fn increment_epoch(&self) {
        self.engine.increment_epoch();
    }
}

fn map_wasm_error(error: wasmtime::Error, reason: InterruptReason) -> HostError {
    let diagnostic = format!("{error:#}").to_ascii_lowercase();
    if diagnostic.contains("resource limit")
        || diagnostic.contains("memory minimum size")
        || diagnostic.contains("table minimum size")
    {
        return HostError::new(
            ReasonCode::ResourceExhausted,
            "Wasm instance exceeds memory/table/instance profile",
        );
    }
    if let Some(trap) = error.downcast_ref::<Trap>() {
        match trap {
            Trap::OutOfFuel => {
                return HostError::new(ReasonCode::WasmFuelExhausted, "Wasm fuel exhausted");
            }
            Trap::Interrupt => {
                return match reason {
                    InterruptReason::Cancelled => {
                        HostError::new(ReasonCode::Cancelled, "Wasm invocation cancelled")
                    }
                    InterruptReason::Deadline => HostError::new(
                        ReasonCode::DeadlineExceeded,
                        "Wasm invocation deadline exceeded",
                    ),
                    InterruptReason::Epoch | InterruptReason::Running => {
                        HostError::new(ReasonCode::WasmEpochInterrupted, "Wasm epoch interrupted")
                    }
                };
            }
            _ => {}
        }
    }
    HostError::new(ReasonCode::PluginTrap, format!("Wasm trap: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits() -> Limits {
        Limits {
            max_bindings: 2,
            global_queue_depth: 4,
            per_binding_queue_depth: 2,
            per_binding_in_flight: 1,
            max_control_message_bytes: 4_194_304,
            max_input_bytes: 2_097_152,
            max_output_bytes: 1_048_576,
            max_deadline_ms: 10_000,
            queue_wait_ms: 100,
            wasm_fuel: 1_000_000,
            wasm_linear_memory_bytes: 67_108_864,
            wasm_table_elements: 1000,
            wasm_instances: 8,
            wasm_memories: 2,
            wasm_tables: 2,
            wasm_stack_bytes: 1_048_576,
            epoch_tick_ms: 10,
            failure_threshold: 3,
            circuit_open_ms: 1000,
            restart_window_ms: 60_000,
            max_restarts_in_window: 5,
            quarantine_ms: 60_000,
            drain_deadline_ms: 1000,
            shutdown_deadline_ms: 1000,
        }
    }

    #[tokio::test]
    async fn executes_component_without_imports() -> Result<(), Box<dyn std::error::Error>> {
        let wat = successful_component();
        let runtime = WasmRuntime::compile(wat.as_bytes(), &limits())?;
        let output = runtime
            .execute(b"abc", 1000, InvocationControl::new())
            .await?;
        assert!(output.is_empty());
        Ok(())
    }

    #[test]
    fn rejects_any_host_import() {
        let wat = r#"(component
          (import "host-clock" (func (param "value" u32)))
          (core module $m
            (memory (export "memory") 1)
            (func (export "transform") (param i32 i32) (result i32) i32.const 0)
            (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) i32.const 64))
          (core instance $i (instantiate $m))
          (func $f (param "input" (list u8)) (result (result (list u8) (error string)))
            (canon lift (core func $i "transform") (memory $i "memory") (realloc (func $i "cabi_realloc"))))
          (export "transform" (func $f)))"#;
        let error = WasmRuntime::compile(wat.as_bytes(), &limits()).err();
        assert_eq!(
            error.map(|value| value.reason),
            Some(ReasonCode::CapabilityDenied)
        );
    }

    #[tokio::test]
    async fn maps_fuel_deadline_cancel_and_epoch_distinctly()
    -> Result<(), Box<dyn std::error::Error>> {
        let wat = looping_component();
        let mut fuel_limits = limits();
        fuel_limits.wasm_fuel = 1000;
        let runtime = WasmRuntime::compile(wat.as_bytes(), &fuel_limits)?;
        let fuel = runtime
            .execute(b"input", 10_000, InvocationControl::new())
            .await
            .err()
            .ok_or("loop unexpectedly completed within fuel")?;
        assert_eq!(fuel.reason, ReasonCode::WasmFuelExhausted);

        let mut deadline_limits = limits();
        deadline_limits.wasm_fuel = 50_000_000;
        let runtime = WasmRuntime::compile(wat.as_bytes(), &deadline_limits)?;
        let deadline = runtime
            .execute(b"input", 1, InvocationControl::new())
            .await
            .err()
            .ok_or("loop unexpectedly completed before deadline")?;
        assert_eq!(deadline.reason, ReasonCode::DeadlineExceeded);

        let runtime = Arc::new(WasmRuntime::compile(wat.as_bytes(), &deadline_limits)?);
        let cancellation = InvocationControl::new();
        let task = {
            let runtime = runtime.clone();
            let cancellation = cancellation.clone();
            tokio::spawn(async move { runtime.execute(b"input", 10_000, cancellation).await })
        };
        tokio::time::sleep(Duration::from_millis(5)).await;
        cancellation.interrupt(InterruptReason::Cancelled);
        runtime.increment_epoch();
        let cancelled = task
            .await?
            .err()
            .ok_or("cancelled loop unexpectedly completed")?;
        assert_eq!(cancelled.reason, ReasonCode::Cancelled);

        let epoch_control = InvocationControl::new();
        let task = {
            let runtime = runtime.clone();
            let epoch_control = epoch_control.clone();
            tokio::spawn(async move { runtime.execute(b"input", 10_000, epoch_control).await })
        };
        tokio::time::sleep(Duration::from_millis(5)).await;
        epoch_control.interrupt(InterruptReason::Epoch);
        runtime.increment_epoch();
        let interrupted = task
            .await?
            .err()
            .ok_or("epoch-interrupted loop unexpectedly completed")?;
        assert_eq!(interrupted.reason, ReasonCode::WasmEpochInterrupted);
        Ok(())
    }

    #[tokio::test]
    async fn enforces_output_limit() -> Result<(), Box<dyn std::error::Error>> {
        let wat = r#"(component
          (core module $m
            (memory (export "memory") 1)
            (data (i32.const 0) "\00\00\00\00\40\00\00\00\80\00\00\00")
            (func (export "transform") (param i32 i32) (result i32) i32.const 0)
            (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) i32.const 256))
          (core instance $i (instantiate $m))
          (func $f (param "input" (list u8)) (result (result (list u8) (error string)))
            (canon lift (core func $i "transform") (memory $i "memory") (realloc (func $i "cabi_realloc"))))
          (export "transform" (func $f)))"#;
        let mut output_limits = limits();
        output_limits.max_output_bytes = 64;
        let runtime = WasmRuntime::compile(wat.as_bytes(), &output_limits)?;
        let error = runtime
            .execute(b"input", 1000, InvocationControl::new())
            .await
            .err()
            .ok_or("oversized output unexpectedly accepted")?;
        assert_eq!(error.reason, ReasonCode::ResourceExhausted);
        Ok(())
    }

    #[tokio::test]
    async fn enforces_linear_memory_limit_without_process_oom()
    -> Result<(), Box<dyn std::error::Error>> {
        let wat = r#"(component
          (core module $m
            (memory (export "memory") 2)
            (func (export "transform") (param i32 i32) (result i32) i32.const 0)
            (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) i32.const 64))
          (core instance $i (instantiate $m))
          (func $f (param "input" (list u8)) (result (result (list u8) (error string)))
            (canon lift (core func $i "transform") (memory $i "memory") (realloc (func $i "cabi_realloc"))))
          (export "transform" (func $f)))"#;
        let mut memory_limits = limits();
        memory_limits.wasm_linear_memory_bytes = 65_536;
        let runtime = WasmRuntime::compile(wat.as_bytes(), &memory_limits)?;
        let error = runtime
            .execute(b"input", 1000, InvocationControl::new())
            .await
            .err()
            .ok_or("oversized Wasm minimum memory unexpectedly instantiated")?;
        assert_eq!(error.reason, ReasonCode::ResourceExhausted);
        Ok(())
    }

    fn successful_component() -> &'static str {
        r#"(component
          (core module $m
            (memory (export "memory") 1)
            (func (export "transform") (param i32 i32) (result i32) i32.const 0)
            (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) i32.const 64))
          (core instance $i (instantiate $m))
          (func $f (param "input" (list u8)) (result (result (list u8) (error string)))
            (canon lift (core func $i "transform") (memory $i "memory") (realloc (func $i "cabi_realloc"))))
          (export "transform" (func $f)))"#
    }

    fn looping_component() -> &'static str {
        r#"(component
          (core module $m
            (memory (export "memory") 1)
            (func (export "transform") (param i32 i32) (result i32)
              (loop $forever br $forever)
              i32.const 0)
            (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) i32.const 64))
          (core instance $i (instantiate $m))
          (func $f (param "input" (list u8)) (result (result (list u8) (error string)))
            (canon lift (core func $i "transform") (memory $i "memory") (realloc (func $i "cabi_realloc"))))
          (export "transform" (func $f)))"#
    }
}
