import os
import sys
import importlib.util
from pathlib import Path


def check_imports(directory):
    success_count = 0
    failure_count = 0
    root_path = Path(directory).resolve()

    # Add project root to sys.path
    project_root = root_path.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    print(f"--- Checking imports in {root_path} ---")

    for py_file in root_path.glob("**/*.py"):
        if "__pycache__" in str(py_file) or ".archive" in str(py_file):
            continue

        # Get module name from path
        rel_path = py_file.relative_to(project_root)
        module_name = str(rel_path.with_suffix("")).replace(os.sep, ".")

        try:
            # Attempt to import the module
            importlib.import_module(module_name)
            print(f"[OK] {module_name}")
            success_count += 1
        except Exception as e:
            print(f"[FAIL] {module_name}: {e}")
            failure_count += 1

    print(f"\nSummary: {success_count} success, {failure_count} failures")
    return failure_count


if __name__ == "__main__":
    src_dir = os.path.join(os.getcwd(), "src")
    failures = check_imports(src_dir)
    sys.exit(1 if failures > 0 else 0)
