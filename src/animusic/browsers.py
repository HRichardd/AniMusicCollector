"""浏览器相关的小规则：家族识别 / profile 目录推导 / 常见安装位置。

为什么单独成模块：这些是**界面**（「浏览…」「改…」「默认」）和**CLI**共用的判断，
而这个项目已经因为"同一个判断写两份"踩过好几次（G.17 的筛选、G.18 的数字）。
放这里还带来一个好处：这些规则**不依赖 Qt**，可以离线单测（`tests/test_browsers.py`）。

三条刻意的决定：

1. **只看 exe 文件名判家族**，不看版本资源、不猜"是不是 Chromium"。
   名字够了：`chrome.exe` / `msedge.exe` / `brave.exe` / `vivaldi.exe` …
   内置一份"Chromium 家族名单"不可靠（国产套壳、改名版），而判错的代价
   （把 Firefox 当 Chromium 用）由启动失败时的提示兜住。
2. **Chrome 的 profile 目录名保持历史值 `browser-profile`**（不加后缀）——
   现有用户的 bgm.tv 登录态就在那个目录里，改名等于让他们重登一次。
3. **只推导路径，绝不创建/删除/移动目录**：目录由浏览器自己在首次启动时建。
"""

from __future__ import annotations

import os

#: profile 目录的基名。Chrome 用**不带后缀**的这个（见文件头第 2 条），
#: 其它家族用 `browser-profile-<家族>`。
PROFILE_BASENAME = "browser-profile"

#: profile 的默认落点根目录（各家族目录都是它的子目录）
DEFAULT_PROFILE_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "AniMusicCollector"
)

#: 常见安装位置 —— **只用来决定「浏览…」对话框从哪打开**，
#: 绝不自动往输入框里填（用户明确要求过"不要自动检测"）。
CANDIDATES: dict[str, list[str]] = {
    "browser": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Vivaldi\Application\vivaldi.exe"),
    ],
    "netease": [
        # ⚠️ 刻意**不写**某个具体盘符下的安装位置（原来这里有一条 `D:\CloudMusic\...`，
        # 那是开发机上的路径）：候选只用来决定对话框从哪打开，写死一台机器的位置
        # 对别人毫无用处，还会让"这台机器"的痕迹留在代码里。
        r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe",
        r"C:\Program Files (x86)\Netease\CloudMusic\cloudmusic.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Netease\CloudMusic\cloudmusic.exe"),
    ],
}


#: 已知浏览器的 "exe 名 → 家族键" 归一化。
#: 为什么需要它：Edge 的可执行文件叫 `msedge.exe`，但用户看到的、我们要用的名字是
#: **edge**（profile 目录叫 `browser-profile-edge`，不是 `-msedge`）。
#: 只收**确定见过**的几个；表里没有的就按 exe 名当键（见文件头第 1 条：不猜）。
_ALIASES = {
    "msedge": "edge",
    "microsoftedge": "edge",
    "googlechrome": "chrome",
    "chromium": "chromium",
    "brave": "brave",
    "bravebrowser": "brave",
    "vivaldi": "vivaldi",
    "opera": "opera",
    "launcher": "opera",          # Opera 的实际进程名
}


def family_of(exe: str) -> str:
    """从 exe 文件名推家族键：`msedge.exe` → `edge`、`Brave.EXE` → `brave`。

    两种"取不到家族"的情况都返回空串（调用方据此判断"还没选浏览器"）：

      · 空串 / 只有空白
      · **不是 `.exe` 结尾** —— 例如用户粘进来一个目录路径、或者 `brave`（没写后缀）。
        这里刻意不当成家族：拿 `Application` 这种目录名去拼
        `browser-profile-application` 只会制造垃圾目录名。
    """
    name = os.path.basename((exe or "").strip()).lower()
    if not name.endswith(".exe"):
        return ""
    name = name[:-4]
    return _ALIASES.get(name, name)


def profile_dir_name(exe: str) -> str:
    """家族 → profile 目录名。Chrome 保持 `browser-profile`（见文件头第 2 条）。"""
    fam = family_of(exe)
    if not fam:
        return ""
    return PROFILE_BASENAME if fam == "chrome" else f"{PROFILE_BASENAME}-{fam}"


def derive_profile(exe: str, base: str | None = None) -> str:
    """按家族推导 profile 目录。

    `base` 传"当前 profile 的父目录"时，用户的摆放位置会被尊重
    （他把 profile 放到别的盘，换浏览器后仍在那附近，不会被拽回默认位置）。
    没传就用默认根目录。
    """
    name = profile_dir_name(exe)
    if not name:
        return ""
    return os.path.join(base or DEFAULT_PROFILE_ROOT, name)


def dialog_start_dir(kind: str, current: str = "") -> str:
    """「浏览…」对话框从哪儿打开。

    顺序：① 当前路径所在目录（存在就用它）② 候选安装位置里第一个存在的
    ③ `C:\\Program Files` ④ 用户主目录。
    **只影响对话框的起始位置，不会往输入框里填任何东西。**
    """
    cur = (current or "").strip()
    if cur:
        d = os.path.dirname(cur)
        if d and os.path.isdir(d):
            return d
    for cand in CANDIDATES.get(kind, []):
        if cand and os.path.isfile(cand):
            return os.path.dirname(cand)
    for fallback in (r"C:\Program Files", os.path.expanduser("~")):
        if os.path.isdir(fallback):
            return fallback
    return ""
