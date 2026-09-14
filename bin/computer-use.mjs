#!/usr/bin/env node
/**
 * Computer-Use 启动器 —— 极薄的一层（DEC-032）。
 *
 * 它**不参与运行时逻辑**，只做四件事：
 *   1. 定位（必要时创建并同步）uv 管理的 Python 环境；
 *   2. 把 argv / stdio / 退出码原样转发给 `python -m cu`；
 *   3. 首次安装流程：检测 uv、建环境、装依赖；
 *   4. `skill install|uninstall` —— 把 skill（SKILL.md 与 references/）装到调用方 Agent 的技能目录。
 *
 * 没有 Node↔Python 的跨语言协议：Node 进程被 Python 进程整体替换（exec 语义），
 * 因此不存在需要维护的中间格式。
 *
 * 依赖管理的选择见 DEC-018 / DEC-037：环境落在 ~/.computer-use/，
 * 与系统 Python 完全隔离；uv 缺失时报错并给安装指引，**不静默回退系统 pip**。
 */

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  closeSync,
  cpSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
  writeSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = resolve(HERE, "..");
const CORE_DIR = join(PACKAGE_ROOT, "core");
const PYPROJECT = join(CORE_DIR, "pyproject.toml");

const DATA_DIR = process.env.COMPUTER_USE_HOME || join(homedir(), ".computer-use");
const VENV_DIR = join(DATA_DIR, "venv");
const VENV_PYTHON = join(VENV_DIR, "Scripts", "python.exe");
const ENV_STAMP = join(VENV_DIR, ".computer-use-stamp");

//: 环境安装的跨进程锁。两个 agent 会话同时首次运行会各跑一次 `uv venv`，撞在同一个
//: `venv\Scripts\python.exe` 上（实测败者报 os error 32）。用独占创建的锁文件把
//: 「建环境 + 装依赖」串行化，败者等赢家做完直接复用。
const ENV_LOCK = join(DATA_DIR, ".env-sync.lock");
//: 等锁上限：首次安装要 uv 建环境并装依赖，冷机器上是分钟级。
const LOCK_WAIT_MS = 15 * 60 * 1000;
//: 锁文件超过这个时长没被更新，视为持有者已崩（正常安装走不到这么久）。
const LOCK_STALE_MS = 30 * 60 * 1000;
const LOCK_POLL_MS = 500;
//: 等对端把 venv 建出来（`uv venv` 失败后的兜底窗口）。
const PEER_VENV_WAIT_MS = 60 * 1000;

const UV_MISSING_HELP = `
uv 未安装 —— Computer-Use 用它管理隔离的 Python 环境（DEC-018）。

  Windows (PowerShell):  irm https://astral.sh/uv/install.ps1 | iex
  或:                    winget install --id=astral-sh.uv -e
  或:                    scoop install uv

安装后重新运行本命令。不会退回系统 pip —— 那会污染全局环境并与其它项目冲突。
`.trim();

/**
 * 环境同步的判据：pyproject.toml 的内容哈希。
 *
 * 用内容而不是时间戳：npm 升级会重写文件，时间戳在复制/还原后不可靠，
 * 而内容变了才真的需要重装。缓存在环境目录里的一个戳文件上，
 * 因此**热路径只读一个几 KB 的文件**，不做任何 uv 调用。
 */
function fingerprint() {
  const content = readFileSync(PYPROJECT);
  return createHash("sha256").update(content).digest("hex").slice(0, 16);
}

function haveUv() {
  const probe = spawnSync("uv", ["--version"], { encoding: "utf8", shell: false, windowsHide: true });
  return probe.status === 0;
}

/**
 * `core/pyproject.toml` 里的 `requires-python`。
 *
 * 建环境时必须把它交给 uv。不给的话 uv 用自己的默认解释器 —— 那是**这台机器**的
 * 默认，不是本项目的要求。实测踩到过：本机默认是 CPython 3.10.20，而项目要求
 * >=3.11，于是 `uv venv` 建出一个 3.10 的环境，紧接着装依赖必然无解，
 * 报错长这样：
 *
 *     Because the current Python version (3.10.20) does not satisfy Python>=3.11
 *     and cu==0.1.0 depends on Python>=3.11, we can conclude that cu==0.1.0
 *     cannot be used. And because only cu==0.1.0 is available and you require cu…
 *
 * 这段话读起来像「PyPI 上有另一个叫 cu 的包」，完全指不到真正的原因（解释器太旧）。
 * 把约束显式传过去，uv 会自己挑一个满足条件的解释器（必要时下载一个托管的），
 * 全新安装路径才不依赖用户机器上恰好装了什么。
 */
function requiresPython() {
  try {
    const text = readFileSync(PYPROJECT, "utf8");
    const matched = text.match(/^\s*requires-python\s*=\s*["']([^"']+)["']/m);
    return matched ? matched[1] : "";
  } catch {
    return "";
  }
}

function run(cmd, args, options = {}) {
  return spawnSync(cmd, args, {
    stdio: options.capture ? "pipe" : "inherit",
    encoding: "utf8",
    shell: false,
    // `windowsHide` 是关键的那一位：Windows 上「没有控制台的创建者」拉起控制台
    // 子系统程序时，系统会给它新开一个控制台窗口；`stdio` 设置压不住它。
    // 少了这一行，`env sync` 期间会弹窗（uv 探测 / uv 安装各一次）。
    windowsHide: true,
    ...options,
  });
}

/** 同步睡眠。Node 没有 sleepSync，`Atomics.wait` 是标准做法且不让出 CPU 空转。 */
function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** 戳文件是否与当前 pyproject 指纹一致（= 环境已经是这一版）。 */
function stampMatches(want) {
  try {
    return existsSync(ENV_STAMP) && readFileSync(ENV_STAMP, "utf8").trim() === want;
  } catch {
    return false;
  }
}

/**
 * `VENV_PYTHON` 是否存在**且真的能跑**。
 *
 * 只判存在不够：对端可能正把 `python.exe` 拷到一半，文件已在但还不可用。
 * 起一个解释器探针是唯一可靠的「可用」判据 —— 这也是「别把正常失败吞掉」的
 * 落点：探针跑不起来就仍是失败。
 */
function venvUsable() {
  if (!existsSync(VENV_PYTHON)) return false;
  return run(VENV_PYTHON, ["-c", "import sys"], { capture: true }).status === 0;
}

/** 独占创建锁文件；已被占用返回 false。 */
function tryTakeLock() {
  try {
    const fd = openSync(ENV_LOCK, "wx");
    try {
      writeSync(fd, `${process.pid}\n`);
    } finally {
      closeSync(fd);
    }
    return true;
  } catch (error) {
    if (error.code === "EEXIST") return false;
    throw error;
  }
}

/** 锁文件的年龄（毫秒）；文件刚被释放时返回 null。 */
function lockAgeMs() {
  try {
    return Date.now() - statSync(ENV_LOCK).mtimeMs;
  } catch {
    return null;
  }
}

/** 等对端把 venv 建出来 —— `uv venv` 撞车之后的兜底。 */
function waitForPeerVenv() {
  const deadline = Date.now() + PEER_VENV_WAIT_MS;
  while (Date.now() < deadline) {
    if (venvUsable()) return true;
    sleep(LOCK_POLL_MS);
  }
  return venvUsable();
}

/** 建环境 + 装依赖。调用方必须已持有 `ENV_LOCK`。 */
function buildEnvironment(want) {
  if (!existsSync(VENV_PYTHON)) {
    process.stderr.write(`computer-use: 创建 Python 环境 ${VENV_DIR}\n`);
    const wanted = requiresPython();
    const venvArgs = ["venv", VENV_DIR, ...(wanted ? ["--python", wanted] : [])];
    const created = run("uv", venvArgs);
    if (created.status !== 0) {
      // 并发首次运行的兜底：锁没覆盖到的极端情况（锁被判为陈旧而夺走、或用户在
      // 加锁之前就手起了两个进程）下，对端可能已经/正在建好同一个环境，此时
      // `uv venv` 会撞在拷贝 `python.exe` 上并报 os error 32。
      // **只有环境确实可用才当成功** —— 探针跑不起来说明这是真正的失败
      // （解释器不满足 requires-python、磁盘没空间），照旧报错退出。
      if (waitForPeerVenv()) {
        process.stderr.write("computer-use: 另一个进程已建好环境，直接复用\n");
      } else {
        process.stderr.write(`computer-use: 创建环境失败（exit ${created.status}）\n`);
        process.exit(1);
      }
    }
  }

  process.stderr.write("computer-use: 安装依赖\n");
  const installArgs = ["pip", "install", "--python", VENV_PYTHON, "-e", CORE_DIR];
  let installed = run("uv", installArgs, { capture: true });
  if (installed.status !== 0) {
    // 单次回退官方源（DEC-042）。**两次的报错都要透出来** —— 只印第二次的话，
    // 真正的原因（镜像 403、索引里没有这个包）会被后一次的失败盖掉，
    // 而用户按提示去换索引时完全不知道第一次错在哪。
    const firstAttempt = (installed.stderr || installed.stdout || "").trim() || "(无输出)";
    process.stderr.write("computer-use: 默认索引失败，回退 https://pypi.org/simple 重试一次\n");
    installed = run(
      "uv",
      [...installArgs, "--index-url", "https://pypi.org/simple"],
      { capture: true },
    );
    if (installed.status !== 0) {
      const secondAttempt = (installed.stderr || installed.stdout || "").trim() || "(无输出)";
      process.stderr.write(
        `computer-use: 依赖安装失败。\n` +
          `  第一次（默认索引）:\n${firstAttempt}\n` +
          `  第二次（https://pypi.org/simple）:\n${secondAttempt}\n` +
          `  提示: 若报 403/404，通常是镜像源不含该包；` +
          `可用 ` + "`COMPUTER_USE_INDEX_URL`" + ` 指定索引后重试。\n`,
      );
      process.exit(1);
    }
  }

  writeFileSync(ENV_STAMP, want, "utf8");
}

/**
 * 安装/同步 base 环境。
 *
 * 镜像回退见 DEC-042：本机 uv 的全局索引指向国内镜像，而镜像对个别包返回 403，
 * 报错信息却像「包不存在」。这里只在**明确失败**时回退官方源一次，
 * 且把索引源与状态码透出来 —— 静默重试会让用户永远搞不清是网络还是包的问题。
 *
 * **并发首次运行**（两个 agent 会话同时第一次调 CLI）必须两个都成功：`ENV_LOCK`
 * 把「建环境 + 装依赖」串行化，后到的进程等赢家做完直接复用，而不是和它抢同一个
 * `uv venv` 目标（实测那是 os error 32 的来源）。
 */
function syncEnvironment({ force = false } = {}) {
  const want = fingerprint();
  if (!force && stampMatches(want) && existsSync(VENV_PYTHON)) return;

  if (!haveUv()) {
    process.stderr.write(UV_MISSING_HELP + "\n");
    process.exit(1);
  }

  mkdirSync(DATA_DIR, { recursive: true });

  // `contended` = 「确实见过对端在装」。只有见过对端时才用「环境已就绪」提前
  // 返回 —— 否则 `env sync`（force）会因为戳文件本来就匹配而变成空操作。
  let contended = false;
  const deadline = Date.now() + LOCK_WAIT_MS;
  for (;;) {
    if (contended && stampMatches(want)) return;
    if (tryTakeLock()) break;
    contended = true;
    const age = lockAgeMs();
    if (age !== null && age > LOCK_STALE_MS) {
      // 持有者多半已经崩了（正常安装走不到这么久）；夺锁继续，而不是永久阻塞。
      rmSync(ENV_LOCK, { force: true });
      continue;
    }
    if (Date.now() >= deadline) {
      process.stderr.write(
        `computer-use: 等待另一个进程完成环境安装超时（${ENV_LOCK}）；` +
          "确认没有安装进程在跑之后删除该文件再重试\n",
      );
      process.exit(1);
    }
    sleep(LOCK_POLL_MS);
  }

  try {
    // 拿锁后复查：等锁的这段时间里赢家可能已经装完并写了戳文件。
    if (contended && stampMatches(want)) return;
    buildEnvironment(want);
  } finally {
    rmSync(ENV_LOCK, { force: true });
  }
}

// ---------------------------------------------------------------------------
// SKILL 安装 / 卸载
// ---------------------------------------------------------------------------

const SKILL_NAME = "computer-use";

//: 技能目录按调用方 Agent 家族探测：只装到本机确实存在的家族根下，
//: 不给没装的 Agent 凭空造目录。`~/.agents` 是跨 Agent 的共享技能根。
const SKILL_FAMILY_ROOTS = [
  [".claude", "skills"],
  [".codex", "skills"],
  [".agents", "skills"],
];

function skillDirs() {
  // 多个调用方 Agent 家族的技能目录。环境变量可覆盖，便于测试与非常规布局。
  if (process.env.COMPUTER_USE_SKILL_DIR) return [process.env.COMPUTER_USE_SKILL_DIR];
  const home = homedir();
  const dirs = SKILL_FAMILY_ROOTS.filter(([root]) => existsSync(join(home, root))).map(
    ([root, sub]) => join(home, root, sub, SKILL_NAME),
  );
  // 一个家族都没探测到时，退回 Claude Code 的历史默认落点（与 0.1.1 行为一致）。
  return dirs.length > 0 ? dirs : [join(home, ".claude", "skills", SKILL_NAME)];
}

function installSkill() {
  const source = join(PACKAGE_ROOT, "skill", SKILL_NAME);
  if (!existsSync(join(source, "SKILL.md"))) {
    process.stderr.write(`computer-use: 包内缺少 skill（${source}）\n`);
    return 1;
  }
  for (const dir of skillDirs()) {
    mkdirSync(dir, { recursive: true });
    // 整目录拷（SKILL.md 与 references/）。带 `-zh` 后缀的是给人校对的译本，不进技能目录。
    cpSync(source, dir, { recursive: true, filter: (src) => !src.endsWith("-zh.md") });
    process.stdout.write(`已安装 SKILL: ${dir}\n`);
  }
  process.stdout.write(
    "\n注意：技能目录写入会让调用方 Agent 在每次会话看到这份说明。\n" +
      "如需撤销：computer-use skill uninstall\n",
  );
  return 0;
}

function uninstallSkill() {
  for (const dir of skillDirs()) {
    if (existsSync(dir)) {
      rmSync(dir, { recursive: true, force: true });
      process.stdout.write(`已移除 SKILL: ${dir}\n`);
    }
  }
  return 0;
}

// ---------------------------------------------------------------------------

function printHelp() {
  process.stdout.write(
    [
      "computer-use —— 面向 AI Agent 的 Windows 桌面控制",
      "",
      "用法: computer-use <命令> [参数...]",
      "",
      "环境管理:",
      "  env sync              重新同步 Python 环境（升级后自动触发）",
      "  skill install         把 skill 装到本机各 Agent 家族的技能目录（Claude Code / Codex / 共享根）",
      "  skill uninstall       移除已安装的 SKILL",
      "",
      "其余命令（begin / windows / screenshot / click / type / key / parse ...）",
      "由 Python 客户端实现，运行 `computer-use --help` 查看完整命令面。",
      "",
    ].join("\n"),
  );
}

function main() {
  const argv = process.argv.slice(2);
  const [first, second] = argv;

  if (first === "env" && second === "sync") {
    syncEnvironment({ force: true });
    process.stdout.write(`环境已就绪: ${VENV_PYTHON}\n`);
    return 0;
  }
  if (first === "skill") {
    if (second === "install") return installSkill();
    if (second === "uninstall") return uninstallSkill();
    process.stderr.write("用法: computer-use skill <install|uninstall>\n");
    return 2;
  }
  if (first === "--launcher-help" || first === "launcher help") {
    printHelp();
    return 0;
  }

  syncEnvironment();

  // 转发：argv / stdio / 退出码全部原样交给 Python 客户端。
  // 用 spawnSync + 手动传播退出码而不是 exec 替换：Windows 上 exec 语义
  // 由 `spawn` 模拟不来，而同步等待已经满足「Node 不参与运行时逻辑」。
  const child = spawnSync(
    VENV_PYTHON,
    ["-m", "cu", ...argv],
    // `windowsHide: true` 是本轮发布阻塞项的直接修复：少了它，**每次** CLI 调用
    // 都会弹一个 python 控制台黑窗（用户报告的就是这个）。stdio 走 inherit 不影响 ——
    // 隐藏窗口靠的是创建标志 CREATE_NO_WINDOW，不是流。
    { stdio: "inherit", shell: false, windowsHide: true },
  );
  if (child.error) {
    process.stderr.write(`computer-use: 启动 Python 客户端失败: ${child.error.message}\n`);
    return 1;
  }
  return child.status ?? 1;
}

process.exit(main());
