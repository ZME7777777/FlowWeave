import pytest
from pydantic import ValidationError

from flowweave.shared.schemas import IOFieldWrite


@pytest.mark.parametrize("payload", [{}, {"field_key": ""}, {"field_key": "existing_1"}])
def test_field_key_accepts_generated_or_existing_identity(payload):
    assert IOFieldWrite.model_validate(payload).field_key == payload.get("field_key", "")


@pytest.mark.parametrize("key", [" ", "1input", "bad-key", "a" * 101])
def test_field_key_rejects_invalid_explicit_identity(key):
    with pytest.raises(ValidationError):
        IOFieldWrite(field_key=key)


def test_catalog_generates_keys_without_colliding_with_later_explicit_fields(client):
    payload = {
        "name": "自动字段标识验收",
        "inputs": [
            {"display_name": "省略标识"},
            {"field_key": "input_1", "display_name": "已有字段"},
            {"field_key": "", "display_name": "空标识"},
        ],
        "outputs": [
            {"field_key": "", "display_name": "输出"},
            {"field_key": "output_1", "display_name": "已有输出"},
        ],
    }
    created = client.post("/api/v1/node-assets", json=payload)
    assert created.status_code == 201, created.text
    node = created.json()
    assert [field["field_key"] for field in node["inputs"]] == ["input_2", "input_1", "input_3"]
    assert [field["field_key"] for field in node["outputs"]] == ["output_2", "output_1"]

    updated = client.put(
        f"/api/v1/node-assets/{node['id']}",
        json={
            **payload,
            "row_version": node["row_version"],
            "inputs": [
                {"field_key": field["field_key"], "display_name": "重命名"}
                for field in node["inputs"]
            ],
            "outputs": [{"field_key": field["field_key"]} for field in node["outputs"]],
        },
    )
    assert updated.status_code == 200, updated.text
    assert [field["field_key"] for field in updated.json()["inputs"]] == [
        field["field_key"] for field in node["inputs"]
    ]
