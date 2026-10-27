"""Which test files can only run on Windows (v1.5, stage S4b).

conftest.py uses this on Linux (the ubuntu-latest CI job) to keep pytest from collecting the
files that import the Windows guard, which cannot import there: hwinfo_12vhpwr_guard loads
kernel32 through ctypes.WinDLL at module scope.

The list is worked out from the source rather than kept by hand, so a new test file lands on
the right side without anyone remembering to update a list. A file is Windows only when its
module body (top level, not inside a function, an if or a try) imports:
- hwinfo_12vhpwr_guard, the Windows guard itself;
- helpers, whose fakes import the Windows guard;
- another test module that is Windows only by these same rules (test_s2_mitigation_platform
  borrows FakeNvml from test_f5_release, for example).

An import inside a try block is deliberately not counted: that is how a file says "I can run
without it", as test_guard_core.py does to keep its platform-neutral suites running on Linux.

Stdlib only, and importable without conftest, so tests/test_linux_packaging.py can check it.
"""

import ast
import os
from typing import Dict, List, Set

WINDOWS_ONLY_MODULES = frozenset({"hwinfo_12vhpwr_guard", "helpers"})


def _top_level_imports(path: str) -> Set[str]:
    """First dotted component of every module the file imports at the top of its body."""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names: Set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def windows_only_test_files(tests_dir: str) -> List[str]:
    """Sorted basenames of the test_*.py files in tests_dir that need the Windows guard."""
    imports: Dict[str, Set[str]] = {}
    for name in os.listdir(tests_dir):
        if name.startswith("test_") and name.endswith(".py"):
            imports[name[:-3]] = _top_level_imports(os.path.join(tests_dir, name))

    windows_only: Set[str] = {mod for mod, used in imports.items() if used & WINDOWS_ONLY_MODULES}
    # Follow test-to-test imports until nothing new turns up.
    changed = True
    while changed:
        changed = False
        for mod, used in imports.items():
            if mod not in windows_only and used & windows_only:
                windows_only.add(mod)
                changed = True
    return sorted(mod + ".py" for mod in windows_only)
