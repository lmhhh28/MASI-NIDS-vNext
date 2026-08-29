//! Stable digest and identifier validation.

use prost::Message;
use sha2::{Digest as _, Sha256};

use crate::{EdgeError, EdgeResult};

/// Compute a lowercase `sha256:` digest.
#[must_use]
pub fn sha256(payload: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(payload)))
}

/// Deterministically encode and digest a protobuf message.
#[must_use]
pub fn message_sha256<M: Message>(message: &M) -> String {
    sha256(&message.encode_to_vec())
}

/// Validate the canonical digest spelling.
pub fn validate_sha256(value: &str, field: &'static str) -> EdgeResult<()> {
    let Some(hex_part) = value.strip_prefix("sha256:") else {
        return Err(EdgeError::invalid(field, "digest must start with sha256:"));
    };
    if hex_part.len() != 64
        || !hex_part
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || hex_part.bytes().all(|byte| byte == b'0')
    {
        return Err(EdgeError::invalid(
            field,
            "digest must contain 64 lowercase hexadecimal characters and must not be all zero",
        ));
    }
    Ok(())
}

/// Validate a stable, bounded contract identity.
pub fn validate_identity(value: &str, field: &'static str) -> EdgeResult<()> {
    if value.is_empty() || value.len() > 128 {
        return Err(EdgeError::invalid(
            field,
            "identity length must be in [1,128]",
        ));
    }
    if !value.bytes().all(|byte| {
        byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':' | b'/')
    }) {
        return Err(EdgeError::invalid(
            field,
            "identity contains a forbidden character",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_spelling_is_strict() {
        assert!(validate_sha256(&format!("sha256:{}", "a".repeat(64)), "d").is_ok());
        assert!(validate_sha256(&format!("sha256:{}", "A".repeat(64)), "d").is_err());
        assert!(validate_sha256(&format!("sha256:{}", "0".repeat(64)), "d").is_err());
        assert!(validate_sha256("a", "d").is_err());
    }

    #[test]
    fn identity_is_bounded() {
        assert!(validate_identity("target-1", "target_id").is_ok());
        assert!(validate_identity("bad identity", "target_id").is_err());
        assert!(validate_identity(&"x".repeat(129), "target_id").is_err());
    }
}
