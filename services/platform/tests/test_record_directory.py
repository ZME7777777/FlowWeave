from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import event

from flowweave.shared.models import FlowRun, NodeRun, RunEvent


def seed_records(client, db_session_factory, count=12):
    owner = client.get("/api/v1/auth/me").json()["id"]
    timestamp = datetime(2026, 10, 27, tzinfo=UTC)
    with db_session_factory() as db:
        parent = FlowRun(
            flow_definition_id=str(uuid4()),
            run_no=1,
            name="directory parent",
            owner_user_id=owner,
        )
        db.add(parent)
        db.flush()
        items = []
        for index in range(count):
            child = FlowRun(
                flow_definition_id=parent.flow_definition_id,
                run_no=100 + index,
                parent_flow_run_id=parent.id,
                name=f"record-{index}",
                run_mode="MANUAL" if index % 2 else "AUTOMATIC",
                state="DRAFT",
                owner_user_id=owner,
                started_at=timestamp - timedelta(seconds=index // 3),
                automation_plan_json={
                    "start_node_key": "first",
                    "reachable_node_keys": ["first", "next"],
                    "node_plans": {"first": {"prompt": "x" * 50000}},
                },
            )
            db.add(child)
            items.append(child)
        db.flush()
        db.add(
            NodeRun(
                flow_run_id=items[0].id,
                flow_node_snapshot_key="first",
                sequence_no=1,
                state="ACCEPTED",
                owner_user_id=owner,
            )
        )
        expected = [
            item.id for item in sorted(items, key=lambda x: (x.started_at, x.id), reverse=True)
        ]
        parent_id = parent.id
        db.commit()
    return parent_id, expected


def test_directory_is_one_small_mixed_page_with_stable_keyset(client, db_session_factory):
    parent, expected = seed_records(client, db_session_factory, 42)
    url = f"/api/v1/flow-runs/{parent}/records"
    statements = []
    engine = db_session_factory.kw["bind"]

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        first = client.get(url, params={"limit": 7})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["items"]) == 7 and len(first.content) < 5000
    assert {item["kind"] for item in body["items"]} == {"MANUAL", "AUTOMATIC"}
    assert all("prompt" not in item and "snapshots" not in item for item in body["items"])
    assert not any(
        "node_attempts" in sql or "run_snapshots" in sql or "artifact_versions" in sql
        for sql in statements
    )
    # A new record before the current window must not shift the next page.
    with db_session_factory() as db:
        source = db.get(FlowRun, parent)
        db.add(
            FlowRun(
                flow_definition_id=source.flow_definition_id,
                run_no=999,
                parent_flow_run_id=parent,
                name="new head",
                run_mode="MANUAL",
                owner_user_id=source.owner_user_id,
                started_at=datetime.now(UTC),
            )
        )
        db.commit()
    seen = [item["id"] for item in body["items"]]
    while body["next_cursor"]:
        response = client.get(url, params={"cursor": body["next_cursor"], "limit": 7})
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
    assert seen == expected and len(seen) == len(set(seen))


def test_directory_filters_cursor_scope_validation_and_delete_exclusion(client, db_session_factory):
    parent, _ = seed_records(client, db_session_factory)
    url = f"/api/v1/flow-runs/{parent}/records"
    first = client.get(url, params={"kind": "MANUAL", "limit": 2}).json()
    assert all(item["kind"] == "MANUAL" for item in first["items"])
    cursor = first["next_cursor"]
    assert client.get(url, params={"kind": "AUTOMATIC", "cursor": cursor}).status_code == 400
    assert client.get(url, params={"cursor": "not-a-cursor"}).status_code == 400
    assert client.get(url, params={"limit": 101}).status_code == 422
    assert client.get(url, params={"kind": "UNKNOWN"}).status_code == 422
    other, _ = seed_records(client, db_session_factory)
    assert (
        client.get(
            f"/api/v1/flow-runs/{other}/records", params={"kind": "MANUAL", "cursor": cursor}
        ).status_code
        == 400
    )
    automatic = client.get(url, params={"kind": "AUTOMATIC"}).json()["items"]
    target = automatic[0]["id"]
    with db_session_factory() as db:
        source = db.get(FlowRun, parent)
        db.add(
            RunEvent(
                flow_run_id=target,
                event_type="AUTOMATIC_RUN_DELETE_REQUESTED",
                payload_json={},
                owner_user_id=source.owner_user_id,
            )
        )
        db.commit()
    assert target not in {item["id"] for item in client.get(url).json()["items"]}


def test_directory_never_exposes_another_users_child_records(
    client, user_client, db_session_factory
):
    parent, _ = seed_records(client, db_session_factory)
    response = user_client.get(f"/api/v1/flow-runs/{parent}/records")
    # The legacy top-level parent can be shared, its child executions cannot.
    assert response.status_code in (200, 404), response.text
    if response.status_code == 200:
        assert response.json() == {"items": [], "next_cursor": None}


def test_directory_search_is_global_case_insensitive_and_cursor_scoped(client, db_session_factory):
    parent, expected = seed_records(client, db_session_factory, 42)
    url = f"/api/v1/flow-runs/{parent}/records"
    with db_session_factory() as db:
        matching = {
            item.id
            for item in db.query(FlowRun).filter(FlowRun.parent_flow_run_id == parent)
            if "record-1" in item.name
        }
    first = client.get(url, params={"q": " RECORD-1 ", "limit": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    cursor = body["next_cursor"]
    assert cursor
    seen = [item["id"] for item in body["items"]]
    while body["next_cursor"]:
        response = client.get(
            url, params={"q": "record-1", "limit": 2, "cursor": body["next_cursor"]}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
    assert seen == [item for item in expected if item in matching]
    assert client.get(url, params={"q": "record-2", "cursor": cursor}).status_code == 400
    assert client.get(url, params={"cursor": cursor}).status_code == 400
    legacy_cursor = client.get(url, params={"limit": 2}).json()["next_cursor"]
    assert client.get(url, params={"q": "record-1", "cursor": legacy_cursor}).status_code == 400
    by_id = client.get(url, params={"q": expected[-1].upper()}).json()["items"]
    assert [item["id"] for item in by_id] == [expected[-1]]
    manual = client.get(url, params={"q": "record-1", "kind": "MANUAL"}).json()["items"]
    assert manual and all(
        item["kind"] == "MANUAL" and "record-1" in item["name"] for item in manual
    )
    assert client.get(url, params={"q": " "}).json() == client.get(url).json()
    assert client.get(url, params={"q": "x" * 221}).status_code == 422
    unicode_term = "😀" * 220
    with db_session_factory() as db:
        for record_id in expected[:2]:
            db.get(FlowRun, record_id).name = unicode_term
        db.commit()
    unicode_page = client.get(url, params={"q": unicode_term, "limit": 1}).json()
    assert len(unicode_page["next_cursor"]) <= 2048
    unicode_next = client.get(
        url, params={"q": unicode_term, "cursor": unicode_page["next_cursor"]}
    )
    assert unicode_next.status_code == 200, unicode_next.text
    assert len(unicode_next.json()["items"]) == 1


def test_directory_search_escapes_literals_and_retains_user_and_delete_filters(
    client, user_client, db_session_factory
):
    parent, expected = seed_records(client, db_session_factory)
    url = f"/api/v1/flow-runs/{parent}/records"
    literal = "资料_100%\\验收"
    with db_session_factory() as db:
        target = db.get(FlowRun, expected[0])
        target.name = literal
        db.commit()
    assert [item["id"] for item in client.get(url, params={"q": literal}).json()["items"]] == [
        expected[0]
    ]
    assert client.get(url, params={"q": "%"}).json()["items"][0]["name"] == literal
    assert len(client.get(url, params={"q": "_"}).json()["items"]) == 1
    assert client.get(url, params={"q": "' OR 1=1 --"}).json()["items"] == []
    assert client.get(url, params={"q": "不存在"}).json() == {"items": [], "next_cursor": None}
    response = user_client.get(url, params={"q": literal})
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert response.json()["items"] == []
    deleted = client.get(url, params={"kind": "AUTOMATIC"}).json()["items"][0]["id"]
    with db_session_factory() as db:
        db.add(
            RunEvent(
                flow_run_id=deleted,
                event_type="AUTOMATIC_RUN_DELETE_REQUESTED",
                payload_json={},
                owner_user_id=db.get(FlowRun, parent).owner_user_id,
            )
        )
        db.commit()
    assert client.get(url, params={"q": deleted}).json()["items"] == []
