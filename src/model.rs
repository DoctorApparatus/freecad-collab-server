use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use strum::{AsRefStr, Display, EnumString};
use uuid::Uuid;

use crate::protocol::Op;

/// Document access level. Ordered: `Viewer < Editor < Owner`.
#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    PartialOrd,
    Ord,
    Hash,
    Serialize,
    Deserialize,
    JsonSchema,
    Display,
    EnumString,
    AsRefStr,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum Role {
    Viewer,
    Editor,
    Owner,
}

/// People use FreeCAD; agents are AI participants driven by a model.
#[derive(
    Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize, JsonSchema, Display, EnumString, AsRefStr,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum UserKind {
    #[default]
    Human,
    Agent,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct User {
    pub id: Uuid,
    pub name: String,
    #[serde(default)]
    pub kind: UserKind,
    pub created_at: i64,
    pub disabled: bool,
}

/// A chat line in a document's conversation (people and agents).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct ChatMessage {
    pub id: Uuid,
    pub session_id: Uuid,
    pub user_id: Uuid,
    pub user_name: String,
    pub user_kind: UserKind,
    pub text: String,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct TokenInfo {
    pub id: Uuid,
    pub user_id: Uuid,
    pub label: Option<String>,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct Document {
    pub id: Uuid,
    pub name: String,
    pub description: Option<String>,
    pub owner_id: Uuid,
    pub created_at: i64,
    pub updated_at: i64,
    /// Sequence number of the most recent committed op batch (0 = empty).
    pub head_seq: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct DocumentWithRole {
    #[serde(flatten)]
    pub document: Document,
    pub role: Role,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct Member {
    pub user_id: Uuid,
    pub user_name: String,
    #[serde(default)]
    pub user_kind: UserKind,
    pub role: Role,
    pub added_at: i64,
}

/// One committed, sequenced batch of operations.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct OpRecord {
    pub seq: i64,
    pub author_id: Uuid,
    pub session_id: Uuid,
    pub client_op_id: String,
    pub ops: Vec<Op>,
    pub created_at: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct SnapshotInfo {
    pub id: Uuid,
    /// The snapshot reflects every op with `seq <= this`.
    pub seq: i64,
    pub author_id: Uuid,
    pub sha256: String,
    pub size_bytes: i64,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct TokenSummary {
    pub id: Uuid,
    pub label: Option<String>,
    pub created_at: i64,
    pub last_used_at: Option<i64>,
    pub revoked_at: Option<i64>,
}

/// Document row for the admin console.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct AdminDocument {
    #[serde(flatten)]
    pub document: Document,
    pub owner_name: String,
    pub member_count: i64,
    pub snapshot_seq: Option<i64>,
    pub snapshot_bytes: Option<i64>,
    /// Filled in from live rooms, not stored.
    #[serde(default)]
    pub live_sessions: usize,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
pub struct Stats {
    pub users: i64,
    pub disabled_users: i64,
    pub documents: i64,
    pub ops: i64,
    pub snapshot_bytes: i64,
}
