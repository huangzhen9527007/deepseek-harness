#!/usr/bin/env python3
"""start_menu.py — DeepSeek Harness 启动菜单与插件管理菜单。

由 start.cmd 调用：双击 start.cmd（不带参数）即进入本菜单。

设计约束：
  * 只用 Python 标准库，不装任何第三方包。
  * 中文不依赖系统代码页：源码是 UTF-8，Windows 控制台走 PEP 528
    （sys.stdout 直接调 WriteConsoleW），重定向时显式按 UTF-8 写。
  * 菜单只做两件事：把命令交给仓库自带的 dsh 启动器（apps/cli/src/bin.ts），
    以及读写 $DSH_HOME/profiles/<name> 下的 profile 清单；不修改任何插件源码。
  * 启动动作统一交给 start.cmd，预检（Node 版本 / 依赖 / 构建产物 / 端口）只有一份实现。

用法：
    python start_menu.py [--repo <仓库根目录>]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_PROFILE = "web"


# --------------------------------------------------------------------------- #
# 控制台编码与颜色
# --------------------------------------------------------------------------- #
def _reconfigure(stream) -> None:
    """把标准流固定成 UTF-8，避免重定向到文件时跟随系统代码页。"""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def _enable_virtual_terminal() -> bool:
    """在 Windows 控制台打开 ANSI 转义序列；失败就退回纯文本。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ok = True
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ok = False
                continue
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                ok = False
        return ok
    except Exception:
        return False


_reconfigure(sys.stdout)
_reconfigure(sys.stderr)
_reconfigure(sys.stdin)

USE_COLOR = sys.stdout.isatty() and _enable_virtual_terminal()

_STYLES = {
    "title": "\033[36m",
    "rule": "\033[36m",
    "info": "\033[90m",
    "step": "\033[97m",
    "cmd": "\033[33m",
    "example": "\033[33m",
    "note": "\033[90m",
    "ok": "\033[32m",
    "warn": "\033[33m",
    "err": "\033[31m",
    "plain": "\033[97m",
}


def _paint(text: str, kind: str) -> str:
    style = _STYLES.get(kind, "")
    if not USE_COLOR or not style:
        return text
    return f"{style}{text}\033[0m"


# --------------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------------- #
def title(text: str) -> None:
    print()
    print(_paint("-" * 70, "rule"))
    print(_paint(f"  {text}", "title"))
    print(_paint("-" * 70, "rule"))


def info(text: str) -> None:
    print(_paint(f"  {text}", "info"))


def step(number, text: str) -> None:
    print(_paint(f"  {number}. {text}", "step"))


def cmd(text: str) -> None:
    print(_paint(f"  > {text}", "cmd"))


def example(text: str) -> None:
    print(_paint(f"  例：{text}", "example"))


def note(text: str) -> None:
    print(_paint(f"  说明：{text}", "note"))


def ok(text: str) -> None:
    print(_paint(f"  [OK] {text}", "ok"))


def warn(text: str) -> None:
    print(_paint(f"  [!] {text}", "warn"))


def err(text: str) -> None:
    print(_paint(f"  [X] {text}", "err"))


def field(label: str, value: str) -> None:
    width = 0
    for character in label:
        width += 2 if _is_wide(character) else 1
    padding = " " * max(1, 16 - width)
    print(_paint(f"  {label}{padding}{value}", "info"))


def _is_wide(character: str) -> bool:
    code = ord(character)
    return 0x2E80 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFF60


def result_code(code: int) -> None:
    if code == 0:
        ok("命令执行成功（退出码 0）")
    else:
        err(f"命令退出码 {code}（请看上面的输出定位原因）")


def block(text: str) -> None:
    """打印一段整块文本（速查表 / 教程），保持原文缩进。"""
    print(_paint(text, "info"))


def quote_arg(value: str) -> str:
    return f'"{value}"' if any(character.isspace() for character in value) else value


# --------------------------------------------------------------------------- #
# 输入辅助
# --------------------------------------------------------------------------- #
def _readline(prompt: str):
    """读一行；EOF（输入被重定向且已读完）返回 None。"""
    try:
        return input(prompt)
    except EOFError:
        return None
    except KeyboardInterrupt:
        print()
        return None


def read_choice(prompt: str):
    return _readline("\n" + _paint(f"  {prompt}： ", "title"))


def read_value(prompt: str, default: str = ""):
    suffix = f" [{default}]" if default else ""
    value = _readline("\n" + _paint(f"  {prompt}{suffix}： ", "title"))
    if value is None:
        return None
    value = value.strip()
    if value == "" and default:
        return default
    return value


def confirm(prompt: str, default_yes: bool = True) -> bool:
    hint = "Y/n" if default_yes else "y/N"
    answer = read_value(f"{prompt} ({hint})", "")
    if answer is None:
        return False
    if answer == "":
        return default_yes
    return answer.strip().lower() in ("y", "yes", "是")


def pause() -> None:
    _readline("\n" + _paint("  按回车键继续…", "info"))


def split_args(raw: str) -> list:
    return [token for token in raw.split() if token]


# --------------------------------------------------------------------------- #
# 路径与 profile 读取
# --------------------------------------------------------------------------- #
def find_repo_root(start: Path) -> Path:
    """从本文件所在目录向上找到含 apps/cli/src/bin.ts 的仓库根目录。"""
    for candidate in (start, *start.parents):
        if (candidate / "apps" / "cli" / "src" / "bin.ts").is_file():
            return candidate
    return start


# 本文件与 start.cmd 同目录（launcher/）；仓库根目录只用于解析仓库内的相对路径。
LAUNCHER_DIR = Path(__file__).resolve().parent
START_CMD = LAUNCHER_DIR / "start.cmd"
REPO_ROOT = find_repo_root(LAUNCHER_DIR)


def dsh_home() -> Path:
    configured = os.environ.get("DSH_HOME")
    return Path(configured) if configured else Path.home() / ".dsh"


def profiles_dir() -> Path:
    return dsh_home() / "profiles"


def profile_dir(profile: str) -> Path:
    return profiles_dir() / profile


def list_profiles() -> list:
    directory = profiles_dir()
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_dir() and entry.name != "node_modules"
    )


def load_manifest(profile: str):
    path = profile_dir(profile) / "package.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        warn(f"profile 清单读取失败：{path}（{error}）")
        return None


def profile_dependencies(profile: str) -> list:
    manifest = load_manifest(profile) or {}
    dependencies = manifest.get("dependencies") or {}
    if not isinstance(dependencies, dict):
        return []
    return [(name, str(spec)) for name, spec in dependencies.items()]


def profile_bundles(profile: str) -> list:
    manifest = load_manifest(profile) or {}
    profile_section = (manifest.get("dsh") or {}).get("profile") or {}
    bundles = profile_section.get("bundles") or []
    return [str(item) for item in bundles]


def profile_patch_reload(profile: str) -> str:
    manifest = load_manifest(profile) or {}
    profile_section = (manifest.get("dsh") or {}).get("profile") or {}
    return str(profile_section.get("patchReload") or "")


def ask_profile(purpose: str = ""):
    profiles = list_profiles()
    if profiles:
        print()
        print(_paint("  现有 profile：", "info"), end="")
        print(_paint("   ".join(profiles), "plain"))
    else:
        warn("目前没有任何 profile；首次对一个新名字执行插件命令时会自动初始化。")
    name = read_value(f"要操作的 profile 名称{purpose}", DEFAULT_PROFILE)
    if name is None:
        return None
    name = name.strip()
    if name == "":
        return None
    if name == "node_modules" or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in name
    ):
        err(f"profile 名称只能用字母、数字、点、下划线、连字符：{name}")
        return None
    return name


# --------------------------------------------------------------------------- #
# 命令执行
# --------------------------------------------------------------------------- #
def have_tool(name: str) -> bool:
    return shutil.which(name) is not None


def _argv_for_console(argv: list) -> list:
    """Windows 上 pnpm/编辑器都是 .cmd 垫片，必须经 cmd.exe 才能启动。"""
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
    return list(argv)


def capture_merged(argv: list):
    """执行一条命令并取回 (退出码, stdout+stderr 文本)。"""
    try:
        completed = subprocess.run(
            _argv_for_console(argv),
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except (OSError, ValueError) as error:
        return (127, str(error))
    return (completed.returncode, completed.stdout.decode("utf-8", errors="replace"))


def capture(argv: list) -> str:
    """执行一条命令并取回 stdout（失败返回空串）。"""
    code, text = capture_merged(argv)
    if code != 0 and text == "":
        return ""
    return text.strip()


def spawn(argv: list) -> int:
    """执行一条命令，stdout/stderr 直接接到当前控制台，返回退出码。"""
    try:
        return subprocess.call(_argv_for_console(argv), cwd=str(REPO_ROOT))
    except FileNotFoundError:
        return 127
    except KeyboardInterrupt:
        return 130
    except OSError:
        return 127


def launch(argv: list) -> None:
    """非阻塞地拉起编辑器等程序，菜单继续可用。"""
    try:
        subprocess.Popen(_argv_for_console(argv), cwd=str(REPO_ROOT))
    except OSError as error:
        err(f"启动失败：{error}")


def run_pnpm_dsh(arguments: list) -> int:
    """在仓库根目录执行 pnpm dsh <参数>；相对路径因此以仓库根为基准。"""
    if not have_tool("pnpm"):
        err("未找到 pnpm。安装方式：corepack enable   或   npm install -g pnpm")
        return 127
    cmd("pnpm dsh " + " ".join(quote_arg(item) for item in arguments))
    return spawn(["pnpm", "dsh", *arguments])


def run_start_cmd(arguments: list) -> int:
    """调用同目录的 start.cmd，复用它的预检与启动逻辑。"""
    executable = START_CMD
    if not executable.is_file():
        err(f"找不到 start.cmd：{executable}")
        note("start.cmd 必须与 start_menu.py 放在同一个目录（launcher/）。")
        return 1
    cmd("start.cmd " + " ".join(quote_arg(item) for item in arguments))
    return spawn([str(executable), *arguments])


def plugin_package_info(directory: Path):
    """读取插件包目录：是否存在、包名、dsh.bundle 声明、是否已构建。"""
    info_result = {"exists": False, "has_json": False, "name": "", "is_bundle": False, "patch": "", "built": False}
    if not directory.is_dir():
        return info_result
    info_result["exists"] = True
    manifest_path = directory / "package.json"
    if not manifest_path.is_file():
        return info_result
    info_result["has_json"] = True
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return info_result
    info_result["name"] = str(manifest.get("name") or "")
    bundle = (manifest.get("dsh") or {}).get("bundle") or {}
    if bundle.get("patch"):
        info_result["is_bundle"] = True
        info_result["patch"] = str(bundle.get("patch"))
    info_result["built"] = any((directory / name).is_dir() for name in ("lib", "dist", "build"))
    return info_result


def show_add_preflight(directory: Path) -> bool:
    details = plugin_package_info(directory)
    if not details["exists"]:
        warn(f"目录不存在：{directory}")
        return False
    if not details["has_json"]:
        err(f"该目录没有 package.json，不是插件包：{directory}")
        return False
    ok(f"插件包：{details['name']}")
    if details["is_bundle"]:
        ok(f"声明了 dsh.bundle.patch={details['patch']} → 会被追加进 profile 的 bundle 层列表")
    else:
        warn("该包没有声明 dsh.bundle.patch → 只会作为普通依赖安装，不会成为一层")
        note('需要它成为一层时，在 package.json 里加："dsh": { "bundle": { "patch": "./cordis.patch.yml" } }')
    if not details["built"]:
        warn("没看到 lib/dist/build 目录：如果这是源码包，先构建它（仓库内的包用 pnpm run build）")
    return True


def invoke_plugin_add(profile: str, specs: list) -> int:
    if not specs:
        err("没有要添加的插件规格")
        return 1
    before = {name for name, _ in profile_dependencies(profile)}
    code = run_pnpm_dsh(["plugin", "--profile", profile, "add", *specs])
    result_code(code)
    if code != 0:
        note("常见原因：包名拼错、网络不可达、git 依赖需要先在 profile 的 pnpm-workspace.yaml 里 allowBuilds")
        return code
    bundles = profile_bundles(profile)
    added = [(name, spec) for name, spec in profile_dependencies(profile) if name not in before]
    if not added:
        note("依赖列表没有新增条目（可能之前就已安装，本次只是重新链接）。")
    for name, _spec in added:
        if name in bundles:
            ok(f"已加入 bundle 层：{name}")
        else:
            warn(f"未加入 bundle 层：{name}（该包没有声明 dsh.bundle.patch）")
    note("bundle 成员在 profile 启动时确定 → 必须重启该 profile 才会生效。")
    if confirm("现在校验组合配置（--dump-config），确认新层已出现", True):
        run_pnpm_dsh(["--profile", profile, "--dump-config"])
    return code


# --------------------------------------------------------------------------- #
# 主菜单：启动类操作
# --------------------------------------------------------------------------- #
def action_start_web() -> None:
    title("启动 Web UI（默认端口 3080）")
    info("用途：本机打开 DeepSeek Harness 的图形界面。")
    step(1, "检查 Node 版本、pnpm、node_modules（缺失才安装）")
    step(2, "检查构建产物 apps\\web\\dist；缺失才执行 pnpm run build")
    step(3, "检查 3080 是否已被占用，避免第二个实例端口冲突")
    step(4, "执行 dsh web；本机启动会自动打开默认浏览器，SSH 下只打印地址")
    example("start.cmd web")
    note("不想自动开浏览器、或要换端口，请用选项 2。")
    if not confirm("现在启动 Web UI"):
        return
    result_code(run_start_cmd(["web"]))
    pause()


def action_start_web_custom() -> None:
    title("启动 Web UI（自定义端口或附加参数）")
    info("用途：3080 被占用、要同时开多个实例，或需要传 Web 应用自己的参数。")
    step(1, "填写端口：留空用默认 3080；填 0 让系统随机分配")
    step(2, "填写附加参数：可留空，多个参数用空格分隔")
    step(3, "执行 start.cmd web --port <端口> <附加参数>")
    example("端口 8080，附加参数 --no-open")
    note("Web 应用支持的参数：--host、--port、--trusted-host、--no-open（--host 不支持 0.0.0.0）")
    port = read_value("端口（留空=3080）", "")
    if port is None:
        return
    if port != "" and not port.isdigit():
        err(f"端口必须是数字：{port}")
        pause()
        return
    extra = read_value("附加参数（留空=不加）", "")
    if extra is None:
        return
    arguments = ["web"]
    if port != "":
        arguments += ["--port", port]
    if extra != "":
        arguments += split_args(extra)
    if not confirm("现在启动"):
        return
    result_code(run_start_cmd(arguments))
    pause()


def action_start_dev() -> None:
    title("开发模式：Web UI + 客户端插件热更新")
    info("用途：正在改 packages/client/** 的浏览器端插件，需要改完保存即生效。")
    step(1, "先启动 pnpm run dev:web（客户端插件 bundle 监听重建，单独开一个窗口）")
    step(2, "再启动 dsh web（页面里的 HMR 接收器一直挂着，等 bundle 重建完成后热替换）")
    example("start.cmd --dev web")
    note("只改服务端（packages/host、packages/api 等）不需要 dev:web；那类改动要重启进程。")
    if not confirm("现在进入开发模式"):
        return
    result_code(run_start_cmd(["--dev", "web"]))
    pause()


def action_start_rebuild() -> None:
    title("强制重新构建后启动 Web UI")
    info("用途：刚改了 packages/** 或 apps/web/** 的源码，产物过期（浏览器跑的还是旧代码）。")
    step(1, "执行 pnpm run build（几分钟；会生成 apps\\web\\dist 与各包 lib）")
    step(2, "构建成功后启动 dsh web")
    example("start.cmd --rebuild web")
    warn("构建失败时不会启动，窗口会停在错误信息上。")
    if not confirm("现在构建并启动"):
        return
    result_code(run_start_cmd(["--rebuild", "web"]))
    pause()


def action_headless_task() -> None:
    title("运行 headless 一次性任务")
    info("用途：不开界面，把一个任务交给 agent 执行，打印最终结果后退出。")
    step(1, "确认已配置模型凭据（DEEPSEEK_API_KEY，或 $DSH_HOME\\.credentials.yaml / .env）")
    step(2, "填写任务文本（整行）")
    step(3, '执行 start.cmd headless "<任务>"')
    example("整理本仓库 README 的命令清单")
    note("退出码 0 表示 completed，非 0 表示失败；结果走 stdout，思考过程走 stderr。")
    task = read_value("任务文本", "")
    if task is None or task == "":
        warn("任务为空，已取消")
        pause()
        return
    if not confirm("现在执行"):
        return
    result_code(run_start_cmd(["headless", task]))
    pause()


def action_dump_config() -> None:
    title("查看组合配置（--dump-config，不启动服务）")
    info("用途：确认某个 profile 实际叠加了哪些层、每行的 config 最终是什么。")
    step(1, "填写 profile 名称")
    step(2, "执行 pnpm dsh --profile <profile> --dump-config")
    step(3, "输出会同时存到临时文件，可选择用记事本打开")
    example("profile 名 web")
    note("bundle 层按列表顺序叠加，后面的覆盖前面的；profile 自己的 cordis.patch.yml 在最后（home 层与 --patch 之外）。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    if not have_tool("pnpm"):
        err("未找到 pnpm。安装方式：corepack enable   或   npm install -g pnpm")
        pause()
        return
    target = Path(os.environ.get("TEMP", "/tmp")) / f"dsh-dump-{profile}.yml"
    cmd(f"pnpm dsh --profile {profile} --dump-config")
    code, output = capture_merged(["pnpm", "dsh", "--profile", profile, "--dump-config"])
    print(output, end="" if output.endswith("\n") else "\n")
    try:
        target.write_text(output, encoding="utf-8")
    except OSError:
        target = None
    result_code(code)
    if target is not None:
        field("已保存到", str(target))
        if confirm("用记事本打开该文件", False):
            launch(["notepad", str(target)])
    pause()


def action_dsh_help() -> None:
    title("dsh 帮助")
    info("启动器自身的帮助（launcher 参数、subcommand 一览）")
    run_pnpm_dsh(["--help"])
    pause()


# --------------------------------------------------------------------------- #
# 环境自检
# --------------------------------------------------------------------------- #
def action_self_check() -> None:
    title("环境自检")
    node_version = capture(["node", "-v"]) if have_tool("node") else ""
    if node_version == "":
        err("Node.js：未找到（需要 22.19+ 或 24+）")
    else:
        field("Node.js", node_version)
    pnpm_version = capture(["pnpm", "-v"]) if have_tool("pnpm") else ""
    if pnpm_version == "":
        err("pnpm：未找到（安装/构建/插件管理都需要）")
    else:
        field("pnpm", pnpm_version)
    field("Python", sys.version.split()[0])
    field("仓库根", str(REPO_ROOT))
    field("DSH_HOME", str(dsh_home()))
    branch = capture(["git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"]) if have_tool("git") else ""
    if branch != "":
        field("git 分支", branch)

    print()
    entry = REPO_ROOT / "apps" / "cli" / "src" / "bin.ts"
    if entry.is_file():
        ok("dsh 入口存在（apps\\cli\\src\\bin.ts）")
    else:
        err(f"缺少 dsh 入口：{entry}")
    dist = REPO_ROOT / "apps" / "web" / "dist" / "index.html"
    if dist.is_file():
        ok("Web 前端产物存在（apps\\web\\dist\\index.html）")
    else:
        warn("缺少 Web 前端产物：启动时会自动执行 pnpm run build")

    has_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    if not has_key:
        credentials = dsh_home() / ".credentials.yaml"
        dotenv = REPO_ROOT / ".env"
        if credentials.is_file():
            has_key = True
        if dotenv.is_file():
            try:
                if "DEEPSEEK_API_KEY" in dotenv.read_text(encoding="utf-8", errors="replace"):
                    has_key = True
            except OSError:
                pass
    if has_key:
        ok("模型凭据：已检测到 DEEPSEEK_API_KEY（环境变量或 .credentials.yaml/.env）")
    else:
        warn("模型凭据：没检测到 DEEPSEEK_API_KEY，跑模型前需要先配置")

    print()
    print(_paint("  profile 一览", "cmd"))
    profiles = list_profiles()
    if not profiles:
        info("（无）")
    shipped = {"web", "headless", "sdk", "sdk-minimal", "acp"}
    for name in profiles:
        bundles = profile_bundles(name)
        dependencies = profile_dependencies(name)
        reload_mode = profile_patch_reload(name)
        kind = "内置模板" if name in shipped else "自定义"
        print(f"   {name:<14} 层 {len(bundles):>2}  依赖 {len(dependencies):>2}  patchReload={reload_mode:<8} {kind}")

    print()
    print(_paint("  端口占用", "cmd"))
    listeners = port_listeners(3080)
    if listeners:
        warn("3080 已被占用（很可能就是正在运行的 Web UI）")
        for line in listeners:
            info(line)
    else:
        ok("3080 空闲")
    pause()


def port_listeners(port: int) -> list:
    output = capture(["netstat", "-ano", "-p", "TCP"])
    if output == "":
        return []
    marker = f":{port} "
    return [
        line.strip()
        for line in output.splitlines()
        if marker in line + " " and "LISTENING" in line.upper()
    ]


# --------------------------------------------------------------------------- #
# 用法速查
# --------------------------------------------------------------------------- #
USAGE_REFERENCE = """  ── 一、启动 ────────────────────────────────────────────────
   双击 start.cmd                 打开本菜单
   start.cmd web                  启动 Web UI（默认 http://127.0.0.1:3080）
   start.cmd web --port 8080      换端口启动
   start.cmd web --no-open        启动但不自动打开浏览器
   start.cmd --dev web            开发模式：同时跑 pnpm run dev:web（客户端插件热更新）
   start.cmd --rebuild web        先 pnpm run build 再启动
   start.cmd headless "任务"       一次性执行任务，打印结果后退出
   start.cmd --launcher-help      查看启动器自己的帮助
   pnpm dsh web                   等价写法（源码运行，需要先 pnpm run build）
   pnpm dsh --profile web --help  Web 应用自己的参数与帮助

  ── 二、查看与诊断 ──────────────────────────────────────────
   pnpm dsh --profile <p> --dump-config         打印组合后的完整配置树（层来源逐行标注）
   pnpm dsh --profile <p> --dump-default-config 只打印 bundle 层，不含用户 patch
   pnpm dsh --profile web --patch ./extra.yml   单次叠加一个 patch 覆盖（不改 profile 文件）

  ── 三、插件管理（把参数原样转发给 profile 目录里的 pnpm）──
   pnpm dsh plugin --profile <p> list                 列出已安装依赖
   pnpm dsh plugin --profile <p> why <包名>            解释某个依赖为何存在
   pnpm dsh plugin --profile <p> outdated             列出可升级的依赖
   pnpm dsh plugin --profile <p> add <规格>            添加（规格见下）
   pnpm dsh plugin --profile <p> remove <包名>         移除依赖及其 bundle 层
   pnpm dsh plugin --profile <p> update               更新全部依赖
   pnpm dsh plugin --profile <p> update <包名>         更新指定依赖

  ── 四、添加插件的各种规格 ──────────────────────────────────
   本仓库内的包（相对路径，以仓库根为基准）
     pnpm dsh plugin --profile web add "packages\\client\\ui-jobs"
   任意本地目录（绝对路径）
     pnpm dsh plugin --profile web add "F:\\project\\my-plugins\\hello-plugin"
   相对当前目录（. 与 ..\\x，以及 file:/link: 前缀同样被支持）
     pnpm dsh plugin --profile web add ..\\my-plugins\\hello-plugin
     pnpm dsh plugin --profile web add link:..\\my-plugins\\hello-plugin
   本地 tarball（先在被装项目里 pnpm pack）
     pnpm dsh plugin --profile web add "F:\\tmp\\hello-plugin-0.1.0.tgz"
   npm 仓库包（可带版本）
     pnpm dsh plugin --profile web add @scope/name
     pnpm dsh plugin --profile web add name@1.2.3
   Git 仓库（建议锁定提交）
     pnpm dsh plugin --profile web add github:you/hello-plugin#<sha>
   一次多个
     pnpm dsh plugin --profile web add a b c
   任意 pnpm 参数（专家模式）
     pnpm dsh plugin --profile web add --save-exact @scope/name

  ── 五、目录与文件 ──────────────────────────────────────────
   $DSH_HOME/profiles/<p>/package.json       依赖 + dsh.profile.bundles（层顺序）
   $DSH_HOME/profiles/<p>/cordis.patch.yml   该 profile 的用户 patch 层（最后叠加，可覆盖 bundle）
   $DSH_HOME/profiles/<p>/pnpm-workspace.yaml pnpm 设置；git 依赖的 allowBuilds 写这里
   $DSH_HOME/cordis.patch.yml                机器级 patch 层，对所有 profile 生效
   层顺序：bundles 列表 → profile patch → home patch → 各 --patch
   同 id 的行：后面的整份 config 覆盖前面的（不是深合并），所以覆盖时要重述所有需要的键。

  ── 六、生效时机 ────────────────────────────────────────────
   普通配置改动：patchReload=live 的 profile 保存即生效；startup 的 profile 需重启。
   插件增删（bundle 成员变化）：必须重启该 profile。
   本地 link 的插件：改源码后重启 profile 即可，不需要重新 add。"""


def action_usage_reference() -> None:
    title("用法速查")
    block(USAGE_REFERENCE)
    pause()


# --------------------------------------------------------------------------- #
# 插件菜单：查询
# --------------------------------------------------------------------------- #
def action_list_plugins() -> None:
    title("查看已安装插件与 bundle 层顺序")
    info("只读：直接读 profile 的 package.json，不会安装或修改任何东西。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    directory = profile_dir(profile)
    if not (directory / "package.json").is_file():
        warn(f"profile '{profile}' 还不存在；执行一次添加/启动后才会初始化。")
        note("初始化会用内置模板：web/headless/sdk/sdk-minimal/acp，其他名字只用 @deepseek-ai/dsh-base。")
        pause()
        return
    dependencies = profile_dependencies(profile)
    bundles = profile_bundles(profile)
    print()
    field("profile 目录", str(directory))
    field("patchReload", profile_patch_reload(profile))
    field("依赖数", str(len(dependencies)))
    field("bundle 层", str(len(bundles)))
    print()
    print(_paint("  —— 已安装依赖 ——", "cmd"))
    if not dependencies:
        info("（无：该 profile 只有内置 bundle，没有第三方依赖）")
    for name, spec in dependencies:
        marker = "[层]" if name in bundles else "[普通依赖]"
        style = "ok" if name in bundles else "info"
        print(_paint(f"   {name:<56} {marker}", style))
        print(_paint(f"        {spec}", "info"))
    print()
    print(_paint("  —— bundle 层顺序（自上而下叠加，下面的覆盖上面的）——", "cmd"))
    for index, bundle in enumerate(bundles, start=1):
        print(_paint(f"   {index:>2}. {bundle}", "plain"))
    print()
    note("要改这些内容请用「添加 / 移除 / 更新」；不要手工编辑 package.json 的 dependencies。")
    pause()


def action_query_plugin() -> None:
    title("查询单个插件")
    info("在 profile 目录里执行 pnpm 的只读查询命令。")
    step(1, "选择查询方式：1=why（为何存在）2=outdated（可升级）3=list（全部依赖树）")
    step(2, "why 需要填写包名")
    example("why dsh-worktable")
    note("这些命令不会改动 profile；outdated 需要联网查询 npm。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    mode = read_value("查询方式（1/2/3）", "1")
    if mode is None:
        pause()
        return
    if mode == "1":
        package = read_value("包名", "")
        if package is None or package == "":
            warn("包名为空，已取消")
            pause()
            return
        run_pnpm_dsh(["plugin", "--profile", profile, "why", package])
    elif mode == "2":
        run_pnpm_dsh(["plugin", "--profile", profile, "outdated"])
    else:
        run_pnpm_dsh(["plugin", "--profile", profile, "list"])
    pause()


# --------------------------------------------------------------------------- #
# 插件菜单：添加
# --------------------------------------------------------------------------- #
def action_add_local_repo() -> None:
    title("添加插件 · 本仓库 packages\\… 里的包")
    info("用途：本仓库里已经写好的插件包（packages/<组>/<包>），链接进某个 profile 让 dsh 加载。")
    note("相对路径以「调用目录」为基准；本菜单固定用仓库根目录，所以直接写 packages\\… 即可。")
    step(1, "确认该包已构建（存在 lib\\ 或 dist\\）；没有就先在仓库根运行 pnpm run build")
    step(2, "填写相对仓库根的包目录")
    step(3, "脚本检查 package.json，并提示它是否会成为 bundle 层")
    step(4, '执行 pnpm dsh plugin --profile <profile> add "<目录>"')
    step(5, "重启对应 profile 后生效")
    example("packages\\client\\ui-jobs")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    relative = read_value("插件包相对路径（相对仓库根）", "")
    if relative is None or relative == "":
        warn("路径为空，已取消")
        pause()
        return
    relative = relative.strip('"')
    if not show_add_preflight(REPO_ROOT / relative):
        pause()
        return
    note(f"将以相对路径传入（pnpm 解析为 link:）：{relative}")
    if not confirm(f"把 {relative} 添加到 profile '{profile}'"):
        pause()
        return
    invoke_plugin_add(profile, [relative])
    pause()


def action_add_absolute() -> None:
    title("添加插件 · 任意本地目录（绝对路径）")
    info("用途：插件放在本仓库之外（例如另一个盘、另一个仓库的 checkout），链接进来开发或使用。")
    note("pnpm 会把它写成 link: 依赖（软链）。改插件源码后重启 profile 即可生效，不需要重新 add。")
    step(1, "确认该插件包已构建（存在 lib\\/dist\\ 或它本身是纯 JS）")
    step(2, "填写插件包目录的绝对路径（可直接从资源管理器复制，含空格也能识别）")
    step(3, "脚本检查 package.json 与 dsh.bundle 声明")
    step(4, '执行 pnpm dsh plugin --profile <profile> add "<绝对路径>"')
    example("F:\\project\\deepseek-harness-resource\\another-plugins\\dsh-deepseek-pet")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    raw = read_value("插件包绝对路径", "")
    if raw is None or raw == "":
        warn("路径为空，已取消")
        pause()
        return
    target = Path(raw.strip('"'))
    if not target.is_absolute():
        warn(f"这不是绝对路径：{target}（相对路径请用选项 1 或选项 3）")
        pause()
        return
    if not show_add_preflight(target):
        pause()
        return
    if not confirm(f"把 {target} 添加到 profile '{profile}'"):
        pause()
        return
    invoke_plugin_add(profile, [str(target)])
    pause()


def action_add_relative() -> None:
    title("添加插件 · 相对当前目录（. 与 ..\\x）")
    info("用途：沿用文档里的写法（add . / add ..\\plugin），或明确使用 file: 与 link: 前缀。")
    note("本菜单固定以仓库根目录作为「调用目录」，即下面的路径都相对仓库根解析。")
    step(1, "填写一个相对规格，可以是这四种形式之一")
    step(2, "脚本去掉 file:/link: 前缀后检查目录是否存在")
    step(3, '执行 pnpm dsh plugin --profile <profile> add "<规格>"')
    example("..\\deepseek-harness-resource\\another-plugins\\dsh-talk-map\\dsh-talk-map")
    note("裸目录路径与 link: 都是软链；file: 指向目录时同样是软链，指向 .tgz 时是复制安装。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    spec = read_value("相对规格（如 . 或 ..\\x 或 link:..\\x）", "")
    if spec is None or spec == "":
        warn("输入为空，已取消")
        pause()
        return
    spec = spec.strip('"')
    probe = spec
    for prefix in ("file:", "link:"):
        if probe.lower().startswith(prefix):
            probe = probe[len(prefix):]
            break
    if not show_add_preflight(REPO_ROOT / probe):
        pause()
        return
    if not confirm(f"把 {spec} 添加到 profile '{profile}'"):
        pause()
        return
    invoke_plugin_add(profile, [spec])
    pause()


def action_add_tarball() -> None:
    title("添加插件 · 本地 tarball（.tgz）")
    info("用途：插件作者用 pnpm pack 打包出的安装包；适合分发已构建产物，不触发任何构建脚本。")
    step(1, "在插件项目里执行 pnpm pack，得到 <包名>-<版本>.tgz")
    step(2, "填写该 tgz 的绝对路径")
    step(3, '执行 pnpm dsh plugin --profile <profile> add "<tgz 路径>"')
    step(4, "重启对应 profile 后生效")
    example("F:\\tmp\\hello-plugin-0.1.0.tgz")
    note("tarball 是复制安装：插件改代码后要重新 pack 再 add；不需要 allowBuilds。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    raw = read_value("tgz 绝对路径", "")
    if raw is None or raw == "":
        warn("路径为空，已取消")
        pause()
        return
    target = Path(raw.strip('"'))
    if not target.is_file():
        err(f"文件不存在：{target}")
        pause()
        return
    if target.suffix.lower() not in (".tgz", ".gz"):
        warn(f"后缀不是 .tgz/.tar.gz：{target}（继续也可以，pnpm 会按内容判断）")
    if not confirm(f"把 {target} 添加到 profile '{profile}'"):
        pause()
        return
    invoke_plugin_add(profile, [str(target)])
    pause()


def action_add_npm() -> None:
    title("添加插件 · npm 仓库包")
    info("用途：安装别人已经发布到 npm 的插件包（已构建产物），也可以在 profile 里固定版本。")
    step(1, "填写包名：name 或 @scope/name；可带版本：name@1.2.3 或 name@latest")
    step(2, '执行 pnpm dsh plugin --profile <profile> add "<包名>"（需要联网）')
    step(3, "重启对应 profile 后生效；升级用「更新插件」或 version 后缀")
    example("@deepseek-ai/dsh-subagent-codex")
    note("包声明了 dsh.bundle.patch 才会成为一层；否则只是普通依赖并给出警告。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    package = read_value("包名（可带 @版本）", "")
    if package is None or package == "":
        warn("包名为空，已取消")
        pause()
        return
    package = package.strip()
    if not re.fullmatch(r"(@[^/\s]+/)?[^@/\s]+(@[^\s]+)?", package):
        warn(f"看起来不像包名：{package}（示例：@scope/name 或 name@1.2.3）")
        if not confirm("仍要继续", False):
            pause()
            return
    if not confirm(f"从 npm 安装 {package} 到 profile '{profile}'"):
        pause()
        return
    invoke_plugin_add(profile, [package])
    pause()


def action_add_git() -> None:
    title("添加插件 · Git 仓库")
    info("用途：直接从 GitHub/Git 安装插件源码；建议锁定提交，避免上游改动悄悄影响运行。")
    step(1, "填写规格：github:user/repo#<sha> 或 git+https://host/repo.git#<sha>")
    step(2, "执行 add；若该包有 prepare 构建脚本，pnpm ≥10 会先拦截")
    step(3, "被拦截时：把 pnpm 打印的那个 key 填进 profile 的 pnpm-workspace.yaml 的 allowBuilds，再重跑")
    step(4, "重启对应 profile 后生效")
    example("github:you/hello-plugin#1a2b3c4")
    warn("allowBuilds 等于允许该包安装时在你机器上执行代码（不受 agent 沙箱约束），只放行你信任的仓库。")
    note("菜单 7-8 可以直接打开 profile 目录，里面有 pnpm-workspace.yaml。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    spec = read_value("git 规格", "")
    if spec is None or spec == "":
        warn("输入为空，已取消")
        pause()
        return
    spec = spec.strip()
    if "#" not in spec:
        warn("没有 #<提交> 锁定版本：上游推送后重新安装会拿到不同代码")
    if not confirm(f"从 git 安装 {spec} 到 profile '{profile}'"):
        pause()
        return
    invoke_plugin_add(profile, [spec])
    pause()


def action_add_multiple() -> None:
    title("添加插件 · 一次添加多个")
    info("用途：批量装同一批插件，pnpm 依次安装，dsh 只做一次 bundle 层汇总。")
    step(1, "逐行输入规格（本地路径、tarball、npm 包名、git 规格都可混用）")
    step(2, "直接回车结束输入；路径含空格时请改用「任意本地目录（绝对路径）」")
    step(3, "脚本汇总后执行一次 add，然后再做层汇总")
    example("packages\\client\\ui-jobs  然后回车，再输 @scope/name")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    specs = []
    while True:
        item = read_value(f"第 {len(specs) + 1} 个规格（回车结束）", "")
        if item is None or item == "":
            break
        specs.append(item.strip().strip('"'))
    if not specs:
        warn("没有输入任何规格，已取消")
        pause()
        return
    print()
    info(f"将安装 {len(specs)} 个：")
    for spec in specs:
        print(_paint(f"     - {spec}", "plain"))
    if not confirm(f"全部添加到 profile '{profile}'"):
        pause()
        return
    invoke_plugin_add(profile, specs)
    pause()


def action_add_expert() -> None:
    title("添加插件 · 专家模式（原样转发 pnpm 参数）")
    info("用途：上面几种是常用写法的封装；这里把你在 profile 目录里想执行的 pnpm 参数原样转发。")
    step(1, "填写完整 pnpm 参数（不含 pnpm 本身，动词要在最前面）")
    step(2, "脚本按空格切分后执行 pnpm dsh plugin --profile <profile> <你的参数>")
    step(3, "dsh 在成功后仍会重新汇总 bundle 层列表")
    example("add --save-exact @scope/name")
    example("add -D ./my-lib")
    example("remove @scope/name")
    warn("含空格的路径在这里无法可靠切分；这种情况请用「任意本地目录（绝对路径）」。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    raw = read_value("pnpm 参数", "")
    if raw is None or raw == "":
        warn("输入为空，已取消")
        pause()
        return
    tokens = split_args(raw)
    if not tokens:
        warn("没有可执行的参数，已取消")
        pause()
        return
    note("下面这条命令会原样执行，pnpm 的报错会直接打印出来。")
    if not confirm(f"执行这些参数（profile '{profile}'）"):
        pause()
        return
    result_code(run_pnpm_dsh(["plugin", "--profile", profile, *tokens]))
    pause()


# --------------------------------------------------------------------------- #
# 插件菜单：移除 / 更新 / 配置 / 目录
# --------------------------------------------------------------------------- #
def action_remove_plugin() -> None:
    title("移除插件")
    info("用途：删掉一个已安装依赖；如果它是 bundle 层，同一层的条目也会一起从列表里去掉。")
    step(1, "选择要移除的依赖（输入编号），或直接输入包名")
    step(2, "执行 pnpm dsh plugin --profile <profile> remove <包名>")
    step(3, "重启该 profile 后生效")
    example("编号 1，或直接输入 dsh-worktable")
    note("内置 bundle（@deepseek-ai/dsh-base、@deepseek-ai/dsh-web-app 等）不是依赖，不在列表里，也删不掉。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    dependencies = profile_dependencies(profile)
    if not dependencies:
        warn(f"profile '{profile}' 没有第三方依赖可移除")
        pause()
        return
    print()
    for index, (name, _spec) in enumerate(dependencies, start=1):
        print(_paint(f"   {index:>2}) {name}", "plain"))
    answer = read_value("编号或包名", "")
    if answer is None or answer == "":
        warn("输入为空，已取消")
        pause()
        return
    answer = answer.strip()
    if answer.isdigit():
        number = int(answer)
        if number < 1 or number > len(dependencies):
            err(f"编号超出范围：{answer}")
            pause()
            return
        target = dependencies[number - 1][0]
    else:
        target = answer
    if not confirm(f"从 profile '{profile}' 移除 {target}"):
        pause()
        return
    code = run_pnpm_dsh(["plugin", "--profile", profile, "remove", target])
    result_code(code)
    if code == 0:
        still_dependency = [name for name, _ in profile_dependencies(profile) if name == target]
        still_bundle = [name for name in profile_bundles(profile) if name == target]
        if not still_dependency:
            ok(f"依赖已移除：{target}")
        else:
            warn(f"依赖仍存在：{target}")
        if not still_bundle:
            ok(f"bundle 层已移除：{target}")
        else:
            warn(f"bundle 层仍在列表里：{target}")
        note("重启该 profile 后生效。")
    pause()


def action_update_plugin() -> None:
    title("更新插件")
    info("用途：把 npm / git 依赖升到新版本（本地 link 的插件不需要更新，改了源码重启即可）。")
    step(1, "选择：1=更新全部  2=更新指定包")
    step(2, "执行 pnpm dsh plugin --profile <profile> update [包名]")
    step(3, "更新后 dsh 会重新汇总 bundle 层；重启 profile 生效")
    example("编号 2 → 包名 dsh-session-manager")
    note("git 依赖若在新版本里带了 prepare 脚本，可能再次需要 allowBuilds。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    mode = read_value("方式（1=全部，2=指定包）", "1")
    if mode is None:
        pause()
        return
    if mode == "2":
        package = read_value("包名", "")
        if package is None or package == "":
            warn("包名为空，已取消")
            pause()
            return
        code = run_pnpm_dsh(["plugin", "--profile", profile, "update", package.strip()])
    else:
        code = run_pnpm_dsh(["plugin", "--profile", profile, "update"])
    result_code(code)
    if code == 0:
        note("重启该 profile 后生效。")
    pause()


def action_open_profile_dir() -> None:
    title("打开 profile 目录")
    info("用途：直接看/改 package.json、cordis.patch.yml、pnpm-workspace.yaml。")
    note("改动 package.json 的 dependencies 会被下次插件命令覆盖；配置改动请写 cordis.patch.yml。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    directory = profile_dir(profile)
    if not directory.is_dir():
        warn(f"目录还不存在：{directory}")
        note("首次对某个 profile 执行添加/启动时会自动初始化它。")
        pause()
        return
    field("目录", str(directory))
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.name == "node_modules":
            continue
        info(f"   {entry.name}")
    if confirm("在资源管理器中打开该目录", True):
        try:
            if os.name == "nt":
                os.startfile(str(directory))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                launch(["open", str(directory)])
            else:
                launch(["xdg-open", str(directory)])
        except OSError as error:
            err(f"打开失败：{error}")
    pause()


def action_edit_patch_layer() -> None:
    title("编辑 profile 的用户 patch 层 cordis.patch.yml")
    info("用途：覆盖某个 bundle 行的 config、禁用某行、或插入新行 —— 不用改插件包本身。")
    step(1, "定位文件：$DSH_HOME/profiles/<profile>/cordis.patch.yml")
    step(2, "它在一个 patchReload=live 的 profile 里是热生效的：保存即重放，不用重启")
    step(3, "文件是 YAML 数组；同 id 的行覆盖前面 bundle 的整份 config（要重述所有需要的键）")
    step(4, "语法错误会在运行中的 profile 日志里报告，并且该次改动不生效")
    example("关闭某个插件行：- id: ui-skin-orca-link")
    example("                disabled: true")
    example("覆盖配置：- id: my-plugin")
    example("            config: { port: 8080 }")
    example("插入新行：- insert:")
    example("              - id: hello")
    example("                name: dsh-hello-plugin")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    path = profile_dir(profile) / "cordis.patch.yml"
    if not path.is_file():
        warn(f"文件不存在：{path}")
        note("该 profile 可能还没初始化；对它执行一次插件命令或启动一次即可生成。")
        pause()
        return
    field("文件", str(path))
    field("patchReload", profile_patch_reload(profile))
    print()
    print(_paint("  —— 当前内容 ——", "cmd"))
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        err(f"读取失败：{error}")
        pause()
        return
    for line in content.splitlines():
        print(_paint(f"   {line}", "info"))
    choice = read_value("用哪个编辑器打开（1=记事本，2=VS Code，回车=不打开）", "")
    if choice is None:
        pause()
        return
    editor = None
    if choice == "1":
        editor = "notepad"
    elif choice == "2":
        editor = "code" if have_tool("code") else None
        if editor is None:
            warn("没找到 VS Code 的 code 命令，改用记事本")
            editor = "notepad"
    if editor is not None:
        launch([editor, str(path)])
    pause()


def action_validate_composition() -> None:
    title("校验组合配置")
    info("用途：确认新加的层已经出现在组合结果里，或排查某行为何没生效。")
    step(1, "执行 pnpm dsh --profile <profile> --dump-config")
    step(2, '输出里会出现 "# == <包名>" 这样的层来源注释，逐层往下找你的插件')
    note("dump 不会启动服务，也不会运行应用参数，是最安全的校验方式。")
    profile = ask_profile()
    if profile is None:
        pause()
        return
    run_pnpm_dsh(["--profile", profile, "--dump-config"])
    pause()


# --------------------------------------------------------------------------- #
# 本地插件教程
# --------------------------------------------------------------------------- #
PLUGIN_TUTORIAL = """  概念先分清（两个 manifest，回答两个不同问题）：
    bundle  你要写的插件包：package.json 里声明 dsh.bundle.patch → “它贡献哪些插件行”
    profile 运行配置：$DSH_HOME/profiles/<名字> → “按什么顺序叠加哪些 bundle”
    两者都不是对方的子集：bundle 是发布物，profile 是本机运行配置。

  第一步：建目录和 package.json（示例）
    hello-plugin/
    ├─ package.json        声明 dsh.bundle
    ├─ cordis.patch.yml    这一层的插件行
    └─ index.js            行里引用的插件模块

    {
      "name": "dsh-hello-plugin",
      "version": "0.1.0",
      "type": "module",
      "main": "index.js",
      "files": ["index.js", "cordis.patch.yml"],
      "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
    }

  第二步：写插件模块 index.js
    export const name = 'hello-plugin'
    export function apply() {
      console.log('[hello-plugin] plugin loaded!')
    }

  第三步：写这一层的 cordis.patch.yml（行里用包名，Node 解析到已安装的代码）
    - insert:
        - id: hello
          name: dsh-hello-plugin

  第四步：安装进 profile（本菜单：7 → 3 → 2，填该目录的绝对路径）
    pnpm dsh plugin --profile web add "F:\\path\\to\\hello-plugin"
    相对路径以“调用目录”为基准；本菜单固定用仓库根目录，所以也可以写相对仓库根的路径。

  第五步：不启动就校验层是否生效
    pnpm dsh --profile web --dump-config      # 找 "# == dsh-hello-plugin"

  第六步：重启 profile（退出正在跑的 dsh，再 start.cmd → 1），日志里应出现 [hello-plugin] plugin loaded!

  第七步：改配置（不改插件包）
    编辑 $DSH_HOME/profiles/web/cordis.patch.yml：
      - id: hello
        config: { greeting: "hi" }
    patchReload=live 时保存即生效；bundle 成员变化（增删插件）才需要重启。

  第八步：单次调试（不写 profile）
    pnpm dsh web --patch ./my-extra.cordis.yml

  第九步：移除
    pnpm dsh plugin --profile web remove dsh-hello-plugin

  常见问题对照
    装了但没生效      → bundle 成员变化必须重启 profile
    控制台有 warning  → 该包没声明 dsh.bundle.patch，只会当普通依赖装，不会成层
    启动后 import 失败 → 源码包没构建（缺 lib/），先在它自己的项目里 build
    相对路径装错目录   → 相对规格以“调用目录”为基准；本菜单固定仓库根，其它目录用绝对路径
    git 依赖装不上     → pnpm ≥10 拦截 prepare，把 pnpm 打印的 key 加进 profile 的 pnpm-workspace.yaml 的 allowBuilds
    想看最终配置       → pnpm dsh --profile web --dump-config"""


def action_plugin_tutorial() -> None:
    title("本地插件开发教程（从 0 到 1）")
    block(PLUGIN_TUTORIAL)
    pause()


# --------------------------------------------------------------------------- #
# 菜单循环
# --------------------------------------------------------------------------- #
def plugin_menu() -> None:
    while True:
        title("插件管理（增 / 删 / 改 / 查）")
        print(_paint(f"  默认 profile：{DEFAULT_PROFILE}（每次操作前都会再确认一次）", "info"))
        print(_paint(f"  profile 根目录：{profiles_dir()}", "info"))
        print()
        print("   1  查看已安装插件与 bundle 层顺序（只读）")
        print("   2  查询单个插件：why / outdated / list（只读）")
        print("   3  添加插件（本地目录 / 绝对路径 / 相对路径 / tarball / npm / git / 多个 / 专家模式）…")
        print("   4  移除插件")
        print("   5  更新插件（update）")
        print("   6  校验组合配置（--dump-config，确认新层已生效）")
        print("   7  编辑 profile 的用户 patch 层 cordis.patch.yml（覆盖配置 / 禁用行 / 插入行）")
        print("   8  打开 profile 目录（package.json / pnpm-workspace.yaml / node_modules）")
        print("   9  本地插件开发教程（从 0 到 1，含示例文件内容）")
        print("   0  返回主菜单")
        choice = read_choice("请选择")
        if choice is None:
            return
        choice = choice.strip().lower()
        if choice == "":
            continue
        if choice == "1":
            action_list_plugins()
        elif choice == "2":
            action_query_plugin()
        elif choice == "3":
            add_menu()
        elif choice == "4":
            action_remove_plugin()
        elif choice == "5":
            action_update_plugin()
        elif choice == "6":
            action_validate_composition()
        elif choice == "7":
            action_edit_patch_layer()
        elif choice == "8":
            action_open_profile_dir()
        elif choice == "9":
            action_plugin_tutorial()
        elif choice in ("0", "q"):
            return
        else:
            warn(f"无效选项：{choice}")


def add_menu() -> None:
    while True:
        title("添加插件 · 选择方式")
        print("   1  本仓库 packages\\… 里的包（相对路径，最常用）")
        print("   2  任意本地目录（绝对路径，开发本地插件首选）")
        print("   3  相对当前目录（. / ..\\x / file:..\\x / link:..\\x）")
        print("   4  本地 tarball（.tgz，pnpm pack 的产物）")
        print("   5  npm 仓库包（name / name@version / @scope/name）")
        print("   6  Git 仓库（github:user/repo#<sha>，注意 allowBuilds）")
        print("   7  一次添加多个（逐行输入，可混用上面任意方式）")
        print("   8  专家模式：原样转发 pnpm 参数")
        print("   0  返回插件菜单")
        choice = read_choice("请选择")
        if choice is None:
            return
        choice = choice.strip().lower()
        if choice == "":
            continue
        if choice == "1":
            action_add_local_repo()
        elif choice == "2":
            action_add_absolute()
        elif choice == "3":
            action_add_relative()
        elif choice == "4":
            action_add_tarball()
        elif choice == "5":
            action_add_npm()
        elif choice == "6":
            action_add_git()
        elif choice == "7":
            action_add_multiple()
        elif choice == "8":
            action_add_expert()
        elif choice in ("0", "q"):
            return
        else:
            warn(f"无效选项：{choice}")


def show_main_menu() -> None:
    title("DeepSeek Harness · 启动与插件管理菜单")
    print(_paint(f"  仓库根目录：{REPO_ROOT}", "info"))
    print(_paint(f"  DSH_HOME ：{dsh_home()}", "info"))
    print()
    print(_paint("  [启动]", "cmd"))
    print("   1  启动 Web UI（默认 3080；缺产物自动构建；本机自动打开浏览器）")
    print("   2  启动 Web UI（自定义端口或附加参数）")
    print("   3  开发模式：Web UI + 客户端插件热更新（pnpm run dev:web）")
    print("   4  强制重新构建后再启动 Web UI")
    print("   5  运行 headless 一次性任务")
    print("   6  只看组合配置（--dump-config，不启动服务）")
    print()
    print(_paint("  [插件]", "cmd"))
    print("   7  插件管理（增 / 删 / 改 / 查）…")
    print()
    print(_paint("  [帮助]", "cmd"))
    print("   8  用法速查（所有命令与示例）")
    print("   9  环境自检（Node / pnpm / 产物 / profile / 端口 / 凭据）")
    print("   H  dsh 启动器帮助")
    print("   0  退出")
    print()
    print(_paint("  提示：直接回车 = 选项 1", "info"))


def main_menu() -> None:
    while True:
        show_main_menu()
        choice = read_choice("请选择")
        if choice is None:
            print()
            info("输入结束，退出菜单。")
            return
        choice = choice.strip().lower()
        if choice in ("", "1"):
            action_start_web()
        elif choice == "2":
            action_start_web_custom()
        elif choice == "3":
            action_start_dev()
        elif choice == "4":
            action_start_rebuild()
        elif choice == "5":
            action_headless_task()
        elif choice == "6":
            action_dump_config()
        elif choice == "7":
            plugin_menu()
        elif choice == "8":
            action_usage_reference()
        elif choice == "9":
            action_self_check()
        elif choice == "h":
            action_dsh_help()
        elif choice in ("0", "q"):
            return
        else:
            warn(f"无效选项：{choice}（输入 0 退出）")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list) -> int:
    global REPO_ROOT
    override = None
    for index, item in enumerate(argv):
        if item == "--repo" and index + 1 < len(argv):
            override = argv[index + 1]
        elif item.startswith("--repo="):
            override = item.split("=", 1)[1]
    if override:
        REPO_ROOT = Path(override).expanduser().resolve()

    if not (REPO_ROOT / "apps" / "cli" / "src" / "bin.ts").is_file():
        err(f"这里看起来不是 DeepSeek Harness 仓库根目录：{REPO_ROOT}")
        note(f"仓库根目录由脚本位置自动推导（当前脚本在 {LAUNCHER_DIR}）；")
        note("也可以显式传入：python start_menu.py --repo <仓库根目录>。")
        return 1

    try:
        main_menu()
    except KeyboardInterrupt:
        print()
        info("已中断，退出菜单。")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
