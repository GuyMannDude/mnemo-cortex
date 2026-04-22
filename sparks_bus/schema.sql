-- Sparks Bus schema. Idempotent: safe to run on every watcher start.
-- Bus messages live here. Mnemo (full mode) holds the recallable payload by tracking_id.

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '{}',
    reply_to INTEGER REFERENCES messages(id),
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    read_at TEXT,
    -- Notification-loop state (added 2026-04-22).
    -- NULL means "not yet done"; non-NULL is the timestamp the action completed.
    tracking_id TEXT,            -- bus-{id}-{iso} or bus-reply-{id}-{iso}; A2A task.id
    mnemo_saved_at TEXT,         -- 'standalone' sentinel in standalone mode
    notified_at TEXT,            -- 📬 / 🔄 posted to dispatch channel
    pickup_notified_at TEXT,     -- ✅ posted to dispatch channel
    stale_notified_at TEXT,      -- ⚠️ STALE posted to alerts channel
    delivery_failed_at TEXT      -- ⚠️ DELIVERY FAILED posted; row excluded from retry
);

CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_agent, read);
CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages(reply_to);
CREATE INDEX IF NOT EXISTS idx_messages_notified ON messages(notified_at, read);
