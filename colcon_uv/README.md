# colcon-uv

[![CI](https://github.com/nzlz/colcon-uv/actions/workflows/ci.yml/badge.svg)](https://github.com/nzlz/colcon-uv/actions/workflows/ci.yml)

A **colcon extension** for building and testing Python packages that use **[uv](https://github.com/astral-sh/uv)** for dependency management.

## Features

- **Fast Dependency Management**: Leverages UV's lightning-fast dependency resolution and installation
- **Modern Python Packaging**: Support for `pyproject.toml`-based packages following PEP 517/518 standards
- **ROS Integration**: Seamless integration with colcon build system and ROS package management
- **Dependency Isolation**: Prevents dependency conflicts between packages

## Configuration

### Data Files

Similar to [colcon-poetry-ros](https://github.com/UrbanMachine/colcon-poetry-ros), you can specify data files using the `[tool.colcon-uv-ros.data-files]` section:

```toml
[tool.colcon-uv-ros.data-files]
"share/ament_index/resource_index/packages" = ["resource/{package_name}"]
"share/{package_name}" = ["package.xml", "launch/", "config/"]
"lib/{package_name}" = ["scripts/"]
```

**Required entries** for all ROS packages:

```toml
[tool.colcon-uv-ros.data-files]
"share/ament_index/resource_index/packages" = ["resource/{package_name}"]
"share/{package_name}" = ["package.xml"]
```

### Package Dependencies

Specify package dependencies for build ordering and to use system libraries (fetched from system paths, not installed in virtual environment):

```toml
[tool.colcon-uv-ros.dependencies]
depend = ["rclpy", "geometry_msgs"]  # System packages (adds to both build_depend and exec_depend)
build_depend = ["bar_package"]       # Build-time only dependency
exec_depend = ["std_msgs"]           # Runtime system library
test_depend = ["qux_package"]        # Test-time only dependency
```

**Important**: ROS system libraries like `rclpy`, `geometry_msgs`, `std_msgs`, etc. should be listed here so they are resolved from the system installation rather than being installed into the virtual environment.

### Python Version Resolution

By default `uv` picks the highest Python on the system when creating a venv, which can break compatibility with system Boost.Python, ROS `rclpy`, and other native libraries that are built against a specific interpreter. To keep the venv compatible with the colcon/ROS environment, colcon-uv resolves the Python version in this order:

1. A `.python-version` file in the package directory (uv / pyenv convention).
2. The interpreter currently running colcon (`sys.executable`) **when it satisfies the package's `requires-python`**. On a sourced ROS shell, colcon runs under the distro's Python, so the venv automatically tracks ROS Humble → 3.10, Jazzy → 3.12, etc.
3. The `requires-python` range, passed to uv to resolve, when colcon's interpreter doesn't satisfy it.
4. `sys.executable` as the final fallback when no range is declared.

The resolved version is passed to `uv venv --python <version>`. With this ordering you can declare a wide support range in `pyproject.toml` (e.g. `requires-python = ">=3.10,<3.13"`) and the venv still tracks the active ROS distro automatically — no per-distro config needed. To force a specific Python regardless of the active distro:

```bash
echo "3.10" > my_package/.python-version
```

### Override Dependencies

`uv pip install` does not natively read `[tool.uv].override-dependencies` from `pyproject.toml`. colcon-uv materialises that list into a temporary requirements file and passes it via `uv pip install --override`, so dependency conflicts (for example, pinning a specific `numpy` version against system packages) resolve correctly.

```toml
[tool.uv]
override-dependencies = [
    "numpy==1.26.4",
]
```

### Incremental Builds (Skip Venv Creation)

If `install/<package>/venv/` already exists, colcon-uv reuses it instead of re-creating it. Repeated `colcon build` invocations only re-run `uv pip install`, which makes incremental builds significantly faster. To force a fresh venv, delete the package's install directory.

### Sharing a Venv Across Packages (`venv-path`)

By default each colcon-uv package gets its own `install/<package>/venv/`. For workspaces that already maintain a top-level `.venv` (e.g. an outer `uv sync`-managed Python project that also ships ROS nodes), this duplicates gigabytes. Point colcon-uv at the existing venv with `venv-path`:

```toml
[tool.colcon-uv-ros]
name = "radarstack_ros"
venv-path = "../../../.venv"   # relative to the package directory
```

`venv-path` is resolved relative to the package's own directory. Absolute paths are accepted too. When set:

- colcon-uv **never creates or destroys** the venv — that is your responsibility (typically `uv sync` in the parent project).
- `uv pip install -e <package>` runs against that venv, so the ROS package and its deps land in the shared site-packages.
- If the path does not exist, the build fails with a clear error rather than silently creating a new venv. Run `uv sync` (or `uv venv <path>`) first.
- Python version resolution (`.python-version`, `requires-python`, …) is **not** applied — the venv's Python is whatever you created it with.

Typical layout this is designed for:

```
my_workspace/
├── pyproject.toml          # outer project (managed with `uv sync`)
├── .venv/                  # shared venv
└── ros2_wrapper/
    └── src/
        └── my_ros_pkg/
            └── pyproject.toml   # has venv-path = "../../../.venv"
```

### Package Source Configuration

Control where `uv pip install` fetches packages from. This is essential for platforms with custom-built wheels (e.g., NVIDIA Jetson with CUDA-specific torch builds) or private package indexes.

```toml
[tool.colcon-uv-ros]
name = "my_package"

# Override the default PyPI index (single string)
# index-url = "https://custom.pypi.org/simple"

# Additional package indexes (list)
extra-index-url = ["https://my-private.pypi.org/simple"]

# Local wheel directories or URLs (list)
find-links = ["/opt/jetson-wheels"]
```

When pyproject.toml keys are absent, the following environment variables are used as fallback:
- `COLCON_UV_INDEX_URL` — overrides default index
- `COLCON_UV_EXTRA_INDEX_URL` — comma-separated list of extra indexes
- `COLCON_UV_FIND_LINKS` — comma-separated list of find-links paths

Precedence: pyproject.toml > environment variables > uv defaults.

#### Jetson / Custom Platform Example

On platforms like NVIDIA Jetson, PyPI wheels for packages like `torch` are CPU-only. The Jetson needs GPU-specific builds. Combine `extra-site-packages` (to pre-seed existing builds) with `find-links` (for local wheel directories):

```toml
[tool.colcon-uv-ros]
name = "my_perception_package"

# Pre-seed the venv with packages from this path so uv skips them.
# Their dist-info is copied into the venv, and a .pth file makes
# Python resolve the actual modules at runtime.
extra-site-packages = ["/opt/venv/lib/python3.12/site-packages"]

# Local Jetson-built wheels for deps not pre-seeded above
find-links = ["/opt/jetson-wheels"]
```

Pin versions in `[project.dependencies]` to match your pre-seeded builds so uv doesn't resolve a different version from PyPI:

```toml
[project]
dependencies = [
    "torch==2.7.0",
    "torchvision==0.22.0",
    "ultralytics",
]
```
