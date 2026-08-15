//! Registry-controlled endpoint resolution and DNS-rebinding defense.

use std::{net::IpAddr, time::Duration};

use tonic::transport::{
    Certificate, Channel, ClientTlsConfig as TonicClientTlsConfig, Endpoint, Identity,
};
use url::Url;

use crate::{
    EdgeError, EdgeResult,
    config::{
        ClientTlsConfig, DeploymentTier, EndpointPolicyConfig, read_pem, validate_server_name,
    },
};

/// An endpoint pinned to a validated resolved address for this connection.
#[derive(Clone, Debug)]
pub struct ResolvedEndpoint {
    /// URI using the validated IP, preventing a second resolver decision.
    pub endpoint: Endpoint,
    /// Resolved address selected from an all-allowed answer set.
    pub ip: IpAddr,
    /// Original registry host for diagnostics.
    pub registry_host: String,
    /// Exact expected TLS DNS name.
    pub server_name: String,
}

/// Resolve and connect an outbound boundary with its dedicated mTLS identity.
pub async fn connect_mtls(
    raw: &str,
    tls: &ClientTlsConfig,
    tier: DeploymentTier,
    policy: &EndpointPolicyConfig,
    connect_deadline: Duration,
) -> EdgeResult<Channel> {
    let resolved = resolve_endpoint(raw, &tls.server_name, tier, policy, connect_deadline).await?;
    let identity = Identity::from_pem(
        read_pem(&tls.certificate_path, false)?,
        read_pem(&tls.private_key_path, true)?,
    );
    let ca = Certificate::from_pem(read_pem(&tls.ca_path, false)?);
    let tls_config = TonicClientTlsConfig::new()
        .ca_certificate(ca)
        .identity(identity)
        .domain_name(tls.server_name.clone());
    let endpoint = resolved
        .endpoint
        .tls_config(tls_config)
        .map_err(|error| EdgeError::TlsIdentityMismatch(error.to_string()))?;
    tokio::time::timeout(connect_deadline, endpoint.connect())
        .await
        .map_err(|_| EdgeError::Deadline("outbound mTLS connect".into()))?
        .map_err(|error| EdgeError::TlsIdentityMismatch(error.to_string()))
}

/// Validate, resolve, and pin an HTTPS endpoint.
pub async fn resolve_endpoint(
    raw: &str,
    server_name: &str,
    tier: DeploymentTier,
    policy: &EndpointPolicyConfig,
    connect_deadline: Duration,
) -> EdgeResult<ResolvedEndpoint> {
    validate_server_name(server_name)?;
    let url = Url::parse(raw)
        .map_err(|error| EdgeError::invalid("endpoint", format!("invalid URL: {error}")))?;
    if url.scheme() != "https" {
        return Err(EdgeError::EndpointNotAllowed(
            "only https endpoints are accepted".into(),
        ));
    }
    if !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || !matches!(url.path(), "" | "/")
    {
        return Err(EdgeError::EndpointNotAllowed(
            "userinfo, path, query, and fragment are forbidden".into(),
        ));
    }
    let host = url
        .host_str()
        .ok_or_else(|| EdgeError::invalid("endpoint", "host is required"))?
        .to_owned();
    let port = url
        .port()
        .ok_or_else(|| EdgeError::invalid("endpoint", "explicit port is required"))?;

    let answers = tokio::time::timeout(
        Duration::from_millis(policy.resolution_deadline_ms),
        tokio::net::lookup_host((host.as_str(), port)),
    )
    .await
    .map_err(|_| EdgeError::Deadline("endpoint DNS resolution".into()))?
    .map_err(|error| EdgeError::EndpointNotAllowed(format!("resolution failed: {error}")))?;
    let mut ips: Vec<IpAddr> = answers.map(|answer| answer.ip()).collect();
    ips.sort_unstable();
    ips.dedup();
    if ips.is_empty() {
        return Err(EdgeError::EndpointNotAllowed(
            "resolution returned no addresses".into(),
        ));
    }
    for ip in &ips {
        if !is_allowed(*ip, tier, policy) {
            return Err(EdgeError::EndpointNotAllowed(format!(
                "resolution includes disallowed address class: {ip}"
            )));
        }
    }
    let ip = ips[0];
    let authority = match ip {
        IpAddr::V4(address) => format!("{address}:{port}"),
        IpAddr::V6(address) => format!("[{address}]:{port}"),
    };
    let endpoint = Endpoint::from_shared(format!("https://{authority}"))
        .map_err(|error| EdgeError::invalid("endpoint", error.to_string()))?
        .connect_timeout(connect_deadline)
        .http2_keep_alive_interval(Duration::from_secs(10))
        .keep_alive_while_idle(true);
    Ok(ResolvedEndpoint {
        endpoint,
        ip,
        registry_host: host,
        server_name: server_name.to_owned(),
    })
}

fn is_allowed(ip: IpAddr, tier: DeploymentTier, policy: &EndpointPolicyConfig) -> bool {
    if ip.is_unspecified() || ip.is_multicast() {
        return false;
    }
    match ip {
        IpAddr::V4(address) => {
            if address.is_link_local() || address.is_broadcast() {
                return false;
            }
        }
        IpAddr::V6(address) => {
            if address.is_unicast_link_local() {
                return false;
            }
        }
    }
    if ip.is_loopback() {
        return tier == DeploymentTier::ModuleTest
            && policy.module_test_loopback_allowlist.contains(&ip);
    }
    let explicitly_allowed = policy
        .allowed_management_cidrs
        .iter()
        .any(|network| network.contains(&ip));
    if !explicitly_allowed {
        return false;
    }
    // Production-capable tiers accept only non-public approved management
    // ranges. Module tests may use TEST-NET ranges when explicitly allowlisted.
    if tier != DeploymentTier::ModuleTest {
        match ip {
            IpAddr::V4(address) => address.is_private(),
            IpAddr::V6(address) => address.is_unique_local(),
        }
    } else {
        true
    }
}

#[cfg(test)]
mod tests {
    use std::str::FromStr as _;

    use super::*;

    fn policy() -> EndpointPolicyConfig {
        EndpointPolicyConfig {
            allowed_management_cidrs: vec![
                ipnet::IpNet::from_str("10.0.0.0/8")
                    .unwrap_or_else(|error| unreachable!("test literal is valid: {error}")),
            ],
            module_test_loopback_allowlist: vec![IpAddr::from([127, 0, 0, 1])],
            resolution_deadline_ms: 100,
        }
    }

    #[test]
    fn endpoint_classes_are_fail_closed() {
        let policy = policy();
        assert!(is_allowed(
            IpAddr::from([10, 1, 2, 3]),
            DeploymentTier::ProductionHa,
            &policy
        ));
        assert!(!is_allowed(
            IpAddr::from([127, 0, 0, 1]),
            DeploymentTier::ProductionHa,
            &policy
        ));
        assert!(is_allowed(
            IpAddr::from([127, 0, 0, 1]),
            DeploymentTier::ModuleTest,
            &policy
        ));
        assert!(!is_allowed(
            IpAddr::from([169, 254, 169, 254]),
            DeploymentTier::ModuleTest,
            &policy
        ));
    }
}
