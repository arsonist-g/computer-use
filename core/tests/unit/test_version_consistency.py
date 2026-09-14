"""守卫：版本号的三个载体必须始终一致（跨文件一致性单测）。

oracle: specified —— 契约：npm 包版本取自 `package.json`，而 `computer-use --version`
走 `cu.__version__`（`client.py` 的 `--version` 动作把 `__version__` 印给用户）。
两者漂移会让「发的是 0.1.1、`--version` 报 0.1.0」这种发布事故无人拦下 —— 用户拿着
`--version` 报出来的版本去查问题，查到的是一份不相干的代码。

为什么必须由测试守：三个载体分散在三种格式里（json / toml / python 常量），
改一个不会想起另外两个。写法参照 `test_storage_ids_consistency.py`：跨文件一致性单测。

解析一律走正规工具（`tomllib` / `json` / 直接 import），不用正则：正则会把
`[tool.ruff] target-version = "py312"` 这类同名字段一并捞进来当版本号。
文件被挪走时 `read_text` 直接报错，用例红 —— 不会静默通过。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import cu

UNIT_DIR = Path(__file__).resolve().parent
PYPROJECT_TOML = UNIT_DIR.parents[1] / "pyproject.toml"
PACKAGE_JSON = UNIT_DIR.parents[2] / "package.json"

#: 三个载体的人读名字 —— 失败信息里出现的就是它们。
PACKAGE = "package.json 的 version"
PYPROJECT = "core/pyproject.toml 的 [project] version"
MODULE = "cu.__version__"

#: 两两比较。参数化之后，失败用例的 id 直接点名是哪两个载体漂了。
CARRIER_PAIRS = [
    (PACKAGE, PYPROJECT),
    (PYPROJECT, MODULE),
    (PACKAGE, MODULE),
]


def _carrier_versions() -> dict[str, str]:
    """三个载体的版本字符串，每个走它自己的正规解析路子。"""
    pyproject = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    return {
        PACKAGE: package["version"],
        PYPROJECT: pyproject["project"]["version"],
        MODULE: cu.__version__,
    }


@pytest.mark.parametrize(("left", "right"), CARRIER_PAIRS,
                         ids=["package-vs-pyproject", "pyproject-vs-module",
                              "package-vs-module"])
def test_version_carriers_agree_pairwise(left: str, right: str) -> None:
    # oracle: specified —— 三个载体两两相等
    versions = _carrier_versions()
    assert versions[left] == versions[right], (
        f"{left}={versions[left]!r} 与 {right}={versions[right]!r} 不一致"
    )


def test_every_carrier_reports_a_nonempty_version() -> None:
    # oracle: implicit —— 空值之间相等没有意义；载体缺失或空值必须自己红
    for carrier, value in _carrier_versions().items():
        assert isinstance(value, str) and value.strip(), f"{carrier} 的版本为空"
