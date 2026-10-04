//! One WebSocket session = one FreeCAD client editing one document.

use std::{sync::Arc, time::Duration};

use axum::{
    extract::{
        ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade},
        Path, State,
    },
    response::Response,
};
use tokio::{
    sync::broadcast::error::RecvError,
    time::{interval, timeout, Instant, MissedTickBehavior},
};
use tracing::{debug, info, warn, Instrument};
use uuid::Uuid;

use crate::{
    auth::{require_role, AuthUser},
    error::AppError,
    model::{Role, SnapshotInfo, User},
    protocol::{ClientMessage, Participant, RejectReason, ServerMessage, PROTOCOL_VERSION},
    room::{Joined, Room, RoomEvent},
    state::AppState,
    util::is_identifier,
};

const HELLO_TIMEOUT: Duration = Duration::from_secs(15);
const CATCHUP_PAGE: i64 = 500;
const MAX_VIOLATIONS: u32 = 16;
const MAX_LOCK_OBJECTS: usize = 1000;
const MAX_SELECTION: usize = 1000;
const MAX_CAMERA_BYTES: usize = 4096;
const MAX_CLIENT_NAME: usize = 128;
const MAX_CLIENT_OP_ID: usize = 128;
const MAX_CHAT_CHARS: usize = 16_000;
const CHAT_HISTORY: i64 = 50;
/// Chat rate limit: at most this many messages per window.
const CHAT_BURST: usize = 10;
const CHAT_WINDOW: Duration = Duration::from_secs(10);

const COLORS: [&str; 12] = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6", "#bcf60c",
    "#008080", "#9a6324", "#800000", "#000075",
];

/// WebSocket close codes sent by the server.
pub mod close_code {
    pub const NORMAL: u16 = 1000;
    pub const GOING_AWAY: u16 = 1001;
    pub const INTERNAL: u16 = 1011;
    pub const RESYNC_REQUIRED: u16 = 4000;
    pub const TIMEOUT: u16 = 4001;
    pub const PROTOCOL_ERROR: u16 = 4002;
    pub const ACCESS_REVOKED: u16 = 4003;
    pub const DOCUMENT_DELETED: u16 = 4004;
}

struct Close {
    code: u16,
    reason: &'static str,
}

impl Close {
    const fn new(code: u16, reason: &'static str) -> Self {
        Self { code, reason }
    }
}

/// Outcome of a handler step: keep going, or close with a reason.
type Step = anyhow::Result<Option<Close>>;

pub async fn ws_handler(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(document_id): Path<Uuid>,
    ws: WebSocketUpgrade,
) -> Result<Response, AppError> {
    let role = require_role(&state, document_id, &user, Role::Viewer).await?;
    let max = state.config.max_ws_message_bytes;
    Ok(ws
        .max_message_size(max)
        .max_frame_size(max)
        .on_upgrade(move |socket| {
            let session = Session {
                state,
                user,
                document_id,
                role,
                session_id: Uuid::new_v4(),
                last_sent_seq: 0,
                violations: 0,
                chat_times: std::collections::VecDeque::new(),
            };
            session.run(socket)
        }))
}

struct Hello {
    client: String,
    last_seen_seq: Option<i64>,
}

struct Session {
    state: AppState,
    user: User,
    document_id: Uuid,
    role: Role,
    session_id: Uuid,
    /// Highest op seq delivered (or deliberately skipped) on this socket.
    last_sent_seq: i64,
    violations: u32,
    chat_times: std::collections::VecDeque<Instant>,
}

async fn send(socket: &mut WebSocket, msg: &ServerMessage) -> anyhow::Result<()> {
    let text = serde_json::to_string(msg)?;
    socket.send(Message::Text(text.into())).await?;
    Ok(())
}

impl Session {
    async fn run(mut self, mut socket: WebSocket) {
        let span = tracing::info_span!(
            "ws",
            session = %self.session_id,
            document = %self.document_id,
            user = %self.user.name
        );
        async move {
            info!("connected");
            let close = match self.run_inner(&mut socket).await {
                Ok(c) => c,
                Err(e) => {
                    warn!(error = %e, "session failed");
                    Close::new(close_code::INTERNAL, "internal error")
                }
            };
            let _ = socket
                .send(Message::Close(Some(CloseFrame {
                    code: close.code,
                    reason: close.reason.into(),
                })))
                .await;
            info!(code = close.code, reason = close.reason, "disconnected");
        }
        .instrument(span)
        .await
    }

    async fn run_inner(&mut self, socket: &mut WebSocket) -> anyhow::Result<Close> {
        let hello = match timeout(HELLO_TIMEOUT, self.recv_hello(socket)).await {
            Err(_) => return Ok(Close::new(close_code::TIMEOUT, "hello timeout")),
            Ok(Err(e)) => return Err(e),
            Ok(Ok(Err(close))) => return Ok(close),
            Ok(Ok(Ok(h))) => h,
        };

        let participant = Participant {
            session_id: self.session_id,
            user_id: self.user.id,
            user_name: self.user.name.clone(),
            kind: self.user.kind,
            role: self.role,
            client: hello.client,
            color: COLORS[(self.session_id.as_u128() % COLORS.len() as u128) as usize].to_string(),
        };

        let joined = match self.state.rooms.join(self.document_id, participant.clone()).await {
            Ok(j) => j,
            Err(crate::db::StoreError::NotFound(_)) => {
                return Ok(Close::new(close_code::DOCUMENT_DELETED, "document not found"))
            }
            Err(e) => return Err(e.into()),
        };
        let result = self.connected(socket, participant, joined, hello.last_seen_seq).await;
        self.state.rooms.leave(self.document_id, self.session_id).await;
        result
    }

    async fn recv_hello(&mut self, socket: &mut WebSocket) -> anyhow::Result<Result<Hello, Close>> {
        loop {
            let text = match socket.recv().await {
                None | Some(Ok(Message::Close(_))) => return Ok(Err(Close::new(close_code::NORMAL, "bye"))),
                Some(Err(e)) => return Err(e.into()),
                Some(Ok(Message::Text(t))) => t,
                Some(Ok(_)) => continue,
            };
            let reply = match serde_json::from_str::<ClientMessage>(&text) {
                Ok(ClientMessage::Hello {
                    protocol_version,
                    client,
                    last_seen_seq,
                }) => {
                    if protocol_version != PROTOCOL_VERSION {
                        ("unsupported_protocol", format!("server speaks protocol {PROTOCOL_VERSION}"))
                    } else if client.is_empty() || client.len() > MAX_CLIENT_NAME {
                        ("bad_message", "client must be 1-128 characters".to_string())
                    } else {
                        return Ok(Ok(Hello { client, last_seen_seq }));
                    }
                }
                Ok(_) => ("hello_required", "first message must be `hello`".to_string()),
                Err(e) => ("bad_message", e.to_string()),
            };
            send(
                socket,
                &ServerMessage::Error {
                    code: reply.0.into(),
                    message: reply.1,
                },
            )
            .await?;
            return Ok(Err(Close::new(close_code::PROTOCOL_ERROR, reply.0)));
        }
    }

    async fn connected(
        &mut self,
        socket: &mut WebSocket,
        you: Participant,
        joined: Joined,
        last_seen_seq: Option<i64>,
    ) -> anyhow::Result<Close> {
        let Joined {
            room,
            mut rx,
            head_seq,
            participants,
            locks,
        } = joined;

        let snapshot = self.state.store.latest_snapshot_info(self.document_id).await?;
        let chat = self.state.store.recent_chat(self.document_id, CHAT_HISTORY).await?;
        send(
            socket,
            &ServerMessage::Welcome {
                protocol_version: PROTOCOL_VERSION,
                session_id: self.session_id,
                you: you.clone(),
                head_seq,
                snapshot: snapshot.clone(),
                participants,
                locks,
                chat,
            },
        )
        .await?;

        if let Some(close) = self.catch_up(socket, last_seen_seq, head_seq, snapshot).await? {
            return Ok(close);
        }

        let mut heartbeat = interval(self.state.config.heartbeat_interval);
        heartbeat.set_missed_tick_behavior(MissedTickBehavior::Delay);
        heartbeat.tick().await;
        let mut last_activity = Instant::now();
        let mut shutdown = self.state.shutdown.clone();
        if *shutdown.borrow() {
            return Ok(Close::new(close_code::GOING_AWAY, "server shutting down"));
        }

        loop {
            let step: Option<Close> = tokio::select! {
                msg = socket.recv() => match msg {
                    None => Some(Close::new(close_code::NORMAL, "peer gone")),
                    Some(Err(e)) => {
                        debug!(error = %e, "socket error");
                        Some(Close::new(close_code::NORMAL, "socket error"))
                    }
                    Some(Ok(m)) => {
                        last_activity = Instant::now();
                        match m {
                            Message::Text(t) => self.handle_text(socket, &room, t.as_str()).await?,
                            Message::Binary(_) => self.violation(socket, "bad_message", "binary frames are not supported".into()).await?,
                            Message::Close(_) => Some(Close::new(close_code::NORMAL, "bye")),
                            Message::Ping(_) | Message::Pong(_) => None,
                        }
                    }
                },
                ev = rx.recv() => match ev {
                    Ok(ev) => self.handle_event(socket, &room, ev).await?,
                    Err(RecvError::Lagged(n)) => {
                        warn!(skipped = n, "session lagged behind room; recovering from store");
                        self.recover(socket, &room).await?
                    }
                    Err(RecvError::Closed) => Some(Close::new(close_code::GOING_AWAY, "room closed")),
                },
                _ = heartbeat.tick() => {
                    if last_activity.elapsed() > self.state.config.client_timeout {
                        Some(Close::new(close_code::TIMEOUT, "client timeout"))
                    } else {
                        socket.send(Message::Ping(Default::default())).await?;
                        None
                    }
                },
                _ = shutdown.changed() => Some(Close::new(close_code::GOING_AWAY, "server shutting down")),
            };
            if let Some(close) = step {
                return Ok(close);
            }
        }
    }

    /// Stream ops the client is missing, or tell it to resync from a snapshot.
    async fn catch_up(
        &mut self,
        socket: &mut WebSocket,
        last_seen_seq: Option<i64>,
        head: i64,
        snapshot: Option<SnapshotInfo>,
    ) -> Step {
        let from = last_seen_seq.unwrap_or_else(|| snapshot.as_ref().map_or(0, |s| s.seq));
        if from < 0 || from > head {
            send(
                socket,
                &ServerMessage::Error {
                    code: "invalid_last_seen_seq".into(),
                    message: format!("last_seen_seq must be within [0, {head}]"),
                },
            )
            .await?;
            return Ok(Some(Close::new(close_code::PROTOCOL_ERROR, "invalid last_seen_seq")));
        }
        if from < head {
            let min = self.state.store.min_op_seq(self.document_id).await?;
            let compacted = min.is_none_or(|m| from + 1 < m);
            if compacted || head - from > self.state.config.max_catchup_ops {
                send(socket, &ServerMessage::ResyncRequired { head_seq: head, snapshot }).await?;
                return Ok(Some(Close::new(close_code::RESYNC_REQUIRED, "resync required")));
            }
            self.stream_ops(socket, from, head, None).await?;
        }
        self.last_sent_seq = head;
        send(socket, &ServerMessage::CatchUpComplete { head_seq: head }).await?;
        Ok(None)
    }

    async fn stream_ops(&self, socket: &mut WebSocket, from: i64, to: i64, skip_session: Option<Uuid>) -> anyhow::Result<()> {
        let mut cursor = from;
        while cursor < to {
            let page = self.state.store.ops_after(self.document_id, cursor, CATCHUP_PAGE).await?;
            if page.is_empty() {
                break;
            }
            for batch in page {
                if batch.seq > to {
                    return Ok(());
                }
                cursor = batch.seq;
                if Some(batch.session_id) != skip_session {
                    send(socket, &ServerMessage::Ops { batch }).await?;
                }
            }
        }
        Ok(())
    }

    /// Broadcast channel overflowed: refill the gap from the database.
    async fn recover(&mut self, socket: &mut WebSocket, room: &Room) -> Step {
        let head = room.head_seq().await;
        if self.last_sent_seq < head {
            let min = self.state.store.min_op_seq(self.document_id).await?;
            if min.is_none_or(|m| self.last_sent_seq + 1 < m) {
                let snapshot = self.state.store.latest_snapshot_info(self.document_id).await?;
                send(socket, &ServerMessage::ResyncRequired { head_seq: head, snapshot }).await?;
                return Ok(Some(Close::new(close_code::RESYNC_REQUIRED, "resync required")));
            }
            self.stream_ops(socket, self.last_sent_seq, head, Some(self.session_id)).await?;
            self.last_sent_seq = head;
        }
        Ok(None)
    }

    async fn violation(&mut self, socket: &mut WebSocket, code: &str, message: String) -> Step {
        send(
            socket,
            &ServerMessage::Error {
                code: code.into(),
                message,
            },
        )
        .await?;
        self.violations += 1;
        if self.violations >= MAX_VIOLATIONS {
            return Ok(Some(Close::new(close_code::PROTOCOL_ERROR, "too many protocol errors")));
        }
        Ok(None)
    }

    async fn reject(
        &self,
        socket: &mut WebSocket,
        room: &Room,
        client_op_id: String,
        reason: RejectReason,
        message: String,
    ) -> Step {
        let head_seq = room.head_seq().await;
        send(
            socket,
            &ServerMessage::Reject {
                client_op_id,
                reason,
                message,
                head_seq,
            },
        )
        .await?;
        Ok(None)
    }

    async fn handle_text(&mut self, socket: &mut WebSocket, room: &Arc<Room>, text: &str) -> Step {
        let msg = match serde_json::from_str::<ClientMessage>(text) {
            Ok(m) => m,
            Err(e) => return self.violation(socket, "bad_message", e.to_string()).await,
        };

        match msg {
            ClientMessage::Hello { .. } => self.violation(socket, "already_greeted", "hello was already received".into()).await,

            ClientMessage::Chat { text } => {
                let text = text.trim().to_string();
                let chars = text.chars().count();
                if chars == 0 || chars > MAX_CHAT_CHARS {
                    return self
                        .violation(socket, "bad_message", format!("chat text must be 1-{MAX_CHAT_CHARS} characters"))
                        .await;
                }
                let now = Instant::now();
                while self.chat_times.front().is_some_and(|t| now.duration_since(*t) > CHAT_WINDOW) {
                    self.chat_times.pop_front();
                }
                if self.chat_times.len() >= CHAT_BURST {
                    return self.violation(socket, "rate_limited", "too many chat messages; slow down".into()).await;
                }
                self.chat_times.push_back(now);
                let message = crate::model::ChatMessage {
                    id: Uuid::new_v4(),
                    session_id: self.session_id,
                    user_id: self.user.id,
                    user_name: self.user.name.clone(),
                    user_kind: self.user.kind,
                    text,
                    created_at: crate::util::now_ms(),
                };
                self.state.store.add_chat(self.document_id, &message).await?;
                room.chat(message);
                Ok(None)
            }

            ClientMessage::Ping { nonce } => {
                send(socket, &ServerMessage::Pong { nonce }).await?;
                Ok(None)
            }

            ClientMessage::Submit {
                client_op_id,
                base_seq,
                ops,
            } => {
                if client_op_id.is_empty() || client_op_id.len() > MAX_CLIENT_OP_ID {
                    return self
                        .violation(socket, "bad_message", "client_op_id must be 1-128 characters".into())
                        .await;
                }
                if self.role < Role::Editor {
                    return self
                        .reject(socket, room, client_op_id, RejectReason::Forbidden, "viewers cannot edit".into())
                        .await;
                }
                if ops.is_empty() {
                    return self
                        .reject(socket, room, client_op_id, RejectReason::InvalidOp, "ops must not be empty".into())
                        .await;
                }
                let max_ops = self.state.config.max_ops_per_submit;
                if ops.len() > max_ops {
                    return self
                        .reject(socket, room, client_op_id, RejectReason::TooLarge, format!("at most {max_ops} ops per submit"))
                        .await;
                }
                let max_value = self.state.config.max_property_value_bytes;
                if let Some((i, e)) = ops.iter().enumerate().find_map(|(i, op)| op.validate(max_value).err().map(|e| (i, e))) {
                    return self
                        .reject(socket, room, client_op_id, RejectReason::InvalidOp, format!("op {i}: {e}"))
                        .await;
                }

                match room
                    .commit(self.state.store.as_ref(), self.user.id, self.session_id, &client_op_id, base_seq, ops)
                    .await
                {
                    Ok(seq) => send(socket, &ServerMessage::Ack { client_op_id, seq }).await?,
                    Err(r) => {
                        debug!(reason = %r.reason, message = %r.message, "submit rejected");
                        send(
                            socket,
                            &ServerMessage::Reject {
                                client_op_id,
                                reason: r.reason,
                                message: r.message,
                                head_seq: r.head_seq,
                            },
                        )
                        .await?
                    }
                }
                Ok(None)
            }

            ClientMessage::Lock { objects } | ClientMessage::Unlock { objects }
                if objects.len() > MAX_LOCK_OBJECTS || !objects.iter().all(|o| is_identifier(o)) =>
            {
                self.violation(socket, "bad_message", format!("objects must be ≤{MAX_LOCK_OBJECTS} valid names")).await
            }

            ClientMessage::Lock { objects } => {
                if self.role < Role::Editor {
                    return self.violation(socket, "forbidden", "viewers cannot lock".into()).await;
                }
                if let Err(held_by) = room.lock(self.session_id, objects.clone()).await {
                    send(socket, &ServerMessage::LockDenied { objects, held_by }).await?;
                }
                Ok(None)
            }

            ClientMessage::Unlock { objects } => {
                room.unlock(self.session_id, objects).await;
                Ok(None)
            }

            ClientMessage::Presence { mut selection, camera } => {
                selection.truncate(MAX_SELECTION);
                selection.retain(|s| s.len() <= 256);
                let camera_ok = camera
                    .as_ref()
                    .is_none_or(|c| serde_json::to_vec(c).map(|b| b.len() <= MAX_CAMERA_BYTES).unwrap_or(false));
                if !camera_ok {
                    return self.violation(socket, "bad_message", format!("camera exceeds {MAX_CAMERA_BYTES} bytes")).await;
                }
                room.presence(self.session_id, selection, camera);
                Ok(None)
            }
        }
    }

    async fn handle_event(&mut self, socket: &mut WebSocket, room: &Room, ev: RoomEvent) -> Step {
        match ev {
            RoomEvent::Message {
                payload,
                origin,
                echo,
                seq,
            } => {
                if let Some(seq) = seq {
                    if seq <= self.last_sent_seq {
                        return Ok(None);
                    }
                    self.last_sent_seq = seq;
                }
                if !echo && origin == Some(self.session_id) {
                    return Ok(None);
                }
                socket.send(Message::Text(payload.as_ref().into())).await?;
                Ok(None)
            }
            RoomEvent::MemberChanged { user_id, role } if user_id == self.user.id => match role {
                None => {
                    send(
                        socket,
                        &ServerMessage::Error {
                            code: "access_revoked".into(),
                            message: "you were removed from this document".into(),
                        },
                    )
                    .await?;
                    Ok(Some(Close::new(close_code::ACCESS_REVOKED, "access revoked")))
                }
                Some(role) => {
                    self.role = role;
                    room.set_participant_role(self.session_id, role).await;
                    if role < Role::Editor {
                        room.unlock_all(self.session_id).await;
                    }
                    send(socket, &ServerMessage::RoleChanged { role }).await?;
                    Ok(None)
                }
            },
            RoomEvent::MemberChanged { .. } => Ok(None),
            RoomEvent::KickUser { user_id, message } if user_id == self.user.id => {
                send(
                    socket,
                    &ServerMessage::Error {
                        code: "access_revoked".into(),
                        message,
                    },
                )
                .await?;
                Ok(Some(Close::new(close_code::ACCESS_REVOKED, "access revoked")))
            }
            RoomEvent::KickUser { .. } => Ok(None),
            RoomEvent::Terminate { code, message } => {
                send(
                    socket,
                    &ServerMessage::Error {
                        code: code.into(),
                        message,
                    },
                )
                .await?;
                Ok(Some(Close::new(close_code::DOCUMENT_DELETED, "document deleted")))
            }
        }
    }
}
