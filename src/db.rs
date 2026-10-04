use std::{str::FromStr, time::Duration};

use async_trait::async_trait;
use sqlx::{
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteRow, SqliteSynchronous},
    Row, SqlitePool,
};
use uuid::Uuid;

use crate::{
    model::{
        AdminDocument, ChatMessage, Document, DocumentWithRole, Member, OpRecord, Role, SnapshotInfo, Stats,
        TokenInfo, TokenSummary, User, UserKind,
    },
    protocol::Op,
    util::{now_ms, sha256_hex},
};

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("{0} already exists")]
    AlreadyExists(&'static str),
    #[error("{0} not found")]
    NotFound(&'static str),
    #[error("corrupt data in database: {0}")]
    Corrupt(String),
    #[error(transparent)]
    Sqlx(#[from] sqlx::Error),
}

pub type StoreResult<T> = Result<T, StoreError>;

pub struct NewOps {
    pub document_id: Uuid,
    pub author_id: Uuid,
    pub session_id: Uuid,
    pub client_op_id: String,
    pub ops: Vec<Op>,
}

pub struct NewSnapshot {
    pub document_id: Uuid,
    pub seq: i64,
    pub author_id: Uuid,
    pub data: Vec<u8>,
}

/// Persistence boundary. Everything the server stores goes through here.
#[async_trait]
pub trait Store: Send + Sync + 'static {
    async fn ping(&self) -> StoreResult<()>;

    async fn create_user(&self, name: &str, kind: UserKind) -> StoreResult<User>;
    async fn user_by_name(&self, name: &str) -> StoreResult<Option<User>>;
    async fn user_by_id(&self, user_id: Uuid) -> StoreResult<Option<User>>;
    async fn list_users(&self) -> StoreResult<Vec<User>>;
    async fn set_user_disabled(&self, user_id: Uuid, disabled: bool) -> StoreResult<bool>;

    async fn create_token(&self, user_id: Uuid, token_hash: &str, label: Option<&str>) -> StoreResult<TokenInfo>;
    async fn user_by_token_hash(&self, token_hash: &str) -> StoreResult<Option<User>>;
    async fn revoke_token(&self, token_id: Uuid) -> StoreResult<bool>;
    async fn list_tokens(&self, user_id: Uuid) -> StoreResult<Vec<TokenSummary>>;
    async fn list_all_documents(&self) -> StoreResult<Vec<AdminDocument>>;
    async fn stats(&self) -> StoreResult<Stats>;

    async fn create_document(&self, owner_id: Uuid, name: &str, description: Option<&str>) -> StoreResult<Document>;
    async fn get_document(&self, document_id: Uuid) -> StoreResult<Option<Document>>;
    async fn list_documents_for_user(&self, user_id: Uuid) -> StoreResult<Vec<DocumentWithRole>>;
    async fn delete_document(&self, document_id: Uuid) -> StoreResult<bool>;

    async fn member_role(&self, document_id: Uuid, user_id: Uuid) -> StoreResult<Option<Role>>;
    async fn set_member(&self, document_id: Uuid, user: &User, role: Role) -> StoreResult<Member>;
    async fn remove_member(&self, document_id: Uuid, user_id: Uuid) -> StoreResult<bool>;
    async fn list_members(&self, document_id: Uuid) -> StoreResult<Vec<Member>>;

    async fn head_seq(&self, document_id: Uuid) -> StoreResult<Option<i64>>;
    async fn find_op_seq(&self, document_id: Uuid, author_id: Uuid, client_op_id: &str) -> StoreResult<Option<i64>>;
    async fn append_ops(&self, new: NewOps) -> StoreResult<OpRecord>;
    async fn ops_after(&self, document_id: Uuid, after: i64, limit: i64) -> StoreResult<Vec<OpRecord>>;
    async fn min_op_seq(&self, document_id: Uuid) -> StoreResult<Option<i64>>;

    async fn put_snapshot(&self, new: NewSnapshot) -> StoreResult<SnapshotInfo>;
    async fn latest_snapshot_info(&self, document_id: Uuid) -> StoreResult<Option<SnapshotInfo>>;
    async fn snapshot_data(&self, snapshot_id: Uuid) -> StoreResult<Option<Vec<u8>>>;

    async fn add_chat(&self, document_id: Uuid, msg: &ChatMessage) -> StoreResult<()>;
    /// Most recent messages, oldest first.
    async fn recent_chat(&self, document_id: Uuid, limit: i64) -> StoreResult<Vec<ChatMessage>>;
}

/// Chat lines kept per document; older ones are pruned.
const CHAT_RETENTION: i64 = 5000;

pub async fn connect(url: &str, max_connections: u32) -> anyhow::Result<SqlitePool> {
    let opts = SqliteConnectOptions::from_str(url)?
        .create_if_missing(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Normal)
        .busy_timeout(Duration::from_secs(10))
        .foreign_keys(true);

    if let Some(parent) = opts.get_filename().parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }

    let pool = SqlitePoolOptions::new()
        .max_connections(max_connections.max(1))
        .acquire_timeout(Duration::from_secs(10))
        .connect_with(opts)
        .await?;
    sqlx::migrate!("./migrations").run(&pool).await?;
    Ok(pool)
}

pub struct SqliteStore {
    pool: SqlitePool,
    snapshot_retention: i64,
}

impl SqliteStore {
    pub fn new(pool: SqlitePool, snapshot_retention: i64) -> Self {
        Self {
            pool,
            snapshot_retention,
        }
    }
}

fn uuid_col(row: &SqliteRow, col: &str) -> StoreResult<Uuid> {
    let raw: String = row.try_get(col)?;
    Uuid::parse_str(&raw).map_err(|e| StoreError::Corrupt(format!("{col}: {e}")))
}

fn role_col(row: &SqliteRow, col: &str) -> StoreResult<Role> {
    let raw: String = row.try_get(col)?;
    Role::from_str(&raw).map_err(|e| StoreError::Corrupt(format!("{col}: {e}")))
}

fn kind_col(row: &SqliteRow, col: &str) -> StoreResult<UserKind> {
    let raw: String = row.try_get(col)?;
    UserKind::from_str(&raw).map_err(|e| StoreError::Corrupt(format!("{col}: {e}")))
}

fn user_from_row(row: &SqliteRow) -> StoreResult<User> {
    Ok(User {
        id: uuid_col(row, "id")?,
        name: row.try_get("name")?,
        kind: kind_col(row, "kind")?,
        created_at: row.try_get("created_at")?,
        disabled: row.try_get::<i64, _>("disabled")? != 0,
    })
}

fn document_from_row(row: &SqliteRow) -> StoreResult<Document> {
    Ok(Document {
        id: uuid_col(row, "id")?,
        name: row.try_get("name")?,
        description: row.try_get("description")?,
        owner_id: uuid_col(row, "owner_id")?,
        created_at: row.try_get("created_at")?,
        updated_at: row.try_get("updated_at")?,
        head_seq: row.try_get("head_seq")?,
    })
}

fn op_from_row(row: &SqliteRow) -> StoreResult<OpRecord> {
    let payload: String = row.try_get("payload")?;
    let ops: Vec<Op> =
        serde_json::from_str(&payload).map_err(|e| StoreError::Corrupt(format!("ops payload: {e}")))?;
    Ok(OpRecord {
        seq: row.try_get("seq")?,
        author_id: uuid_col(row, "author_id")?,
        session_id: uuid_col(row, "session_id")?,
        client_op_id: row.try_get("client_op_id")?,
        ops,
        created_at: row.try_get("created_at")?,
    })
}

fn snapshot_from_row(row: &SqliteRow) -> StoreResult<SnapshotInfo> {
    Ok(SnapshotInfo {
        id: uuid_col(row, "id")?,
        seq: row.try_get("seq")?,
        author_id: uuid_col(row, "author_id")?,
        sha256: row.try_get("sha256")?,
        size_bytes: row.try_get("size_bytes")?,
        created_at: row.try_get("created_at")?,
    })
}

fn is_unique_violation(e: &sqlx::Error) -> bool {
    matches!(e, sqlx::Error::Database(db) if db.is_unique_violation())
}

const USER_COLS: &str = "id, name, kind, created_at, disabled";
const DOC_COLS: &str = "d.id, d.name, d.description, d.owner_id, d.created_at, d.updated_at, d.head_seq";
const SNAPSHOT_COLS: &str = "id, seq, author_id, sha256, size_bytes, created_at";

#[async_trait]
impl Store for SqliteStore {
    async fn ping(&self) -> StoreResult<()> {
        sqlx::query("SELECT 1").execute(&self.pool).await?;
        Ok(())
    }

    async fn create_user(&self, name: &str, kind: UserKind) -> StoreResult<User> {
        let user = User {
            id: Uuid::new_v4(),
            name: name.to_string(),
            kind,
            created_at: now_ms(),
            disabled: false,
        };
        sqlx::query("INSERT INTO users (id, name, kind, created_at, disabled) VALUES (?, ?, ?, ?, 0)")
            .bind(user.id.to_string())
            .bind(&user.name)
            .bind(kind.as_ref())
            .bind(user.created_at)
            .execute(&self.pool)
            .await
            .map_err(|e| {
                if is_unique_violation(&e) {
                    StoreError::AlreadyExists("user")
                } else {
                    e.into()
                }
            })?;
        Ok(user)
    }

    async fn user_by_name(&self, name: &str) -> StoreResult<Option<User>> {
        let row = sqlx::query(&format!("SELECT {USER_COLS} FROM users WHERE name = ?"))
            .bind(name)
            .fetch_optional(&self.pool)
            .await?;
        row.as_ref().map(user_from_row).transpose()
    }

    async fn user_by_id(&self, user_id: Uuid) -> StoreResult<Option<User>> {
        let row = sqlx::query(&format!("SELECT {USER_COLS} FROM users WHERE id = ?"))
            .bind(user_id.to_string())
            .fetch_optional(&self.pool)
            .await?;
        row.as_ref().map(user_from_row).transpose()
    }

    async fn list_tokens(&self, user_id: Uuid) -> StoreResult<Vec<TokenSummary>> {
        let rows = sqlx::query(
            "SELECT id, label, created_at, last_used_at, revoked_at FROM api_tokens
             WHERE user_id = ? ORDER BY created_at DESC",
        )
        .bind(user_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        rows.iter()
            .map(|r| {
                Ok(TokenSummary {
                    id: uuid_col(r, "id")?,
                    label: r.try_get("label")?,
                    created_at: r.try_get("created_at")?,
                    last_used_at: r.try_get("last_used_at")?,
                    revoked_at: r.try_get("revoked_at")?,
                })
            })
            .collect()
    }

    async fn list_all_documents(&self) -> StoreResult<Vec<AdminDocument>> {
        let rows = sqlx::query(&format!(
            "SELECT {DOC_COLS}, u.name AS owner_name,
                (SELECT COUNT(*) FROM document_members m WHERE m.document_id = d.id) AS member_count,
                (SELECT s.seq FROM snapshots s WHERE s.document_id = d.id
                    ORDER BY s.seq DESC, s.created_at DESC LIMIT 1) AS snapshot_seq,
                (SELECT s.size_bytes FROM snapshots s WHERE s.document_id = d.id
                    ORDER BY s.seq DESC, s.created_at DESC LIMIT 1) AS snapshot_bytes
             FROM documents d JOIN users u ON u.id = d.owner_id
             ORDER BY d.updated_at DESC"
        ))
        .fetch_all(&self.pool)
        .await?;
        rows.iter()
            .map(|r| {
                Ok(AdminDocument {
                    document: document_from_row(r)?,
                    owner_name: r.try_get("owner_name")?,
                    member_count: r.try_get("member_count")?,
                    snapshot_seq: r.try_get("snapshot_seq")?,
                    snapshot_bytes: r.try_get("snapshot_bytes")?,
                    live_sessions: 0,
                })
            })
            .collect()
    }

    async fn stats(&self) -> StoreResult<Stats> {
        let r = sqlx::query(
            "SELECT (SELECT COUNT(*) FROM users) AS users,
                    (SELECT COUNT(*) FROM users WHERE disabled = 1) AS disabled_users,
                    (SELECT COUNT(*) FROM documents) AS documents,
                    (SELECT COUNT(*) FROM ops) AS ops,
                    (SELECT COALESCE(SUM(size_bytes), 0) FROM snapshots) AS snapshot_bytes",
        )
        .fetch_one(&self.pool)
        .await?;
        Ok(Stats {
            users: r.try_get("users")?,
            disabled_users: r.try_get("disabled_users")?,
            documents: r.try_get("documents")?,
            ops: r.try_get("ops")?,
            snapshot_bytes: r.try_get("snapshot_bytes")?,
        })
    }

    async fn list_users(&self) -> StoreResult<Vec<User>> {
        let rows = sqlx::query(&format!("SELECT {USER_COLS} FROM users ORDER BY name"))
            .fetch_all(&self.pool)
            .await?;
        rows.iter().map(user_from_row).collect()
    }

    async fn set_user_disabled(&self, user_id: Uuid, disabled: bool) -> StoreResult<bool> {
        let res = sqlx::query("UPDATE users SET disabled = ? WHERE id = ?")
            .bind(disabled as i64)
            .bind(user_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(res.rows_affected() > 0)
    }

    async fn create_token(&self, user_id: Uuid, token_hash: &str, label: Option<&str>) -> StoreResult<TokenInfo> {
        let info = TokenInfo {
            id: Uuid::new_v4(),
            user_id,
            label: label.map(str::to_string),
            created_at: now_ms(),
        };
        sqlx::query(
            "INSERT INTO api_tokens (id, user_id, token_hash, label, created_at) VALUES (?, ?, ?, ?, ?)",
        )
        .bind(info.id.to_string())
        .bind(user_id.to_string())
        .bind(token_hash)
        .bind(&info.label)
        .bind(info.created_at)
        .execute(&self.pool)
        .await
        .map_err(|e| match &e {
            sqlx::Error::Database(db) if db.is_foreign_key_violation() => StoreError::NotFound("user"),
            _ => e.into(),
        })?;
        Ok(info)
    }

    async fn user_by_token_hash(&self, token_hash: &str) -> StoreResult<Option<User>> {
        let row = sqlx::query(
            "SELECT u.id, u.name, u.kind, u.created_at, u.disabled
             FROM api_tokens t JOIN users u ON u.id = t.user_id
             WHERE t.token_hash = ? AND t.revoked_at IS NULL AND u.disabled = 0",
        )
        .bind(token_hash)
        .fetch_optional(&self.pool)
        .await?;
        let Some(row) = row else { return Ok(None) };
        let user = user_from_row(&row)?;

        // Throttled bookkeeping: at most one write per token per minute.
        let now = now_ms();
        sqlx::query(
            "UPDATE api_tokens SET last_used_at = ?
             WHERE token_hash = ? AND (last_used_at IS NULL OR last_used_at < ?)",
        )
        .bind(now)
        .bind(token_hash)
        .bind(now - 60_000)
        .execute(&self.pool)
        .await?;
        Ok(Some(user))
    }

    async fn revoke_token(&self, token_id: Uuid) -> StoreResult<bool> {
        let res = sqlx::query("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL")
            .bind(now_ms())
            .bind(token_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(res.rows_affected() > 0)
    }

    async fn create_document(&self, owner_id: Uuid, name: &str, description: Option<&str>) -> StoreResult<Document> {
        let now = now_ms();
        let doc = Document {
            id: Uuid::new_v4(),
            name: name.to_string(),
            description: description.map(str::to_string),
            owner_id,
            created_at: now,
            updated_at: now,
            head_seq: 0,
        };
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO documents (id, name, description, owner_id, created_at, updated_at, head_seq)
             VALUES (?, ?, ?, ?, ?, ?, 0)",
        )
        .bind(doc.id.to_string())
        .bind(&doc.name)
        .bind(&doc.description)
        .bind(owner_id.to_string())
        .bind(now)
        .bind(now)
        .execute(&mut *tx)
        .await?;
        sqlx::query("INSERT INTO document_members (document_id, user_id, role, added_at) VALUES (?, ?, ?, ?)")
            .bind(doc.id.to_string())
            .bind(owner_id.to_string())
            .bind(Role::Owner.as_ref())
            .bind(now)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(doc)
    }

    async fn get_document(&self, document_id: Uuid) -> StoreResult<Option<Document>> {
        let row = sqlx::query(&format!("SELECT {DOC_COLS} FROM documents d WHERE d.id = ?"))
            .bind(document_id.to_string())
            .fetch_optional(&self.pool)
            .await?;
        row.as_ref().map(document_from_row).transpose()
    }

    async fn list_documents_for_user(&self, user_id: Uuid) -> StoreResult<Vec<DocumentWithRole>> {
        let rows = sqlx::query(&format!(
            "SELECT {DOC_COLS}, m.role FROM documents d
             JOIN document_members m ON m.document_id = d.id
             WHERE m.user_id = ? ORDER BY d.updated_at DESC"
        ))
        .bind(user_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        rows.iter()
            .map(|r| {
                Ok(DocumentWithRole {
                    document: document_from_row(r)?,
                    role: role_col(r, "role")?,
                })
            })
            .collect()
    }

    async fn delete_document(&self, document_id: Uuid) -> StoreResult<bool> {
        let res = sqlx::query("DELETE FROM documents WHERE id = ?")
            .bind(document_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(res.rows_affected() > 0)
    }

    async fn member_role(&self, document_id: Uuid, user_id: Uuid) -> StoreResult<Option<Role>> {
        let row = sqlx::query("SELECT role FROM document_members WHERE document_id = ? AND user_id = ?")
            .bind(document_id.to_string())
            .bind(user_id.to_string())
            .fetch_optional(&self.pool)
            .await?;
        row.as_ref().map(|r| role_col(r, "role")).transpose()
    }

    async fn set_member(&self, document_id: Uuid, user: &User, role: Role) -> StoreResult<Member> {
        let now = now_ms();
        let row = sqlx::query(
            "INSERT INTO document_members (document_id, user_id, role, added_at) VALUES (?, ?, ?, ?)
             ON CONFLICT (document_id, user_id) DO UPDATE SET role = excluded.role
             RETURNING added_at",
        )
        .bind(document_id.to_string())
        .bind(user.id.to_string())
        .bind(role.as_ref())
        .bind(now)
        .fetch_one(&self.pool)
        .await?;
        Ok(Member {
            user_id: user.id,
            user_name: user.name.clone(),
            user_kind: user.kind,
            role,
            added_at: row.try_get("added_at")?,
        })
    }

    async fn remove_member(&self, document_id: Uuid, user_id: Uuid) -> StoreResult<bool> {
        let res = sqlx::query("DELETE FROM document_members WHERE document_id = ? AND user_id = ?")
            .bind(document_id.to_string())
            .bind(user_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(res.rows_affected() > 0)
    }

    async fn list_members(&self, document_id: Uuid) -> StoreResult<Vec<Member>> {
        let rows = sqlx::query(
            "SELECT m.user_id, u.name, u.kind, m.role, m.added_at FROM document_members m
             JOIN users u ON u.id = m.user_id WHERE m.document_id = ? ORDER BY u.name",
        )
        .bind(document_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        rows.iter()
            .map(|r| {
                Ok(Member {
                    user_id: uuid_col(r, "user_id")?,
                    user_name: r.try_get("name")?,
                    user_kind: kind_col(r, "kind")?,
                    role: role_col(r, "role")?,
                    added_at: r.try_get("added_at")?,
                })
            })
            .collect()
    }

    async fn head_seq(&self, document_id: Uuid) -> StoreResult<Option<i64>> {
        let row = sqlx::query("SELECT head_seq FROM documents WHERE id = ?")
            .bind(document_id.to_string())
            .fetch_optional(&self.pool)
            .await?;
        Ok(row.map(|r| r.try_get("head_seq")).transpose()?)
    }

    async fn find_op_seq(&self, document_id: Uuid, author_id: Uuid, client_op_id: &str) -> StoreResult<Option<i64>> {
        let row = sqlx::query("SELECT seq FROM ops WHERE document_id = ? AND author_id = ? AND client_op_id = ?")
            .bind(document_id.to_string())
            .bind(author_id.to_string())
            .bind(client_op_id)
            .fetch_optional(&self.pool)
            .await?;
        Ok(row.map(|r| r.try_get("seq")).transpose()?)
    }

    async fn append_ops(&self, new: NewOps) -> StoreResult<OpRecord> {
        let now = now_ms();
        let payload = serde_json::to_string(&new.ops).map_err(|e| StoreError::Corrupt(e.to_string()))?;
        let mut tx = self.pool.begin().await?;
        let row = sqlx::query(
            "UPDATE documents SET head_seq = head_seq + 1, updated_at = ? WHERE id = ? RETURNING head_seq",
        )
        .bind(now)
        .bind(new.document_id.to_string())
        .fetch_optional(&mut *tx)
        .await?
        .ok_or(StoreError::NotFound("document"))?;
        let seq: i64 = row.try_get("head_seq")?;

        sqlx::query(
            "INSERT INTO ops (document_id, seq, author_id, session_id, client_op_id, payload, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(new.document_id.to_string())
        .bind(seq)
        .bind(new.author_id.to_string())
        .bind(new.session_id.to_string())
        .bind(&new.client_op_id)
        .bind(&payload)
        .bind(now)
        .execute(&mut *tx)
        .await
        .map_err(|e| {
            if is_unique_violation(&e) {
                StoreError::AlreadyExists("client_op_id")
            } else {
                e.into()
            }
        })?;
        tx.commit().await?;

        Ok(OpRecord {
            seq,
            author_id: new.author_id,
            session_id: new.session_id,
            client_op_id: new.client_op_id,
            ops: new.ops,
            created_at: now,
        })
    }

    async fn ops_after(&self, document_id: Uuid, after: i64, limit: i64) -> StoreResult<Vec<OpRecord>> {
        let rows = sqlx::query(
            "SELECT seq, author_id, session_id, client_op_id, payload, created_at FROM ops
             WHERE document_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
        )
        .bind(document_id.to_string())
        .bind(after)
        .bind(limit)
        .fetch_all(&self.pool)
        .await?;
        rows.iter().map(op_from_row).collect()
    }

    async fn min_op_seq(&self, document_id: Uuid) -> StoreResult<Option<i64>> {
        let row = sqlx::query("SELECT MIN(seq) AS min_seq FROM ops WHERE document_id = ?")
            .bind(document_id.to_string())
            .fetch_one(&self.pool)
            .await?;
        Ok(row.try_get("min_seq")?)
    }

    async fn put_snapshot(&self, new: NewSnapshot) -> StoreResult<SnapshotInfo> {
        let info = SnapshotInfo {
            id: Uuid::new_v4(),
            seq: new.seq,
            author_id: new.author_id,
            sha256: sha256_hex(&new.data),
            size_bytes: new.data.len() as i64,
            created_at: now_ms(),
        };
        let doc = new.document_id.to_string();
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO snapshots (id, document_id, seq, author_id, sha256, size_bytes, data, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(info.id.to_string())
        .bind(&doc)
        .bind(info.seq)
        .bind(info.author_id.to_string())
        .bind(&info.sha256)
        .bind(info.size_bytes)
        .bind(&new.data)
        .bind(info.created_at)
        .execute(&mut *tx)
        .await?;

        if self.snapshot_retention > 0 {
            // Keep the newest N snapshots; compact ops fully covered by the
            // oldest one we keep.
            let oldest_kept = sqlx::query(
                "SELECT seq FROM snapshots WHERE document_id = ?
                 ORDER BY seq DESC, created_at DESC LIMIT 1 OFFSET ?",
            )
            .bind(&doc)
            .bind(self.snapshot_retention - 1)
            .fetch_optional(&mut *tx)
            .await?;
            if let Some(row) = oldest_kept {
                let floor: i64 = row.try_get("seq")?;
                sqlx::query(
                    "DELETE FROM snapshots WHERE document_id = ? AND id NOT IN (
                        SELECT id FROM snapshots WHERE document_id = ?
                        ORDER BY seq DESC, created_at DESC LIMIT ?)",
                )
                .bind(&doc)
                .bind(&doc)
                .bind(self.snapshot_retention)
                .execute(&mut *tx)
                .await?;
                sqlx::query("DELETE FROM ops WHERE document_id = ? AND seq <= ?")
                    .bind(&doc)
                    .bind(floor)
                    .execute(&mut *tx)
                    .await?;
            }
        }
        tx.commit().await?;
        Ok(info)
    }

    async fn latest_snapshot_info(&self, document_id: Uuid) -> StoreResult<Option<SnapshotInfo>> {
        let row = sqlx::query(&format!(
            "SELECT {SNAPSHOT_COLS} FROM snapshots WHERE document_id = ?
             ORDER BY seq DESC, created_at DESC LIMIT 1"
        ))
        .bind(document_id.to_string())
        .fetch_optional(&self.pool)
        .await?;
        row.as_ref().map(snapshot_from_row).transpose()
    }

    async fn add_chat(&self, document_id: Uuid, msg: &ChatMessage) -> StoreResult<()> {
        let doc = document_id.to_string();
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "INSERT INTO chat_messages (id, document_id, session_id, user_id, user_name, user_kind, text, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(msg.id.to_string())
        .bind(&doc)
        .bind(msg.session_id.to_string())
        .bind(msg.user_id.to_string())
        .bind(&msg.user_name)
        .bind(msg.user_kind.as_ref())
        .bind(&msg.text)
        .bind(msg.created_at)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "DELETE FROM chat_messages WHERE document_id = ? AND created_at < (
                SELECT created_at FROM chat_messages WHERE document_id = ?
                ORDER BY created_at DESC LIMIT 1 OFFSET ?)",
        )
        .bind(&doc)
        .bind(&doc)
        .bind(CHAT_RETENTION)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn recent_chat(&self, document_id: Uuid, limit: i64) -> StoreResult<Vec<ChatMessage>> {
        let rows = sqlx::query(
            "SELECT id, session_id, user_id, user_name, user_kind, text, created_at FROM chat_messages
             WHERE document_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        )
        .bind(document_id.to_string())
        .bind(limit)
        .fetch_all(&self.pool)
        .await?;
        let mut out = rows
            .iter()
            .map(|r| {
                Ok(ChatMessage {
                    id: uuid_col(r, "id")?,
                    session_id: uuid_col(r, "session_id")?,
                    user_id: uuid_col(r, "user_id")?,
                    user_name: r.try_get("user_name")?,
                    user_kind: kind_col(r, "user_kind")?,
                    text: r.try_get("text")?,
                    created_at: r.try_get("created_at")?,
                })
            })
            .collect::<StoreResult<Vec<_>>>()?;
        out.reverse();
        Ok(out)
    }

    async fn snapshot_data(&self, snapshot_id: Uuid) -> StoreResult<Option<Vec<u8>>> {
        let row = sqlx::query("SELECT data FROM snapshots WHERE id = ?")
            .bind(snapshot_id.to_string())
            .fetch_optional(&self.pool)
            .await?;
        Ok(row.map(|r| r.try_get("data")).transpose()?)
    }
}
