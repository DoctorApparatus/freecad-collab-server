use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

use crate::db::StoreError;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("missing or invalid credentials")]
    Unauthorized,
    #[error("insufficient permissions")]
    Forbidden,
    #[error("{0} not found")]
    NotFound(&'static str),
    #[error("{0}")]
    BadRequest(String),
    #[error("{0}")]
    Conflict(String),
    #[error("{0}")]
    Gone(String),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Internal(#[from] anyhow::Error),
}

impl AppError {
    fn status_and_code(&self) -> (StatusCode, &'static str) {
        match self {
            AppError::Unauthorized => (StatusCode::UNAUTHORIZED, "unauthorized"),
            AppError::Forbidden => (StatusCode::FORBIDDEN, "forbidden"),
            AppError::NotFound(_) => (StatusCode::NOT_FOUND, "not_found"),
            AppError::BadRequest(_) => (StatusCode::BAD_REQUEST, "bad_request"),
            AppError::Conflict(_) => (StatusCode::CONFLICT, "conflict"),
            AppError::Gone(_) => (StatusCode::GONE, "gone"),
            AppError::Store(StoreError::AlreadyExists(_)) => (StatusCode::CONFLICT, "conflict"),
            AppError::Store(_) | AppError::Internal(_) => {
                (StatusCode::INTERNAL_SERVER_ERROR, "internal")
            }
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, code) = self.status_and_code();
        let message = if status.is_server_error() {
            tracing::error!(error = ?self, "request failed");
            "internal server error".to_string()
        } else {
            self.to_string()
        };
        let mut resp = (status, Json(json!({ "error": code, "message": message }))).into_response();
        if status == StatusCode::UNAUTHORIZED {
            resp.headers_mut().insert(
                axum::http::header::WWW_AUTHENTICATE,
                axum::http::HeaderValue::from_static("Bearer"),
            );
        }
        resp
    }
}
