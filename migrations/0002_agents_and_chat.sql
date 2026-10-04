ALTER TABLE users ADD COLUMN kind TEXT NOT NULL DEFAULT 'human' CHECK (kind IN ('human', 'agent'));

CREATE TABLE chat_messages (
    id           TEXT PRIMARY KEY NOT NULL,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    session_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    user_name    TEXT NOT NULL,
    user_kind    TEXT NOT NULL,
    text         TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX chat_messages_document_time ON chat_messages(document_id, created_at DESC);
