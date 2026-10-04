//! Administration: JSON API under `/v1/admin` (bearer `COLLAB_ADMIN_TOKEN`)
//! and the browser console at `/admin`, embedded in the binary.

use axum::{
    extract::{Path, State},
    http::{header, HeaderName, HeaderValue, StatusCode},
    response::{IntoResponse, Redirect, Response},
    routing::{delete, get, put},
    Json, Router,
};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::{
    api::{apply_delete_document, apply_remove_member, apply_set_member},
    auth::AdminAuth,
    error::AppError,
    model::{AdminDocument, Document, Member, Role, SnapshotInfo, Stats, TokenInfo, TokenSummary, User, UserKind},
    protocol::Participant,
    state::AppState,
    util::{generate_token, hash_token, is_valid_user_name},
};

const INDEX_HTML: &str = include_str!("../admin/index.html");
const APP_JS: &str = include_str!("../admin/app.js");
const APP_CSS: &str = include_str!("../admin/app.css");

const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; \
                   connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; \
                   frame-ancestors 'none'";

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/", get(|| async { Redirect::temporary("/admin") }))
        .route("/admin", get(index))
        .route("/admin/", get(index))
        .route("/admin/app.js", get(app_js))
        .route("/admin/app.css", get(app_css))
        .route("/v1/admin/overview", get(overview))
        .route("/v1/admin/users", get(list_users).post(create_user))
        .route("/v1/admin/users/{id}/tokens", get(list_tokens).post(issue_token_handler))
        .route("/v1/admin/users/{id}/disabled", put(set_disabled))
        .route("/v1/admin/tokens/{id}", delete(revoke_token))
        .route("/v1/admin/documents", get(list_documents).post(create_document))
        .route("/v1/admin/documents/{id}", get(get_document).delete(delete_document))
        .route("/v1/admin/documents/{id}/members", put(set_member))
        .route("/v1/admin/documents/{id}/members/{user_id}", delete(remove_member))
}

// ---------------------------------------------------------------- UI assets

fn asset(content_type: &'static str, body: &'static str) -> Response {
    let mut resp = body.into_response();
    let h = resp.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    h.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static(CSP));
    h.insert(header::X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    h.insert(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    h.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    h.insert(
        HeaderName::from_static("permissions-policy"),
        HeaderValue::from_static("camera=(), microphone=(), geolocation=()"),
    );
    resp
}

async fn index() -> Response {
    asset("text/html; charset=utf-8", INDEX_HTML)
}

async fn app_js() -> Response {
    asset("text/javascript; charset=utf-8", APP_JS)
}

async fn app_css() -> Response {
    asset("text/css; charset=utf-8", APP_CSS)
}

// ---------------------------------------------------------------- overview

#[derive(Debug, Serialize, JsonSchema)]
pub struct LiveDocument {
    pub document_id: Uuid,
    pub document_name: String,
    pub participants: Vec<Participant>,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct Overview {
    pub version: &'static str,
    pub uptime_secs: u64,
    pub stats: Stats,
    pub live: Vec<LiveDocument>,
}

async fn overview(_: AdminAuth, State(state): State<AppState>) -> Result<Json<Overview>, AppError> {
    let stats = state.store.stats().await?;
    let mut live = Vec::new();
    for (document_id, participants) in state.rooms.live().await {
        if participants.is_empty() {
            continue;
        }
        let document_name = state
            .store
            .get_document(document_id)
            .await?
            .map(|d| d.name)
            .unwrap_or_else(|| "(deleted)".into());
        live.push(LiveDocument {
            document_id,
            document_name,
            participants,
        });
    }
    live.sort_by(|a, b| a.document_name.cmp(&b.document_name));
    Ok(Json(Overview {
        version: env!("CARGO_PKG_VERSION"),
        uptime_secs: state.started.elapsed().as_secs(),
        stats,
        live,
    }))
}

// ---------------------------------------------------------------- users

#[derive(Debug, Deserialize, JsonSchema)]
pub struct CreateUser {
    pub name: String,
    #[serde(default)]
    pub kind: UserKind,
}

#[derive(Debug, Deserialize, JsonSchema, Default)]
pub struct IssueToken {
    #[serde(default)]
    pub label: Option<String>,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct IssuedToken {
    /// Shown once. Only its hash is stored.
    pub token: String,
    #[serde(flatten)]
    pub info: TokenInfo,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct CreatedUser {
    pub user: User,
    pub token: IssuedToken,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct SetDisabled {
    pub disabled: bool,
}

async fn list_users(_: AdminAuth, State(state): State<AppState>) -> Result<Json<Vec<User>>, AppError> {
    Ok(Json(state.store.list_users().await?))
}

async fn create_user(
    _: AdminAuth,
    State(state): State<AppState>,
    Json(body): Json<CreateUser>,
) -> Result<(StatusCode, Json<CreatedUser>), AppError> {
    let name = body.name.trim();
    if !is_valid_user_name(name) {
        return Err(AppError::BadRequest(
            "user names are 1-64 characters: letters, digits, dot, dash, underscore".into(),
        ));
    }
    let user = state.store.create_user(name, body.kind).await?;
    let token = issue_token(&state, user.id, Some("initial")).await?;
    tracing::info!(user = %user.name, kind = %user.kind, "user created via admin API");
    Ok((StatusCode::CREATED, Json(CreatedUser { user, token })))
}

async fn list_tokens(
    _: AdminAuth,
    State(state): State<AppState>,
    Path(user_id): Path<Uuid>,
) -> Result<Json<Vec<TokenSummary>>, AppError> {
    state.store.user_by_id(user_id).await?.ok_or(AppError::NotFound("user"))?;
    Ok(Json(state.store.list_tokens(user_id).await?))
}

async fn issue_token_handler(
    _: AdminAuth,
    State(state): State<AppState>,
    Path(user_id): Path<Uuid>,
    body: Option<Json<IssueToken>>,
) -> Result<(StatusCode, Json<IssuedToken>), AppError> {
    let label = body
        .and_then(|Json(b)| b.label)
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty());
    if label.as_ref().is_some_and(|l| l.len() > 128) {
        return Err(AppError::BadRequest("labels are at most 128 characters".into()));
    }
    state.store.user_by_id(user_id).await?.ok_or(AppError::NotFound("user"))?;
    let token = issue_token(&state, user_id, label.as_deref()).await?;
    Ok((StatusCode::CREATED, Json(token)))
}

async fn set_disabled(
    _: AdminAuth,
    State(state): State<AppState>,
    Path(user_id): Path<Uuid>,
    Json(body): Json<SetDisabled>,
) -> Result<StatusCode, AppError> {
    if !state.store.set_user_disabled(user_id, body.disabled).await? {
        return Err(AppError::NotFound("user"));
    }
    if body.disabled {
        state
            .rooms
            .kick_user(user_id, "your account was disabled by an administrator".into())
            .await;
    }
    Ok(StatusCode::NO_CONTENT)
}

async fn revoke_token(
    _: AdminAuth,
    State(state): State<AppState>,
    Path(token_id): Path<Uuid>,
) -> Result<StatusCode, AppError> {
    if !state.store.revoke_token(token_id).await? {
        return Err(AppError::NotFound("token"));
    }
    Ok(StatusCode::NO_CONTENT)
}

pub async fn issue_token(state: &AppState, user_id: Uuid, label: Option<&str>) -> Result<IssuedToken, AppError> {
    let token = generate_token();
    let info = state.store.create_token(user_id, &hash_token(&token), label).await?;
    Ok(IssuedToken { token, info })
}

// ---------------------------------------------------------------- documents

#[derive(Debug, Deserialize, JsonSchema)]
pub struct AdminCreateDocument {
    pub name: String,
    pub owner_name: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct AdminSetMember {
    pub user_name: String,
    pub role: Role,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct AdminDocumentDetails {
    #[serde(flatten)]
    pub document: Document,
    pub snapshot: Option<SnapshotInfo>,
    pub members: Vec<Member>,
    pub participants: Vec<Participant>,
}

async fn list_documents(_: AdminAuth, State(state): State<AppState>) -> Result<Json<Vec<AdminDocument>>, AppError> {
    let live = state.rooms.live().await;
    let mut docs = state.store.list_all_documents().await?;
    for d in &mut docs {
        d.live_sessions = live.get(&d.document.id).map_or(0, Vec::len);
    }
    Ok(Json(docs))
}

async fn create_document(
    _: AdminAuth,
    State(state): State<AppState>,
    Json(body): Json<AdminCreateDocument>,
) -> Result<(StatusCode, Json<Document>), AppError> {
    let name = body.name.trim();
    if name.is_empty() || name.len() > 200 {
        return Err(AppError::BadRequest("document names are 1-200 characters".into()));
    }
    let owner = state
        .store
        .user_by_name(body.owner_name.trim())
        .await?
        .filter(|u| !u.disabled)
        .ok_or(AppError::NotFound("user"))?;
    let doc = state.store.create_document(owner.id, name, None).await?;
    tracing::info!(document = %doc.id, owner = %owner.name, "document created via admin API");
    Ok((StatusCode::CREATED, Json(doc)))
}

async fn get_document(
    _: AdminAuth,
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<AdminDocumentDetails>, AppError> {
    let document = state.store.get_document(id).await?.ok_or(AppError::NotFound("document"))?;
    let participants = state.rooms.live().await.remove(&id).unwrap_or_default();
    Ok(Json(AdminDocumentDetails {
        document,
        snapshot: state.store.latest_snapshot_info(id).await?,
        members: state.store.list_members(id).await?,
        participants,
    }))
}

async fn delete_document(_: AdminAuth, State(state): State<AppState>, Path(id): Path<Uuid>) -> Result<StatusCode, AppError> {
    apply_delete_document(&state, id, "admin").await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn set_member(
    _: AdminAuth,
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(body): Json<AdminSetMember>,
) -> Result<Json<Member>, AppError> {
    Ok(Json(apply_set_member(&state, id, body.user_name.trim(), body.role).await?))
}

async fn remove_member(
    _: AdminAuth,
    State(state): State<AppState>,
    Path((id, user_id)): Path<(Uuid, Uuid)>,
) -> Result<StatusCode, AppError> {
    apply_remove_member(&state, id, user_id).await?;
    Ok(StatusCode::NO_CONTENT)
}
