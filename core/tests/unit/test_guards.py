"""守卫测试：把几乎不会被单点测试发现的「整条链」约束钉死。

本文件包含入口文档点名的**第二条具名守卫**（base 环境 import 链不含重型库，DEC-039）。
第一条具名守卫（错误码 ↔ hint ↔ 退出码一一对应）在 ``test_errors.py`` 中，
因为 ``errors.py`` 的 docstring 明确把守卫责任指向那里。
"""

from __future__ import annotations

import subprocess
import sys

# oracle: specified —— DEC-039 / 入口文档约束 15 的禁用清单。
FORBIDDEN_IMPORT_MODULES = [
    "torch",
    "transformers",
    "paddleocr",
    "paddlepaddle",
    "easyocr",
    "cv2",  # opencv
    "PIL",  # Pillow
    "numpy",
]

# 契约层三件：客户端与 daemon 都 import 它们，必须绝对干净。
CONTRACT_LAYER_MODULES = ["cu.errors", "cu.protocol", "cu.config"]

# 在干净子进程里导入契约层后检查 sys.modules。
# 为什么必须起新解释器：本 pytest 进程可能已被插件/其他测试拉入 numpy 等，
# 同进程检查会把「别人的污染」误判为契约层引入的依赖。
_GUARD_SCRIPT = """
import importlib
import json
import sys

for name in {modules!r}:
    importlib.import_module(name)

forbidden = {forbidden!r}
bad = sorted(m for m in forbidden if m in sys.modules)
print(json.dumps(bad))
""".format(modules=CONTRACT_LAYER_MODULES, forbidden=FORBIDDEN_IMPORT_MODULES)


def test_guard_base_env_import_chain_excludes_heavy_libraries() -> None:
    """具名守卫测试②：``import cu.errors, cu.protocol, cu.config`` 不得拉入重型库。

    DEC-039：CLI 每次命令都是新解释器，import 什么就付什么代价。
    """
    proc = subprocess.run(
        [sys.executable, "-c", _GUARD_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"子进程导入契约层失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"

    import json

    offending = json.loads(proc.stdout.strip().splitlines()[-1])
    assert offending == [], f"契约层 import 链拉入了重型库：{offending}"


def test_guard_heavy_libraries_are_not_importable_via_contract_layer_only() -> None:
    """反向确认：守卫本身有效 —— 该子进程机制能观察到被显式导入的重型库。

    若 numpy 在本机根本不存在，上一条守卫会「因为装了才会红」而失去意义；
    这里显式 import 一个已知存在的重型库（numpy），证明检测逻辑真的能报告出它。
    """
    probe = (
        "import importlib,json,sys\n"
        "importlib.import_module('numpy')\n"
        "print(json.dumps(sorted(m for m in ['numpy'] if m in sys.modules)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        # numpy 未安装：守卫失去探针意义，显式跳过而非假装通过。
        import pytest

        pytest.skip("本机未安装 numpy，重型库守卫缺少阳性探针")
    assert proc.stdout.strip() == '["numpy"]'
