"""`computer-use skill install` 的子 Agent 落点与卸载边界。

契约来源（唯一权威）：

- ``bin/computer-use.mjs`` 的 ``agentDirs`` / ``installAgents`` / ``uninstallAgents``；
- ``agents/computer-use.md`` 的定义本身（frontmatter 的 ``name`` 必须与文件名一致）；
- ``skill/computer-use/references/install-and-config.md`` 的
  ``## Installing this skill into an agent`` 段与 ``## Local commands`` 表；
- ``README.md`` 的 ``## Agent skill`` 段。

为什么要守：

1. **落点比技能少一个家族**。技能装三个家族根，Agent 只装 Claude Code ——
   把两者合成一次探测，会让「Codex 用户拿不到 Agent」看起来像正常结果。
2. **卸载不能删目录**。``~/.claude/agents/`` 是调用方自己的地盘，里面还可能有别人写的
   定义；技能目录可以整个删（``skills/computer-use`` 是我们独占的），这个不行。
   删错一次是**不可逆**的。
3. **进包**。0.1.3 发出去的包里没有 ``omni/worker.py``，而源码树里永远复现不出来 ——
   同一类问题：定义在开发机上好好的，装出去就没有。

安全前提：每个用例都把子进程的 ``USERPROFILE`` / ``HOME`` 指到 ``tmp_path``，
且 ``_temp_home_guard`` 在任何写操作之前先确认 ``node`` 也这么认为。

每条断言上方标注 oracle 分类：specified / derived / implicit。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "bin" / "computer-use.mjs"
AGENT_SOURCE = REPO / "agents" / "computer-use.md"
PACKAGE_JSON = REPO / "package.json"
AGENT_NAME = "computer-use"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="本机没有 node，无法驱动启动器命令")


def _child_env(home: Path, *, agent_dir: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    env["COMPUTER_USE_HOME"] = str(home / ".computer-use")
    # 技能那一侧也钉死，免得这些用例顺手往真实技能目录里写。
    env["COMPUTER_USE_SKILL_DIR"] = str(home / "skills-elsewhere")
    if agent_dir is None:
        env.pop("COMPUTER_USE_AGENT_DIR", None)
    else:
        env["COMPUTER_USE_AGENT_DIR"] = str(agent_dir)
    return env


def _launch(args: list[str], home: Path, *,
            agent_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [NODE or "node", str(LAUNCHER), *args],
        env=_child_env(home, agent_dir=agent_dir),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _tail(result: subprocess.CompletedProcess[str]) -> str:
    """失败信息带上启动器自己的最后一行输出，否则只剩一个退出码。"""
    out = (result.stdout or "").strip().splitlines()[-1:]
    err = (result.stderr or "").strip().splitlines()[-1:]
    return f"exit={result.returncode} stdout={out} stderr={err}"


@pytest.fixture(autouse=True)
def _temp_home_guard(tmp_path: Path) -> None:
    """安全闸：动手之前先确认子进程看到的 home 就在 tmp_path 里。"""
    home = _home(tmp_path)
    probe = subprocess.run(
        [NODE or "node", "-e", "process.stdout.write(require('node:os').homedir())"],
        env=_child_env(home),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    resolved = Path((probe.stdout or "").strip()).resolve()
    assert resolved == tmp_path.resolve() or tmp_path.resolve() in resolved.parents, (
        f"子进程看到的 home 是 {resolved}，不在 tmp_path（{tmp_path}）里，拒绝写任何 Agent 目录"
    )


# ---------------------------------------------------------------------------
# 落点探测
# ---------------------------------------------------------------------------


def test_the_agent_lands_in_the_claude_agents_directory(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / ".claude").mkdir()

    result = _launch(["skill", "install"], home)

    assert result.returncode == 0, _tail(result)
    assert (home / ".claude" / "agents" / f"{AGENT_NAME}.md").is_file()


def test_codex_and_shared_roots_do_not_get_an_agent_definition(tmp_path: Path) -> None:
    # oracle: specified —— 只有 Claude Code 有「Agent 定义」这个概念。
    #   这条防的是「技能装三个家族，Agent 就跟着装三个」的顺手写法。
    home = _home(tmp_path)
    for family in (".claude", ".codex", ".agents"):
        (home / family).mkdir()

    _launch(["skill", "install"], home)

    assert (home / ".claude" / "agents" / f"{AGENT_NAME}.md").is_file()
    assert not (home / ".codex" / "agents").exists()
    assert not (home / ".agents" / "agents").exists()


def test_no_claude_root_means_no_agent_install(tmp_path: Path) -> None:
    """没有 `~/.claude` 就什么都不装，**也不建那个目录**。

    与技能那边不同：技能「一个家族都没探测到就退回 ~/.claude」是为兼容历史落点，
    而 Agent 定义只对 Claude Code 有用 —— 凭空建一个没人读的目录，
    同时违反 DEC-087 的「不存在的家族一个目录都不许建」。
    """
    home = _home(tmp_path)

    result = _launch(["skill", "install"], home)

    assert result.returncode == 0, _tail(result)
    assert not (home / ".claude").exists(), "没有 ~/.claude 却建了一个"
    # 不装要说出来，否则表现是「主会话永远不委派」，靠现象查不到原因。
    assert "子 Agent" in (result.stdout or "")


def test_the_agent_directory_can_be_overridden(tmp_path: Path) -> None:
    # oracle: specified —— COMPUTER_USE_AGENT_DIR 是唯一落点，覆盖探测。
    home = _home(tmp_path)
    (home / ".claude").mkdir()
    elsewhere = tmp_path / "somewhere" / "agents"

    _launch(["skill", "install"], home, agent_dir=elsewhere)

    assert (elsewhere / f"{AGENT_NAME}.md").is_file()
    assert not (home / ".claude" / "agents").exists()


# ---------------------------------------------------------------------------
# 卸载边界
# ---------------------------------------------------------------------------


def test_uninstall_removes_only_our_definition(tmp_path: Path) -> None:
    # oracle: specified —— 卸载删的是那一个文件，不是目录：里面可能有别人写的定义。
    home = _home(tmp_path)
    agents = home / ".claude" / "agents"
    agents.mkdir(parents=True)
    stranger = agents / "someone-elses-agent.md"
    stranger.write_text("---\nname: someone-elses-agent\n---\n", encoding="utf-8")

    _launch(["skill", "install"], home)
    result = _launch(["skill", "uninstall"], home)

    assert result.returncode == 0, _tail(result)
    assert not (agents / f"{AGENT_NAME}.md").exists()
    assert stranger.is_file(), "卸载把别人的定义一起删了"


def test_uninstall_is_quiet_when_nothing_was_installed(tmp_path: Path) -> None:
    # oracle: derived —— 没装过也要成功退出，否则「先卸载再装」这种常规写法会先炸一次。
    result = _launch(["skill", "uninstall"], _home(tmp_path))

    assert result.returncode == 0, _tail(result)


# ---------------------------------------------------------------------------
# 定义本身
# ---------------------------------------------------------------------------


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} 缺少 frontmatter"
    block = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    key = None
    for line in block.splitlines():
        if line.startswith(("  ", "\t")) and key:      # YAML 折行（`>-`）的续行
            fields[key] = f"{fields[key]} {line.strip()}"
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip().lstrip(">-").strip()
    return fields


def test_the_definition_names_itself_the_way_the_installer_does() -> None:
    # oracle: specified —— Claude Code 按 frontmatter 的 name 注册这个 Agent，
    #   与文件名不一致时会出现「装了却叫不出那个名字」。
    fields = _frontmatter(AGENT_SOURCE)

    assert fields["name"] == AGENT_NAME
    assert fields["description"], "没有 description 的 Agent 不会被主会话选中"
    assert "computer-use" in fields["description"]


def test_the_definition_can_run_the_cli() -> None:
    # oracle: derived —— 定义让子 Agent 跑 CLI，所以 Bash 是必需的；
    #   少了它，这个 Agent 装上之后一步都走不动。
    tools = _frontmatter(AGENT_SOURCE)["tools"]

    assert "Bash" in tools


# ---------------------------------------------------------------------------
# 进包
# ---------------------------------------------------------------------------


def test_the_definition_is_listed_in_the_package_files() -> None:
    """与 `test_pack_contents.py` 的 worker 那条同源：源码树里好好的，装出去就缺文件。"""
    listed = set(json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["files"])

    assert "agents/" in listed
    assert AGENT_SOURCE.is_file(), "发布清单里有 agents/，源码树里却没有定义文件"


def test_the_installed_definition_matches_the_source(tmp_path: Path) -> None:
    # oracle: derived —— 装出去的那份必须与源码逐字节一致，否则改的是源码、跑的是旧文。
    home = _home(tmp_path)
    (home / ".claude").mkdir()

    _launch(["skill", "install"], home)

    installed = home / ".claude" / "agents" / f"{AGENT_NAME}.md"
    assert installed.read_bytes() == AGENT_SOURCE.read_bytes()
