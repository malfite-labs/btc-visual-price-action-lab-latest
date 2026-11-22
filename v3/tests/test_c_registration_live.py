import hashlib
import json

from backend.strategy import StrategyRegistry


def test_registered_c_loads_only_matching_immutable_strategy(tmp_path):
    registry = StrategyRegistry(tmp_path, activate_c=True)
    path = tmp_path / "C" / "v1.0.0.md"
    path.write_text("Tony user-owned strategy\ncore_lock: true\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "C" / "v1.0.0.metadata.json").write_text(
        json.dumps(
            {
                "agent": "C",
                "version": "v1.0.0",
                "md_sha256": digest,
                "core_lock_sha256": "a" * 64,
                "immutable": True,
                "status": "ACTIVE",
            }
        ),
        encoding="utf-8",
    )
    assert registry.current_strategy("C") == ("v1.0.0", path.read_bytes().decode("utf-8"))
    assert registry.list()[2]["current_version"] == "v1.0.0"
    path.write_text("tampered", encoding="utf-8")
    assert registry.current_strategy("C") is None
