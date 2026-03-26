import sys
from pathlib import Path


# Function to find project root by looking for pyproject.toml
def find_project_root(path):
    current_path = Path(path).resolve()
    while current_path:
        if (current_path / "pyproject.toml").exists():
            return current_path
        current_path = current_path.parent
    return None


project_root = find_project_root(__file__)
if project_root:
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    src_path = project_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

else:
    msg = "Could not find project root (pyproject.toml not found)."
    raise FileNotFoundError(msg)

# conftest.py should not typically import application modules directly unless necessary for fixtures.
# The primary goal here is path setup.
