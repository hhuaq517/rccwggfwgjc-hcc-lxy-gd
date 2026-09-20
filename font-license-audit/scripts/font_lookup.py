#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
字体文件定位器（共用模块，被 glyph_match.py / check_fonts.py 调用）。

为什么需要它（这是本技能移植性上的头号坑）：
清单里若把字体文件写死成某一台机器的绝对路径，例如
  C:\\Users\\某人\\AppData\\Local\\Microsoft\\Windows\\Fonts\\FZLTHJW.TTF
那么换一台电脑后这些路径**全部指向空**。后果分两种，后一种非常危险：

  1. 候选字体一款都解析不到 → 脚本会报「没有任何可用的候选字体文件」并退出。**能察觉**。
  2. 只缺已购、系统自带字体（黑体 / 微软雅黑 / 宋体 / 等线）还在 → 脚本**不报错**，
     照常输出一份排名，把本该当「未购对照」的系统字体推到第 1 名。**看上去正常，结论全错。**

做法：清单里只存**文件名**，运行时在若干「字体目录」里按文件名查找。
只要目标机器装了同样的字体 —— 装在哪个目录都行 —— 就能自动找到。
同时保留对旧版绝对路径的兼容：路径若真实存在就直接用，否则退回按文件名索引。
"""
import os
import sys
import unicodedata

# 追加字体目录的环境变量（多个目录用系统路径分隔符隔开：Windows 是 ;，类 Unix 是 :）
ENV_KEY = 'FONT_LICENSE_AUDIT_FONT_DIRS'

FONT_EXT = ('.ttf', '.otf', '.ttc', '.otc', '.ttf2')


def display_width(s):
    """字符串在终端里的显示宽度：CJK / 全角字符占 2 列。

    Python 的 %-24s 是按**字符数**补空格的，而中文名（如「方正仿宋简体」）每个字占 2 列，
    直接用它对齐会参差不齐，所以表格类输出统一走这里。
    """
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
               for c in str(s))


def pad(s, width):
    """按**显示宽度**右侧补空格（够宽就原样返回）。"""
    s = str(s)
    return s + ' ' * max(0, width - display_width(s))


def default_dirs(extra=None):
    """返回要扫描的字体目录列表（已去重，顺序即优先级）。

    顺序：系统字体目录 → 用户字体目录 → 环境变量追加 → 调用方追加。
    用户字体目录排在后面，因为同名时系统目录里的通常是「基准款」。
    """
    dirs = []

    if os.name == 'nt':
        windir = os.environ.get('WINDIR') or r'C:\Windows'
        dirs.append(os.path.join(windir, 'Fonts'))
        local = os.environ.get('LOCALAPPDATA')
        if local:
            dirs.append(os.path.join(local, 'Microsoft', 'Windows', 'Fonts'))
        # 有些机器用另一个账号装过字体，兜一层
        profile = os.environ.get('USERPROFILE')
        if profile:
            dirs.append(os.path.join(profile, 'AppData', 'Local',
                                     'Microsoft', 'Windows', 'Fonts'))
    else:
        home = os.path.expanduser('~')
        dirs += [
            '/Library/Fonts',
            '/System/Library/Fonts',
            '/System/Library/Fonts/Supplemental',
            os.path.join(home, 'Library', 'Fonts'),
            '/usr/share/fonts',
            '/usr/local/share/fonts',
            os.path.join(home, '.fonts'),
            os.path.join(home, '.local', 'share', 'fonts'),
        ]

    env = os.environ.get(ENV_KEY)
    if env:
        dirs += [d for d in env.split(os.pathsep) if d and d.strip()]

    if extra:
        dirs += list(extra)

    out, seen = [], set()
    for d in dirs:
        try:
            d = os.path.abspath(os.path.expanduser(str(d).strip()))
        except Exception:
            continue
        key = d.lower() if os.name == 'nt' else d
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def basename_of(target):
    """从「绝对路径」或「纯文件名」里取出文件名。同时兼容 \\ 与 / 两种分隔符。"""
    if not target:
        return ''
    return os.path.basename(str(target).replace('\\', '/'))


class FontIndex:
    """字体文件索引：先把所有字体目录列一遍，之后按文件名 O(1) 查找。

    一次列目录而不是每款字体扫一遍，是因为清单有 30 款，
    每款都 walk 一次字体目录在 Windows 上要重复读 400+ 个文件项，白花时间。
    """

    def __init__(self, dirs=None):
        self.dirs = list(dirs) if dirs is not None else default_dirs()
        self._index = None

    def build(self, force=False):
        """建立 {小写文件名: 完整路径} 索引。同名时保留**先出现的**目录（顺序即优先级）。"""
        if self._index is not None and not force:
            return self._index
        idx = {}
        for d in self.dirs:
            if not os.path.isdir(d):
                continue
            for root, _subdirs, files in os.walk(d):
                for fn in files:
                    if os.path.splitext(fn)[1].lower() in FONT_EXT:
                        idx.setdefault(fn.lower(), os.path.join(root, fn))
        self._index = idx
        return idx

    def find(self, target):
        """把「绝对路径」或「文件名」解析成真实存在的字体文件路径；找不到返回 None。"""
        if not target:
            return None
        raw = str(target)

        # 1) 旧版清单里的绝对路径：文件如果真在，直接用（向后兼容）
        try:
            expanded = os.path.expanduser(raw)
            if os.path.isfile(expanded):
                return expanded
        except Exception:
            pass

        # 2) 按文件名在索引里找
        name = basename_of(raw).lower()
        if not name:
            return None
        return self.build().get(name)

    def existing_dirs(self):
        """实际存在的字体目录（用于在报告里说明「扫了哪些地方」）。"""
        return [d for d in self.dirs if os.path.isdir(d)]

    def count(self):
        return len(self.build())


def resolve_many(items, index=None):
    """批量解析。items 是 [(label, target), ...]，返回 (找到的, 缺失的)。

    找到的: [{'label':…, 'path':…, 'target':…}, ...]
    缺失的: [{'label':…, 'target':…}, ...]
    """
    idx = index or FontIndex()
    found, missing = [], []
    for label, target in items:
        p = idx.find(target)
        if p:
            found.append({'label': label, 'path': p, 'target': target})
        else:
            missing.append({'label': label, 'target': target})
    return found, missing


def main():
    """直接运行时做一次自检：打印扫到的目录与索引里的字体数量。"""
    extra = sys.argv[1:] or None
    idx = FontIndex(default_dirs(extra))
    print('字体目录（%d 个，其中存在 %d 个）：'
          % (len(idx.dirs), len(idx.existing_dirs())))
    for d in idx.dirs:
        print('  %s %s' % ('[有]' if os.path.isdir(d) else '[无]', d))
    if os.environ.get(ENV_KEY):
        print('环境变量 %s = %s' % (ENV_KEY, os.environ[ENV_KEY]))
    print('\n索引到 %d 个字体文件' % idx.count())
    print('提示：想追加目录可设环境变量 %s（多个目录用 %r 分隔），'
          '或用各脚本的 --font-dir 参数。' % (ENV_KEY, os.pathsep))
    return 0


if __name__ == '__main__':
    sys.exit(main())
