from __future__ import annotations

from tests.conftest import UI, cookie_name_for


async def test_logged_out_pages_redirect_to_login(local_ui: UI) -> None:
    resp = await local_ui.client.get("/ui/changes?x=1")
    assert resp.status_code == 303 and resp.headers["location"] == "/ui/login?next=/ui/changes%3Fx%3D1"
    htmx = await local_ui.client.get("/ui/changes", headers={"HX-Request": "true"})
    assert htmx.status_code == 204 and htmx.headers["hx-redirect"] == "/ui/login"
    assert (await local_ui.client.get("/")).headers["location"] == "/ui"
    assert "cairn ui" in (await local_ui.client.get("/ui/login")).text


async def test_ticket_login_sets_a_session_cookie(local_ui: UI) -> None:
    await local_ui.login()
    cookie = local_ui.client.cookies[cookie_name_for(local_ui.app)]
    assert cookie and cookie_name_for(local_ui.app) == "cairn_session_6804"
    bad = await local_ui.client.get("/ui/login?ticket=nope")
    assert bad.status_code == 400 and "invalid or expired" in bad.text


async def test_posts_need_the_csrf_token(local_ui: UI) -> None:
    await local_ui.login()
    stale = await local_ui.client.post("/ui/logout", headers={"Referer": "http://127.0.0.1:6804/ui"})
    assert stale.status_code == 303 and stale.headers["location"] == "/ui"
    away = await local_ui.client.post("/ui/logout", headers={"Referer": "https://evil.example/ui/x"})
    assert away.headers["location"] == "/ui"
    out = await local_ui.client.post("/ui/logout", headers=local_ui.csrf())
    assert out.status_code == 303 and out.headers["location"] == "/ui/login"


async def test_local_ui_keeps_the_host_check(local_ui: UI) -> None:
    resp = await local_ui.client.get("/ui/login", headers={"Host": "evil.example"})
    assert resp.status_code == 421


async def test_hosted_password_flow(hosted_ui: UI) -> None:
    c = hosted_ui.client
    link = hosted_ui.app.state.password_link
    token = link.split("token=")[1]
    assert "Set your password" in (await c.get(link)).text
    mismatch = await c.post("/ui/set-password", data={"token": token, "password": "a" * 12, "confirm": "b" * 12})
    assert mismatch.status_code == 400 and "match" in mismatch.text
    password = "root password!"
    done = await c.post("/ui/set-password", data={"token": token, "password": password, "confirm": password})
    assert done.status_code == 303
    wrong = await c.post("/ui/login", data={"email": "root@acme.dev", "password": "nope"})
    assert wrong.status_code == 401
    ok = await c.post("/ui/login", data={"email": "root@acme.dev", "password": "root password!", "next": "/ui/admin"})
    assert ok.status_code == 303 and ok.headers["location"] == "/ui/admin" and c.cookies["cairn_session"]
    evil = await c.post("/ui/login", data={"email": "root@acme.dev", "password": "root password!",
                                            "next": "https://evil.example"})
    assert evil.headers["location"] == "/ui"
