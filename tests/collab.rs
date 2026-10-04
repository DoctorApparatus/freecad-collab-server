use std::{net::SocketAddr, time::Duration};

use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use freecad_collab_server::{build, model::Role, util::{generate_token, hash_token}, Config, Server};
use futures_util::{SinkExt, StreamExt};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio_tungstenite::{
    connect_async,
    tungstenite::{client::IntoClientRequest, Message},
    MaybeTlsStream, WebSocketStream,
};
use tower::ServiceExt;
use uuid::Uuid;

type Ws = WebSocketStream<MaybeTlsStream<TcpStream>>;

struct Harness {
    addr: SocketAddr,
    server: Server,
    doc: Uuid,
    alice: String,
    bob: String,
    carol: String,
    _dir: tempfile::TempDir,
}

async fn harness(snapshot_retention: i64) -> Harness {
    harness_with(snapshot_retention, None).await
}

async fn harness_with(snapshot_retention: i64, admin_token: Option<&str>) -> Harness {
    let dir = tempfile::tempdir().unwrap();
    let config = Config::builder()
        .database_url(format!("sqlite://{}/test.db", dir.path().display()))
        .snapshot_retention(snapshot_retention)
        .admin_token(admin_token.map(str::to_string))
        .build();
    let server = build(config).await.unwrap();
    let store = server.state.store.clone();

    let mut tokens = Vec::new();
    let mut users = Vec::new();
    for name in ["alice", "bob", "carol"] {
        let u = store.create_user(name, Default::default()).await.unwrap();
        let t = generate_token();
        store.create_token(u.id, &hash_token(&t), None).await.unwrap();
        tokens.push(t);
        users.push(u);
    }
    let doc = store.create_document(users[0].id, "Bracket", None).await.unwrap();
    store.set_member(doc.id, &users[1], Role::Editor).await.unwrap();
    store.set_member(doc.id, &users[2], Role::Viewer).await.unwrap();

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let router = server.router.clone();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });

    Harness {
        addr,
        server,
        doc: doc.id,
        alice: tokens[0].clone(),
        bob: tokens[1].clone(),
        carol: tokens[2].clone(),
        _dir: dir,
    }
}

async fn connect(h: &Harness, token: &str, last_seen: Option<i64>) -> Ws {
    let mut req = format!("ws://{}/v1/documents/{}/ws", h.addr, h.doc)
        .into_client_request()
        .unwrap();
    req.headers_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    let (mut ws, _) = connect_async(req).await.unwrap();
    send(&mut ws, json!({"type":"hello","protocol_version":1,"client":"test","last_seen_seq":last_seen})).await;
    ws
}

async fn send(ws: &mut Ws, v: Value) {
    ws.send(Message::Text(v.to_string().into())).await.unwrap();
}

/// Next server message with the given `type`, skipping others.
async fn expect(ws: &mut Ws, ty: &str) -> Value {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            match ws.next().await.expect("socket closed").unwrap() {
                Message::Text(t) => {
                    let v: Value = serde_json::from_str(&t).unwrap();
                    if v["type"] == ty {
                        return v;
                    }
                }
                Message::Close(f) => panic!("closed while waiting for {ty}: {f:?}"),
                _ => {}
            }
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {ty}"))
}

fn set_prop(obj: &str, prop: &str, v: f64) -> Value {
    json!({"op":"set_property","object":obj,"property":prop,
           "value":{"type_id":"App::PropertyLength","value":v}})
}

async fn submit(ws: &mut Ws, id: &str, base: i64, ops: Value) {
    send(ws, json!({"type":"submit","client_op_id":id,"base_seq":base,"ops":ops})).await;
}

#[tokio::test]
async fn ops_propagate_conflicts_and_idempotency() {
    let h = harness(3).await;
    let mut a = connect(&h, &h.alice, None).await;
    expect(&mut a, "catch_up_complete").await;
    let mut b = connect(&h, &h.bob, None).await;
    expect(&mut b, "catch_up_complete").await;

    submit(&mut a, "a1", 0, json!([{"op":"add_object","name":"Box","type_id":"Part::Box"}])).await;
    assert_eq!(expect(&mut a, "ack").await["seq"], 1);
    let ops = expect(&mut b, "ops").await;
    assert_eq!(ops["batch"]["seq"], 1);
    assert_eq!(ops["batch"]["ops"][0]["name"], "Box");

    // Same property concurrently: second writer is rejected.
    submit(&mut a, "a2", 1, json!([set_prop("Box", "Length", 20.0)])).await;
    assert_eq!(expect(&mut a, "ack").await["seq"], 2);
    submit(&mut b, "b1", 1, json!([set_prop("Box", "Length", 30.0)])).await;
    let r = expect(&mut b, "reject").await;
    assert_eq!(r["reason"], "conflict");
    assert_eq!(r["head_seq"], 2);

    // Different property from the same base is fine.
    submit(&mut b, "b2", 1, json!([set_prop("Box", "Width", 5.0)])).await;
    assert_eq!(expect(&mut b, "ack").await["seq"], 3);

    // Resubmitting the same id is idempotent.
    submit(&mut a, "a2", 1, json!([set_prop("Box", "Length", 20.0)])).await;
    assert_eq!(expect(&mut a, "ack").await["seq"], 2);

    // A late joiner catches up in order.
    let mut c = connect(&h, &h.carol, Some(0)).await;
    for seq in 1..=3 {
        assert_eq!(expect(&mut c, "ops").await["batch"]["seq"], seq);
    }
    assert_eq!(expect(&mut c, "catch_up_complete").await["head_seq"], 3);

    // Viewers cannot edit.
    submit(&mut c, "c1", 3, json!([set_prop("Box", "Height", 1.0)])).await;
    assert_eq!(expect(&mut c, "reject").await["reason"], "forbidden");

    // Invalid ops are rejected before touching the store.
    submit(&mut a, "a3", 3, json!([{"op":"remove_object","name":"9bad"}])).await;
    assert_eq!(expect(&mut a, "reject").await["reason"], "invalid_op");
}

#[tokio::test]
async fn locks_block_others_and_release_on_disconnect() {
    let h = harness(3).await;
    let mut a = connect(&h, &h.alice, None).await;
    expect(&mut a, "catch_up_complete").await;
    let mut b = connect(&h, &h.bob, None).await;
    expect(&mut b, "catch_up_complete").await;

    send(&mut a, json!({"type":"lock","objects":["Box"]})).await;
    expect(&mut a, "locked").await;
    let locked = expect(&mut b, "locked").await;
    assert_eq!(locked["objects"], json!(["Box"]));

    send(&mut b, json!({"type":"lock","objects":["Box"]})).await;
    expect(&mut b, "lock_denied").await;

    submit(&mut b, "b1", 0, json!([set_prop("Box", "Length", 1.0)])).await;
    assert_eq!(expect(&mut b, "reject").await["reason"], "locked_by_other");

    a.close(None).await.unwrap();
    let unlocked = expect(&mut b, "unlocked").await;
    assert_eq!(unlocked["objects"], json!(["Box"]));
    expect(&mut b, "left").await;

    submit(&mut b, "b2", 0, json!([set_prop("Box", "Length", 1.0)])).await;
    expect(&mut b, "ack").await;
}

#[tokio::test]
async fn snapshots_compact_history_and_force_resync() {
    let h = harness(1).await;
    let mut a = connect(&h, &h.alice, None).await;
    expect(&mut a, "catch_up_complete").await;
    for i in 1..=3 {
        submit(&mut a, &format!("a{i}"), i - 1, json!([set_prop("Box", "Length", i as f64)])).await;
        expect(&mut a, "ack").await;
    }

    let fcstd = b"PK\x03\x04fake-fcstd-archive".to_vec();
    let req = Request::put(format!("/v1/documents/{}/snapshot?seq=2", h.doc))
        .header("authorization", format!("Bearer {}", h.alice))
        .body(Body::from(fcstd.clone()))
        .unwrap();
    let resp = h.server.router.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    let announced = expect(&mut a, "snapshot_available").await;
    assert_eq!(announced["snapshot"]["seq"], 2);

    let req = Request::get(format!("/v1/documents/{}/snapshot", h.doc))
        .header("authorization", format!("Bearer {}", h.bob))
        .body(Body::empty())
        .unwrap();
    let resp = h.server.router.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(resp.headers()["x-snapshot-seq"], "2");
    let body = resp.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(body.as_ref(), fcstd.as_slice());

    // Ops <= 2 are compacted.
    let req = Request::get(format!("/v1/documents/{}/ops?after=0", h.doc))
        .header("authorization", format!("Bearer {}", h.bob))
        .body(Body::empty())
        .unwrap();
    let resp = h.server.router.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::GONE);

    // A client far behind is told to resync...
    let mut b = connect(&h, &h.bob, Some(0)).await;
    let r = expect(&mut b, "resync_required").await;
    assert_eq!(r["snapshot"]["seq"], 2);

    // ...and one starting from the snapshot gets only the tail.
    let mut b = connect(&h, &h.bob, Some(2)).await;
    assert_eq!(expect(&mut b, "ops").await["batch"]["seq"], 3);
    expect(&mut b, "catch_up_complete").await;

    // Non-zip uploads are refused.
    let req = Request::put(format!("/v1/documents/{}/snapshot?seq=3", h.doc))
        .header("authorization", format!("Bearer {}", h.alice))
        .body(Body::from("not a zip"))
        .unwrap();
    let resp = h.server.router.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn auth_and_membership_are_enforced() {
    let h = harness(3).await;
    let get = |token: Option<&str>, path: String| {
        let mut b = Request::get(path);
        if let Some(t) = token {
            b = b.header("authorization", format!("Bearer {t}"));
        }
        h.server.router.clone().oneshot(b.body(Body::empty()).unwrap())
    };

    assert_eq!(get(None, "/v1/me".into()).await.unwrap().status(), StatusCode::UNAUTHORIZED);
    assert_eq!(get(Some("fcc_nope"), "/v1/me".into()).await.unwrap().status(), StatusCode::UNAUTHORIZED);
    assert_eq!(get(Some(&h.bob), "/v1/me".into()).await.unwrap().status(), StatusCode::OK);

    let other = Uuid::new_v4();
    let resp = get(Some(&h.bob), format!("/v1/documents/{other}")).await.unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // Editors cannot delete; owners can, which also kicks live sessions.
    let mut b = connect(&h, &h.bob, None).await;
    expect(&mut b, "catch_up_complete").await;
    let del = |t: &str| {
        Request::delete(format!("/v1/documents/{}", h.doc))
            .header("authorization", format!("Bearer {t}"))
            .body(Body::empty())
            .unwrap()
    };
    let resp = h.server.router.clone().oneshot(del(&h.bob)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let resp = h.server.router.clone().oneshot(del(&h.alice)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);
    let err = expect(&mut b, "error").await;
    assert_eq!(err["code"], "document_deleted");
}

const ADMIN: &str = "test-admin-token-0123456789abcdef0123456789";

async fn admin_call(h: &Harness, method: &str, path: &str, token: &str, body: Option<Value>) -> (StatusCode, Value) {
    let mut b = Request::builder().method(method).uri(path).header("authorization", format!("Bearer {token}"));
    let body = match body {
        Some(v) => {
            b = b.header("content-type", "application/json");
            Body::from(v.to_string())
        }
        None => Body::empty(),
    };
    let resp = h.server.router.clone().oneshot(b.body(body).unwrap()).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

#[tokio::test]
async fn admin_console_and_api() {
    let h = harness_with(3, Some(ADMIN)).await;

    // UI is served with a strict CSP.
    let resp = h.server.router.clone()
        .oneshot(Request::get("/admin").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert!(resp.headers()["content-security-policy"].to_str().unwrap().contains("script-src 'self'"));
    for asset in ["/admin/app.js", "/admin/app.css"] {
        let r = h.server.router.clone().oneshot(Request::get(asset).body(Body::empty()).unwrap()).await.unwrap();
        assert_eq!(r.status(), StatusCode::OK, "{asset}");
    }

    // Wrong or user tokens are refused.
    assert_eq!(admin_call(&h, "GET", "/v1/admin/overview", "nope", None).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(admin_call(&h, "GET", "/v1/admin/overview", &h.alice, None).await.0, StatusCode::UNAUTHORIZED);

    let mut b = connect(&h, &h.bob, None).await;
    expect(&mut b, "catch_up_complete").await;

    let (s, o) = admin_call(&h, "GET", "/v1/admin/overview", ADMIN, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(o["stats"]["users"], 3);
    assert_eq!(o["live"][0]["participants"][0]["user_name"], "bob");

    // Create a user, their token works, list and revoke tokens.
    let (s, created) = admin_call(&h, "POST", "/v1/admin/users", ADMIN, Some(json!({"name":"dave"}))).await;
    assert_eq!(s, StatusCode::CREATED);
    let dave_token = created["token"]["token"].as_str().unwrap().to_string();
    let dave_id = created["user"]["id"].as_str().unwrap().to_string();
    assert_eq!(admin_call(&h, "GET", "/v1/me", &dave_token, None).await.0, StatusCode::OK);
    let (_, tokens) = admin_call(&h, "GET", &format!("/v1/admin/users/{dave_id}/tokens"), ADMIN, None).await;
    let token_id = tokens[0]["id"].as_str().unwrap().to_string();
    assert_eq!(admin_call(&h, "DELETE", &format!("/v1/admin/tokens/{token_id}"), ADMIN, None).await.0, StatusCode::NO_CONTENT);
    assert_eq!(admin_call(&h, "GET", "/v1/me", &dave_token, None).await.0, StatusCode::UNAUTHORIZED);

    // Membership through the admin API.
    let doc = h.doc;
    let (s, m) = admin_call(&h, "PUT", &format!("/v1/admin/documents/{doc}/members"), ADMIN,
        Some(json!({"user_name":"dave","role":"viewer"}))).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(m["role"], "viewer");
    let (_, docs) = admin_call(&h, "GET", "/v1/admin/documents", ADMIN, None).await;
    assert_eq!(docs[0]["member_count"], 4);
    assert_eq!(docs[0]["owner_name"], "alice");
    assert_eq!(docs[0]["live_sessions"], 1);

    // Disabling a user disconnects their live sessions.
    let bob_id = admin_call(&h, "GET", "/v1/me", &h.bob, None).await.1["id"].as_str().unwrap().to_string();
    assert_eq!(admin_call(&h, "PUT", &format!("/v1/admin/users/{bob_id}/disabled"), ADMIN,
        Some(json!({"disabled": true}))).await.0, StatusCode::NO_CONTENT);
    let err = expect(&mut b, "error").await;
    assert_eq!(err["code"], "access_revoked");
    assert_eq!(admin_call(&h, "GET", "/v1/me", &h.bob, None).await.0, StatusCode::UNAUTHORIZED);

    // Admin-created documents and deletion.
    let (s, d) = admin_call(&h, "POST", "/v1/admin/documents", ADMIN,
        Some(json!({"name":"Spare","owner_name":"carol"}))).await;
    assert_eq!(s, StatusCode::CREATED);
    let id = d["id"].as_str().unwrap();
    assert_eq!(admin_call(&h, "DELETE", &format!("/v1/admin/documents/{id}"), ADMIN, None).await.0, StatusCode::NO_CONTENT);
}

#[tokio::test]
async fn chat_is_broadcast_persisted_and_marks_agents() {
    let h = harness_with(3, Some(ADMIN)).await;
    let (_, created) = admin_call(&h, "POST", "/v1/admin/users", ADMIN,
        Some(json!({"name":"designbot","kind":"agent"}))).await;
    assert_eq!(created["user"]["kind"], "agent");
    let bot_token = created["token"]["token"].as_str().unwrap().to_string();
    admin_call(&h, "PUT", &format!("/v1/admin/documents/{}/members", h.doc), ADMIN,
        Some(json!({"user_name":"designbot","role":"editor"}))).await;

    let mut a = connect(&h, &h.alice, None).await;
    expect(&mut a, "catch_up_complete").await;
    let mut bot = connect(&h, &bot_token, None).await;
    let joined = expect(&mut a, "joined").await;
    assert_eq!(joined["participant"]["kind"], "agent");
    expect(&mut bot, "catch_up_complete").await;

    send(&mut a, json!({"type":"chat","text":"@designbot add 2 mm fillets"})).await;
    let mine = expect(&mut a, "chat").await;
    assert_eq!(mine["message"]["user_name"], "alice");
    let seen = expect(&mut bot, "chat").await;
    assert_eq!(seen["message"]["text"], "@designbot add 2 mm fillets");
    send(&mut bot, json!({"type":"chat","text":"Done: filleted 12 edges."})).await;
    let reply = expect(&mut a, "chat").await;
    assert_eq!(reply["message"]["user_kind"], "agent");

    // History: in the welcome of a new session and over REST.
    let mut c = connect(&h, &h.carol, None).await;
    let welcome = expect(&mut c, "welcome").await;
    assert_eq!(welcome["chat"].as_array().unwrap().len(), 2);
    let (s, hist) = admin_call(&h, "GET", &format!("/v1/documents/{}/chat", h.doc), &h.bob, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(hist[1]["text"], "Done: filleted 12 edges.");

    // Empty and oversized messages are refused.
    send(&mut a, json!({"type":"chat","text":"   "})).await;
    assert_eq!(expect(&mut a, "error").await["code"], "bad_message");
}
