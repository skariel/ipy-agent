import pytest

from py_agent.configuration import ApplyAt, ConfigError, ConfigField, ConfigLayer, ConfigRegistry, ConfigStore


def test_precedence_provenance_and_snapshot_immutability():
    fields = ConfigRegistry([ConfigField("model.effort", "model", str, "medium", choices=("low", "medium", "high"))])
    store = ConfigStore(fields, (ConfigLayer("user", {"model.effort": "low"}), ConfigLayer("flags", {"model.effort": "high"})))
    before = store.snapshot
    change = store.set({"model.effort": "medium"})
    assert before.get("model.effort") == "high"
    assert change.after.entries["model.effort"].masked_sources == ("default", "user", "flags")
    assert change.after.entries["model.effort"].source == "session"
    with pytest.raises(TypeError):
        before.entries["model.effort"] = "bad"


def test_transactional_failure_and_redaction():
    fields = ConfigRegistry([ConfigField("secret", "core", str, "", sensitive=True),
                             ConfigField("limit", "core", int, 5, minimum=1, apply_at=ApplyAt.RESTART)])
    store = ConfigStore(fields)
    with pytest.raises(ConfigError):
        store.set({"secret": "hidden", "limit": 0})
    assert store.snapshot.revision == 0
    assert store.snapshot.get("secret") == ""
    change = store.set({"secret": "hidden", "limit": 9})
    assert change.changed["limit"] == ApplyAt.RESTART
    assert fields.display(store.snapshot)["secret"]["value"] == "<redacted>"
    with pytest.raises(ConfigError):
        store.persistence_values(["secret"])
    with pytest.raises(ConfigError):
        store.reload([ConfigLayer("user", {"unknown": 1})])
    assert store.snapshot.revision == 1


def test_json_parsing_is_data_not_python():
    store = ConfigStore(ConfigRegistry([ConfigField("count", "core", int, 1)]))
    with pytest.raises(ConfigError):
        store.set_json("count", "__import__('os').system('true')")
    with pytest.raises(ConfigError):
        store.set_json("count", "true")
    for invalid in ("[]", "{}", "NaN", "Infinity"):
        with pytest.raises(ConfigError):
            store.set_json("count", invalid)
    with pytest.raises(ConfigError):
        store.set_json("count", None)
    assert store.set_json("count", "3").after.get("count") == 3


def test_schema_rejects_nonfinite_and_malformed_numeric_values():
    with pytest.raises(ConfigError):
        ConfigField("count", "core", int, True)
    with pytest.raises(ConfigError):
        ConfigField("ratio", "core", float, float("nan"))
    with pytest.raises(ConfigError):
        ConfigField("label", "core", str, "ok", minimum=1)
    with pytest.raises(ConfigError):
        ConfigField("ratio", "core", float, 1.0, choices=(float("nan"),))


def test_layers_copy_inputs_and_schema_snapshots_are_immutable():
    original = {"count": 2}
    layer = ConfigLayer("user", original)
    original["count"] = 99
    registry = ConfigRegistry([ConfigField("count", "core", int, 1)])
    store = ConfigStore(registry, (layer,))
    assert store.snapshot.get("count") == 2
    with pytest.raises(TypeError):
        layer.values["count"] = 3
    with pytest.raises(TypeError):
        registry.fields["other"] = ConfigField("other", "core", str, "x")
    with pytest.raises(AttributeError, match="immutable"):
        registry.fields = {}
    with pytest.raises(ConfigError):
        ConfigLayer("user", {"count": float("inf")})


def test_reload_and_invalid_candidates_leave_snapshot_and_provenance_intact():
    registry = ConfigRegistry([ConfigField("count", "core", int, 1)])
    store = ConfigStore(registry, (ConfigLayer("user", {"count": 2}),))
    store.set({"count": 3})
    active = store.snapshot
    with pytest.raises(ConfigError):
        store.reload((ConfigLayer("user", {"count": 4}),
                      ConfigLayer("user", {"count": 5})))
    with pytest.raises(ConfigError):
        store.set({"count": 6, "unknown": 7})
    assert store.snapshot is active
    assert store.snapshot.revision == 1
    assert store.snapshot.get("count") == 3
    assert store.snapshot.entries["count"].source == "session"


def test_sensitive_values_are_hidden_from_representations_and_change_display():
    registry = ConfigRegistry([ConfigField("secret", "core", str, "", sensitive=True)])
    store = ConfigStore(registry)
    change = store.set({"secret": "do-not-print-this"})
    assert "do-not-print-this" not in repr(store.snapshot)
    assert "do-not-print-this" not in repr(change)
    assert registry.display(store.snapshot)["secret"]["value"] == "<redacted>"


@pytest.mark.parametrize(("value_type", "default", "low", "high"), [
    (int, 2, 0, 4),
    (float, 2.0, 0.0, 4.0),
])
def test_numeric_bounds_are_inclusive_and_rejections_are_atomic(value_type, default, low, high):
    store = ConfigStore(ConfigRegistry([
        ConfigField("count", "core", value_type, default, minimum=1, maximum=3),
    ]))
    for value in (low, high):
        before = store.snapshot
        with pytest.raises(ConfigError):
            store.set({"count": value})
        assert store.snapshot is before
    for value in (value_type(1), value_type(3)):
        assert store.set({"count": value}).after.get("count") == value


@pytest.mark.parametrize("result", [True, None, False])
def test_validator_return_contract(result):
    def validate(value):
        return None if value == 1 else result

    store = ConfigStore(ConfigRegistry([
        ConfigField("count", "core", int, 1, validator=validate),
    ]))
    before = store.snapshot
    if result is False:
        with pytest.raises(ConfigError, match="Validation failed"):
            store.set({"count": 2})
        assert store.snapshot is before
    else:
        assert store.set({"count": 2}).after.get("count") == 2


def test_validator_exception_is_redacted_and_does_not_commit():
    def validate(value):
        if value != "initial":
            raise ValueError(f"secret: {value}")

    store = ConfigStore(ConfigRegistry([
        ConfigField("secret", "core", str, "initial", sensitive=True, validator=validate),
    ]))
    before = store.snapshot
    with pytest.raises(ConfigError, match="^Validation failed for secret$") as exc:
        store.set({"secret": "private"})
    assert "private" not in str(exc.value)
    assert store.snapshot is before
