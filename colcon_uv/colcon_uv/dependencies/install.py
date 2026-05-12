"""Install dependencies for UV packages."""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import tomli
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

logger = logging.getLogger("colcon.uv.dependencies")


class NotAUvPackageError(Exception):
    """Raised when a directory is not a UV package."""

    pass


class UvPackage:
    """A package whose pyproject.toml has a [tool.colcon-uv-ros] section."""

    def __init__(self, path: Path, logger=None):
        self.path = path
        self.logger = logger or logging.getLogger(__name__)

        self.pyproject_file = path / "pyproject.toml"
        if not self.pyproject_file.exists():
            raise NotAUvPackageError(f"No pyproject.toml found in {path}")

        with open(self.pyproject_file, "rb") as f:
            self.pyproject_data = tomli.load(f)

        if "colcon-uv-ros" not in self.pyproject_data.get("tool", {}):
            raise NotAUvPackageError(
                f"No [tool.colcon-uv-ros] section found in {self.pyproject_file}"
            )

        self.name = self.pyproject_data.get("project", {}).get("name", path.name)

    @property
    def uv_ros_config(self) -> dict:
        """The [tool.colcon-uv-ros] table (empty if absent)."""
        return self.pyproject_data.get("tool", {}).get("colcon-uv-ros", {})


def main():
    """Main entry point for UV dependency installation."""
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s: %(message)s",
    )

    for project in discover_packages(args.base_paths):
        logger.info(f"Installing dependencies for {project.path.name}...")
        install_dependencies(project, args.install_base, args.merge_install)

    logger.info("Dependencies installed!")


def discover_packages(base_paths: List[Path]) -> List[UvPackage]:
    """Discover UV packages in the given base paths."""
    projects: List[UvPackage] = []

    potential_packages = []
    for path in base_paths:
        potential_packages += list(path.glob("*"))

    for path in potential_packages:
        if path.is_dir():
            try:
                project = UvPackage(path)
            except NotAUvPackageError:
                continue
            else:
                projects.append(project)

    if len(projects) == 0:
        base_paths_str = ", ".join([str(p) for p in base_paths])
        logger.error(
            f"No UV packages were found in the following paths: {base_paths_str}"
        )
        sys.exit(1)

    return projects


def resolve_venv_path(
    pyproject_data: dict, project_path: Path, install_base: Path
) -> Path:
    """Return venv-path from [tool.colcon-uv-ros] (resolved against project_path) or install_base/venv."""
    venv_path_str = (
        pyproject_data.get("tool", {}).get("colcon-uv-ros", {}).get("venv-path")
    )
    if venv_path_str:
        return (project_path / venv_path_str).resolve()
    return install_base / "venv"


def _resolve_python_version(project: UvPackage) -> str:
    """Pick a Python for the venv.

    Priority: .python-version → colcon's interpreter (if it satisfies
    requires-python) → requires-python → colcon's interpreter.
    """
    python_version_file = project.path / ".python-version"
    if python_version_file.exists():
        version = python_version_file.read_text().strip()
        if version:
            logger.info(f"Using Python version from .python-version: {version}")
            return version

    requires_python = project.pyproject_data.get("project", {}).get(
        "requires-python", ""
    )

    if requires_python:
        try:
            current = Version(
                f"{sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}"
            )
            if current in SpecifierSet(requires_python):
                logger.info(
                    f"Using colcon's Python {current} "
                    f"(satisfies requires-python {requires_python}): {sys.executable}"
                )
                return sys.executable
        except (InvalidSpecifier, InvalidVersion) as e:
            logger.debug(f"Could not parse requires-python {requires_python!r}: {e}")

        logger.info(f"Using requires-python from pyproject.toml: {requires_python}")
        return requires_python

    logger.info(f"Using colcon's Python: {sys.executable}")
    return sys.executable


def _preseed_extra_site_packages(project: UvPackage, venv_path: Path) -> None:
    """Copy *.dist-info from extra-site-packages paths into the venv and write a .pth pointer."""
    extra_site_packages = project.uv_ros_config.get("extra-site-packages", [])
    if not extra_site_packages:
        return

    venv_site_dirs = list((venv_path / "lib").glob("python*/site-packages"))
    if not venv_site_dirs:
        logger.warning("Could not locate site-packages inside the venv")
        return

    venv_site = venv_site_dirs[0]
    pth_lines = []

    for extra_path_str in extra_site_packages:
        extra_path = Path(extra_path_str)
        if not extra_path.is_dir():
            logger.warning(f"extra-site-packages path not found: {extra_path}")
            continue

        for dist_info in extra_path.glob("*.dist-info"):
            dest = venv_site / dist_info.name
            if not dest.exists():
                shutil.copytree(dist_info, dest)

        pth_lines.append(str(extra_path))

    if pth_lines:
        pth_file = venv_site / "colcon_uv_extra.pth"
        pth_file.write_text("\n".join(pth_lines) + "\n")
        logger.info(f"Pre-seeded venv with extra site-packages: {', '.join(pth_lines)}")


def _get_index_flags(project: UvPackage) -> List[str]:
    """Build uv index/find-links flags from [tool.colcon-uv-ros] (with COLCON_UV_* env fallback)."""
    uv_ros_config = project.uv_ros_config
    flags: List[str] = []

    index_url = uv_ros_config.get("index-url") or os.environ.get("COLCON_UV_INDEX_URL")
    if index_url:
        flags.extend(["--index-url", index_url])

    extra_index_urls = uv_ros_config.get("extra-index-url", [])
    if not extra_index_urls:
        env_val = os.environ.get("COLCON_UV_EXTRA_INDEX_URL", "")
        extra_index_urls = [u.strip() for u in env_val.split(",") if u.strip()]
    for url in extra_index_urls:
        flags.extend(["--extra-index-url", url])

    # find-links: list of local/remote wheel locations
    find_links = uv_ros_config.get("find-links", [])
    if not find_links:
        env_val = os.environ.get("COLCON_UV_FIND_LINKS", "")
        find_links = [u.strip() for u in env_val.split(",") if u.strip()]
    for link in find_links:
        flags.extend(["--find-links", link])

    if flags:
        logger.info(f"Using custom index flags: {' '.join(flags)}")

    return flags


def _surface_uv_error_and_exit(e: subprocess.CalledProcessError, what: str) -> None:
    """Pass uv's stderr through and exit without the Python traceback."""
    if e.stderr:
        sys.stderr.write(e.stderr)
        sys.stderr.flush()
    logger.error(f"Failed to {what}")
    sys.exit(1)


def install_dependencies(
    project: UvPackage, install_base: Path, merge_install: bool
) -> None:
    """Install a UV package and its dependencies into its venv."""
    # install_base may already include the package name when called from the
    # build task; for direct `colcon uv install` it points at the workspace.
    if not merge_install and install_base.name != project.name:
        install_base /= project.name
    install_base.mkdir(parents=True, exist_ok=True)

    venv_path = resolve_venv_path(project.pyproject_data, project.path, install_base)
    uses_external_venv = venv_path != install_base / "venv"

    if uses_external_venv:
        if not venv_path.exists():
            logger.error(
                f"venv-path {venv_path} does not exist. Create it first "
                f"(e.g. `uv sync` or `uv venv {venv_path}`) before running "
                f"colcon build."
            )
            sys.exit(1)
        logger.info(f"Using shared venv from venv-path: {venv_path}")
    elif not venv_path.exists():
        # --system-site-packages exposes the system rclpy and other ROS C
        # extensions that aren't on PyPI.
        try:
            subprocess.run(
                [
                    "uv",
                    "venv",
                    "--system-site-packages",
                    "--python",
                    _resolve_python_version(project),
                    str(venv_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create venv: {e.stderr}")
            raise

    _preseed_extra_site_packages(project, venv_path)

    python_exe = venv_path / "bin" / "python"
    index_flags = _get_index_flags(project)

    optional_deps = project.pyproject_data.get("project", {}).get(
        "optional-dependencies", {}
    )
    if optional_deps:
        extras = ",".join(optional_deps.keys())
        install_target = f"{project.path}[{extras}]"
        logger.info(f"Installing with optional dependencies: {extras}")
    else:
        install_target = str(project.path)

    # uv pip install ignores [tool.uv].override-dependencies; materialise to a
    # temp requirements file and pass via --override.
    override_args: List[str] = []
    override_deps = (
        project.pyproject_data.get("tool", {})
        .get("uv", {})
        .get("override-dependencies", [])
    )
    override_file: Optional[Path] = None
    if override_deps:
        fd, override_file_str = tempfile.mkstemp(
            prefix="colcon_uv_override_", suffix=".txt"
        )
        os.close(fd)
        override_file = Path(override_file_str)
        override_file.write_text("\n".join(override_deps) + "\n")
        override_args = ["--override", str(override_file)]
        logger.info(f"Using override-dependencies: {override_deps}")

    try:
        try:
            subprocess.run(
                [
                    "uv",
                    "--no-progress",
                    "pip",
                    "install",
                    *index_flags,
                    "--python",
                    str(python_exe),
                    *override_args,
                    "-e",
                    install_target,
                ],
                check=True,
                stdout=sys.stdout,
                stderr=sys.stderr,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            _surface_uv_error_and_exit(e, f"install dependencies for {install_target}")

        dependency_groups = project.pyproject_data.get("dependency-groups", {})
        if dependency_groups:
            group_names = list(dependency_groups.keys())
            logger.info(f"Installing dependency groups: {', '.join(group_names)}")

            cmd = [
                "uv",
                "--no-progress",
                "pip",
                "install",
                *index_flags,
                "--python",
                str(python_exe),
                *override_args,
            ]
            for group in group_names:
                cmd.extend(["--group", group])
            cmd.append(".")

            try:
                subprocess.run(
                    cmd,
                    check=True,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    text=True,
                    cwd=str(project.path),
                )
            except subprocess.CalledProcessError as e:
                _surface_uv_error_and_exit(
                    e, f"install dependency groups for {project.name}"
                )
    finally:
        if override_file and override_file.exists():
            override_file.unlink()


def install_dependencies_from_descriptor(
    pkg_descriptor, install_base: Path, merge_install: bool
):
    """Install dependencies from a PackageDescriptor object.

    This is a convenience function for use by colcon build tasks.
    """
    try:
        uv_package = UvPackage(pkg_descriptor.path)
        install_dependencies(uv_package, install_base, merge_install)
    except NotAUvPackageError as e:
        # Skip packages that aren't UV packages
        logger.debug(f"Skipping non-UV package {pkg_descriptor.name}: {e}")
        return


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Searches for UV packages and installs their dependencies "
        "to a configurable install base"
    )

    parser.add_argument(
        "--base-paths",
        nargs="+",
        type=Path,
        default=[Path.cwd()],
        help="The paths to start looking for UV projects in. Defaults to the "
        "current directory.",
    )

    parser.add_argument(
        "--install-base",
        type=Path,
        default=Path("install"),
        help="The base path for all install prefixes (default: install)",
    )

    parser.add_argument(
        "--merge-install",
        action="store_true",
        help="Merge all install prefixes into a single location",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="If provided, debug logs will be printed",
    )

    return parser.parse_args()


if __name__ == "__main__":
    main()
