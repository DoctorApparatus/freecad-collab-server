use std::{sync::Arc, time::Instant};

use tokio::sync::watch;

use crate::{config::Config, db::Store, room::RoomManager};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub store: Arc<dyn Store>,
    pub rooms: Arc<RoomManager>,
    /// Flips to `true` when the server is shutting down.
    pub shutdown: watch::Receiver<bool>,
    pub started: Instant,
}
