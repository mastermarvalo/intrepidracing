CREATE TABLE stat_boards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    sheet_id TEXT NOT NULL,
    sheet_range TEXT NOT NULL DEFAULT 'Sheet1',
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
