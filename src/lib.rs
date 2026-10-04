//! FreeCAD collaboration server.
//!
//! Clients (the FreeCAD plugin) connect over WebSocket per document, submit
//! object-level operations, and receive a totally ordered stream of operations
//! from every other participant. Full-document snapshots (`.FCStd`) are stored
//! over REST so late joiners can bootstrap without replaying all history.

pub mod admin;
pub mod api;
pub mod auth;
pub mod config;
pub mod db;
pub mod error;
pub mod model;
pub mod protocol;
pub mod room;
pub mod state;
pub mod util;
pub mod ws;

use std::sync::Arc;

use axum::Router;
use sqlx::SqlitePool;
use tokio::sync::watch;

pub use config::Config;
pub use state::AppState;

use db::{SqliteStore, Store};
use room::RoomManager;

/// A fully wired server, ready to be served.
pub struct Server {
    pub router: Router,
    pub state: AppState,
    pub pool: SqlitePool,
    /// Send `true` to ask every live WebSocket session to close.
    pub shutdown: watch::Sender<bool>,
}

pub async fn build(config: Config) -> anyhow::Result<Server> {
    let config = Arc::new(config);
    let pool = db::connect(&config.database_url, config.db_max_connections).await?;
    let store: Arc<dyn Store> = Arc::new(SqliteStore::new(pool.clone(), config.snapshot_retention));
    let rooms = Arc::new(RoomManager::new(store.clone(), config.clone()));
    let (shutdown, shutdown_rx) = watch::channel(false);

    let state = AppState {
        config,
        store,
        rooms,
        shutdown: shutdown_rx,
        started: std::time::Instant::now(),
    };

    Ok(Server {
        router: api::router(state.clone()),
        state,
        pool,
        shutdown,
    })
}
