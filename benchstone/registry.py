from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .manifest import load as load_manifest


class RegistryError(Exception):
    """Raised when a registry operation fails (unknown project, bad path, etc.)."""


@dataclass(frozen=True)
class RegisteredProject:
    name: str
    path: Path
    manifest_hash: str


class Registry:
    """JSON-backed project registry.

    Stored at $BENCHSTONE_HOME/registry.json because the registry is harness-managed
    machine state (TOML is reserved for human-edited manifests).
    """

    def __init__(self, registry_path: Path | None = None):
        self.path = Path(registry_path) if registry_path else paths.registry_path()

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise RegistryError(f"{self.path}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RegistryError(f"{self.path}: expected a JSON object at the top level")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def register(self, project_path: str | Path) -> RegisteredProject:
        project_path = Path(project_path).expanduser().resolve()
        if not project_path.is_dir():
            raise RegistryError(f"project path is not a directory: {project_path}")
        manifest = load_manifest(project_path)
        data = self._read()
        projects = data.setdefault("projects", {})
        projects[manifest.project.name] = {
            "path": str(project_path),
            "manifest_hash": manifest.content_hash,
        }
        self._write(data)
        return RegisteredProject(
            name=manifest.project.name,
            path=project_path,
            manifest_hash=manifest.content_hash,
        )

    def list_projects(self) -> list[RegisteredProject]:
        data = self._read()
        projects = data.get("projects", {})
        return [
            RegisteredProject(
                name=name,
                path=Path(entry["path"]),
                manifest_hash=entry["manifest_hash"],
            )
            for name, entry in sorted(projects.items())
        ]

    def resolve(self, name: str) -> RegisteredProject:
        for p in self.list_projects():
            if p.name == name:
                return p
        raise RegistryError(f"project not registered: {name!r}")
