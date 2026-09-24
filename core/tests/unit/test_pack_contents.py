"""守卫：运行时会去找的文件，必须在 npm 的发布清单里。

2026-09-24 查出来的实情：**0.1.3 及其之前发出去的包里没有 `omni/worker.py`**。
`omni.worker_script()` 在装出来的包里解析到 `<包根>/omni/worker.py`，而 `package.json`
的 `files` 只列了 `bin/`、`core/src/`、`core/pyproject.toml`、`core/README.md`、`skill/`
—— `omni/` 从来没进去过。表现是：`npm i -g` 装出来的那份，`parse` 直接报
`omni_not_installed`（worker 脚本不存在），**核心的截图/窗口/输入全都正常，只有解析是死的**。

为什么单测要守：开发机上 `parse` 走的是工作树，`omni/worker.py` 就在那儿，
于是这个缺陷在源码树里**永远复现不出来**；只有把包真的装进干净目录才会露出来。
这正是 `test_omni_isolation.py` 那条守卫的同一类问题（本地能跑、装出去就坏），
区别是它守的是「环境隔离」，这条守的是「文件有没有进包」。

写法刻意不是「断言字符串 `omni/worker.py` 在列表里」，而是**按运行时的解析结果算**：
先问运行时它要哪个文件（`omni.worker_script()`），再把这个路径换算成包内相对路径，
最后断言发布清单覆盖它。将来 worker 挪到别处，这条守卫跟着挪，不会变成一条只认旧路径的死断言。
"""

from __future__ import annotations

import json
from pathlib import Path

from cu.desktop import omni

UNIT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = UNIT_DIR.parents[2]
PACKAGE_JSON = PACKAGE_ROOT / "package.json"


def test_the_omni_worker_shipped_in_the_package_is_listed_in_files() -> None:
    listed = set(json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["files"])

    worker = omni.worker_script().resolve()
    relative = worker.relative_to(PACKAGE_ROOT.resolve()).as_posix()

    assert relative in listed, (
        f"运行时要去取 {relative}，但 package.json 的 files 里没有它：{sorted(listed)}\n"
        "装出来的包里缺这个文件时，`parse` 会因为「worker 脚本不存在」直接失败 —— "
        "而源码树里跑得好好的，看不出来。"
    )
    assert worker.is_file(), f"{relative} 在源码树里都不存在，先修文件本身"


def test_the_worker_is_a_single_self_contained_file() -> None:
    """`omni/` 只发 worker.py 一个文件（它必须是自足的）。

    反面：如果 worker 开始依赖同目录的别的模块，上面那条守卫会通过，而装出来的包会缺文件。
    """
    omni_dir = PACKAGE_ROOT / "omni"
    py_files = sorted(p.name for p in omni_dir.glob("*.py"))
    assert py_files == ["worker.py"], f"omni/*.py = {py_files}；发布清单只带了 worker.py"
