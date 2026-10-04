//! In-memory coordination for one live document.
//!
//! A [`Room`] exists while at least one session is connected. It serializes
//! commits (assigning a total order via the database), detects concurrent
//! edits of the same object property, tracks edit locks and fans every event
//! out to sessions through a broadcast channel.

use std::{
    collections::{BTreeSet, HashMap},
    sync::Arc,
};

use tokio::sync::{broadcast, Mutex};
use tracing::{debug, error};
use uuid::Uuid;

use crate::{
    config::Config,
    db::{NewOps, Store, StoreError},
    model::Role,
    protocol::{ConflictKey, LockInfo, Op, Participant, RejectReason, ServerMessage},
};

#[derive(Debug, Clone)]
pub enum RoomEvent {
    /// Pre-serialized server message.
    Message {
        payload: Arc<str>,
        origin: Option<Uuid>,
        /// Deliver to the originating session too.
        echo: bool,
        /// Present for `ops` messages; used for gap-free delivery.
        seq: Option<i64>,
    },
    MemberChanged { user_id: Uuid, role: Option<Role> },
    /// Close every session of this user (account disabled).
    KickUser { user_id: Uuid, message: String },
    Terminate { code: &'static str, message: String },
}

#[derive(Debug)]
pub struct Rejection {
    pub reason: RejectReason,
    pub message: String,
    pub head_seq: i64,
}

struct RoomState {
    head_seq: i64,
    /// Conflict information is complete for every seq greater than this.
    index_floor: i64,
    last_modified: HashMap<ConflictKey, (i64, Uuid)>,
    locks: HashMap<String, Uuid>,
    participants: HashMap<Uuid, Participant>,
}

pub struct Room {
    document_id: Uuid,
    tx: broadcast::Sender<RoomEvent>,
    state: Mutex<RoomState>,
    index_capacity: usize,
}

pub struct Joined {
    pub room: Arc<Room>,
    pub rx: broadcast::Receiver<RoomEvent>,
    pub head_seq: i64,
    pub participants: Vec<Participant>,
    pub locks: Vec<LockInfo>,
}

impl Room {
    fn new(document_id: Uuid, head_seq: i64, channel_capacity: usize, index_capacity: usize) -> Self {
        let (tx, _) = broadcast::channel(channel_capacity);
        Self {
            document_id,
            tx,
            state: Mutex::new(RoomState {
                head_seq,
                index_floor: head_seq,
                last_modified: HashMap::new(),
                locks: HashMap::new(),
                participants: HashMap::new(),
            }),
            index_capacity,
        }
    }

    pub fn document_id(&self) -> Uuid {
        self.document_id
    }

    fn publish(&self, msg: &ServerMessage, origin: Option<Uuid>, echo: bool, seq: Option<i64>) {
        match serde_json::to_string(msg) {
            Ok(s) => {
                // No receivers is fine: nobody to tell.
                let _ = self.tx.send(RoomEvent::Message {
                    payload: s.into(),
                    origin,
                    echo,
                    seq,
                });
            }
            Err(e) => error!(error = %e, "failed to serialize room message"),
        }
    }

    fn send_event(&self, ev: RoomEvent) {
        let _ = self.tx.send(ev);
    }

    pub async fn head_seq(&self) -> i64 {
        self.state.lock().await.head_seq
    }

    /// Validate against concurrent edits and locks, persist, broadcast.
    /// Returns the assigned seq (or the existing one for a duplicate id).
    pub async fn commit(
        &self,
        store: &dyn Store,
        author_id: Uuid,
        session_id: Uuid,
        client_op_id: &str,
        base_seq: i64,
        ops: Vec<Op>,
    ) -> Result<i64, Rejection> {
        let mut st = self.state.lock().await;
        let head = st.head_seq;
        let reject = |reason, message: String| Rejection {
            reason,
            message,
            head_seq: head,
        };
        let internal = |e: StoreError| {
            error!(error = %e, document = %self.document_id, "commit failed");
            Rejection {
                reason: RejectReason::Internal,
                message: "internal error, retry with the same client_op_id".into(),
                head_seq: head,
            }
        };

        // Idempotent resubmission (e.g. after a reconnect).
        if let Some(seq) = store
            .find_op_seq(self.document_id, author_id, client_op_id)
            .await
            .map_err(internal)?
        {
            return Ok(seq);
        }

        if base_seq < 0 || base_seq > head {
            return Err(reject(
                RejectReason::InvalidBaseSeq,
                format!("base_seq {base_seq} is outside [0, {head}]"),
            ));
        }
        if base_seq < st.index_floor {
            return Err(reject(
                RejectReason::Conflict,
                format!(
                    "base_seq {base_seq} predates the conflict window (starts after {}); rebase onto {head}",
                    st.index_floor
                ),
            ));
        }

        let locked: BTreeSet<&str> = ops
            .iter()
            .map(Op::object)
            .filter(|o| st.locks.get(*o).is_some_and(|h| *h != session_id))
            .collect();
        if !locked.is_empty() {
            return Err(reject(
                RejectReason::LockedByOther,
                format!(
                    "locked by another session: {}",
                    locked.into_iter().collect::<Vec<_>>().join(", ")
                ),
            ));
        }

        let mut conflicts = BTreeSet::new();
        for op in &ops {
            for key in op.read_keys() {
                if let Some((seq, by)) = st.last_modified.get(&key) {
                    if *seq > base_seq && *by != session_id {
                        conflicts.insert(format!("{}.{}", key.0, key.1));
                    }
                }
            }
        }
        if !conflicts.is_empty() {
            return Err(reject(
                RejectReason::Conflict,
                format!(
                    "concurrently modified since seq {base_seq}: {}",
                    conflicts.into_iter().collect::<Vec<_>>().join(", ")
                ),
            ));
        }

        let record = store
            .append_ops(NewOps {
                document_id: self.document_id,
                author_id,
                session_id,
                client_op_id: client_op_id.to_string(),
                ops,
            })
            .await
            .map_err(internal)?;

        st.head_seq = record.seq;
        for op in &record.ops {
            st.last_modified.insert(op.write_key(), (record.seq, session_id));
        }
        if st.last_modified.len() > self.index_capacity {
            compact_index(&mut st);
        }

        let seq = record.seq;
        self.publish(&ServerMessage::Ops { batch: record }, Some(session_id), false, Some(seq));
        Ok(seq)
    }

    /// All-or-nothing lock acquisition. Re-locking your own lock is a no-op.
    pub async fn lock(&self, session_id: Uuid, objects: Vec<String>) -> Result<(), Vec<LockInfo>> {
        let objects: Vec<String> = objects.into_iter().collect::<BTreeSet<_>>().into_iter().collect();
        let mut st = self.state.lock().await;
        let held: Vec<LockInfo> = objects
            .iter()
            .filter_map(|o| {
                st.locks
                    .get(o)
                    .filter(|h| **h != session_id)
                    .map(|h| LockInfo {
                        object: o.clone(),
                        session_id: *h,
                    })
            })
            .collect();
        if !held.is_empty() {
            return Err(held);
        }
        for o in &objects {
            st.locks.insert(o.clone(), session_id);
        }
        drop(st);
        self.publish(&ServerMessage::Locked { objects, session_id }, Some(session_id), true, None);
        Ok(())
    }

    pub async fn unlock(&self, session_id: Uuid, objects: Vec<String>) {
        let mut st = self.state.lock().await;
        let released: Vec<String> = objects
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .filter(|o| st.locks.get(o) == Some(&session_id))
            .collect();
        for o in &released {
            st.locks.remove(o);
        }
        drop(st);
        self.publish(
            &ServerMessage::Unlocked {
                objects: released,
                session_id,
            },
            Some(session_id),
            true,
            None,
        );
    }

    pub async fn unlock_all(&self, session_id: Uuid) {
        let mut st = self.state.lock().await;
        let released = take_locks(&mut st, session_id);
        drop(st);
        if !released.is_empty() {
            self.publish(
                &ServerMessage::Unlocked {
                    objects: released,
                    session_id,
                },
                Some(session_id),
                true,
                None,
            );
        }
    }

    pub async fn set_participant_role(&self, session_id: Uuid, role: Role) {
        if let Some(p) = self.state.lock().await.participants.get_mut(&session_id) {
            p.role = role;
        }
    }

    pub fn chat(&self, message: crate::model::ChatMessage) {
        let origin = message.session_id;
        self.publish(&ServerMessage::Chat { message }, Some(origin), true, None);
    }

    pub fn presence(&self, session_id: Uuid, selection: Vec<String>, camera: Option<serde_json::Value>) {
        self.publish(
            &ServerMessage::Presence {
                session_id,
                selection,
                camera,
            },
            Some(session_id),
            false,
            None,
        );
    }
}

fn take_locks(st: &mut RoomState, session_id: Uuid) -> Vec<String> {
    let mut mine: Vec<String> = st
        .locks
        .iter()
        .filter(|(_, h)| **h == session_id)
        .map(|(o, _)| o.clone())
        .collect();
    for o in &mine {
        st.locks.remove(o);
    }
    mine.sort_unstable();
    mine
}

/// Drop the older half of the conflict index and raise the floor so that
/// submits based before it are (correctly) treated as conflicting.
fn compact_index(st: &mut RoomState) {
    let mut seqs: Vec<i64> = st.last_modified.values().map(|(s, _)| *s).collect();
    seqs.sort_unstable();
    let cutoff = seqs[seqs.len() / 2];
    st.last_modified.retain(|_, (s, _)| *s > cutoff);
    st.index_floor = st.index_floor.max(cutoff);
    debug!(cutoff, remaining = st.last_modified.len(), "compacted conflict index");
}

/// Owns the live rooms. Joins and leaves are serialized here so a room is
/// never evicted while someone is joining it.
pub struct RoomManager {
    rooms: Mutex<HashMap<Uuid, Arc<Room>>>,
    store: Arc<dyn Store>,
    config: Arc<Config>,
}

impl RoomManager {
    pub fn new(store: Arc<dyn Store>, config: Arc<Config>) -> Self {
        Self {
            rooms: Mutex::new(HashMap::new()),
            store,
            config,
        }
    }

    pub async fn join(&self, document_id: Uuid, participant: Participant) -> Result<Joined, StoreError> {
        let mut rooms = self.rooms.lock().await;
        let room = match rooms.get(&document_id) {
            Some(r) => r.clone(),
            None => {
                let head = self
                    .store
                    .head_seq(document_id)
                    .await?
                    .ok_or(StoreError::NotFound("document"))?;
                let r = Arc::new(Room::new(
                    document_id,
                    head,
                    self.config.room_channel_capacity,
                    self.config.conflict_index_capacity,
                ));
                rooms.insert(document_id, r.clone());
                r
            }
        };

        let mut st = room.state.lock().await;
        // Subscribe before releasing the state lock so no event is missed
        // between the snapshot below and the first `recv`.
        let rx = room.tx.subscribe();
        st.participants.insert(participant.session_id, participant.clone());
        let participants = st.participants.values().cloned().collect();
        let locks = st
            .locks
            .iter()
            .map(|(o, s)| LockInfo {
                object: o.clone(),
                session_id: *s,
            })
            .collect();
        let head_seq = st.head_seq;
        drop(st);

        let sid = participant.session_id;
        room.publish(&ServerMessage::Joined { participant }, Some(sid), false, None);

        Ok(Joined {
            room,
            rx,
            head_seq,
            participants,
            locks,
        })
    }

    pub async fn leave(&self, document_id: Uuid, session_id: Uuid) {
        let mut rooms = self.rooms.lock().await;
        let Some(room) = rooms.get(&document_id).cloned() else {
            return;
        };
        let mut st = room.state.lock().await;
        st.participants.remove(&session_id);
        let released = take_locks(&mut st, session_id);
        let empty = st.participants.is_empty();
        drop(st);

        if !released.is_empty() {
            room.publish(
                &ServerMessage::Unlocked {
                    objects: released,
                    session_id,
                },
                Some(session_id),
                false,
                None,
            );
        }
        room.publish(&ServerMessage::Left { session_id }, Some(session_id), false, None);
        if empty {
            rooms.remove(&document_id);
            debug!(document = %document_id, "room closed");
        }
    }

    async fn get(&self, document_id: Uuid) -> Option<Arc<Room>> {
        self.rooms.lock().await.get(&document_id).cloned()
    }

    pub async fn broadcast(&self, document_id: Uuid, msg: &ServerMessage) {
        if let Some(room) = self.get(document_id).await {
            room.publish(msg, None, true, None);
        }
    }

    pub async fn member_changed(&self, document_id: Uuid, user_id: Uuid, role: Option<Role>) {
        if let Some(room) = self.get(document_id).await {
            room.send_event(RoomEvent::MemberChanged { user_id, role });
        }
    }

    pub async fn terminate(&self, document_id: Uuid, code: &'static str, message: String) {
        if let Some(room) = self.get(document_id).await {
            room.send_event(RoomEvent::Terminate { code, message });
        }
    }

    pub async fn active_rooms(&self) -> usize {
        self.rooms.lock().await.len()
    }

    pub async fn kick_user(&self, user_id: Uuid, message: String) {
        let rooms: Vec<Arc<Room>> = self.rooms.lock().await.values().cloned().collect();
        for room in rooms {
            room.send_event(RoomEvent::KickUser {
                user_id,
                message: message.clone(),
            });
        }
    }

    /// Live participants per document.
    pub async fn live(&self) -> HashMap<Uuid, Vec<Participant>> {
        let rooms: Vec<Arc<Room>> = self.rooms.lock().await.values().cloned().collect();
        let mut out = HashMap::new();
        for room in rooms {
            let mut people: Vec<Participant> = room.state.lock().await.participants.values().cloned().collect();
            people.sort_by(|a, b| a.user_name.cmp(&b.user_name));
            out.insert(room.document_id, people);
        }
        out
    }
}
