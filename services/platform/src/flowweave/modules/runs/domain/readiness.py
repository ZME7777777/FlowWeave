from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InputField:
    key: str
    data_type: str


@dataclass(frozen=True, slots=True)
class Binding:
    field_key: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    artifact_type: str


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    missing: tuple[str, ...]
    incompatible: tuple[str, ...]


def evaluate_readiness(
    input_fields: tuple[InputField, ...],
    bindings: tuple[Binding, ...],
    artifacts: dict[str, Artifact],
) -> ReadinessResult:
    by_field = {item.field_key: item for item in bindings}
    missing: list[str] = []
    incompatible: list[str] = []
    for field in input_fields:
        binding = by_field.get(field.key)
        if not binding or binding.artifact_id not in artifacts:
            missing.append(field.key)
        elif artifacts[binding.artifact_id].artifact_type != field.data_type:
            incompatible.append(field.key)
    return ReadinessResult(not missing and not incompatible, tuple(missing), tuple(incompatible))
