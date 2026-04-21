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


@dataclass(frozen=True)
class RegisterResult:
    """Outcome of Registry.register: the stored project, and the prior path
    at the same name (if any) so the caller can flag unexpected overwrites."""
    project: RegisteredProject
    prior_path: Path | None


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

    def register(self, project_path: str | Path) -> "RegisterResult":
        project_path = Path(project_path).expanduser().resolve()
        if not project_path.is_dir():
            raise RegistryError(f"project path is not a directory: {project_path}")
        manifest = load_manifest(project_path)
        data = self._read()
        projects = data.setdefault("projects", {})
        prior = projects.get(manifest.project.name)
        prior_path = Path(prior["path"]) if prior else None
        projects[manifest.project.name] = {
            "path": str(project_path),
            "manifest_hash": manifest.content_hash,
        }
        self._write(data)
        return RegisterResult(
            project=RegisteredProject(
                name=manifest.project.name,
                path=project_path,
                manifest_hash=manifest.content_hash,
            ),
            prior_path=prior_path,
        )

    def unregister(self, name: str) -> Path:
        """Remove the named project from the registry. Returns the path it held.

        Raises RegistryError if the project is not registered. Does not touch
        the project directory itself or any runs already in the store.
        """
        data = self._read()
        projects = data.get("projects", {})
        if name not in projects:
            raise RegistryError(f"project not registered: {name!r}")
        removed = projects.pop(name)
        self._write(data)
        return Path(removed["path"])

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
