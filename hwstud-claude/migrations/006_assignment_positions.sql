CREATE TABLE assignments_new (
    id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 10),
    title TEXT NOT NULL,
    condition TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT '',
    criteria TEXT NOT NULL DEFAULT '',
    typical_errors TEXT NOT NULL DEFAULT '',
    threshold REAL NOT NULL DEFAULT 0.7 CHECK (threshold BETWEEN 0 AND 1),
    due_at TEXT,
    max_attempts INTEGER NOT NULL DEFAULT 4 CHECK (max_attempts = 4),
    position INTEGER NOT NULL UNIQUE
);

INSERT INTO assignments_new(
    id, title, condition, reference, criteria, typical_errors, threshold,
    due_at, max_attempts, position
)
SELECT id, title, condition, reference, criteria, typical_errors, threshold,
       due_at, max_attempts, id
FROM assignments;

DROP TABLE assignments;
ALTER TABLE assignments_new RENAME TO assignments;

INSERT OR IGNORE INTO settings(key, value) VALUES (
    'assignments_count',
    (SELECT CAST(COUNT(*) AS TEXT) FROM assignments)
);
