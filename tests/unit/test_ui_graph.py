from __future__ import annotations

from typing import Any

from tests.conftest import UI


async def api(ui: UI, path: str, **body: Any) -> Any:
    resp = await ui.client.post(f"/api/v1/{path}", headers={"Authorization": f"Bearer {ui.api_token}"}, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def seed(ui: UI) -> tuple[str, str, str]:
    await api(ui, "admin", op="create_team", args={"slug": "eng"})
    await api(ui, "admin", op="create_project", args={"team": "eng", "slug": "alpha", "name": "Alpha"})
    project = (await api(ui, "admin", op="create_project", args={"team": "eng", "slug": "platform",
                                                                 "name": "Platform"}))["result"]["project"]
    billing = (await api(ui, "write", op="create_node", project=project,
                         node_fields={"slug": "billing", "title": "Billing"}))["ref"]
    hooks = (await api(ui, "write", op="create_node", node=billing,
                       node_fields={"slug": "webhooks", "title": "Webhooks"}))["ref"]
    invoices = (await api(ui, "write", op="add", node=billing,
                          entry={"type": "general", "title": "Invoices are numbered per account"}))["ref"]
    await api(ui, "write", op="add", node=hooks, entry={"type": "gotcha", "title": "Webhook signatures expire",
                                                        "links": [{"to": invoices, "reason": "numbers in payloads"}]})
    return project, billing, hooks


async def test_graph_json_has_parents_and_folded_links(hosted_ui: UI) -> None:
    project, billing, hooks = await seed(hosted_ui)
    await hosted_ui.login()
    resp = await hosted_ui.client.get(f"/ui/graph.json?project={project}")
    assert resp.status_code == 200
    data = resp.json()
    assert [(n["ref"], n["title"], n["parent"], n["entries"], n["inject"]) for n in data["nodes"]] == [
        (billing, "Billing", None, 1, False), (hooks, "Webhooks", billing, 1, False)]
    assert data["edges"] == [{"source": hooks, "target": billing, "count": 1, "reasons": ["numbers in payloads"]}]
    bad = await hosted_ui.client.get("/ui/graph.json?project=nope")
    assert bad.status_code == 400 and bad.json()["error"]


async def test_graph_page_has_project_selector(hosted_ui: UI) -> None:
    project, _, _ = await seed(hosted_ui)
    await hosted_ui.login()
    first = await hosted_ui.client.get("/ui/graph")
    assert first.status_code == 200 and 'id="graph-project"' in first.text and "eng / Alpha" in first.text
    assert 'data-project="acme:p1"' in first.text and "cytoscape.min.js" in first.text
    page = await hosted_ui.client.get(f"/ui/graph?project={project}")
    assert f'<option value="{project}" selected>eng / Platform</option>' in page.text
    assert f'data-project="{project}"' in page.text


async def test_graph_needs_project_access(hosted_ui: UI) -> None:
    project, _, _ = await seed(hosted_ui)
    key = (await api(hosted_ui, "admin", op="create_user", args={"email": "otto@acme.dev"}))["result"]["api_key"]
    await api(hosted_ui, "admin", op="set_team_member", args={"team": "eng", "email": "otto@acme.dev",
                                                              "role": "member"})
    hosted_ui.api_token = key
    await hosted_ui.login()
    resp = await hosted_ui.client.get(f"/ui/graph.json?project={project}")
    assert resp.status_code == 404 and resp.json() == {"error": "not found (or not visible to you)"}
    page = await hosted_ui.client.get(f"/ui/graph?project={project}")
    assert page.status_code == 200 and "not found (or not visible to you)" in page.text
    assert "data-project" not in page.text and "Billing" not in page.text
    assert "No projects you can read yet" in (await hosted_ui.client.get("/ui/graph")).text


async def test_local_graph_defaults_to_personal_project(local_ui: UI) -> None:
    assert (await local_ui.client.get("/ui/graph")).status_code == 303
    await local_ui.login()
    page = await local_ui.client.get("/ui/graph")
    assert page.status_code == 200 and 'data-project="me:p1"' in page.text and "me / Personal" in page.text
    assert (await local_ui.client.get("/ui/graph.json?project=me:p1")).json() == {"nodes": [], "edges": []}


async def test_graph_static_files_are_served(local_ui: UI) -> None:
    js = await local_ui.client.get("/ui/static/graph.js")
    assert js.status_code == 200 and "/ui/graph.json" in js.text
    lib = await local_ui.client.get("/ui/static/cytoscape.min.js")
    assert lib.status_code == 200 and "cytoscape" in lib.text[:2000].lower()
