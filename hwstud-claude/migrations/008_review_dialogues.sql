ALTER TABLE review_requests ADD COLUMN question TEXT NOT NULL DEFAULT '';

UPDATE review_requests
SET question = 'Уточняющий вопрос не указан в запросе прежней версии'
WHERE trim(question) = '';

CREATE TABLE review_messages (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES review_requests(id) ON DELETE CASCADE,
    author TEXT NOT NULL CHECK (author IN ('student', 'teacher')),
    body TEXT NOT NULL CHECK (length(trim(body)) > 0 AND length(body) <= 2000),
    created_at TEXT NOT NULL
);

CREATE INDEX review_messages_request_idx ON review_messages(request_id, id);

INSERT INTO review_messages(request_id, author, body, created_at)
SELECT id, 'teacher', answer, created_at FROM review_requests
WHERE answer IS NOT NULL AND trim(answer) <> '';
