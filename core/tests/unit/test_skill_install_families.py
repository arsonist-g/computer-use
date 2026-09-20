"""``computer-use skill install / uninstall`` 的家族探测契约（retro-fit 补的守卫）。

契约来源（唯一权威）：

- ``README.md`` 的 ``## Agent skill`` 段
- ``skill/computer-use/references/install-and-config.md`` 的
  ``## Installing this skill into an agent`` 段，以及 ``## Local commands`` 表里
  ``skill install`` / ``skill uninstall`` 那一行
- ``bin/computer-use.mjs`` 的 ``--launcher-help`` 文案

契约要点：

- 装到「本机确实存在」的每个 Agent 家族技能目录（``~/.claude/skills/``、
  ``~/.codex/skills/``、``~/.agents/skills/``）；家族根目录不存在就不给它建目录。
- 三个家族都不存在时退回 ``~/.claude/skills/``（历史落点）。
- ``COMPUTER_USE_SKILL_DIR`` 存在时是**唯一**落点，覆盖探测。
- 只装英文在役文件，``*-zh.md`` 校对本留在包里。
- install / uninstall 成功退出码 0。

为什么必须由测试守：``core/tests/native/acceptance_install.py`` 第 8.4/8.5 项与
``acceptance_pack_install.py`` 第 7 项都用 ``COMPUTER_USE_SKILL_DIR`` 做隔离，
**探测分支一次都没被跑过** —— 而「装了 Codex 的用户拿不到这份技能」正是这个分支的
缺陷面。探测是「按现场环境分叉」的行为，单点传参的验收脚本天然够不着。

安全前提：本文件每个用例都把子进程的 ``USERPROFILE`` / ``HOME`` 指到 ``tmp_path``，
且 ``_temp_home_guard`` 在任何写操作之前先确认 ``node`` 也这么认为；真实用户目录不在
任何一条用例的作用域里。

每条断言上方标注 oracle 分类：specified / derived / implicit。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "bin" / "computer-use.mjs"
SKILL_SOURCE = REPO / "skill" / "computer-use"
SKILL_NAME = "computer-use"

#: 契约点名的三个 Agent 家族根（相对 home）。
FAMILIES = (".claude", ".codex", ".agents")

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="本机没有 node，无法驱动启动器命令")


# ---------------------------------------------------------------------------
# 驱动启动器：home 必须在 temp 里
# ---------------------------------------------------------------------------


def _child_env(home: Path, skill_dir: Path | None = None) -> dict[str, str]:
    """子进程环境：把 home 钉死在 temp 里，并掐掉可能指向真实目录的变量。"""
    env = dict(os.environ)
    # Node 的 os.homedir() 在 Windows 上先看 USERPROFILE；HOME 与 HOMEDRIVE/HOMEPATH
    # 一并改掉，任何回退路径都出不了 tmp_path。
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    # 技能命令不碰数据目录，钉住它是为了消掉「环境里带着真实路径」这一整类意外。
    env["COMPUTER_USE_HOME"] = str(home / ".computer-use")
    if skill_dir is None:
        env.pop("COMPUTER_USE_SKILL_DIR", None)
    else:
        env["COMPUTER_USE_SKILL_DIR"] = str(skill_dir)
    return env


def _launch(args: list[str], home: Path,
            skill_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [NODE or "node", str(LAUNCHER), *args],
        env=_child_env(home, skill_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _tail(result: subprocess.CompletedProcess[str]) -> str:
    """失败信息带上启动器自己的最后一行输出，否则只剩一个退出码。"""
    out = (result.stdout or "").strip().splitlines()[-1:]
    err = (result.stderr or "").strip().splitlines()[-1:]
    return f"exit={result.returncode} stdout={out} stderr={err}"


def _temp_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _skill_dir(home: Path, family: str) -> Path:
    return home / family / "skills" / SKILL_NAME


def _installed(home: Path, family: str) -> bool:
    return (_skill_dir(home, family) / "SKILL.md").is_file()


def _tree(root: Path) -> list[Path]:
    """目录下全部文件的相对路径，按 posix 形式排序。"""
    return sorted(
        (path.relative_to(root) for path in root.rglob("*") if path.is_file()),
        key=lambda rel: rel.as_posix(),
    )


@pytest.fixture(autouse=True)
def _temp_home_guard(tmp_path: Path) -> None:
    """安全闸：动手之前先确认子进程看到的 home 就在 tmp_path 里。

    本文件会对 home 下的技能目录做创建与删除。若哪天 Node 改了 home 解析优先级，
    这个断言会在任何写操作之前把用例拦住，而不是去动真实用户目录。
    """
    home = _temp_home(tmp_path)
    probe = subprocess.run(
        [NODE or "node", "-e", "process.stdout.write(require('node:os').homedir())"],
        env=_child_env(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    resolved = Path((probe.stdout or "").strip()).resolve()
    assert resolved == tmp_path.resolve() or tmp_path.resolve() in resolved.parents, (
        f"子进程看到的 home 是 {resolved}，不在 tmp_path（{tmp_path}）里，拒绝写任何技能目录"
    )


# ---------------------------------------------------------------------------
# 家族探测：决策表
# ---------------------------------------------------------------------------

#: 决策表：家族根目录的存在组合 → 契约要求装上的家族集合。
#: 契约把三个家族当作彼此的独立条件（「only when its root directory already exists」），
#: 故每个存在组合就是一条规则；7 条非空规则逐条覆盖，空组合（走历史回退）单独一条。
FAMILY_CASES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    ((".claude",), (".claude",)),
    ((".codex",), (".codex",)),
    ((".agents",), (".agents",)),
    ((".claude", ".codex"), (".claude", ".codex")),
    ((".claude", ".agents"), (".claude", ".agents")),
    ((".codex", ".agents"), (".codex", ".agents")),
    (FAMILIES, FAMILIES),
]

FAMILY_CASE_IDS = [
    "claude", "codex", "agents", "claude+codex", "claude+agents", "codex+agents", "all-three",
]


@pytest.mark.parametrize(("present", "expected"), FAMILY_CASES, ids=FAMILY_CASE_IDS)
def test_install_covers_exactly_the_families_present(
    tmp_path: Path, present: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    """存在的家族都要装上；不存在的家族一个目录都不许建。

    刻意只建家族根、不建 ``skills/`` 子目录：契约的判据是根目录本身存在与否
    （边界取在「根存在 / 根不存在」，不是「根下的 skills 目录存在」）。
    """
    home = _temp_home(tmp_path)
    for family in present:
        (home / family).mkdir()

    result = _launch(["skill", "install"], home=home)

    # oracle: specified —— 契约「install 成功退出码 0」
    assert result.returncode == 0, _tail(result)
    for family in expected:
        # oracle: specified —— 「copies this skill into the skill directory of every agent
        # family present on this machine」
        assert _installed(home, family), (
            f"家族 {family} 的根目录存在，install 却没落到 {_skill_dir(home, family)}；"
            f"{_tail(result)}"
        )
    for family in sorted(set(FAMILIES) - set(expected)):
        # oracle: specified —— 「nothing is created for an agent that is not installed」
        assert not (home / family).exists(), f"没装的家族 {family} 被凭空建了目录"


def test_install_falls_back_to_the_claude_path_when_no_family_exists(tmp_path: Path) -> None:
    """一个家族都没有时走历史落点，而不是什么都不装。"""
    home = _temp_home(tmp_path)

    result = _launch(["skill", "install"], home=home)

    # oracle: specified —— 契约「install 成功退出码 0」
    assert result.returncode == 0, _tail(result)
    # oracle: specified —— 「when no family is found, the install falls back to the
    # Claude Code path」
    assert _installed(home, ".claude"), f"没有家族时未退回 Claude 落点；{_tail(result)}"
    for family in (".codex", ".agents"):
        # oracle: specified —— 回退不等于顺手把其余家族也建出来
        assert not (home / family).exists(), f"回退落点之外还建了 {family}"


# ---------------------------------------------------------------------------
# 环境变量覆盖
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("present", [FAMILIES, ()],
                         ids=["all-families-present", "no-family-present"])
def test_skill_dir_env_var_is_the_only_landing_point(
    tmp_path: Path, present: tuple[str, ...]
) -> None:
    """``COMPUTER_USE_SKILL_DIR`` 是唯一落点：它覆盖探测，不是往探测结果上追加。"""
    home = _temp_home(tmp_path)
    for family in present:
        (home / family).mkdir()
    custom = tmp_path / "custom-skill-dir" / SKILL_NAME

    result = _launch(["skill", "install"], home=home, skill_dir=custom)

    # oracle: specified —— 契约「install 成功退出码 0」
    assert result.returncode == 0, _tail(result)
    # oracle: specified —— 「Set COMPUTER_USE_SKILL_DIR to install into exactly one
    # directory instead」
    assert (custom / "SKILL.md").is_file(), f"环境变量落点没拿到 SKILL.md；{_tail(result)}"
    for family in FAMILIES:
        # oracle: specified —— 「exactly one directory」：环境变量之下不走探测，也不留回退
        assert not (home / family / "skills").exists(), (
            f"环境变量已指定唯一落点，却仍写了 {family}/skills"
        )


# ---------------------------------------------------------------------------
# 落盘内容：英文在役文件，逐字节
# ---------------------------------------------------------------------------


def test_install_copies_the_english_skill_tree_byte_for_byte(tmp_path: Path) -> None:
    """落盘树形 == 包内英文在役文件清单，且逐字节一致；译本不落盘。"""
    home = _temp_home(tmp_path)
    (home / ".claude").mkdir()

    result = _launch(["skill", "install"], home=home)

    # oracle: specified —— 契约「install 成功退出码 0」
    assert result.returncode == 0, _tail(result)
    installed = _skill_dir(home, ".claude")

    # oracle: specified —— 文档点名 ``references/`` 里是「完整命令参考 + 封闭错误码集合」
    assert (installed / "SKILL.md").is_file(), f"SKILL.md 没落盘；{_tail(result)}"
    assert (installed / "references" / "errors.md").is_file()
    assert (installed / "references" / "install-and-config.md").is_file()

    # oracle: derived —— 差分：落盘树形必须等于「包内清单去掉 `-zh.md`」，一条不多一条不少
    expected = [rel.as_posix() for rel in _tree(SKILL_SOURCE) if not rel.name.endswith("-zh.md")]
    assert [rel.as_posix() for rel in _tree(installed)] == expected, (
        f"落盘树形与包内英文清单不一致；{_tail(result)}"
    )
    for rel in _tree(installed):
        # oracle: derived —— 拷贝的独立性：每一份都逐字节等于包内源文件
        assert (installed / rel).read_bytes() == (SKILL_SOURCE / rel).read_bytes(), (
            f"{rel.as_posix()} 与包内源文件不一致"
        )
    # oracle: specified —— 「the `*-zh.md` proofreading translations stay in the package」
    assert sorted(path.name for path in installed.rglob("*-zh.md")) == []


# ---------------------------------------------------------------------------
# 卸载：与安装对称
# ---------------------------------------------------------------------------


def test_uninstall_removes_every_installed_family_directory(tmp_path: Path) -> None:
    """install 装了几处，uninstall 就得拆几处。"""
    home = _temp_home(tmp_path)
    for family in FAMILIES:
        (home / family).mkdir()

    install = _launch(["skill", "install"], home=home)
    # oracle: specified —— 前提：三个家族都在场时都该被装上
    assert install.returncode == 0 and all(_installed(home, f) for f in FAMILIES), (
        f"前提不成立：三个家族都该装上；{_tail(install)}"
    )

    uninstall = _launch(["skill", "uninstall"], home=home)

    # oracle: specified —— 契约「uninstall 成功退出码 0」
    assert uninstall.returncode == 0, _tail(uninstall)
    for family in FAMILIES:
        # oracle: specified —— 「copies ... or removes it」：对称，同一批目录全移除
        assert not _skill_dir(home, family).exists(), f"{family} 的安装残留没被移除"


def test_install_and_uninstall_leave_neighbouring_skills_alone(tmp_path: Path) -> None:
    """落点是 ``<家族>/skills/computer-use``，同级的别的技能目录不在作用域内。"""
    home = _temp_home(tmp_path)
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    neighbours = [home / family / "skills" / "other-skill" for family in (".claude", ".codex")]
    for neighbour in neighbours:
        neighbour.mkdir(parents=True)
        (neighbour / "SKILL.md").write_text("synthetic neighbour skill\n", encoding="utf-8")

    install = _launch(["skill", "install"], home=home)
    # oracle: specified —— 契约「install 成功退出码 0」
    assert install.returncode == 0, _tail(install)
    for neighbour in neighbours:
        # oracle: derived —— 契约给出的落点是 ``<家族>/skills/<name>``，兄弟目录不属于本命令
        assert (neighbour / "SKILL.md").is_file(), f"install 动了不属于它的 {neighbour}"

    uninstall = _launch(["skill", "uninstall"], home=home)
    # oracle: specified —— 契约「uninstall 成功退出码 0」
    assert uninstall.returncode == 0, _tail(uninstall)
    for neighbour in neighbours:
        # oracle: derived —— 「removes it」指的是自己那一份，不是整层 skills 目录
        assert (neighbour / "SKILL.md").is_file(), f"uninstall 连 {neighbour} 一起删了"
    for family in (".claude", ".codex"):
        # oracle: specified —— 对称：本命令自己的落点必须被移除
        assert not _skill_dir(home, family).exists(), f"{family} 的安装残留没被移除"


# ---------------------------------------------------------------------------
# 幂等与帮助文案
# ---------------------------------------------------------------------------


def test_second_install_leaves_the_same_tree(tmp_path: Path) -> None:
    """升级后再跑一次 install（目标目录已存在）必须照样成功、结果不变。

    oracle: implicit —— 契约没写重复安装，这里只查不变量。
    """
    home = _temp_home(tmp_path)
    (home / ".codex").mkdir()

    first = _launch(["skill", "install"], home=home)
    # oracle: specified —— 契约「install 成功退出码 0」（第一次）
    assert first.returncode == 0, _tail(first)
    installed = _skill_dir(home, ".codex")
    before = {rel.as_posix(): (installed / rel).read_bytes() for rel in _tree(installed)}

    second = _launch(["skill", "install"], home=home)

    # oracle: implicit —— 契约没写重复安装，这里只查不变量：不报错
    assert second.returncode == 0, _tail(second)
    # oracle: implicit —— 且落盘内容与第一次一致
    after = {rel.as_posix(): (installed / rel).read_bytes() for rel in _tree(installed)}
    assert after == before


def test_launcher_help_lists_the_commands_the_launcher_handles(tmp_path: Path) -> None:
    """``--launcher-help`` 要列出启动器自己处理的那几条命令。"""
    home = _temp_home(tmp_path)

    result = _launch(["--launcher-help"], home=home)

    # oracle: specified —— 契约「`--launcher-help` 成功退出码 0」
    assert result.returncode == 0, _tail(result)
    for command in ("env sync", "skill install", "skill uninstall"):
        # oracle: specified —— 安装文档：``--launcher-help`` 列出「启动器自己处理的命令」，
        # 而 Local commands 表把这三条标为 Launcher commands
        assert command in result.stdout, f"--launcher-help 没列出启动器自处理的 {command!r}"
