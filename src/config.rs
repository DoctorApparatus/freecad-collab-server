use std::{net::SocketAddr, str::FromStr, time::Duration};

use anyhow::{bail, Context};
use strum::{Display, EnumString};
use typed_builder::TypedBuilder;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Display, EnumString)]
#[strum(serialize_all = "snake_case", ascii_case_insensitive)]
pub enum LogFormat {
    Pretty,
    Json,
}

/// Runtime configuration. Every field can be overridden by an environment
/// variable named `COLLAB_<FIELD_NAME_UPPERCASE>` (see `.env.example`).
#[derive(Debug, Clone, TypedBuilder)]
pub struct Config {
    #[builder(default = SocketAddr::from(([0, 0, 0, 0], 14008)))]
    pub bind_addr: SocketAddr,
    #[builder(default = "sqlite://data/collab.db".to_string())]
    pub database_url: String,
    #[builder(default = 8)]
    pub db_max_connections: u32,
    /// Bearer token for `/v1/admin/*`. Admin API is disabled when unset.
    #[builder(default)]
    pub admin_token: Option<String>,
    #[builder(default = 4 * 1024 * 1024)]
    pub max_ws_message_bytes: usize,
    #[builder(default = 512 * 1024 * 1024)]
    pub max_snapshot_bytes: usize,
    #[builder(default = 256 * 1024)]
    pub max_property_value_bytes: usize,
    #[builder(default = 1000)]
    pub max_ops_per_submit: usize,
    /// Joins further behind than this are told to download the snapshot.
    #[builder(default = 50_000)]
    pub max_catchup_ops: i64,
    /// Snapshots kept per document. Ops older than the oldest kept snapshot
    /// are compacted. `0` disables compaction.
    #[builder(default = 3)]
    pub snapshot_retention: i64,
    /// Max entries in a room's in-memory conflict index before it is halved.
    #[builder(default = 200_000)]
    pub conflict_index_capacity: usize,
    #[builder(default = 1024)]
    pub room_channel_capacity: usize,
    #[builder(default = Duration::from_secs(20))]
    pub heartbeat_interval: Duration,
    #[builder(default = Duration::from_secs(60))]
    pub client_timeout: Duration,
    #[builder(default = LogFormat::Pretty)]
    pub log_format: LogFormat,
    /// Append logs to this file instead of stdout (used by the Windows service).
    #[builder(default)]
    pub log_file: Option<std::path::PathBuf>,
}

fn env_var(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.trim().is_empty())
}

fn env_parse<T>(key: &str) -> anyhow::Result<Option<T>>
where
    T: FromStr,
    T::Err: std::fmt::Display,
{
    match env_var(key) {
        None => Ok(None),
        Some(raw) => raw
            .trim()
            .parse::<T>()
            .map(Some)
            .map_err(|e| anyhow::anyhow!("invalid value for {key}: {e}")),
    }
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        let mut c = Config::builder().build();

        if let Some(v) = env_parse("COLLAB_BIND_ADDR")? {
            c.bind_addr = v;
        }
        if let Some(v) = env_var("COLLAB_DATABASE_URL").or_else(|| env_var("DATABASE_URL")) {
            c.database_url = v;
        }
        if let Some(v) = env_parse("COLLAB_DB_MAX_CONNECTIONS")? {
            c.db_max_connections = v;
        }
        c.admin_token = env_var("COLLAB_ADMIN_TOKEN");
        if let Some(v) = env_parse("COLLAB_MAX_WS_MESSAGE_BYTES")? {
            c.max_ws_message_bytes = v;
        }
        if let Some(v) = env_parse("COLLAB_MAX_SNAPSHOT_BYTES")? {
            c.max_snapshot_bytes = v;
        }
        if let Some(v) = env_parse("COLLAB_MAX_PROPERTY_VALUE_BYTES")? {
            c.max_property_value_bytes = v;
        }
        if let Some(v) = env_parse("COLLAB_MAX_OPS_PER_SUBMIT")? {
            c.max_ops_per_submit = v;
        }
        if let Some(v) = env_parse("COLLAB_MAX_CATCHUP_OPS")? {
            c.max_catchup_ops = v;
        }
        if let Some(v) = env_parse("COLLAB_SNAPSHOT_RETENTION")? {
            c.snapshot_retention = v;
        }
        if let Some(v) = env_parse("COLLAB_CONFLICT_INDEX_CAPACITY")? {
            c.conflict_index_capacity = v;
        }
        if let Some(v) = env_parse("COLLAB_ROOM_CHANNEL_CAPACITY")? {
            c.room_channel_capacity = v;
        }
        if let Some(v) = env_parse::<u64>("COLLAB_HEARTBEAT_INTERVAL_SECS")? {
            c.heartbeat_interval = Duration::from_secs(v);
        }
        if let Some(v) = env_parse::<u64>("COLLAB_CLIENT_TIMEOUT_SECS")? {
            c.client_timeout = Duration::from_secs(v);
        }
        if let Some(v) = env_parse("COLLAB_LOG_FORMAT")? {
            c.log_format = v;
        }
        c.log_file = env_var("COLLAB_LOG_FILE").map(std::path::PathBuf::from);

        c.validate().context("invalid configuration")?;
        Ok(c)
    }

    pub fn validate(&self) -> anyhow::Result<()> {
        if let Some(t) = &self.admin_token {
            if t.len() < 32 {
                bail!("COLLAB_ADMIN_TOKEN must be at least 32 characters");
            }
        }
        if self.max_ops_per_submit == 0 {
            bail!("max_ops_per_submit must be > 0");
        }
        if self.snapshot_retention < 0 {
            bail!("snapshot_retention must be >= 0");
        }
        if self.room_channel_capacity < 16 {
            bail!("room_channel_capacity must be >= 16");
        }
        if self.client_timeout <= self.heartbeat_interval {
            bail!("client_timeout must be greater than heartbeat_interval");
        }
        if self.conflict_index_capacity < 1024 {
            bail!("conflict_index_capacity must be >= 1024");
        }
        Ok(())
    }
}
