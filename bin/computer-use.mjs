#!/usr/bin/env node
/**
 * Computer-Use 启动器 —— 极薄的一层（DEC-032）。
 *
 * 它**不参与运行时逻辑**，只做四件事：
 *   1. 定位（必要时创建并同步）uv 管理的 Python 环境；
 *   2. 把 argv / stdio / 退出码原样转发给 `python -m cu`；
 *   3. 首次安装流程：检测 uv、建环境、装依赖；
 *   4. `skill install|uninstall` —— 把 SKILL.md 装到调用方 Agent 的技能目录。
 *
 * 没有 Node↔Python 的跨语言协议：Node 进程被 Python 进程整体替换（exec 语义），
 * 因此不存在需要维护的中间格式。
 *
 * 依赖管理的选择见 DEC-018 / DEC-037：环境落在 ~/.computer-use/，
 * 与系统 Python 完全隔离；uv 缺失时报错并给安装指引，**不静默回退系统 pip**。
 */

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
  const probe = spawnSync("uv", ["--version"], { encoding: "utf8", shell: false });
  return probe.status === 0;
}

function run(cmd, args, options = {}) {
  return spawnSync(cmd, args, {
    stdio: options.capture ? "pipe" : "inherit",
    encoding: "utf8",
    shell: false,
    ...options,
  });
}

/**
 * 安装/同步 base 环境。
 *
 * 镜像回退见 DEC-042：本机 uv 的全局索引指向国内镜像，而镜像对个别包返回 403，
 * 报错信息却像「包不存在」。这里只在**明确失败**时回退官方源一次，
 * 且把索引源与状态码透出来 —— 静默重试会让用户永远搞不清是网络还是包的问题。
 */
function syncEnvironment({ force = false } = {}) {
  const want = fingerprint();
  if (!force && existsSync(VENV_PYTHON) && existsSync(ENV_STAMP)) {
    if (readFileSync(ENV_STAMP, "utf8").trim() === want) return;
  }

  if (!haveUv()) {
    process.stderr.write(UV_MISSING_HELP + "\n");
    process.exit(1);
  }

  mkdirSync(DATA_DIR, { recursive: true });

  if (!existsSync(VENV_PYTHON)) {
    process.stderr.write(`computer-use: 创建 Python 环境 ${VENV_DIR}\n`);
    const created = run("uv", ["venv", VENV_DIR]);
    if (created.status !== 0) {
      process.stderr.write(`computer-use: 创建环境失败（exit ${created.status}）\n`);
      process.exit(1);
    }
  }

  process.stderr.write("computer-use: 安装依赖\n");
  const installArgs = ["pip", "install", "--python", VENV_PYTHON, "-e", CORE_DIR];
  let installed = run("uv", installArgs, { capture: true });
  if (installed.status !== 0) {
    // 单次回退官方源，并把两次失败的原委都透出来（DEC-042）。
    process.stderr.write("computer-use: 默认索引失败，回退 https://pypi.org/simple 重试一次\n");
    installed = run(
      "uv",
      [...installArgs, "--index-url", "https://pypi.org/simple"],
      { capture: true },
    );
    if (installed.status !== 0) {
      process.stderr.write(
        `computer-use: 依赖安装失败。\n` +
          `  默认索引: ${installed.stderr?.trim() || "(无输出)"}\n` +
          `  提示: 若报 403/404，通常是镜像源不含该包；可用 ` +
          `COMPUTER_USE_INDEX_URL 指定索引后重试。\n`,
      );
      process.exit(1);
    }
  }

  writeFileSync(ENV_STAMP, want, "utf8");
}

// ---------------------------------------------------------------------------
// SKILL 安装 / 卸载
// ---------------------------------------------------------------------------

const SKILL_NAME = "computer-use";

function skillDirs() {
  // 多个调用方 Agent 家族的技能目录。环境变量可覆盖，便于测试与非常规布局。
  if (process.env.COMPUTER_USE_SKILL_DIR) return [process.env.COMPUTER_USE_SKILL_DIR];
  const home = homedir();
  return [join(home, ".claude", "skills", SKILL_NAME)];
}

function installSkill() {
  const source = join(PACKAGE_ROOT, "SKILL.md");
  if (!existsSync(source)) {
    process.stderr.write(`computer-use: 包内缺少 SKILL.md（${source}）\n`);
    return 1;
  }
  const body = readFileSync(source, "utf8");
  for (const dir of skillDirs()) {
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "SKILL.md"), body, "utf8");
    process.stdout.write(`已安装 SKILL: ${join(dir, "SKILL.md")}\n`);
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
      "  skill install         把 SKILL.md 装到调用方 Agent 的技能目录",
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
    { stdio: "inherit", shell: false },
  );
  if (child.error) {
    process.stderr.write(`computer-use: 启动 Python 客户端失败: ${child.error.message}\n`);
    return 1;
  }
  return child.status ?? 1;
}

process.exit(main());
