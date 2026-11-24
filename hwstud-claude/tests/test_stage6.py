from __future__ import annotations

from types import SimpleNamespace

from app.auth import Actor, create_student
from app.db import connect, utc_now_iso
from app.services import cabinet
from app.web import _back_navigation

from .conftest import csrf_from, login_admin, login_student


def add_open_lecture(settings, title="Open Lecture"):
    conn = connect(settings)
    try:
        cursor = conn.execute(
            "INSERT INTO lectures(slug, title, title_norm, status, updated_at) "
            "VALUES ('open-lecture', ?, ?, 'open', ?)",
            (title, title.lower(), utc_now_iso()),
        )
        lecture_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO slides(lecture_id, n, title, notes_html) VALUES (?, 1, 'Slide', '<p>Text</p>')",
            (lecture_id,),
        )
        return lecture_id
    finally:
        conn.close()


def test_full_help_roles_and_exam_access(client, settings):
    login_student(client, "help_student", 1)
    response = client.get("/help")
    assert "Работа с сайтом" in response.text
    assert "Работа с сайтом" in response.text
    assert "Помощь администратора" not in response.text
    assert "Проверить ИИ снова" not in response.text
    assert client.get("/help?view=teacher").status_code == 404
    response = client.get("/help/upload")
    assert "DOCX со скриншотами" in response.text
    assert "Обязательная пара PPTX + PDF" not in response.text
    conn = connect(settings)
    try:
        conn.execute("UPDATE exam SET enabled = 1 WHERE id = 1")
    finally:
        conn.close()
    assert client.get("/help").status_code == 200
    login_admin(client)
    response = client.get("/help")
    assert "Помощь администратора" in response.text
    assert "ProxyAPI" in response.text
    response = client.get("/help?view=student")
    assert "Помощь администратора" not in response.text
    response = client.get("/help/upload?view=teacher")
    assert "Обязательная пара PPTX + PDF" in response.text


def test_exam_group_access_and_remaining_site(client, settings):
    lecture_id = add_open_lecture(settings)
    login_admin(client)
    response = client.get("/admin/exam")
    response = client.post(
        "/admin/exam",
        data={
            "csrf": csrf_from(response), "enabled": "1",
            "start_at": "2020-01-01T10:00", "end_at": "2030-01-01T18:00",
            "selected_groups": ["1"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    login_student(client, "exam_one", 1)
    response = client.get("/lectures/open-lecture")
    assert "Доступ к учебным материалам закрыт" in response.text
    assert "Окончание:" in response.text
    assert client.get("/assignments").status_code == 200
    assert client.get("/messages").status_code == 200
    assert client.get("/help").status_code == 200
    login_student(client, "exam_two", 2)
    assert client.get("/lectures/open-lecture").status_code == 200


def seed_group_results(settings, count=5, all_assignments=False):
    conn = connect(settings)
    try:
        ids = []
        for index in range(count):
            row, error = create_student(conn, f"student_{index}", 1)
            assert not error
            ids.append(row["id"])
        assignments = (
            [row["id"] for row in conn.execute("SELECT id FROM assignments ORDER BY id")]
            if all_assignments
            else (1,)
        )
        for assignment in assignments:
            for index, student_id in enumerate(ids):
                conn.execute(
                    "INSERT INTO submissions(student_id, assignment_id, attempt, file, mime, size, "
                    "created_at, status, score, decided_at) VALUES (?, ?, 1, 'x', 'text/plain', 1, ?, 'accepted', ?, ?)",
                    (student_id, assignment, utc_now_iso(), index + 5, utc_now_iso()),
                )
        return ids
    finally:
        conn.close()


def test_distribution_and_swot(client, settings):
    ids = seed_group_results(settings, 5)
    login_student(client, "student_0", 1)
    response = client.get("/results?variant=1")
    assert "distribution-chart" in response.text
    assert "вы здесь" in response.text
    assert "student_1" not in response.text
    response = client.get("/cabinet")
    assert "Мой SWOT" in response.text
    assert "student_0" in response.text
    login_admin(client)
    response = client.get("/admin/settings")
    response = client.post(
        "/admin/settings/distribution",
        data={"csrf": csrf_from(response)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    login_student(client, "student_0", 1)
    assert "Кривая отключена преподавателем" in client.get("/results").text


def test_distribution_requires_five_and_sum_requires_all(client, settings):
    ids = seed_group_results(settings, 4, all_assignments=True)
    conn = connect(settings)
    try:
        scores, own = cabinet.decided_scores(conn, type("Actor", (), {"student_id": ids[0], "group_id": 1})(), "sum")
        assert len(scores) == 4
        assert own == 25
        assert cabinet.distribution_svg(scores, own, "sum") is None
        conn.execute("UPDATE settings SET value = '4' WHERE key = 'assignments_count'")
        scores, own = cabinet.decided_scores(conn, type("Actor", (), {"student_id": ids[0], "group_id": 1})(), "sum")
        assert len(scores) == 4
        assert own == 20
        conn.execute("UPDATE settings SET value = '0' WHERE key = 'assignments_count'")
        assert cabinet.decided_scores(conn, type("Actor", (), {"student_id": ids[0], "group_id": 1})(), "sum") == ([], None)
        chart = cabinet.distribution_svg([5, 6, 7, 8, 9], 5, "sum", assignment_count=5)
        assert chart["maximum"] == 50
    finally:
        conn.close()
    login_student(client, "student_0", 1)
    assert "не менее 5 результатов" in client.get("/results?variant=sum").text


def test_contextual_back_links_and_back_to_top(client, settings):
    add_open_lecture(settings)
    login_student(client, "navigation_student", 1)
    response = client.get("/glossary?lecture=open-lecture")
    assert 'href="/lectures/open-lecture"' in response.text
    assert "Вернуться к лекции" in response.text
    response = client.get("/cabinet")
    assert 'class="back-link" href="/"' in response.text
    assert 'data-back-to-top' in response.text
    response = client.get("/literature?lecture=open-lecture")
    assert "Вернуться к лекции" in response.text
    login_admin(client)
    response = client.get("/admin/groups")
    assert 'class="back-link" href="/admin"' in response.text
    response = client.get("/admin/settings/password")
    assert 'class="back-link" href="/admin/settings"' in response.text
    response = client.get("/help")
    assert 'class="back-link" href="/admin"' in response.text


def test_back_navigation_covers_nested_forms_and_fallbacks():
    admin = Actor(role="admin")
    student = Actor(role="student", student_id=1)
    cases = (
        (admin, "/admin/announcements", ("/admin", "Обзор")),
        (admin, "/admin/submissions/7", ("/admin/submissions", "К очереди проверки")),
        (admin, "/admin/lectures/import", ("/admin/lectures", "К списку лекций")),
        (admin, "/admin/imports/4", ("/admin/lectures", "К списку лекций")),
        (admin, "/admin/data/delete", ("/admin/data", "К данным курса")),
        (admin, "/admin/students/3/delete", ("/admin/groups", "К группам и никам")),
        (admin, "/admin/settings/proxyapi", ("/admin/settings", "К настройкам")),
        (admin, "/admin/new-form", ("/admin", "Обзор")),
        (student, "/lectures/open-lecture", ("/lectures", "К списку лекций")),
        (student, "/lectures/open-lecture/conspectus", ("/lectures/open-lecture", "К просмотру лекции")),
        (student, "/new-form", ("/", "Карта курса")),
        (None, "/login", ("/about", "О курсе")),
        (None, "/admin/login", ("/login", "Ко входу студента")),
        (None, "/unlisted", ("/login", "Ко входу")),
    )
    for actor, path, expected in cases:
        request = SimpleNamespace(url=SimpleNamespace(path=path))
        assert _back_navigation(request, actor, {}) == expected, path

    assert _back_navigation(SimpleNamespace(url=SimpleNamespace(path="/admin")), admin, {}) is None
    assert _back_navigation(SimpleNamespace(url=SimpleNamespace(path="/")), student, {}) is None


def test_about_editor_groups_and_session_revocation(client, app, settings):
    login_admin(client)
    response = client.get("/admin/about")
    response = client.post(
        "/admin/about",
        data={"csrf": csrf_from(response), "about_text": "Новое описание курса"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Новое описание курса" in client.get("/about").text
    response = client.get("/admin/groups")
    response = client.post(
        "/admin/groups/1",
        data={"csrf": csrf_from(response), "name": "Аналитики", "enroll_open": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    login_student(client, "blocked_student", 1)
    assert client.get("/").status_code == 200
    from fastapi.testclient import TestClient
    with TestClient(app) as admin_client:
        login_admin(admin_client)
        response = admin_client.get("/admin/groups")
        response = admin_client.post(
            "/admin/students/1/block",
            data={"csrf": csrf_from(response)},
            follow_redirects=False,
        )
        assert response.status_code == 303
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
