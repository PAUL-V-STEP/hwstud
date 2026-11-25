ALTER TABLE groups ADD COLUMN enroll_until TEXT;
ALTER TABLE groups ADD COLUMN enroll_window_started TEXT;
UPDATE groups SET enroll_open = 0;

ALTER TABLE students ADD COLUMN key_hash TEXT;
ALTER TABLE students ADD COLUMN key_issued_at TEXT;
ALTER TABLE students ADD COLUMN key_issued_by TEXT
    CHECK (key_issued_by IN ('student', 'admin', 'transition') OR key_issued_by IS NULL);
ALTER TABLE students ADD COLUMN key_failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE students ADD COLUMN key_locked_until TEXT;
ALTER TABLE students ADD COLUMN last_login_at TEXT;

INSERT OR IGNORE INTO settings(key, value) VALUES
    ('registration_window_minutes', '10'),
    ('class_code_required', '0'),
    ('class_code_period', '120');
