from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_E_V11_VISIBLE_STATUSES = {"CANDIDATE_NOT_ACTIVE", "AUTO_PAPER"}


def present_e_runtime(item: dict, runtime: dict | None) -> dict:
    """Copy E display fields from a live runtime snapshot.

    candidate_version stays in place so the auto-cycle integrity check still sees E-V1.1-QUANT.
    """
    if item.get("agent") != "E" or not runtime:
        return item
    view = dict(item)
    if (
        runtime.get("mode") == "AUTO_PAPER"
        and runtime.get("version") == "E-V1.1-QUANT"
        and view.get("candidate_version") == "E-V1.1-QUANT"
    ):
        view["runtime_mode"] = "AUTO_PAPER"
        view["driving_version"] = "E-V1.1-QUANT"
        view["candidate_status"] = "AUTO_PAPER"
        view["status"] = "AUTO_PAPER"
        view["archived_version"] = view.get("archived_version") or view.get("current_version")
        return view
    if runtime.get("present") and (
        view.get("status") == "AUTO_PAPER"
        or view.get("candidate_status") == "AUTO_PAPER"
        or view.get("runtime_mode") == "AUTO_PAPER"
    ):
        view["runtime_mode"] = runtime.get("mode") or "NOT_AUTO"
        view["status"] = "REGISTERED_NOT_RUNNING"
        view["candidate_status"] = "REGISTERED_NOT_RUNNING"
    return view


STRATEGIES = {
    "A": """---\nschema_version: 1\nagent_id: A\nversion: v1.0.0\nparent_version: null\nstatus: seed\ncore_lock: false\ncreated_at_utc: 2026-10-27\nmarket: Alpaca US BTC/USD spot\ntimeframe: 15m\n---\n# A Trend and Structure — initial V1.0 seed\n\n## Identity and market\nBTC/USD spot, 15-minute naked candles.\n\n## Core principle\nObserve three days of global structure, then one day of local structure. Prefer continuation only when swing structure and closes support direction.\n\n## Entry and exit\nTrade only after a completed candle; state direction, structural invalidation stop, and reasons. Close when structure invalidates or the stop is reached. WAIT when unclear.\n\n## Evolvable sections\nExamples, market-regime descriptions, and setup wording may evolve from reviewed evidence.\n\n## System constraints\nOne position; 1x notional cap; 2% planned risk; no averaging; paper only.\n""",
    "B": """---\nschema_version: 1\nagent_id: B\nversion: v1.0.0\nparent_version: null\nstatus: seed\ncore_lock: false\ncreated_at_utc: 2026-10-27\nmarket: Alpaca US BTC/USD spot\ntimeframe: 15m\n---\n# B Key-Level Reversal — initial V1.0 seed\n\n## Identity and market\nBTC/USD spot, 15-minute naked candles.\n\n## Core principle\nObserve three days of global structure, then one day of local structure. Consider reversal only at a visible prior reaction/turning area with completed-candle rejection evidence.\n\n## Entry and exit\nRequire a specific structural invalidation stop. Avoid entries in the middle of unclear ranges; WAIT when evidence is insufficient.\n\n## Evolvable sections\nExamples, setup descriptions, and context weighting may evolve from reviewed evidence.\n\n## System constraints\nOne position; 1x notional cap; 2% planned risk; no averaging; paper only.\n""",
}


@dataclass(frozen=True)
class StrategyVersion:
    agent: str
    version: str
    sha256: str
    content: str
    status: str


class StrategyRegistry:
    def __init__(
        self,
        root: str | Path = "strategies",
        *,
        activate_c: bool = False,
        c_expected_sha256: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.activate_c = activate_c
        self.c_expected_sha256 = c_expected_sha256
        self.root.mkdir(parents=True, exist_ok=True)
        for agent, content in STRATEGIES.items():
            base = self.root / agent
            base.mkdir(exist_ok=True)
            seed = base / "v1.0.0.md"
            if not seed.exists():
                seed.write_text(content, encoding="utf-8")
            meta_file = base / "v1.0.0.metadata.json"
            if not meta_file.exists():
                import json

                meta_file.write_text(
                    json.dumps(
                        {
                            "agent": agent,
                            "version": "v1.0.0",
                            "parent": None,
                            "status": "SEED",
                            "created_at_utc": "2026-10-27",
                            "source": "V1.0 frozen principles",
                            "evolvable_sections": ["Examples", "Evolvable sections"],
                            "immutable": True,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        cdir = self.root / "C"
        cdir.mkdir(exist_ok=True)
        marker = cdir / "C_NOT_CONFIGURED.md"
        if not marker.exists():
            marker.write_text(
                "C_NOT_CONFIGURED: awaiting user-authored C strategy MD. No strategy content is inferred.\n",
                encoding="utf-8",
            )

    def list(self) -> list[dict]:
        out = []
        for agent in "ABC":
            files = sorted((self.root / agent).glob("v*.md"))
            if agent == "C" and not files:
                out.append(
                    {
                        "agent": "C",
                        "current_version": None,
                        "status": "C_NOT_CONFIGURED",
                        "core_lock": "NOT_IMPORTED",
                        "versions": [],
                    }
                )
                continue
            versions = [
                {
                    "version": f.stem,
                    "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                    "path": f"strategies/{agent}/{f.name}",
                }
                for f in files
            ]
            if agent == "C":
                active = self.current_strategy("C") if self.activate_c else None
                out.append(
                    {
                        "agent": agent,
                        "current_version": active[0] if active else None,
                        "candidate_version": versions[-1]["version"] if versions else None,
                        "status": "ACTIVE"
                        if active
                        else "IMPORTED_REVIEW_REQUIRED"
                        if versions
                        else "C_NOT_CONFIGURED",
                        "core_lock": "PROTECTED"
                        if active
                        else "PROTECTED_PENDING_USER_APPROVAL"
                        if versions
                        else "NOT_IMPORTED",
                        "core_lock_sha256": (
                            json.loads(
                                (
                                    self.root / "C" / f"{versions[-1]['version']}.metadata.json"
                                ).read_text(encoding="utf-8")
                            ).get("core_lock_sha256")
                            if versions
                            else None
                        ),
                        "versions": versions,
                    }
                )
            else:
                out.append(
                    {
                        "agent": agent,
                        "current_version": versions[0]["version"] if versions else None,
                        "status": "SEED_VERSION_MODEL_BLOCKED",
                        "core_lock": "NOT_APPLICABLE",
                        "versions": versions,
                    }
                )
        edir = self.root / "E"
        if edir.is_dir():
            active = self.current_strategy("E")
            candidate = None
            candidate_status = None
            candidate_file = edir / "E-V1.1-QUANT.md"
            try:
                candidate_meta = json.loads((edir / "E-V1.1-QUANT.metadata.json").read_text(encoding="utf-8"))
                candidate_bytes = candidate_file.read_bytes()
                seed_bytes = (edir / "HANBA_SEED_V1.md").read_bytes()
                meta_status = candidate_meta.get("status")
                if (candidate_meta.get("agent") == "E" and candidate_meta.get("version") == "E-V1.1-QUANT"
                    and meta_status in _E_V11_VISIBLE_STATUSES and candidate_meta.get("immutable") is True
                    and candidate_meta.get("seed_sha256") == hashlib.sha256(seed_bytes).hexdigest()
                    and candidate_meta.get("md_sha256") == hashlib.sha256(candidate_bytes).hexdigest()):
                    candidate = {"version": "E-V1.1-QUANT", "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                                 "path": "strategies/E/E-V1.1-QUANT.md"}
                    candidate_status = meta_status
            except (OSError, UnicodeError, ValueError, TypeError):
                pass
            row = {
                "agent": "E", "current_version": active[0] if active else None,
                "status": "READY_FOR_VALIDATION" if active else "INTEGRITY_ERROR",
                "candidate_version": candidate["version"] if candidate else None,
                "candidate_status": candidate_status if candidate else "NOT_AVAILABLE_OR_INTEGRITY_ERROR",
                "core_lock": "PROTECTED", "versions": [{
                    "version": "E-V1.0",
                    "sha256": hashlib.sha256((edir / "E-V1.0.md").read_bytes()).hexdigest(),
                    "path": "strategies/E/E-V1.0.md",
                }] + ([candidate] if candidate else []) if (edir / "E-V1.0.md").is_file() else ([candidate] if candidate else []),
            }
            if candidate_status == "AUTO_PAPER":
                row["runtime_mode"] = "AUTO_PAPER"
                row["driving_version"] = "E-V1.1-QUANT"
                row["archived_version"] = active[0] if active else None
                if active:
                    row["status"] = "AUTO_PAPER"
            out.append(row)
        return out

    def current_strategy(self, agent: str) -> tuple[str, str] | None:
        """Return the current immutable A/B MD; C remains import-pending until user input."""
        if agent == "E":
            try:
                directory = self.root / "E"
                metadata = json.loads((directory / "E-V1.0.metadata.json").read_text(encoding="utf-8"))
                seed = (directory / "HANBA_SEED_V1.md").read_bytes()
                data = (directory / "E-V1.0.md").read_bytes()
                if (metadata.get("agent") != "E" or metadata.get("version") != "E-V1.0"
                    or metadata.get("immutable") is not True
                    or metadata.get("seed_sha256") != hashlib.sha256(seed).hexdigest()
                    or metadata.get("md_sha256") != hashlib.sha256(data).hexdigest()):
                    return None
                return "E-V1.0", data.decode("utf-8-sig")
            except (OSError, UnicodeError, ValueError, TypeError):
                return None
        if agent == "C" and self.activate_c:
            path = self.root / "C" / "v1.0.0.md"
            try:
                metadata = json.loads(
                    path.with_suffix(".metadata.json").read_text(encoding="utf-8")
                )
                data = path.read_bytes()
                if (
                    metadata.get("agent") != "C"
                    or metadata.get("version") != "v1.0.0"
                    or metadata.get("status") != "ACTIVE"
                    or metadata.get("immutable") is not True
                    or len(str(metadata.get("core_lock_sha256", ""))) != 64
                    or metadata.get("md_sha256") != hashlib.sha256(data).hexdigest()
                    or (
                        self.c_expected_sha256 is not None
                        and self.c_expected_sha256 != hashlib.sha256(data).hexdigest()
                    )
                ):
                    return None
                content = data.decode("utf-8")
                if "core_lock: true" not in content:
                    return None
                return "v1.0.0", content
            except (OSError, UnicodeError, ValueError, TypeError):
                return None
        if agent not in {"A", "B"}:
            return None
        item = next((row for row in self.list() if row["agent"] == agent), None)
        version = item.get("current_version") if item else None
        if not version:
            return None
        path = self.root / agent / f"{version}.md"
        if not path.is_file():
            return None
        return version, path.read_text(encoding="utf-8")

    def import_c(self, content: str) -> dict:
        if len(content) < 120 or "核心操盘理念" not in content:
            return {"accepted": False, "reason": "C_MD_MUST_INCLUDE_USER_CORE_IDEA"}
        cdir = self.root / "C"
        current = sorted(cdir.glob("v*.md"))
        version = f"v1.0.{len(current)}.md"
        path = cdir / version
        if path.exists():
            return {"accepted": False, "reason": "VERSION_EXISTS"}
        match = re.search(r"(?m)^##\s*核心操盘理念\s*$([\s\S]*?)(?=^##\s|\Z)", content)
        if not match or not match.group(1).strip():
            return {"accepted": False, "reason": "C_CORE_SECTION_EMPTY"}
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        core_hash = hashlib.sha256(match.group(1).strip().encode("utf-8")).hexdigest()
        import json

        (cdir / f"{path.stem}.metadata.json").write_text(
            json.dumps(
                {
                    "agent": "C",
                    "version": path.stem,
                    "parent": current[-1].stem if current else None,
                    "status": "IMPORTED_REVIEW_REQUIRED",
                    "md_sha256": digest,
                    "core_lock_sha256": core_hash,
                    "evolvable_sections": "awaiting user-designated sections",
                    "immutable": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "accepted": True,
            "status": "IMPORTED_REVIEW_REQUIRED",
            "version": path.stem,
            "sha256": digest,
            "core_lock_sha256": core_hash,
        }
