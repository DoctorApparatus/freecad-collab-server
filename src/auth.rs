use axum::{extract::FromRequestParts, http::request::Parts};
use uuid::Uuid;

use crate::{
    error::AppError,
    model::{Role, User},
    state::AppState,
    util::{constant_time_eq, hash_token},
};

const MAX_TOKEN_LEN: usize = 256;

fn bearer_token(parts: &Parts) -> Option<String> {
    let value = parts.headers.get(axum::http::header::AUTHORIZATION)?.to_str().ok()?;
    let (scheme, token) = value.split_once(' ')?;
    scheme
        .eq_ignore_ascii_case("bearer")
        .then(|| token.trim().to_string())
        .filter(|t| !t.is_empty())
}

/// `?access_token=` fallback for WebSocket clients that cannot set headers.
fn query_token(parts: &Parts) -> Option<String> {
    parts
        .uri
        .query()?
        .split('&')
        .find_map(|kv| kv.strip_prefix("access_token="))
        .map(str::to_string)
        .filter(|t| !t.is_empty())
}

/// An authenticated, enabled user.
pub struct AuthUser(pub User);

impl FromRequestParts<AppState> for AuthUser {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, Self::Rejection> {
        let token = bearer_token(parts)
            .or_else(|| query_token(parts))
            .ok_or(AppError::Unauthorized)?;
        if token.len() > MAX_TOKEN_LEN {
            return Err(AppError::Unauthorized);
        }
        let user = state
            .store
            .user_by_token_hash(&hash_token(&token))
            .await?
            .ok_or(AppError::Unauthorized)?;
        Ok(AuthUser(user))
    }
}

/// Holder of `COLLAB_ADMIN_TOKEN`. The admin API 404s when no token is set.
pub struct AdminAuth;

impl FromRequestParts<AppState> for AdminAuth {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, Self::Rejection> {
        let Some(expected) = state.config.admin_token.as_deref() else {
            return Err(AppError::NotFound("route"));
        };
        let token = bearer_token(parts).unwrap_or_default();
        if constant_time_eq(token.as_bytes(), expected.as_bytes()) {
            Ok(AdminAuth)
        } else {
            // Slow down guessing; the token itself has 256 bits of entropy.
            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
            tracing::warn!("rejected admin credentials");
            Err(AppError::Unauthorized)
        }
    }
}

/// Resolve the caller's role on a document, enforcing a minimum.
/// Non-members get 404 so document ids are not enumerable.
pub async fn require_role(state: &AppState, document_id: Uuid, user: &User, min: Role) -> Result<Role, AppError> {
    let role = state
        .store
        .member_role(document_id, user.id)
        .await?
        .ok_or(AppError::NotFound("document"))?;
    if role < min {
        return Err(AppError::Forbidden);
    }
    Ok(role)
}
