from __future__ import annotations

import click

from . import __version__
from .manifest import ManifestError, load as load_manifest
from .registry import Registry, RegistryError


@click.group()
@click.version_option(__version__, prog_name="bench")
def main() -> None:
    """benchstone — portfolio-wide benchmark harness."""


@main.command()
@click.argument(
    "project_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
def register(project_path: str) -> None:
    """Register a project located at PROJECT_PATH."""
    registry = Registry()
    try:
        rp = registry.register(project_path)
    except (RegistryError, ManifestError) as exc:
        raise click.ClickException(str(exc))
    click.echo(f"registered {rp.name}  {rp.path}")
    click.echo(f"  manifest: {rp.manifest_hash}")


@main.command("list")
@click.option("--project", "-p", default=None, help="Limit output to a single project.")
def list_cmd(project: str | None) -> None:
    """List registered projects and the benchmarks each one declares."""
    registry = Registry()
    projects = registry.list_projects()
    if project is not None:
        projects = [p for p in projects if p.name == project]
        if not projects:
            raise click.ClickException(f"no registered project named {project!r}")
    if not projects:
        click.echo("(no projects registered)")
        return
    for p in projects:
        click.echo(f"{p.name}  {p.path}")
        try:
            manifest = load_manifest(p.path)
        except ManifestError as exc:
            click.echo(f"  (manifest error: {exc})")
            continue
        for b in manifest.benchmarks:
            click.echo(
                f"  - {b.name}  tier={b.tier}  reps={b.repetitions}  "
                f"threads={b.threads}  gpu={b.gpu}"
            )


if __name__ == "__main__":
    main()
