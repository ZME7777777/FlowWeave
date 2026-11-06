"""Integration coverage for the runtime-port API brought in by the remote merge."""

from test_api import create_asset, create_flow


def _record(client, skill_capability):
    asset = create_asset(client, skill_capability, "Runtime port integration")
    flow = create_flow(client, asset["id"])
    parent = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={"environment_version_id": client.environment_version_id},
    )
    assert parent.status_code == 201, parent.text
    record = client.post(
        f"/api/v1/flow-runs/{parent.json()['id']}/stepwise-runs",
        json={"start_node_key": flow["nodes"][0]["instance_key"]},
    )
    assert record.status_code == 201, record.text
    return flow, parent.json(), record.json()


def _active(record):
    return next(s for s in record["snapshots"] if s["id"] == record["active_snapshot_id"])


def test_runtime_ports_freeze_a_new_snapshot_without_changing_the_template(
    client, skill_capability
):
    flow, parent, record = _record(client, skill_capability)
    source, target = [node["instance_key"] for node in flow["nodes"]]
    original = _active(record)
    payload = {
        "expected_active_snapshot_version": original["version"],
        "node_ports": {
            source: {
                "outputs": [
                    {
                        "field_key": "extra_output",
                        "display_name": "Extra output",
                        "data_type": "URL",
                    }
                ]
            },
            target: {
                "inputs": [
                    {"field_key": "extra_input", "display_name": "Extra input", "data_type": "URL"}
                ]
            },
        },
        "port_mappings": [
            {
                "source_instance_key": source,
                "source_output_key": "extra_output",
                "target_instance_key": target,
                "target_input_key": "extra_input",
            }
        ],
    }
    saved = client.put(f"/api/v1/flow-runs/{record['id']}/stepwise-runtime-ports", json=payload)
    assert saved.status_code == 200, saved.text
    active = _active(saved.json())
    assert active["version"] == original["version"] + 1
    definition = active["definition"]
    nodes = {node["instance_key"]: node for node in definition["nodes"]}
    assert nodes[source]["runtime_port_overrides"]["outputs"][0]["runtime_port"] is True
    assert nodes[target]["runtime_port_overrides"]["inputs"][0]["required"] is False
    assert definition["port_mappings"][-1]["runtime_port"] is True
    assert client.get(f"/api/v1/flows/{flow['id']}").json() == flow
    assert (
        client.get(f"/api/v1/flow-runs/{parent['id']}").json()["snapshots"] == parent["snapshots"]
    )
    stale = client.put(f"/api/v1/flow-runs/{record['id']}/stepwise-runtime-ports", json=payload)
    assert stale.status_code == 409, stale.text


def test_runtime_ports_reject_unknown_and_started_nodes(client, skill_capability):
    flow, parent, record = _record(client, skill_capability)
    node_key = flow["nodes"][0]["instance_key"]
    payload = {
        "expected_active_snapshot_version": _active(record)["version"],
        "node_ports": {"unknown": {"inputs": []}},
    }
    endpoint = f"/api/v1/flow-runs/{record['id']}/stepwise-runtime-ports"
    unknown = client.put(endpoint, json=payload)
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "RUN_PORTS_INVALID"
    payload["node_ports"] = {node_key: {"inputs": []}}
    top_level = client.put(f"/api/v1/flow-runs/{parent['id']}/stepwise-runtime-ports", json=payload)
    assert top_level.status_code == 409, top_level.text
    started = client.post(
        f"/api/v1/flow-runs/{record['id']}/nodes/{node_key}/runs",
        json={"startup_mode": "CHAT"},
    )
    assert started.status_code == 201, started.text
    immutable = client.put(endpoint, json=payload)
    assert immutable.status_code == 409, immutable.text
    assert immutable.json()["error"]["code"] == "RUN_PORTS_STARTED_NODE_IMMUTABLE"
    missing = client.delete(
        f"/api/v1/flow-runs/{record['id']}/stepwise-node-drafts/{node_key}/rerun-edit"
    )
    assert missing.status_code == 409, missing.text
    assert missing.json()["error"]["code"] == "STEPWISE_RERUN_EDIT_MISSING"


def _put(client, record, version, node_ports, **extra):
    return client.put(
        f"/api/v1/flow-runs/{record['id']}/stepwise-runtime-ports",
        json={"expected_active_snapshot_version": version, "node_ports": node_ports, **extra},
    )


def _mapping(source, target):
    return {
        "source_instance_key": source,
        "source_output_key": "extra_output",
        "target_instance_key": target,
        "target_input_key": "extra_input",
    }


def test_runtime_ports_accept_their_own_saved_shape_then_delete_and_disconnect(
    client, skill_capability
):
    flow, _parent, record = _record(client, skill_capability)
    source, target = [node["instance_key"] for node in flow["nodes"]]
    out = {"field_key": "extra_output", "display_name": "Out", "data_type": "URL"}
    inp = {"field_key": "extra_input", "display_name": "In", "data_type": "URL"}
    first = _put(
        client,
        record,
        _active(record)["version"],
        {source: {"outputs": [out]}, target: {"inputs": [inp]}},
        port_mappings=[_mapping(source, target)],
    )
    assert first.status_code == 200, first.text
    nodes = {n["instance_key"]: n for n in _active(first.json())["definition"]["nodes"]}
    # The browser re-submits the ports as read, including server-owned markers.
    saved_out = nodes[source]["runtime_port_overrides"]["outputs"]
    saved_in = nodes[target]["runtime_port_overrides"]["inputs"]
    assert saved_out[0]["runtime_port"] is True and saved_in[0]["required"] is False
    again = _put(
        client,
        first.json(),
        _active(first.json())["version"],
        {source: {"outputs": saved_out}, target: {"inputs": saved_in}},
        port_mappings=[_mapping(source, target)],
    )
    assert again.status_code == 200, again.text

    # Disconnect only: the ports stay, the connection goes.
    cut = _put(
        client,
        again.json(),
        _active(again.json())["version"],
        {},
        remove_port_mappings=[_mapping(source, target)],
    )
    assert cut.status_code == 200, cut.text
    assert not [
        m for m in _active(cut.json())["definition"]["port_mappings"] if m.get("runtime_port")
    ]
    # Removing a connection that is not there is an explicit error.
    missing = _put(
        client,
        cut.json(),
        _active(cut.json())["version"],
        {},
        remove_port_mappings=[_mapping(source, target)],
    )
    assert missing.status_code == 422, missing.text

    # Reconnect, then delete the target port: its connection disappears with it.
    joined = _put(
        client,
        cut.json(),
        _active(cut.json())["version"],
        {},
        port_mappings=[_mapping(source, target)],
    )
    assert joined.status_code == 200, joined.text
    gone = _put(
        client,
        joined.json(),
        _active(joined.json())["version"],
        {target: {"inputs": []}},
    )
    assert gone.status_code == 200, gone.text
    definition = _active(gone.json())["definition"]
    assert not [m for m in definition["port_mappings"] if m.get("runtime_port")]
    nodes = {n["instance_key"]: n for n in definition["nodes"]}
    assert nodes[target]["runtime_port_overrides"]["inputs"] == []
    assert nodes[source]["runtime_port_overrides"]["outputs"][0]["field_key"] == "extra_output"
