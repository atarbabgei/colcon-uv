"""Build task for UV-based Python packages."""

import shutil
from pathlib import Path

import tomli
from colcon_core.logging import colcon_logger
from colcon_core.plugin_system import satisfies_version
from colcon_core.task import TaskExtensionPoint

from colcon_uv.dependencies.install import (
    install_dependencies_from_descriptor,
    resolve_venv_path,
)

logger = colcon_logger.getChild("colcon.uv.task.build")


class UvBuildTask(TaskExtensionPoint):
    """Build task for UV-based Python packages."""

    def __init__(self):  # noqa: D107
        super().__init__()
        satisfies_version(TaskExtensionPoint.EXTENSION_POINT_VERSION, "^1.0")

    def add_arguments(self, *, parser):  # noqa: D102
        parser.add_argument(
            "--uv-args",
            nargs="*",
            metavar="*",
            type=str.lstrip,
            help="Pass arguments to UV. "
            "Arguments matching other options must be prefixed by a space,\n"
            'e.g. --uv-args " --help"',
        )

    def _read_pyproject(self) -> dict:
        try:
            with open(self.context.pkg.path / "pyproject.toml", "rb") as f:
                return tomli.load(f)
        except FileNotFoundError:
            return {}

    async def build(self, *, additional_hooks=None):
        logger.info("Installing package with all dependencies...")
        install_dependencies_from_descriptor(
            self.context.pkg, Path(self.context.args.install_base), False
        )

        return_code = await self._add_data_files()
        if return_code != 0:
            return return_code

        self._create_executable_symlinks()
        self._create_environment_hooks()

    async def _add_data_files(self) -> int:
        """Install files declared in [tool.colcon-uv-ros.data-files]."""
        pkg = self.context.pkg
        install_base = Path(self.context.args.install_base)

        data_files = (
            self._read_pyproject()
            .get("tool", {})
            .get("colcon-uv-ros", {})
            .get("data-files")
        )
        if data_files is None:
            return 0

        if not isinstance(data_files, dict):
            logger.error("data-files must be a table")
            return 1

        for destination, sources in data_files.items():
            if not isinstance(sources, list):
                logger.error(f"Field '{destination}' in data-files must be an array")
                return 1

            dest_path = install_base / destination
            dest_path.mkdir(parents=True, exist_ok=True)

            for source in sources:
                source_path = pkg.path / Path(source)
                if not source_path.exists():
                    continue
                if source_path.is_dir():
                    try:
                        shutil.copytree(
                            source_path,
                            dest_path / source_path.name,
                            dirs_exist_ok=True,
                        )
                    except shutil.Error:
                        # --symlink-install can leave source == dest.
                        pass
                else:
                    try:
                        shutil.copy2(source_path, dest_path)
                    except shutil.SameFileError:
                        pass

        return 0

    def _create_executable_symlinks(self):
        """Symlink each [project.scripts] entry into install/<pkg>/lib/<pkg>/ for ros2 run/launch."""
        pkg = self.context.pkg
        install_base = Path(self.context.args.install_base)
        pyproject = self._read_pyproject()

        scripts = pyproject.get("project", {}).get("scripts", {})
        if not scripts:
            return

        lib_dir = install_base / "lib" / pkg.name
        lib_dir.mkdir(parents=True, exist_ok=True)
        venv_bin = resolve_venv_path(pyproject, pkg.path, install_base) / "bin"

        for script_name in scripts:
            venv_executable = venv_bin / script_name
            ros_executable = lib_dir / script_name

            if not venv_executable.exists():
                logger.warning(
                    f"Entry-point script {venv_executable} not found; "
                    f"`ros2 run {pkg.name} {script_name}` will not work. "
                    f"Did `uv pip install` complete successfully?"
                )
                continue

            if ros_executable.exists() or ros_executable.is_symlink():
                ros_executable.unlink()
            ros_executable.symlink_to(venv_executable)
            logger.info(
                f"Created executable symlink: {ros_executable} -> {venv_executable}"
            )

    def _create_environment_hooks(self):
        """Create ROS environment hooks."""
        from colcon_core.environment import (
            create_environment_hooks,
            create_environment_scripts,
        )
        from colcon_core.shell import create_environment_hook

        pkg = self.context.pkg
        args = self.context.args

        additional_hooks = create_environment_hook(
            "ament_prefix_path",
            Path(args.install_base) / pkg.name,
            pkg.name,
            "AMENT_PREFIX_PATH",
            "",
            mode="prepend",
        )

        hooks = create_environment_hooks(Path(args.install_base) / pkg.name, pkg.name)
        create_environment_scripts(
            pkg, args, default_hooks=list(hooks), additional_hooks=additional_hooks
        )
