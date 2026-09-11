"""守卫：`omni/` 与 base 环境**零代码共享**（架构 §1.5 第 3 条 / DEC-037）。

这条约束的性质决定了它必须由测试来守：`omni/` 跑在 `venv-omni` 里，
那个解释器里**没有** `cu` 包。一旦有人在 `omni/` 里写下 `from cu.xxx import ...`，
它在开发机上可能偶然能跑通（因为 base 的 `cu` 也在同一台机器上），
到了用户的机器上才炸。这种「本地能跑、装出去就坏」的缺陷，只有静态检查拦得住。

第二条：`omni/` 也不得依赖 base 的依赖（`windows-capture` 等）——
它的依赖清单是独立的，交叉引用会让两个环境的隔离形同虚设。
"""

from __future__ import annotations

import ast
from pathlib import Path

UNIT_DIR = Path(__file__).resolve().parent
OMNI_DIR = UNIT_DIR.parents[2] / "omni"

#: `omni/` 里禁止出现的顶层模块名。
FORBIDDEN_IMPORTS = {"cu", "windows_capture"}


def _omni_modules() -> list[Path]:
    return sorted(p for p in OMNI_DIR.glob("*.py") if p.name != "__init__.py")


def _imported_top_level(path: Path) -> set[str]:
    """收集一个文件里所有 import 的顶层模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # `level > 0` 是相对导入，只能在包内使用；顶层名记为空。
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_omni_directory_exists_and_has_modules() -> None:
    """守卫的前提：目录真的存在且有内容。目录被挪走时不能静默通过。"""
    modules = _omni_modules()
    assert modules, f"{OMNI_DIR} 下没有 Python 模块，守卫失去对象"
    assert {p.name for p in modules} >= {"worker.py", "markdown.py"}, \
        f"omni 模块集合与预期不符：{[p.name for p in modules]}"


def test_omni_does_not_import_the_base_package() -> None:
    """零代码共享：`omni/` 不得 import `cu`，也不得 import base 的依赖。"""
    violations: list[str] = []
    for path in _omni_modules():
        offending = _imported_top_level(path) & FORBIDDEN_IMPORTS
        if offending:
            violations.append(f"{path.name}: {sorted(offending)}")
    assert not violations, (
        "omni/ 与 base 环境必须零代码共享，但它 import 了 base 的东西：\n  "
        + "\n  ".join(violations)
        + "\n两边只通过管道与文件交互（架构 §1.5 第 3 条）。"
    )


def test_omni_can_be_parsed_without_the_base_package() -> None:
    """反向确认：`omni/` 的每个文件都能独立解析，不依赖任何运行时上下文。

    前一条测的是「有没有写 import cu」，这一条测的是「有没有别的隐式依赖」
    （例如从 `cu` 借来的模块级常量、类型别名）。独立解析是最廉价的代理。

    刻意用 AST 而不是 `"from cu" in source` 这种字符串匹配：后者会被文档里的
    说明文字误伤（本项目在注释里大量引用 `cu.xxx` 来说明边界），
    一条会因为注释而变红的守卫，很快就会被改成 `# noqa` 然后失效。
    """
    for path in _omni_modules():
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        assert not (_imported_top_level(path) & FORBIDDEN_IMPORTS), \
            f"{path.name} 里出现了对 base 的 import"
