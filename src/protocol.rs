//! Wire protocol spoken over `/v1/documents/{id}/ws`.
//!
//! All frames are JSON text frames. Client frames are [`ClientMessage`],
//! server frames are [`ServerMessage`]; both are tagged by a `type` field.
//! The JSON schema is served at `GET /v1/protocol`.

use std::collections::BTreeMap;

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use strum::{AsRefStr, Display};
use uuid::Uuid;

use crate::{
    model::{ChatMessage, OpRecord, Role, SnapshotInfo, UserKind},
    util::is_identifier,
};

pub const PROTOCOL_VERSION: u32 = 1;

/// Wildcard property key used to represent "the object itself" in the
/// conflict index (creation / deletion).
pub const OBJECT_KEY: &str = "*";

/// Conflict-index key: `(object name, property key)`.
pub type ConflictKey = (String, String);

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct PropertyValue {
    /// FreeCAD property type id, e.g. `App::PropertyLength`.
    pub type_id: String,
    /// Plugin-defined JSON encoding of the value. Opaque to the server.
    pub value: serde_json::Value,
}

/// A single object-level document mutation. Objects are addressed by their
/// FreeCAD internal `Name`, which is stable for the lifetime of the object.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Op {
    AddObject {
        name: String,
        type_id: String,
        #[serde(default)]
        label: Option<String>,
        #[serde(default)]
        properties: BTreeMap<String, PropertyValue>,
    },
    RemoveObject {
        name: String,
    },
    SetLabel {
        name: String,
        label: String,
    },
    SetProperty {
        object: String,
        property: String,
        value: PropertyValue,
    },
    /// Adds a dynamic property (`obj.addProperty(...)`).
    AddProperty {
        object: String,
        property: String,
        type_id: String,
        #[serde(default)]
        group: Option<String>,
        #[serde(default)]
        doc: Option<String>,
    },
    RemoveProperty {
        object: String,
        property: String,
    },
    /// Sets (`Some`) or clears (`None`) an expression binding.
    SetExpression {
        object: String,
        path: String,
        expression: Option<String>,
    },
}

#[derive(Debug, thiserror::Error, PartialEq)]
pub enum OpValidationError {
    #[error("`{0}` is not a valid FreeCAD identifier")]
    InvalidIdentifier(String),
    #[error("field `{0}` is empty or too long")]
    BadLength(&'static str),
    #[error("field `{0}` contains invalid characters")]
    BadCharacters(&'static str),
    #[error("property value is larger than {0} bytes")]
    ValueTooLarge(usize),
}

fn check_ident(s: &str) -> Result<(), OpValidationError> {
    if is_identifier(s) {
        Ok(())
    } else {
        Err(OpValidationError::InvalidIdentifier(s.to_string()))
    }
}

fn check_type_id(s: &str) -> Result<(), OpValidationError> {
    if s.is_empty() || s.len() > 128 {
        return Err(OpValidationError::BadLength("type_id"));
    }
    if !s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == ':') {
        return Err(OpValidationError::BadCharacters("type_id"));
    }
    Ok(())
}

fn check_len(field: &'static str, s: &str, max: usize) -> Result<(), OpValidationError> {
    if s.len() > max {
        Err(OpValidationError::BadLength(field))
    } else {
        Ok(())
    }
}

fn check_value(v: &PropertyValue, max_bytes: usize) -> Result<(), OpValidationError> {
    check_type_id(&v.type_id)?;
    let size = serde_json::to_vec(&v.value).map(|b| b.len()).unwrap_or(usize::MAX);
    if size > max_bytes {
        return Err(OpValidationError::ValueTooLarge(max_bytes));
    }
    Ok(())
}

fn check_path(s: &str) -> Result<(), OpValidationError> {
    if s.is_empty() || s.len() > 256 {
        return Err(OpValidationError::BadLength("path"));
    }
    if !s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '[' | ']')) {
        return Err(OpValidationError::BadCharacters("path"));
    }
    Ok(())
}

impl Op {
    /// Internal name of the object this op targets.
    pub fn object(&self) -> &str {
        match self {
            Op::AddObject { name, .. } | Op::RemoveObject { name } | Op::SetLabel { name, .. } => name,
            Op::SetProperty { object, .. }
            | Op::AddProperty { object, .. }
            | Op::RemoveProperty { object, .. }
            | Op::SetExpression { object, .. } => object,
        }
    }

    /// The conflict key this op writes.
    pub fn write_key(&self) -> ConflictKey {
        let obj = self.object().to_string();
        let prop = match self {
            Op::AddObject { .. } | Op::RemoveObject { .. } => OBJECT_KEY.to_string(),
            Op::SetLabel { .. } => "Label".to_string(),
            Op::SetProperty { property, .. }
            | Op::AddProperty { property, .. }
            | Op::RemoveProperty { property, .. } => property.clone(),
            Op::SetExpression { path, .. } => format!("expr:{path}"),
        };
        (obj, prop)
    }

    /// Keys whose concurrent modification conflicts with this op: its own
    /// write key plus the object's existence key.
    pub fn read_keys(&self) -> Vec<ConflictKey> {
        let write = self.write_key();
        if write.1 == OBJECT_KEY {
            vec![write]
        } else {
            let existence = (write.0.clone(), OBJECT_KEY.to_string());
            vec![write, existence]
        }
    }

    pub fn validate(&self, max_value_bytes: usize) -> Result<(), OpValidationError> {
        check_ident(self.object())?;
        match self {
            Op::AddObject {
                type_id,
                label,
                properties,
                ..
            } => {
                check_type_id(type_id)?;
                if let Some(l) = label {
                    check_len("label", l, 512)?;
                }
                if properties.len() > 512 {
                    return Err(OpValidationError::BadLength("properties"));
                }
                for (k, v) in properties {
                    check_ident(k)?;
                    check_value(v, max_value_bytes)?;
                }
            }
            Op::RemoveObject { .. } => {}
            Op::SetLabel { label, .. } => check_len("label", label, 512)?,
            Op::SetProperty { property, value, .. } => {
                check_ident(property)?;
                check_value(value, max_value_bytes)?;
            }
            Op::AddProperty {
                property,
                type_id,
                group,
                doc,
                ..
            } => {
                check_ident(property)?;
                check_type_id(type_id)?;
                if let Some(g) = group {
                    check_len("group", g, 128)?;
                }
                if let Some(d) = doc {
                    check_len("doc", d, 2048)?;
                }
            }
            Op::RemoveProperty { property, .. } => check_ident(property)?,
            Op::SetExpression { path, expression, .. } => {
                check_path(path)?;
                if let Some(e) = expression {
                    check_len("expression", e, 4096)?;
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientMessage {
    /// Must be the first frame. `last_seen_seq` is the highest seq the client
    /// has applied (from ops or a snapshot); omit to start from the latest
    /// snapshot.
    Hello {
        protocol_version: u32,
        client: String,
        #[serde(default)]
        last_seen_seq: Option<i64>,
    },
    /// Propose ops. `base_seq` is the highest seq applied locally when the
    /// ops were produced. `client_op_id` must be unique per user+document and
    /// makes resubmission after reconnect idempotent.
    Submit {
        client_op_id: String,
        base_seq: i64,
        ops: Vec<Op>,
    },
    /// Acquire exclusive edit locks (all-or-nothing).
    Lock { objects: Vec<String> },
    Unlock { objects: Vec<String> },
    /// Ephemeral awareness data; not persisted.
    Presence {
        #[serde(default)]
        selection: Vec<String>,
        #[serde(default)]
        camera: Option<serde_json::Value>,
    },
    Ping { nonce: u64 },
    /// Post to the document's chat (1-16000 characters). Stored and
    /// broadcast to everyone, including the sender.
    Chat { text: String },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Participant {
    pub session_id: Uuid,
    pub user_id: Uuid,
    pub user_name: String,
    #[serde(default)]
    pub kind: UserKind,
    pub role: Role,
    pub client: String,
    /// Stable per-session highlight color (`#rrggbb`).
    pub color: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct LockInfo {
    pub object: String,
    pub session_id: Uuid,
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema, Display, AsRefStr,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum RejectReason {
    /// Your role does not allow editing.
    Forbidden,
    /// An op failed validation.
    InvalidOp,
    /// `base_seq` is negative or ahead of the server.
    InvalidBaseSeq,
    /// Another session changed something you touched after `base_seq`.
    /// Apply pending remote ops, rebase, and resubmit with a new id.
    Conflict,
    /// A touched object is locked by another session.
    LockedByOther,
    /// Too many ops in one submit.
    TooLarge,
    /// Server-side failure; safe to retry with the same `client_op_id`.
    Internal,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerMessage {
    Welcome {
        protocol_version: u32,
        session_id: Uuid,
        you: Participant,
        head_seq: i64,
        snapshot: Option<SnapshotInfo>,
        participants: Vec<Participant>,
        locks: Vec<LockInfo>,
        /// Recent chat, oldest first.
        chat: Vec<ChatMessage>,
    },
    /// A committed batch by another session. Apply in `seq` order.
    Ops { batch: OpRecord },
    /// Catch-up after `hello` finished; live ops follow.
    CatchUpComplete { head_seq: i64 },
    /// The client is too far behind: download the snapshot, then reconnect
    /// with `last_seen_seq = snapshot.seq`. The socket closes with 4000.
    ResyncRequired {
        head_seq: i64,
        snapshot: Option<SnapshotInfo>,
    },
    Ack { client_op_id: String, seq: i64 },
    Reject {
        client_op_id: String,
        reason: RejectReason,
        message: String,
        head_seq: i64,
    },
    Locked { objects: Vec<String>, session_id: Uuid },
    Unlocked { objects: Vec<String>, session_id: Uuid },
    LockDenied {
        objects: Vec<String>,
        held_by: Vec<LockInfo>,
    },
    Presence {
        session_id: Uuid,
        selection: Vec<String>,
        camera: Option<serde_json::Value>,
    },
    Joined { participant: Participant },
    Left { session_id: Uuid },
    RoleChanged { role: Role },
    SnapshotAvailable { snapshot: SnapshotInfo },
    Error { code: String, message: String },
    Pong { nonce: u64 },
    Chat { message: ChatMessage },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn op_round_trip_and_keys() {
        let json = r#"{"op":"set_property","object":"Box","property":"Length",
            "value":{"type_id":"App::PropertyLength","value":10.0}}"#;
        let op: Op = serde_json::from_str(json).unwrap();
        assert_eq!(op.object(), "Box");
        assert_eq!(op.write_key(), ("Box".into(), "Length".into()));
        assert_eq!(op.read_keys().len(), 2);
        op.validate(1024).unwrap();
    }

    #[test]
    fn rejects_bad_identifiers() {
        let op = Op::RemoveObject { name: "1bad".into() };
        assert!(op.validate(1024).is_err());
    }

    #[test]
    fn rejects_large_values() {
        let op = Op::SetProperty {
            object: "Box".into(),
            property: "Data".into(),
            value: PropertyValue {
                type_id: "App::PropertyString".into(),
                value: serde_json::Value::String("x".repeat(100)),
            },
        };
        assert_eq!(op.validate(10), Err(OpValidationError::ValueTooLarge(10)));
    }
}
