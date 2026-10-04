CREATE TABLE users (
    id          TEXT PRIMARY KEY NOT NULL,
    name        TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE api_tokens (
    id            TEXT PRIMARY KEY NOT NULL,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    label         TEXT,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER,
    revoked_at    INTEGER
);
CREATE INDEX api_tokens_user ON api_tokens(user_id);

CREATE TABLE documents (
    id           TEXT PRIMARY KEY NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT,
    owner_id     TEXT NOT NULL REFERENCES users(id),
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    head_seq     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE document_members (
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    added_at     INTEGER NOT NULL,
    PRIMARY KEY (document_id, user_id)
);
CREATE INDEX document_members_user ON document_members(user_id);

CREATE TABLE ops (
    document_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    author_id     TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    client_op_id  TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (document_id, seq)
);
CREATE UNIQUE INDEX ops_idempotency ON ops(document_id, author_id, client_op_id);

CREATE TABLE snapshots (
    id           TEXT PRIMARY KEY NOT NULL,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    author_id    TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    data         BLOB NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX snapshots_document_seq ON snapshots(document_id, seq DESC, created_at DESC);
