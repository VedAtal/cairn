from __future__ import annotations

import re
from typing import Any

from tests.conftest import UI


async def api(ui: UI, path: str, **body: Any) -> Any:
    resp = await ui.client.post(f"/api/v1/{path}", headers={"Authorization": f"Bearer {ui.api_token}"}, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def admin(ui: UI, op: str, **args: Any) -> dict[str, Any]:
    result: dict[str, Any] = (await api(ui, "admin", op=op, args=args))["result"]
    return result


async def seed(ui: UI) -> tuple[str, str]:
    await admin(ui, "create_team", slug="eng", admin="root@acme.dev")
    project = (await admin(ui, "create_project", team="eng", slug="platform", name="Platform"))["project"]
    node = (await api(ui, "write", op="create_node", project=project,
                      node_fields={"slug": "billing", "title": "Billing"}))["ref"]
    return project, node


async def post(ui: UI, url: str, **data: str) -> str:
    resp = await ui.client.post(url, headers=ui.csrf(), data=data)
    assert resp.status_code == 200, resp.text
    return resp.text


async def test_changes_lists_recent_ops_and_paginates(hosted_ui: UI) -> None:
    _, node = await seed(hosted_ui)
    for i in range(55):
        await api(hosted_ui, "write", op="create_node", node=node, node_fields={"slug": f"n{i}", "title": "x"})
    await hosted_ui.login()
    first = await hosted_ui.client.get("/ui/changes")
    assert first.status_code == 200
    assert first.text.count("<tr>") == 51 and "create_node" in first.text and "billing/n54" in first.text
    assert "billing/n4<" not in first.text
    before = re.search(r"before=(\d+)", first.text)
    assert before
    older = (await hosted_ui.client.get(f"/ui/changes?before={before[1]}")).text
    assert "billing/n4<" in older and "billing/n54" not in older and "Newest" in older
    assert "before=" not in older.split("Newest")[1]


async def test_archived_lists_and_unarchives(hosted_ui: UI) -> None:
    _, node = await seed(hosted_ui)
    entry = (await api(hosted_ui, "write", op="add", node=node,
                       entry={"type": "general", "title": "Stripe webhooks retry for three days", "body": "b"}))["ref"]
    await api(hosted_ui, "write", op="archive", ref=entry, reason="superseded by the queue")
    await hosted_ui.login()
    page = (await hosted_ui.client.get("/ui/archived")).text
    assert "Stripe webhooks retry" in page and "superseded by the queue" in page and "Unarchive" in page
    done = await post(hosted_ui, "/ui/archived", ref=entry)
    assert f"Restored <a href=\"/ui/r/{entry}\">{entry}</a>" in done and "superseded" not in done
    assert (await api(hosted_ui, "get", refs=[entry]))["items"][0]["status"] == "ok"
    again = await post(hosted_ui, "/ui/archived", ref="acme:e999")
    assert 'class="notice error"' in again


async def test_instance_admin_manages_users_and_teams(hosted_ui: UI) -> None:
    await seed(hosted_ui)
    await hosted_ui.login()
    page = (await hosted_ui.client.get("/ui/admin")).text
    assert "<h2>Users</h2>" in page and "root@acme.dev" in page and "<strong>eng</strong>" in page
    assert "Platform" in page and "New project" in page
    created = await post(hosted_ui, "/ui/admin", op="create_user", email="wes@acme.dev", name="Wes")
    assert re.search(r'class="secret">cairn_pat_[\w-]+</div>', created)
    assert 'class="secret">http://testserver/ui/set-password?token=' in created
    team = await post(hosted_ui, "/ui/admin", op="set_team_member", team="eng", email="wes@acme.dev", role="member")
    assert "Updated wes@acme.dev in eng." in team
    made = await post(hosted_ui, "/ui/admin", op="create_team", slug="ops", admin="wes@acme.dev")
    assert "Created team ops." in made and "<strong>ops</strong>" in made
    assert "<strong>ops</strong>" in (await hosted_ui.client.get("/ui/admin")).text  # listed without membership
    project = re.search(r'name="project" value="(acme:p\d+)"', page)
    assert project
    archived = await post(hosted_ui, "/ui/admin", op="archive_project", project=project.group(1))
    assert "Unarchive" in archived
    assert "Archive" in await post(hosted_ui, "/ui/admin", op="unarchive_project", project=project.group(1))
    off = await post(hosted_ui, "/ui/admin", op="set_user_active", email="wes@acme.dev", active="false")
    assert "Updated wes@acme.dev." in off and "Activate" in off


async def test_member_sees_no_users_section_and_gets_server_errors(hosted_ui: UI) -> None:
    project, _ = await seed(hosted_ui)
    key = (await admin(hosted_ui, "create_user", email="wes@acme.dev"))["api_key"]
    await admin(hosted_ui, "set_team_member", team="eng", email="wes@acme.dev", role="member")
    await admin(hosted_ui, "set_project_member", project=project, email="wes@acme.dev", role="write")
    hosted_ui.api_token = key
    await hosted_ui.login()
    page = (await hosted_ui.client.get("/ui/admin")).text
    assert "<h2>Users</h2>" not in page and "<h2>Projects</h2>" not in page and "<strong>eng</strong>" in page
    denied = await post(hosted_ui, "/ui/admin", op="create_user", email="eve@acme.dev")
    assert "instance admin required" in denied
    team = await post(hosted_ui, "/ui/admin", op="set_team_member", team="eng", email="root@acme.dev", role="")
    assert "team admin required" in team


async def test_api_keys_can_be_created_and_revoked(hosted_ui: UI) -> None:
    await hosted_ui.login()
    created = await post(hosted_ui, "/ui/admin", op="create_key", name="laptop", read_only="true")
    key = re.search(r'class="secret">(cairn_pat_[\w-]+)</div>', created)
    assert key and "laptop" in created and "read_only" in created
    hosted_ui.api_token = key[1]
    assert (await admin(hosted_ui, "whoami"))["email"] == "root@acme.dev"
    assert key[1] not in (await hosted_ui.client.get("/ui/admin")).text
    bare = await hosted_ui.client.post("/ui/admin", data={"op": "create_key", "name": "forged"})
    assert bare.status_code == 303 and "forged" not in (await hosted_ui.client.get("/ui/admin")).text
    revoked = await post(hosted_ui, "/ui/admin", op="revoke_key", prefix=key[1][:16])
    assert "Key revoked." in revoked and "revoked 20" in revoked
    resp = await hosted_ui.client.post("/api/v1/admin", headers={"Authorization": f"Bearer {key[1]}"},
                                       json={"op": "whoami"})
    assert resp.status_code == 401
