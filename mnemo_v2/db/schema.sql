PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conversations (
  conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  title TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(agent_id, session_id)
);

CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant', 'tool')),
  content TEXT NOT NULL,
  token_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  UNIQUE(conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS message_parts (
  part_id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  part_type TEXT NOT NULL CHECK(part_type IN (
    'text', 'reasoning', 'tool', 'patch', 'file', 'subtask',
    'compaction', 'snapshot', 'retry', 'agent', 'metadata'
  )),
  content TEXT,
  data_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE,
  UNIQUE(message_id, ordinal)
);

CREATE TABLE IF NOT EXISTS summaries (
  summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('leaf', 'condensed')),
  depth INTEGER NOT NULL DEFAULT 0,
  content TEXT NOT NULL,
  token_count INTEGER NOT NULL DEFAULT 0,
  earliest_seq INTEGER,
  latest_seq INTEGER,
  descendant_count INTEGER NOT NULL DEFAULT 0,
  descendant_token_count INTEGER NOT NULL DEFAULT 0,
  source_message_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS summary_messages (
  summary_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  PRIMARY KEY(summary_id, message_id),
  FOREIGN KEY(summary_id) REFERENCES summaries(summary_id) ON DELETE CASCADE,
  FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS summary_sources (
  summary_id INTEGER NOT NULL,
  source_summary_id INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  PRIMARY KEY(summary_id, source_summary_id),
  FOREIGN KEY(summary_id) REFERENCES summaries(summary_id) ON DELETE CASCADE,
  FOREIGN KEY(source_summary_id) REFERENCES summaries(summary_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS context_items (
  context_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  item_type TEXT NOT NULL CHECK(item_type IN ('message', 'summary')),
  message_id INTEGER,
  summary_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE,
  FOREIGN KEY(summary_id) REFERENCES summaries(summary_id) ON DELETE CASCADE,
  UNIQUE(conversation_id, ordinal),
  CHECK((item_type = 'message' AND message_id IS NOT NULL AND summary_id IS NULL) OR
        (item_type = 'summary' AND summary_id IS NOT NULL AND message_id IS NULL))
);

CREATE TABLE IF NOT EXISTS compaction_events (
  compaction_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL,
  trigger_type TEXT NOT NULL CHECK(trigger_type IN ('leaf', 'threshold', 'manual')),
  phase TEXT NOT NULL CHECK(phase IN ('leaf', 'condensed')),
  source_count INTEGER NOT NULL,
  summary_id INTEGER,
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(summary_id) REFERENCES summaries(summary_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ingest_events (
  ingest_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER,
  session_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ok', 'error')),
  error_text TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS raw_tape (
  tape_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_seq
  ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_context_items_conversation_ordinal
  ON context_items(conversation_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_summaries_conversation_depth
  ON summaries(conversation_id, depth, created_at);
CREATE INDEX IF NOT EXISTS idx_summary_messages_summary
  ON summary_messages(summary_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_summary_sources_summary
  ON summary_sources(summary_id, ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content,
  tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
  summary_id UNINDEXED,
  content,
  tokenize='porter unicode61'
);
