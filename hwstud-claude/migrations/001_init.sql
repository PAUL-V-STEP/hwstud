CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    max_members INTEGER NOT NULL DEFAULT 20 CHECK (max_members > 0),
    enroll_open INTEGER NOT NULL DEFAULT 1 CHECK (enroll_open IN (0, 1))
);

CREATE TABLE students (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    nick TEXT NOT NULL,
    nick_norm TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
    login_token TEXT NOT NULL
);

CREATE TABLE lectures (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    num INTEGER,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'hidden' CHECK (status IN ('hidden', 'open')),
    planned_date TEXT,
    announce INTEGER NOT NULL DEFAULT 0 CHECK (announce IN (0, 1)),
    header_lines TEXT,
    files_dir TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE lecture_groups (
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (lecture_id, group_id)
);

CREATE TABLE lecture_sections (
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    first_slide INTEGER NOT NULL,
    PRIMARY KEY (lecture_id, idx)
);

CREATE TABLE slides (
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    title TEXT,
    notes_html TEXT,
    img TEXT,
    thumb TEXT,
    section_idx INTEGER,
    missing_notes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lecture_id, n)
);

CREATE TABLE slide_views (
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    viewed_at TEXT NOT NULL,
    PRIMARY KEY (student_id, lecture_id, n)
);

CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    ready_at TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'processing', 'waiting_gpt', 'paused_no_funds', 'ready', 'saving',
        'saved', 'expired', 'cancelled', 'failed'
    )),
    target_lecture_id INTEGER REFERENCES lectures(id) ON DELETE SET NULL,
    version_dir TEXT,
    report_json TEXT
);

CREATE TABLE terms (
    id INTEGER PRIMARY KEY,
    term TEXT NOT NULL,
    expansion TEXT NOT NULL,
    english TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'verified', 'hidden')),
    unused INTEGER NOT NULL DEFAULT 0 CHECK (unused IN (0, 1))
);

CREATE TABLE term_refs (
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    slide_n INTEGER NOT NULL,
    PRIMARY KEY (term_id, lecture_id, slide_n)
);

CREATE TABLE term_conflicts (
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    slide_n INTEGER NOT NULL,
    variant TEXT NOT NULL
);

CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    author TEXT,
    title TEXT NOT NULL,
    url TEXT,
    type TEXT,
    annotation TEXT,
    priority TEXT NOT NULL DEFAULT 'extra' CHECK (priority IN ('main', 'extra'))
);

CREATE TABLE source_refs (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    slide_n INTEGER NOT NULL,
    local_number INTEGER,
    PRIMARY KEY (source_id, lecture_id, slide_n)
);

CREATE TABLE assignments (
    id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 4),
    title TEXT NOT NULL,
    condition TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT '',
    criteria TEXT NOT NULL DEFAULT '',
    typical_errors TEXT NOT NULL DEFAULT '',
    threshold REAL NOT NULL DEFAULT 0.7 CHECK (threshold BETWEEN 0 AND 1),
    due_at TEXT,
    max_attempts INTEGER NOT NULL DEFAULT 4 CHECK (max_attempts = 4)
);

CREATE TABLE submissions (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 4),
    file TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    pages INTEGER,
    chars INTEGER,
    created_at TEXT NOT NULL,
    late INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN (
        'not_submitted', 'queued', 'checking', 'awaiting_approval',
        'needs_review', 'revise', 'accepted', 'exhausted'
    )),
    verdict TEXT CHECK (verdict IN ('accept', 'revise') OR verdict IS NULL),
    score INTEGER CHECK ((score BETWEEN 0 AND 10) OR score IS NULL),
    comment TEXT,
    errors_json TEXT,
    classification TEXT,
    confidence REAL,
    decided_at TEXT,
    UNIQUE (student_id, assignment_id, attempt)
);

CREATE TABLE review_requests (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered')),
    answer TEXT
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    priority INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'paused_no_funds', 'done', 'failed', 'cancelled'
    )),
    tries INTEGER NOT NULL DEFAULT 0,
    run_after TEXT NOT NULL,
    started_at TEXT,
    request_sent_at TEXT,
    error TEXT,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_est REAL
);

CREATE INDEX jobs_claim_idx ON jobs(status, run_after, priority, id);
CREATE INDEX jobs_kind_status_idx ON jobs(kind, status);

CREATE TABLE announcements (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    link TEXT,
    kind TEXT NOT NULL DEFAULT 'normal' CHECK (kind IN ('normal', 'important', 'deadline')),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    publish_at TEXT NOT NULL,
    unpublish_at TEXT
);

CREATE TABLE announcement_groups (
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (announcement_id, group_id)
);

CREATE TABLE announcement_reads (
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    read_at TEXT NOT NULL,
    PRIMARY KEY (announcement_id, student_id)
);

CREATE TABLE threads (
    student_id INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
    closed_at TEXT
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    author TEXT NOT NULL CHECK (author IN ('student', 'teacher')),
    body TEXT NOT NULL CHECK (length(body) <= 2000),
    ref_lecture INTEGER REFERENCES lectures(id) ON DELETE SET NULL,
    ref_slide INTEGER,
    ref_assignment INTEGER REFERENCES assignments(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    teacher_read_at TEXT,
    student_read_at TEXT,
    mailed_at TEXT
);

CREATE TABLE exam (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    start_at TEXT,
    end_at TEXT
);

CREATE TABLE exam_groups (
    group_id INTEGER PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE balance_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    balance REAL,
    budget_limit REAL,
    budget_used REAL,
    source TEXT NOT NULL CHECK (source IN ('poll', 'manual', 'probe'))
);

CREATE TABLE balance_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    level TEXT NOT NULL DEFAULT 'unknown' CHECK (level IN (
        'ok', 'low', 'critical', 'no_funds', 'unknown'
    )),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    paused_since TEXT,
    balance_access TEXT NOT NULL DEFAULT 'unknown' CHECK (
        balance_access IN ('allowed', 'denied', 'unknown')
    ),
    last_ok_at TEXT,
    last_error TEXT,
    gpt_stopped INTEGER NOT NULL DEFAULT 0 CHECK (gpt_stopped IN (0, 1))
);

CREATE TABLE balance_alerts (
    level TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object TEXT,
    student_id INTEGER REFERENCES students(id) ON DELETE SET NULL,
    details_json TEXT
);

CREATE TABLE maintenance (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    deletion_step TEXT CHECK (deletion_step IN (
        'db_deleted', 'db_done', 'backup_done', 'files_done'
    ) OR deletion_step IS NULL),
    started_at TEXT
);

INSERT INTO groups(id, name) VALUES
    (1, 'Группа 1'), (2, 'Группа 2'), (3, 'Группа 3'), (4, 'Группа 4');

INSERT INTO assignments(id, title) VALUES
    (1, 'Задание 1'), (2, 'Задание 2'), (3, 'Задание 3'), (4, 'Задание 4');

INSERT INTO exam(id) VALUES (1);
INSERT INTO balance_state(id) VALUES (1);
INSERT INTO maintenance(id) VALUES (1);

INSERT INTO settings(key, value) VALUES
    ('balance_low', '300'),
    ('balance_critical', '100'),
    ('reserve_factor', '1.5'),
    ('image_tokens', '1500'),
    ('price_grading_in', '260'),
    ('price_grading_out', '1300'),
    ('price_aux_in', '30'),
    ('price_aux_out', '150'),
    ('billing_url', ''),
    ('mail_mode', 'immediate'),
    ('distribution_enabled', '1'),
    ('about_text', 'Курс по бизнес-анализу и проектированию процессов.');
