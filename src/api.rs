use axum::{
    body::Bytes,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get},
    Json, Router,
};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tower_http::trace::TraceLayer;
use uuid::Uuid;

use crate::{
    auth::{require_role, AuthUser},
    db::NewSnapshot,
    error::AppError,
    model::{Document, DocumentWithRole, Member, OpRecord, Role, SnapshotInfo, User},
    protocol::{ClientMessage, ServerMessage, PROTOCOL_VERSION},
    state::AppState,
};

const ZIP_MAGIC: &[u8] = b"PK\x03\x04";

pub fn router(state: AppState) -> Router {
    let snapshot_limit = state.config.max_snapshot_bytes;
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/v1/protocol", get(protocol_schema))
        .route("/v1/me", get(me))
        .route("/v1/documents", get(list_documents).post(create_document))
        .route("/v1/documents/{id}", get(get_document).delete(delete_document))
        .route("/v1/documents/{id}/members", get(list_members).put(set_member))
        .route("/v1/documents/{id}/members/{user_id}", delete(remove_member))
        .route("/v1/documents/{id}/ops", get(list_ops))
        .route("/v1/documents/{id}/chat", get(list_chat))
        .route(
            "/v1/documents/{id}/snapshot",
            get(get_snapshot)
                .put(put_snapshot)
                .layer(DefaultBodyLimit::max(snapshot_limit)),
        )
        .route("/v1/documents/{id}/ws", get(crate::ws::ws_handler))
        .merge(crate::admin::routes())
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

// ---------------------------------------------------------------- health

async fn healthz() -> &'static str {
    "ok"
}

async fn readyz(State(state): State<AppState>) -> Result<Json<serde_json::Value>, AppError> {
    state.store.ping().await?;
    Ok(Json(json!({
        "status": "ready",
        "active_rooms": state.rooms.active_rooms().await,
    })))
}

async fn protocol_schema() -> Json<serde_json::Value> {
    Json(json!({
        "protocol_version": PROTOCOL_VERSION,
        "client_message": schemars::schema_for!(ClientMessage),
        "server_message": schemars::schema_for!(ServerMessage),
    }))
}

async fn me(AuthUser(user): AuthUser) -> Json<User> {
    Json(user)
}

// ---------------------------------------------------------------- documents

#[derive(Debug, Deserialize, JsonSchema)]
pub struct CreateDocument {
    pub name: String,
    #[serde(default)]
    pub description: Option<String>,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct DocumentDetails {
    #[serde(flatten)]
    pub document: Document,
    pub role: Role,
    pub snapshot: Option<SnapshotInfo>,
    pub members: Vec<Member>,
}

async fn list_documents(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
) -> Result<Json<Vec<DocumentWithRole>>, AppError> {
    Ok(Json(state.store.list_documents_for_user(user.id).await?))
}

async fn create_document(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Json(body): Json<CreateDocument>,
) -> Result<(StatusCode, Json<DocumentWithRole>), AppError> {
    let name = body.name.trim();
    if name.is_empty() || name.len() > 200 {
        return Err(AppError::BadRequest("name must be 1-200 characters".into()));
    }
    let description = body.description.as_deref().map(str::trim).filter(|d| !d.is_empty());
    if description.is_some_and(|d| d.len() > 2000) {
        return Err(AppError::BadRequest("description must be ≤2000 characters".into()));
    }
    let document = state.store.create_document(user.id, name, description).await?;
    tracing::info!(document = %document.id, owner = %user.name, "document created");
    Ok((
        StatusCode::CREATED,
        Json(DocumentWithRole {
            document,
            role: Role::Owner,
        }),
    ))
}

async fn get_document(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<Uuid>,
) -> Result<Json<DocumentDetails>, AppError> {
    let role = require_role(&state, id, &user, Role::Viewer).await?;
    let document = state.store.get_document(id).await?.ok_or(AppError::NotFound("document"))?;
    Ok(Json(DocumentDetails {
        document,
        role,
        snapshot: state.store.latest_snapshot_info(id).await?,
        members: state.store.list_members(id).await?,
    }))
}

async fn delete_document(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<Uuid>,
) -> Result<StatusCode, AppError> {
    require_role(&state, id, &user, Role::Owner).await?;
    apply_delete_document(&state, id, &user.name).await?;
    Ok(StatusCode::NO_CONTENT)
}

pub(crate) async fn apply_delete_document(state: &AppState, id: Uuid, by: &str) -> Result<(), AppError> {
    if !state.store.delete_document(id).await? {
        return Err(AppError::NotFound("document"));
    }
    state
        .rooms
        .terminate(id, "document_deleted", "the document was deleted".into())
        .await;
    tracing::info!(document = %id, by, "document deleted");
    Ok(())
}

// ---------------------------------------------------------------- members

#[derive(Debug, Deserialize, JsonSchema)]
pub struct SetMember {
    pub user_name: String,
    pub role: Role,
}

async fn list_members(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<Uuid>,
) -> Result<Json<Vec<Member>>, AppError> {
    require_role(&state, id, &user, Role::Viewer).await?;
    Ok(Json(state.store.list_members(id).await?))
}

async fn set_member(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<Uuid>,
    Json(body): Json<SetMember>,
) -> Result<Json<Member>, AppError> {
    require_role(&state, id, &user, Role::Owner).await?;
    Ok(Json(apply_set_member(&state, id, &body.user_name, body.role).await?))
}

pub(crate) async fn apply_set_member(state: &AppState, id: Uuid, user_name: &str, role: Role) -> Result<Member, AppError> {
    let document = state.store.get_document(id).await?.ok_or(AppError::NotFound("document"))?;
    let target = state
        .store
        .user_by_name(user_name)
        .await?
        .filter(|u| !u.disabled)
        .ok_or(AppError::NotFound("user"))?;
    if target.id == document.owner_id {
        return Err(AppError::BadRequest("the document creator's role cannot be changed".into()));
    }
    let member = state.store.set_member(id, &target, role).await?;
    state.rooms.member_changed(id, target.id, Some(role)).await;
    Ok(member)
}

async fn remove_member(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path((id, user_id)): Path<(Uuid, Uuid)>,
) -> Result<StatusCode, AppError> {
    // Owners can remove anyone; everyone can remove themselves.
    let min = if user_id == user.id { Role::Viewer } else { Role::Owner };
    require_role(&state, id, &user, min).await?;
    apply_remove_member(&state, id, user_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

pub(crate) async fn apply_remove_member(state: &AppState, id: Uuid, user_id: Uuid) -> Result<(), AppError> {
    let document = state.store.get_document(id).await?.ok_or(AppError::NotFound("document"))?;
    if user_id == document.owner_id {
        return Err(AppError::BadRequest("the document creator cannot be removed".into()));
    }
    if !state.store.remove_member(id, user_id).await? {
        return Err(AppError::NotFound("member"));
    }
    state.rooms.member_changed(id, user_id, None).await;
    Ok(())
}

// ---------------------------------------------------------------- ops

#[derive(Debug, Deserialize)]
pub struct OpsQuery {
    #[serde(default)]
    pub after: i64,
    pub limit: Option<i64>,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct OpsPage {
    pub head_seq: i64,
    pub ops: Vec<OpRecord>,
}

async fn list_ops(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<Uuid>,
    Query(q): Query<OpsQuery>,
) -> Result<Json<OpsPage>, AppError> {
    require_role(&state, id, &user, Role::Viewer).await?;
    let head_seq = state.store.head_seq(id).await?.ok_or(AppError::NotFound("document"))?;
    if q.after < 0 || q.after > head_seq {
        return Err(AppError::BadRequest(format!("after must be within [0, {head_seq}]")));
    }
    if q.after < head_seq {
        let min = state.store.min_op_seq(id).await?;
        if min.is_none_or(|m| q.after + 1 < m) {
            return Err(AppError::Gone(
                "ops in that range were compacted; download the snapshot first".into(),
            ));
        }
    }
    let limit = q.limit.unwrap_or(500).clamp(1, 5000);
    let ops = state.store.ops_after(id, q.after, limit).await?;
    Ok(Json(OpsPage { head_seq, ops }))
}

// ---------------------------------------------------------------- chat

#[derive(Debug, Deserialize)]
pub struct ChatQuery {
    pub limit: Option<i64>,
}

async fn list_chat(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<Uuid>,
    Query(q): Query<ChatQuery>,
) -> Result<Json<Vec<crate::model::ChatMessage>>, AppError> {
    require_role(&state, id, &user, Role::Viewer).await?;
    let limit = q.limit.unwrap_or(100).clamp(1, 1000);
    Ok(Json(state.store.recent_chat(id, limit).await?))
}

// ---------------------------------------------------------------- snapshots

#[derive(Debug, Deserialize)]
pub struct SnapshotQuery {
    pub seq: i64,
}

fn etag(info: &SnapshotInfo) -> String {
    format!("\"{}\"", info.sha256)
}

async fn get_snapshot(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, AppError> {
    require_role(&state, id, &user, Role::Viewer).await?;
    let info = state
        .store
        .latest_snapshot_info(id)
        .await?
        .ok_or(AppError::NotFound("snapshot"))?;
    let tag = etag(&info);

    let mut out = HeaderMap::new();
    out.insert(header::ETAG, HeaderValue::from_str(&tag).map_err(anyhow::Error::from)?);
    out.insert("x-snapshot-seq", HeaderValue::from(info.seq));
    out.insert(
        "x-snapshot-id",
        HeaderValue::from_str(&info.id.to_string()).map_err(anyhow::Error::from)?,
    );

    if headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v == tag)
    {
        return Ok((StatusCode::NOT_MODIFIED, out).into_response());
    }

    let data = state
        .store
        .snapshot_data(info.id)
        .await?
        .ok_or(AppError::NotFound("snapshot"))?;
    out.insert(header::CONTENT_TYPE, HeaderValue::from_static("application/zip"));
    out.insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_str(&format!("attachment; filename=\"{id}.FCStd\"")).map_err(anyhow::Error::from)?,
    );
    Ok((StatusCode::OK, out, data).into_response())
}

async fn put_snapshot(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<Uuid>,
    Query(q): Query<SnapshotQuery>,
    body: Bytes,
) -> Result<(StatusCode, Json<SnapshotInfo>), AppError> {
    require_role(&state, id, &user, Role::Editor).await?;
    if !body.starts_with(ZIP_MAGIC) {
        return Err(AppError::BadRequest("body must be an .FCStd (zip) archive".into()));
    }
    let head = state.store.head_seq(id).await?.ok_or(AppError::NotFound("document"))?;
    if q.seq < 0 || q.seq > head {
        return Err(AppError::BadRequest(format!("seq must be within [0, {head}]")));
    }
    if let Some(latest) = state.store.latest_snapshot_info(id).await? {
        if latest.seq > q.seq {
            return Err(AppError::Conflict(format!(
                "a newer snapshot exists (seq {})",
                latest.seq
            )));
        }
    }
    let info = state
        .store
        .put_snapshot(NewSnapshot {
            document_id: id,
            seq: q.seq,
            author_id: user.id,
            data: body.to_vec(),
        })
        .await?;
    tracing::info!(document = %id, seq = info.seq, bytes = info.size_bytes, "snapshot stored");
    state
        .rooms
        .broadcast(id, &ServerMessage::SnapshotAvailable { snapshot: info.clone() })
        .await;
    Ok((StatusCode::CREATED, Json(info)))
}
