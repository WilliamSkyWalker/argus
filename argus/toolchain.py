"""argus 自带工具链安装 —— `argus mcp init` 的后端。

目标：让**零基础用户一条命令**把跑测所需的 Appium 工具链装齐，而**绝不触碰系统
的 node / npm / 全局包**（多语言/多版本共存的开发机是有意配置，动它会连累别的项目）。

策略：一切装进沙盒 `~/.argus/runtime/`，与系统隔离——
  - Node：系统已有可用 node(≥18) 就复用（只读不改）；没有才下载 Node LTS 便携版
          解压进沙盒。永不 nvm/brew/改 PATH/改 shebang。
  - Appium：`npm install appium@3` **本地**装进沙盒（不是 -g，不污染全局）。
  - Drivers：uiautomator2（安卓）+ xcuitest（iOS，仅 macOS），装进沙盒 APPIUM_HOME。
  - adb：探测 platform-tools；缺则下载独立包进沙盒（uiautomator2 driver 的 server
         端需要 adb —— 这跟「argus runtime 不碰 adb」不冲突：那说的是跑测时 argus
         自身不靠 adb 截图/点击，而 Appium server 内部用 adb 是它的事）。
  - iOS WDA（webview）：签名依赖 Apple 账号无法全自动；能自动的自动，其余给清单。

`sandbox_paths()` 供 appium_server 在运行时定位沙盒 appium/node/APPIUM_HOME。
"""

import glob
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from .logger import get_logger

log = get_logger("toolchain")

ARGUS_HOME = Path(os.environ.get("ARGUS_HOME_DIR", Path.home() / ".argus"))
RUNTIME = ARGUS_HOME / "runtime"
NODE_DIR = RUNTIME / "node"                 # 沙盒 node 便携版解压处
APPIUM_HOME = RUNTIME / "appium_home"        # appium 存 driver 的地方
PLATFORM_TOOLS = RUNTIME / "platform-tools"  # 沙盒 adb

_APPIUM_SPEC = "appium@3"
_NODE_FALLBACK = "v22.12.0"   # index.json 拉不到时的兜底 LTS（须满足下方 appium 3 范围）


# ── 终端友好输出（零基础用户看得懂）──────────────────────────────
def _say(msg: str) -> None:
    print(msg, flush=True)


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}", flush=True)


def _step(n: int, total: int, msg: str) -> None:
    print(f"\n\033[1m[{n}/{total}] {msg}\033[0m", flush=True)


def _run(cmd: list[str], env: dict | None = None, cwd: str | None = None,
         timeout: int = 900) -> subprocess.CompletedProcess:
    """跑子命令，实时不重要——拿结果即可。失败抛异常带 stderr 便于诊断。"""
    log.debug("run: %s", " ".join(cmd))
    return subprocess.run(cmd, env=env, cwd=cwd, timeout=timeout,
                          capture_output=True, text=True)


# ── 平台/架构 ────────────────────────────────────────────────
def _os_arch() -> tuple[str, str]:
    sysname = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    if not sysname:
        raise RuntimeError(f"暂不支持的系统: {platform.system()}（仅 macOS / Linux）")
    mach = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64",
            "x86_64": "x64", "amd64": "x64"}.get(mach)
    if not arch:
        raise RuntimeError(f"暂不支持的架构: {mach}")
    return sysname, arch


# ── Node ─────────────────────────────────────────────────────
def _node_ver(node_bin: str) -> tuple[int, int, int] | None:
    """(major, minor, patch)；失败返回 None。"""
    try:
        out = _run([node_bin, "--version"]).stdout.strip()  # vXX.Y.Z
        parts = out.lstrip("v").split(".")
        return tuple(int(p) for p in parts[:3]) if len(parts) >= 3 else None
    except Exception:
        return None


def _node_ok(ver: tuple[int, int, int] | None) -> bool:
    """满足 Appium 3 的 node 版本范围：^20.19 || ^22.12 || >=24。
    奇数「Current」版（19/21/23）及过老版一律不合格。"""
    if not ver:
        return False
    mj, mn, _ = ver
    return (mj == 20 and mn >= 19) or (mj == 22 and mn >= 12) or mj >= 24


def _sandbox_node_bin() -> str | None:
    b = NODE_DIR / "bin" / "node"
    return str(b) if b.is_file() else None


def detect_system_node() -> str | None:
    """系统里满足 Appium 3 版本范围的 node 路径；没有返回 None。只读，不改系统。

    先看 PATH 上的 node；不合格再扫 nvm 各版本挑满足范围的最高版（很多机器 shell
    默认指向的是个不合格的 Current 版，但 nvm 里其实装着合格的 LTS）。选二进制而已，
    不 nvm use、不改 PATH，不违反「不动 node 版本」铁律。"""
    which = shutil.which("node")
    if which and _node_ok(_node_ver(which)):
        return which
    cands = []
    for b in glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")):
        v = _node_ver(b)
        if _node_ok(v):
            cands.append((v, b))
    if cands:
        return sorted(cands)[-1][1]   # 满足范围里的最高版
    return None


def _latest_lts_version(sysname: str, arch: str) -> str:
    """从 nodejs.org 拉最新 LTS 版本号；失败用兜底。"""
    try:
        with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=15) as r:
            data = json.load(r)
        wanted = f"{sysname}-{arch}"
        lts = [d for d in data if d.get("lts") and wanted in d.get("files", [])]
        if lts:
            # index.json 按新→旧排列，第一个即最新 LTS
            return lts[0]["version"]
    except Exception as e:
        log.debug("拉取 node index.json 失败，用兜底 %s: %s", _NODE_FALLBACK, e)
    return _NODE_FALLBACK


def install_sandbox_node() -> str:
    """下载 Node LTS 便携版解压进沙盒，返回 node bin 路径。绝不动系统 node。"""
    existing = _sandbox_node_bin()
    if existing and _node_ok(_node_ver(existing)):
        _ok(f"沙盒 Node 已就绪: {existing}")
        return existing

    sysname, arch = _os_arch()
    ver = _latest_lts_version(sysname, arch)
    name = f"node-{ver}-{sysname}-{arch}"
    url = f"https://nodejs.org/dist/{ver}/{name}.tar.gz"
    _say(f"  下载 Node {ver} ({sysname}-{arch}) …")
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "node.tar.gz"
        urllib.request.urlretrieve(url, tgz)
        with tarfile.open(tgz) as tf:
            tf.extractall(td)
        src = Path(td) / name
        # 解压结果搬进 NODE_DIR（扁平化：NODE_DIR/bin/node）
        for item in src.iterdir():
            dst = NODE_DIR / item.name
            if dst.exists():
                shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
            shutil.move(str(item), str(dst))
    node_bin = _sandbox_node_bin()
    if not node_bin:
        raise RuntimeError("Node 解压后仍找不到 bin/node")
    _ok(f"沙盒 Node 安装完成: {node_bin}")
    return node_bin


def ensure_node(force_sandbox: bool = False) -> str:
    """返回可用 node 路径：优先复用系统 node（除非 force_sandbox），否则装沙盒 node。"""
    if not force_sandbox:
        sys_node = detect_system_node()
        if sys_node:
            v = _node_ver(sys_node)
            _ok(f"复用系统 Node: {sys_node} (v{'.'.join(map(str, v)) if v else '?'})")
            return sys_node
    return install_sandbox_node()


def _node_env(node_bin: str) -> dict:
    """把 node 所在 bin 放 PATH 最前，令 npm/appium 的 shebang 命中这个 node。"""
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(node_bin) + os.pathsep + env.get("PATH", "")
    return env


# ── Appium + drivers ────────────────────────────────────────
def _appium_local_bin() -> Path:
    return RUNTIME / "node_modules" / ".bin" / "appium"


_NODE_MARKER = RUNTIME / "node_path.txt"


def install_appium(node_bin: str) -> str:
    """在沙盒里本地装 appium（非 -g），返回 appium bin 路径。"""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # 记下装 appium 用的 node —— 运行时据此确定性地拼 PATH，不靠环境里恰好有 node
    _NODE_MARKER.write_text(node_bin + "\n")
    pkg = RUNTIME / "package.json"
    if not pkg.exists():
        pkg.write_text('{"name":"argus-runtime","private":true}\n')
    env = _node_env(node_bin)
    npm = os.path.join(os.path.dirname(node_bin), "npm")
    _say(f"  npm install {_APPIUM_SPEC}（本地，沙盒内，不动全局）…")
    r = _run([npm, "install", _APPIUM_SPEC, "--no-fund", "--no-audit"],
             env=env, cwd=str(RUNTIME))
    if r.returncode != 0:
        raise RuntimeError(f"appium 安装失败:\n{r.stderr[-2000:]}")
    ab = _appium_local_bin()
    if not ab.is_file():
        raise RuntimeError("appium 装完却找不到 node_modules/.bin/appium")
    _ok(f"Appium 安装完成: {ab}")
    return str(ab)


def install_drivers(node_bin: str, appium_bin: str, ios: bool) -> None:
    """装 appium driver 到沙盒 APPIUM_HOME。uiautomator2 必装；xcuitest 仅 macOS。"""
    APPIUM_HOME.mkdir(parents=True, exist_ok=True)
    env = _node_env(node_bin)
    env["APPIUM_HOME"] = str(APPIUM_HOME)

    def _installed() -> set[str]:
        r = _run([appium_bin, "driver", "list", "--installed", "--json"], env=env)
        try:
            return set(json.loads(r.stdout or "{}").keys())
        except Exception:
            return set()

    have = _installed()
    drivers = ["uiautomator2"]
    if ios and platform.system() == "Darwin":
        drivers.append("xcuitest")
    for d in drivers:
        if d in have:
            _ok(f"driver 已装: {d}")
            continue
        _say(f"  appium driver install {d} …")
        r = _run([appium_bin, "driver", "install", d], env=env)
        if r.returncode != 0:
            _warn(f"driver {d} 安装失败: {r.stderr[-500:]}")
        else:
            _ok(f"driver 安装完成: {d}")


# ── Android adb ─────────────────────────────────────────────
def detect_adb() -> str | None:
    for cand in (
        shutil.which("adb"),
        str(PLATFORM_TOOLS / "adb"),
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return None


def install_platform_tools() -> str | None:
    """下载独立 platform-tools（含 adb）进沙盒。仅 macOS/Linux。"""
    sysname, _ = _os_arch()
    slug = {"darwin": "darwin", "linux": "linux"}[sysname]
    url = f"https://dl.google.com/android/repository/platform-tools-latest-{slug}.zip"
    _say(f"  下载 Android platform-tools（adb）…")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            z = Path(td) / "pt.zip"
            urllib.request.urlretrieve(url, z)
            with zipfile.ZipFile(z) as zf:
                zf.extractall(RUNTIME)   # 解出 RUNTIME/platform-tools/
        adb = PLATFORM_TOOLS / "adb"
        if adb.is_file():
            adb.chmod(0o755)
            _ok(f"adb 安装完成: {adb}")
            return str(adb)
    except Exception as e:
        _warn(f"platform-tools 下载失败: {e}")
    return None


# ── iOS WDA（webview）─────────────────────────────────────────
def setup_ios_wda(node_bin: str, appium_bin: str, team_id: str | None,
                  device: str | None) -> None:
    """iOS WebDriverAgent（俗称 iOS 端的 webview）准备。

    签名必须 Apple 账号，无法全自动。这里做能自动的：探测 Xcode、检查 team_id、
    有设备时尝试预构建 WDA；其余以清单形式告诉用户手动步骤。
    """
    if platform.system() != "Darwin":
        _warn("非 macOS，跳过 iOS WDA（iOS 自动化只能在 Mac 上签名/构建）")
        return
    xc = _run(["xcodebuild", "-version"])
    if xc.returncode != 0:
        _warn("未检测到 Xcode。iOS 需要：App Store 装 Xcode → 打开一次同意协议 → "
              "`sudo xcode-select -s /Applications/Xcode.app`")
        return
    _ok(f"Xcode: {xc.stdout.splitlines()[0] if xc.stdout else 'ok'}")

    if not team_id:
        _warn("未提供 Apple team id（--ios-team-id 或 .env 的 IOS_TEAM_ID）——"
              "WDA 首次签名需要它，先跳过预构建。")
    else:
        _ok(f"Apple team id: {team_id}")

    if team_id and device:
        env = _node_env(node_bin)
        env["APPIUM_HOME"] = str(APPIUM_HOME)
        _say("  预构建 WebDriverAgent（首次较慢）…")
        r = _run([appium_bin, "driver", "run", "xcuitest", "build-wda",
                  "--udid", device], env=env, timeout=1200)
        if r.returncode == 0:
            _ok("WebDriverAgent 预构建完成")
        else:
            _warn(f"WDA 预构建未成功（可后续首次跑 iOS 用例时自动构建）：{r.stderr[-400:]}")

    _say("  iOS 首次自动化清单（零基础照做）：")
    _say("    1) iPhone 用数据线连 Mac，手机上点『信任此电脑』")
    _say("    2) 设置→隐私与安全性→开发者模式 打开，重启手机")
    _say("    3) Xcode 登录你的 Apple ID，把设备加入该账号的开发设备")
    _say("    4) .env 填 IOS_TEAM_ID / IOS_BUNDLE_ID（被测 App）")
    _say("       首次跑 iOS 用例时 Appium 会自动签名并装 WDA 到手机")


# ── 供运行时定位沙盒工具链 ────────────────────────────────────
def sandbox_paths() -> dict:
    """返回沙盒里已装好的工具链路径，供 AppiumServerManager 运行时使用。
    未安装的键不出现（值恒为存在的路径）。"""
    out: dict = {}
    ab = _appium_local_bin()
    if ab.is_file():
        out["appium_bin"] = str(ab)
    nb = _sandbox_node_bin()
    if not nb and _NODE_MARKER.is_file():
        # 复用系统 node 的情形：读安装时记下的 node 路径
        cand = _NODE_MARKER.read_text().strip()
        nb = cand if cand and os.path.isfile(cand) else None
    if nb:
        out["node_bin"] = nb
    if APPIUM_HOME.is_dir():
        out["appium_home"] = str(APPIUM_HOME)
    adb = PLATFORM_TOOLS / "adb"
    if adb.is_file():
        out["platform_tools"] = str(PLATFORM_TOOLS)
    return out


# ── doctor（体检）+ init（安装）────────────────────────────────
def doctor() -> None:
    _say("\033[1margus 工具链体检\033[0m")
    sb = sandbox_paths()
    sys_node = detect_system_node()
    _say(f"  可用 Node: {sys_node or '无(需 20.19+/22.12+/24+)'}")
    _say(f"  Appium Node: {sb.get('node_bin', '未记录')}")
    _say(f"  Appium:    {sb.get('appium_bin', '未装')}")
    _say(f"  APPIUM_HOME: {sb.get('appium_home', '未建')}")
    _say(f"  adb:       {detect_adb() or '未找到'}")
    if platform.system() == "Darwin":
        xc = _run(["xcodebuild", "-version"])
        _say(f"  Xcode:     {(xc.stdout.splitlines()[0] if xc.returncode==0 and xc.stdout else '未装')}")


def mcp_init(ios: bool = True, ios_team_id: str | None = None,
             device: str | None = None, force_node: bool = False,
             skip_ios: bool = False) -> None:
    """一键装齐 argus 跑测工具链（全部沙盒，不动系统）。"""
    _say("\033[1margus 工具链初始化\033[0m — 全部装进 ~/.argus/runtime（不动系统 node/npm）\n")
    total = 5 if (ios and not skip_ios) else 4

    _step(1, total, "准备 Node（优先复用系统，缺则装沙盒便携版）")
    node_bin = ensure_node(force_sandbox=force_node)

    _step(2, total, "安装 Appium（沙盒本地，不污染全局）")
    appium_bin = install_appium(node_bin)

    _step(3, total, "安装 Appium drivers")
    install_drivers(node_bin, appium_bin, ios=ios and not skip_ios)

    _step(4, total, "准备 Android adb")
    if detect_adb():
        _ok(f"adb 已就绪: {detect_adb()}")
    else:
        install_platform_tools()

    if ios and not skip_ios:
        _step(5, total, "准备 iOS WebDriverAgent（webview）")
        team = ios_team_id or os.environ.get("IOS_TEAM_ID")
        setup_ios_wda(node_bin, appium_bin, team, device)

    _say("\n\033[32m\033[1m✓ 完成\033[0m argus 会自动使用 ~/.argus/runtime 里的工具链。")
    _say("  验证：\033[1margus mcp doctor\033[0m   跑测：\033[1margus run <target> --device <serial> --apk <apk>\033[0m")
