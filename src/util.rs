use std::time::{SystemTime, UNIX_EPOCH};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use rand::RngCore;
use sha2::{Digest, Sha256};

pub const TOKEN_PREFIX: &str = "fcc_";

/// Milliseconds since the Unix epoch.
pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// A new random API token (256 bits of entropy).
pub fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    format!("{TOKEN_PREFIX}{}", URL_SAFE_NO_PAD.encode(bytes))
}

/// Tokens are high-entropy, so a plain SHA-256 is sufficient for storage.
pub fn hash_token(token: &str) -> String {
    sha256_hex(token.as_bytes())
}

pub fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// Length-independent constant-time comparison.
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    let ha = Sha256::digest(a);
    let hb = Sha256::digest(b);
    ha.iter().zip(hb.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// FreeCAD internal object / property names: `[A-Za-z_][A-Za-z0-9_]*`.
pub fn is_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() || c == '_' => {}
        _ => return false,
    }
    s.len() <= 128 && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

pub fn is_valid_user_name(s: &str) -> bool {
    (1..=64).contains(&s.len())
        && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers() {
        assert!(is_identifier("Box001"));
        assert!(is_identifier("_x"));
        assert!(!is_identifier("1Box"));
        assert!(!is_identifier(""));
        assert!(!is_identifier("a b"));
    }

    #[test]
    fn tokens_are_unique_and_hash_stably() {
        let a = generate_token();
        let b = generate_token();
        assert_ne!(a, b);
        assert!(a.starts_with(TOKEN_PREFIX));
        assert_eq!(hash_token(&a), hash_token(&a));
        assert!(constant_time_eq(a.as_bytes(), a.as_bytes()));
        assert!(!constant_time_eq(a.as_bytes(), b.as_bytes()));
    }
}
