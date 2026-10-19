from __future__ import annotations

from typing import Any

from cairn.ui.pages import concepts
from tests.conftest import UI


async def api(ui: UI, path: str, token: str = "", **body: Any) -> Any:
    resp = await ui.client.post(f"/api/v1/{path}", headers={"Authorization": f"Bearer {token or ui.api_token}"},
                                json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def node(ui: UI, parent: str, slug: str, title: str, token: str = "") -> str:
    where = {"project" if ":p" in parent else "node": parent}
    fields = {"slug": slug, "title": title, "summary": f"{title} notes"}
    ref: str = (await api(ui, "write", token, op="create_node", node_fields=fields, **where))["ref"]
    return ref


async def add(ui: UI, parent: str, title: str, body: str = "", token: str = "", **entry: Any) -> str:
    fields = {"type": "gotcha", "title": title, "body": body, **entry}
    ref: str = (await api(ui, "write", token, op="add", node=parent, entry=fields))["ref"]
    return ref


async def personal(ui: UI) -> str:
    await ui.login()
    ref: str = (await api(ui, "map"))["stores"][0]["projects"][0]["ref"]
    return ref


async def post(ui: UI, url: str, **data: str) -> Any:
    return await ui.client.post(url, headers=ui.csrf(), data=data)


async def test_home_lists_projects_and_nodes(local_ui: UI) -> None:
    ui = local_ui
    project = await personal(ui)
    empty = (await ui.client.get("/ui")).text
    assert "Personal" in empty and "No nodes yet" in empty and "New top-level node" in empty
    billing = await node(ui, project, "billing", "Billing service")
    stripe = await node(ui, billing, "stripe", "Stripe webhooks")
    await add(ui, stripe, "Stripe webhooks retry for up to 3 days")
    page = (await ui.client.get("/ui")).text
    assert f'href="/ui/r/{billing}">Billing service' in page and f'href="/ui/r/{stripe}">Stripe webhooks' in page
    assert 'title="entries">1<' in page and f"/ui/graph?project={project}" in page and "admin" in page
    made = await post(ui, f"/ui/r/{project}/node", slug="auth", title="Auth", summary="", keywords="login, sso")
    assert made.status_code == 204 and made.headers["hx-redirect"].startswith("/ui/r/me:n")
    bad = await post(ui, f"/ui/r/{project}/node", slug="Bad Slug", title="x")
    assert bad.status_code == 200 and "slug must match" in bad.text
    proj = (await ui.client.get(f"/ui/r/{project}")).text
    assert "Billing service" in proj and "Stripe webhooks" in proj and "Auth" in proj and "Graph" in proj


async def test_entry_page_shows_body_links_and_history(local_ui: UI) -> None:
    ui = local_ui
    billing = await node(ui, await personal(ui), "billing", "Billing service")
    first = await add(ui, billing, "Stripe webhooks retry for up to 3 days", "Handlers must be <idempotent>.\nAlways.",
                      keywords=["payments"])
    second = await add(ui, billing, "Idempotency keys are stored per event id", f"Because of {first}.",
                       type="decision", links=[{"to": first, "reason": "retries replay the same event"}])
    page = (await ui.client.get(f"/ui/r/{second}")).text
    assert "Idempotency keys are stored per event id" in page and "decision" in page
    assert f'Because of <a href="/ui/r/{first}">{first}</a>.' in page
    assert "Links to" in page and "retries replay the same event" in page
    assert f'href="/ui/r/{billing}">billing</a>' in page and "rev 1" in page and "Mark verified" in page
    back = (await ui.client.get(f"/ui/r/{first}")).text
    assert "&lt;idempotent&gt;.<br>Always." in back and "payments" in back
    assert "Linked from" in back and "Idempotency keys are stored per event id" in back
    hist = (await ui.client.get(f"/ui/r/{first}/history")).text
    assert "rev 1" in hist and "Stripe webhooks retry" in hist
    assert (await post(ui, f"/ui/r/{first}/verify")).headers["hx-refresh"] == "true"


async def test_edit_updates_and_stale_revision_conflicts(local_ui: UI) -> None:
    ui = local_ui
    billing = await node(ui, await personal(ui), "billing", "Billing service")
    other = await add(ui, billing, "Invoices are generated nightly by a cron job")
    entry = await add(ui, billing, "Stripe webhooks retry for up to 3 days")
    form = {"base_revision": "1", "type": "reference", "title": "Stripe retries webhooks for 72 hours",
            "body": "Use idempotent handlers.", "keywords": "stripe, payments", "to0": other,
            "reason0": "the cron replays failures", "to1": "", "reason1": ""}
    done = await post(ui, f"/ui/r/{entry}/edit", **form)
    assert done.status_code == 204 and done.headers["hx-refresh"] == "true"
    page = (await ui.client.get(f"/ui/r/{entry}")).text
    assert "Stripe retries webhooks for 72 hours" in page and "rev 2" in page and "reference" in page
    assert "the cron replays failures" in page
    stale = await post(ui, f"/ui/r/{entry}/edit", **form)
    assert stale.status_code == 200 and "conflict" in stale.text and "Reload" in stale.text
    dup = await post(ui, f"/ui/r/{billing}/add", type="gotcha", title="Invoices are generated nightly by cron")
    assert "possible duplicates" in dup.text and other in dup.text and 'name="distinct_from"' in dup.text
    again = await post(ui, f"/ui/r/{billing}/add", type="gotcha", title="Invoices are generated nightly by cron",
                       distinct_from=other)
    assert again.status_code == 204 and again.headers["hx-redirect"].startswith("/ui/r/me:e")


async def test_archive_shows_tombstone_then_unarchive(local_ui: UI) -> None:
    ui = local_ui
    billing = await node(ui, await personal(ui), "billing", "Billing service")
    entry = await add(ui, billing, "Stripe webhooks retry for up to 3 days", "Handlers must be idempotent.")
    assert (await post(ui, f"/ui/r/{entry}/archive", reason="moved to docs")).headers["hx-refresh"] == "true"
    gone = (await ui.client.get(f"/ui/r/{entry}")).text
    assert "archived" in gone and "Unarchive" in gone and "Handlers must be idempotent" not in gone
    assert "Stripe webhooks retry" not in (await ui.client.get(f"/ui/r/{billing}")).text
    assert (await post(ui, f"/ui/r/{entry}/unarchive")).status_code == 204
    assert "Handlers must be idempotent" in (await ui.client.get(f"/ui/r/{entry}")).text
    assert (await post(ui, f"/ui/r/{billing}/archive")).status_code == 204
    inside = (await ui.client.get(f"/ui/r/{entry}")).text
    assert "inside an archived node" in inside and "hx-post" not in inside
    assert "Unarchive" in (await ui.client.get(f"/ui/r/{billing}")).text
    missing = (await ui.client.get("/ui/r/me:e999")).text
    assert "Not found" in missing and "Unarchive" not in missing


async def test_node_page_actions(local_ui: UI) -> None:
    ui = local_ui
    project = await personal(ui)
    billing = await node(ui, project, "billing", "Billing service")
    page = (await ui.client.get(f"/ui/r/{billing}")).text
    assert "Billing service notes" in page and f"/ui/graph?project={project}" in page
    assert all(a in page for a in ("Add entry", "New child node", "Edit node", "Archive", "Flag (inject"))
    added = await post(ui, f"/ui/r/{billing}/add", type="practice", title="Run stripe listen locally", body="")
    entry = added.headers["hx-redirect"].removeprefix("/ui/r/")
    child = await post(ui, f"/ui/r/{billing}/node", slug="stripe", title="Stripe", summary="Webhooks")
    stripe = child.headers["hx-redirect"].removeprefix("/ui/r/")
    edited = await post(ui, f"/ui/r/{billing}/update-node", base_revision="1", title="Billing", summary="Invoices",
                        keywords="", to0=stripe, reason0="billing calls stripe")
    assert edited.status_code == 204
    moves = (await ui.client.get(f"/ui/r/{entry}/move?project={project}&node={billing}")).text
    assert f'value="{stripe}"' in moves and f'value="{billing}"' not in moves
    assert (await post(ui, f"/ui/r/{entry}/move", node=stripe)).status_code == 204
    assert "Run stripe listen locally" in (await ui.client.get(f"/ui/r/{stripe}")).text
    assert (await post(ui, f"/ui/r/{billing}/flag")).status_code == 204
    flagged = (await ui.client.get(f"/ui/r/{billing}")).text
    assert "injected" in flagged and "Unflag" in flagged and "billing calls stripe" in flagged
    assert "Invoices" in flagged and "Run stripe listen locally" not in flagged
    unknown = await post(ui, f"/ui/r/{billing}/explode")
    assert "unknown action" in unknown.text


async def test_search_groups_hits_by_node(local_ui: UI) -> None:
    ui = local_ui
    project = await personal(ui)
    billing = await node(ui, project, "billing", "Billing service")
    auth = await node(ui, project, "auth", "Auth")
    await add(ui, billing, "Stripe webhooks retry for up to 3 days")
    await add(ui, billing, "Invoice emails retry with backoff", type="practice")
    await add(ui, auth, "Login retry is rate limited per address")
    page = (await ui.client.get("/ui/search?q=retry")).text
    assert page.count('class="group"') == 2 and f'href="/ui/r/{billing}"' in page and f'href="/ui/r/{auth}"' in page
    assert "matched: retry (title/keywords)" in page and 'value="retry"' in page
    typed = (await ui.client.get("/ui/search?q=retry&types=practice")).text
    assert "Invoice emails" in typed and "Login retry" not in typed
    assert "No matches" in (await ui.client.get("/ui/search?q=kubernetes")).text
    assert "Commas separate" in (await ui.client.get("/ui/search")).text
    assert concepts("stripe webhook") == ["stripe", "webhook"]
    assert concepts("stripe | webhook, retry|backoff ,") == ["stripe|webhook", "retry|backoff"]


async def _team(ui: UI) -> tuple[str, str, str]:
    await api(ui, "admin", op="create_team", args={"slug": "eng", "name": "Engineering"})
    project = (await api(ui, "admin", op="create_project", args={"team": "eng", "slug": "platform",
                                                                  "name": "Platform"}))["result"]["project"]
    billing = await node(ui, project, "billing", "Billing service")
    return project, billing, await add(ui, billing, "Stripe webhooks retry for up to 3 days", "Be idempotent.")


async def _login_as(ui: UI, email: str, project: str | None = None, role: str = "read") -> None:
    key = (await api(ui, "admin", op="create_user", args={"email": email}))["result"]["api_key"]
    await api(ui, "admin", op="set_team_member", args={"team": "eng", "email": email, "role": "member"})
    if project:
        await api(ui, "admin", op="set_project_member", args={"project": project, "email": email, "role": role})
    ui.api_token = key
    await ui.login()


async def test_member_without_projects_sees_nothing(hosted_ui: UI) -> None:
    ui = hosted_ui
    project, billing, entry = await _team(ui)
    await _login_as(ui, "otto@acme.dev")
    home = (await ui.client.get("/ui")).text
    assert "No projects visible" in home and "Platform" not in home
    for ref in (project, billing, entry):
        assert "Not found" in (await ui.client.get(f"/ui/r/{ref}")).text
    assert "No matches" in (await ui.client.get("/ui/search?q=stripe")).text


async def test_reader_writes_show_read_only(hosted_ui: UI) -> None:
    ui = hosted_ui
    project, billing, entry = await _team(ui)
    await _login_as(ui, "rita@acme.dev", project)
    page = (await ui.client.get(f"/ui/r/{entry}")).text
    assert "Be idempotent." in page and "hx-post" not in page
    assert "New top-level node" not in (await ui.client.get("/ui")).text
    edit = await post(ui, f"/ui/r/{entry}/edit", base_revision="1", type="gotcha", title="Changed", body="")
    assert edit.status_code == 200 and "read-only access" in edit.text
    assert "read-only access" in (await post(ui, f"/ui/r/{billing}/add", type="gotcha", title="New fact")).text


async def test_posts_without_csrf_are_refused(hosted_ui: UI) -> None:
    ui = hosted_ui
    _, _, entry = await _team(ui)
    await ui.login()
    stale = await ui.client.post(f"/ui/r/{entry}/archive", data={"reason": "x"}, headers={"HX-Request": "true"})
    assert stale.status_code == 204 and stale.headers["hx-refresh"] == "true"
    assert "Be idempotent." in (await ui.client.get(f"/ui/r/{entry}")).text  # nothing was archived
    token = ui.csrf()["X-CSRF"]
    assert (await ui.client.post(f"/ui/r/{entry}/verify", data={"csrf": token})).status_code == 204


async def test_edit_keeps_links_the_page_cannot_show(local_ui: UI) -> None:
    ui = local_ui
    billing = await node(ui, await personal(ui), "billing", "Billing service")
    target = await add(ui, billing, "Invoices are generated nightly by a cron job")
    entry = await add(ui, billing, "Stripe webhooks retry for up to 3 days",
                      links=[{"to": target, "reason": "the cron replays failures"}])
    await api(ui, "write", op="archive", ref=target)
    assert 'name="was0"' not in (await ui.client.get(f"/ui/r/{entry}")).text
    form = {"base_revision": "1", "type": "gotcha", "title": "Stripe retries webhooks for 72 hours", "body": "",
            "keywords": "", "to0": "", "reason0": "", "to1": "", "reason1": ""}
    assert (await post(ui, f"/ui/r/{entry}/edit", **form)).status_code == 204
    await api(ui, "write", op="unarchive", ref=target)
    page = (await ui.client.get(f"/ui/r/{entry}")).text
    assert "Stripe retries webhooks for 72 hours" in page and "the cron replays failures" in page
    assert f'name="was0" value="{target}"' in page
    shown = {"was0": target, "why0": "the cron replays failures"}
    assert (await post(ui, f"/ui/r/{entry}/edit", **form | shown | {"base_revision": "2"})).status_code == 204
    assert "the cron replays failures" not in (await ui.client.get(f"/ui/r/{entry}")).text


async def test_untrusted_text_is_escaped(local_ui: UI) -> None:
    ui = local_ui
    await ui.login()
    evil = '<img src=x onerror=alert(1)>"'
    project = (await api(ui, "admin", op="create_project",
                         args={"team": "me", "slug": "x", "name": evil}))["result"]["project"]
    billing = await node(ui, project, "billing", evil)
    entry = await add(ui, billing, f"Webhooks retry {evil}", f"Body {evil}")
    await add(ui, billing, "Invoices are numbered per account", links=[{"to": entry, "reason": f"why {evil}"}])
    urls = ["/ui", f"/ui/r/{project}", f"/ui/r/{billing}", f"/ui/r/{entry}", f"/ui/r/{entry}/history",
            "/ui/search?q=retry", f"/ui/graph?project={project}", "/ui/changes", f"/ui/r/{evil}"]
    for url in [*urls, "/ui/archived"]:
        if url == "/ui/archived":
            await api(ui, "write", op="archive", ref=billing, reason=evil)
        text = (await ui.client.get(url)).text
        assert "<img" not in text and "&lt;img" in text, url
