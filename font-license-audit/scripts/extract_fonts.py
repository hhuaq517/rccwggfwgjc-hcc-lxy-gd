#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
第一层：从设计源文件 / 导出稿中提取字体名称（确定性方案，不做任何猜测）。

支持格式（按文件魔数自动判定，不看扩展名）：
  - docx / pptx / xlsx  : zip + OOXML，读 w:rFonts / a:latin / a:ea / xl <name val>
  - pdf / ai            : 读 /BaseFont、/FontName（自动去掉 ABCDEF+ 子集前缀）
  - psd                 : 读 TySh 文本图层描述符中的 /Name (...) 与 FontSet
  - png / jpg / tif     : 读 PNG tEXt/iTXt 与 JPEG EXIF，一般无字体信息

PSD 分支会按 `/RunLengthArray` 把字符**逐段归属**到字体（见 parse_psd_text_block），
只报「真正分到字符」的字体；只要 psd_tools 可用，还会把**智能对象**（logo / 素材图）
里内嵌的文件拆出来继续解析（见 from_psd_smart_objects）。

用法：
  python extract_fonts.py <文件1> [文件2 ...] [--json 输出.json]
                          [--psd-layers] [--no-smart-objects]

退出码：0 = 至少提取到 1 个字体名；1 = 全部文件都没提取到（多半只有成图）
"""
import argparse
import json
import os
import re
import sys
import tempfile
import zipfile

SUBSET_PREFIX = re.compile(r'^[A-Z]{6}\+')
PSCANDIDATE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-_,\.]{1,63}$')

# 明显不是字体名的噪声（PSD 描述符里 /Name 会出现在别处）
NOISE = {
    'null', 'true', 'false', 'none', 'normal', 'root', 'layer', 'group',
    'document', 'text', 'background', 'layer 1', 'psd', 'adobe',
}


def clean(name):
    """去掉 PDF 子集前缀、首尾空白与包裹引号。"""
    if not name:
        return ''
    n = name.strip().strip('\'"()')
    n = SUBSET_PREFIX.sub('', n)
    return n.strip()


def add(bucket, name, where):
    n = clean(name)
    if not n or len(n) > 80:
        return
    if n.lower() in NOISE:
        return
    bucket.setdefault(n, set()).add(where)


def pdf_unescape(s):
    """PDF 名字对象里的 #XX 转义。"""
    def rep(m):
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)
    return re.sub(r'#([0-9A-Fa-f]{2})', rep, s)


# ---------------------------------------------------------------- OOXML
def from_ooxml(path, names):
    with zipfile.ZipFile(path) as z:
        kids = z.namelist()
        ct = ''
        if '[Content_Types].xml' in kids:
            ct = z.read('[Content_Types].xml').decode('utf-8', 'ignore')
        if 'wordprocessingml' in ct:
            kind = 'docx'
        elif 'presentationml' in ct:
            kind = 'pptx'
        elif 'spreadsheetml' in ct:
            kind = 'xlsx'
        else:
            kind = 'ooxml'

        for n in kids:
            if not n.endswith('.xml'):
                continue
            if not (n.startswith('word/') or n.startswith('ppt/')
                    or n.startswith('xl/')):
                continue
            txt = z.read(n).decode('utf-8', 'ignore')

            # Word：<w:rFonts w:ascii="X" w:eastAsia="Y" .../>
            for m in re.finditer(r'<w:rFonts\b[^>]*>', txt):
                tag = m.group(0)
                for attr in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'):
                    v = re.search(r'\b' + attr + r'="([^"]+)"', tag)
                    if v:
                        add(names, v.group(1), '%s:%s' % (kind, n))

            # PPT / 主题字体：<a:latin typeface="X"/> <a:ea typeface="Y"/>
            for m in re.finditer(r'<a:(?:latin|ea|cs)\b[^>]*?typeface="([^"]+)"', txt):
                add(names, m.group(1), '%s:%s' % (kind, n))

            # Excel：<name val="X"/>
            for m in re.finditer(r'<name\s+val="([^"]+)"', txt):
                add(names, m.group(1), '%s:%s' % (kind, n))
    return kind


# ---------------------------------------------------------------- PDF
def from_pdf(path, names):
    data = open(path, 'rb').read()
    hit = 0
    for pat in (rb'/BaseFont\s*/([#\w\+\-\.\,]+)',
                rb'/FontName\s*/([#\w\+\-\.\,]+)'):
        for m in re.finditer(pat, data):
            add(names, pdf_unescape(m.group(1).decode('latin-1', 'ignore')),
                'pdf:font-resource')
            hit += 1
    return hit


# ---------------------------------------------------------------- PSD
# PSD 描述符里这些不是「字体」，是 Photoshop 的占位/设置项，必须剔除，否则会污染清单比对：
#   PhotoshopKinsokuHard / Soft —— 日文禁则换行规则集，不是字体
#   AdobeInvisFont              —— 隐形占位字体（用于量算）
#   AdobeHeitiStd-Regular       —— 出现在 /StyleSheetSet 的文档默认样式「正常 RGB」里，
#                                  不是图层实际用字；实测误报过，是本节最坑的一条
PSD_NOISE = (
    'photoshopkinsoku',
    'adobeinv',
    'adobeinvisfont',
    '正常 rgb',
    'sans-serif',
    'serif',
)


def _psd_noise(n):
    low = n.replace(' ', '').lower()
    return any(k in low for k in PSD_NOISE) or not n.strip()


def psd_string(buf, start):
    """从 start（左括号后一位）读 PSD 字符串。
    PSD 字符串里 '\\' 与 ')' 都会被反斜杠转义，正则贪婪匹配会把整块吞掉。

    ⚠️ 转义是**按字节**的，UTF-16BE 汉字中间也会出现 0x5C / 0x29：
    实测「天」= U+5929 在文件里写作 `59 5C 29`（0x29 被转义），
    「尾」= U+5C3E 写作 `5C 5C 3E`（0x5C 被转义）。
    所以必须逐字节扫描、遇到 0x5C 就吞掉其后一个字节，
    既不能把转义字节当正文（会多出一个字节、后面全部错位），
    也不能把被转义的 0x29 当成字符串结束（文字会被腰斩）。"""
    out, i = [], start
    while i < len(buf):
        c = buf[i]
        if c == 0x5c and i + 1 < len(buf):
            out.append(buf[i + 1]); i += 2; continue
        if c == 0x29:
            break
        out.append(c); i += 1
    return bytes(out)


def psd_decode(raw):
    """PSD 描述符字符串 → str。

    顺序：`\\xfe\\xff` BOM → UTF-16BE；纯 ASCII → ascii；
    含成对 NUL（漏写 BOM 的 UTF-16BE）→ UTF-16BE；其余先试 GBK，再 utf-8，最后 latin-1。

    ⚠️ 两个都踩过的坑：
    ① 只按 latin-1 解码 → 中文 PSD 的字体名全变乱码、被字符过滤一过就全丢，
       表现为「未提取到任何字体名」，很容易误判成"这批素材只有成图"。
    ② 反过来也不行：**无 BOM 的中文名要按 GBK 解**（中文 Windows 上存的旧格式就是这个），
       用 latin-1 会得到一串能过长度检查的乱码，静默给出错的名字。"""
    if raw.startswith(b'\xfe\xff'):
        return raw[2:].decode('utf-16-be', 'ignore')
    if not raw:
        return ''
    if all(32 <= c < 127 or c in (9, 10, 13) for c in raw):
        return raw.decode('ascii', 'ignore')
    if len(raw) % 2 == 0 and raw.count(0) >= len(raw) // 4:
        return raw.decode('utf-16-be', 'ignore')      # 漏写 BOM 的 UTF-16BE
    for enc in ('gbk', 'utf-8'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1', 'ignore')


def _skip_string(buf, i):
    """i 指向左括号，返回配对的右括号下标；找不到返回 -1。"""
    j = i + 1
    while j < len(buf):
        if buf[j] == 0x5c:
            j += 2; continue
        if buf[j] == 0x29:
            return j
        j += 1
    return -1


def _scan_block(buf, key, open_tok, close_tok, start=0):
    """找 key 之后的**平衡**块（`<< >>` 或 `[ ]`），返回 (内容, 起, 止)。

    必须自己做括号配对，不能正则非贪婪：StyleSheetData 里还嵌着
    `/Values [ 0.0 0.0 ]` 这类数组，非贪婪匹配会停在里面那个 `]` 上。
    字符串（`(...)`，含转义）内部的括号一律不计数。"""
    m = re.compile(re.escape(key) + rb'\s*' + re.escape(open_tok)).search(buf, start)
    if not m:
        return None
    i, depth, j = m.end() - len(open_tok), 0, m.end() - len(open_tok)
    while j < len(buf):
        c = buf[j]
        if c == 0x28:
            k = _skip_string(buf, j)
            if k < 0:
                return None
            j = k + 1; continue
        if buf[j:j + len(open_tok)] == open_tok:
            depth += 1; j += len(open_tok); continue
        if buf[j:j + len(close_tok)] == close_tok:
            depth -= 1
            j += len(close_tok)
            if depth == 0:
                return buf[i + len(open_tok):j - len(close_tok)], i, j
            continue
        j += 1
    return None


def parse_psd_text_block(blk):
    """解析一个 TySh 文字块，回答「这一层到底用了哪几款字、各多少字」。

    返回 dict：
      text    图层文字（`\\r` 为 PSD 的换行）
      fontset FontSet 里声明的全部字体名
      runs    [(字体下标, 字符数)]，字符数取自 `/RunLengthArray`
      used    实际用字 {字体名: 字符数}，**只含 run 长度 > 0 的**
      unused  FontSet 里声明过、但一个字都没分到的字体名
      exact   是否拿到了可用的 `/RunLengthArray`（False = 只能退化成整份 FontSet）
      matched run 字符数合计是否与文字长度一致

    ⚠️ 坑：`/FontSet` 里出现 ≠ 该层用了它。一个层的 FontSet 常列着 3–5 个字体名
    （历史编辑留下的），但 `/RunArray` 里只有部分 run 真正分到字符。
    必须按 `/RunLengthArray` **按位对齐**：run 长度合计 = `/Text` 的字符数（含行尾 `\\r`）。
    只看 FontSet 列表，会把没用的字体报成 🚫 —— 这是**误报**，会让设计白改一轮字。"""
    fontset = []
    fa = _scan_block(blk, rb'/FontSet', b'[', b']')
    if fa:
        for m in re.finditer(rb'/Name\s*\(', fa[0]):
            fontset.append(psd_decode(psd_string(fa[0], m.end())).strip())

    text = ''
    tm = re.search(rb'/Text\s*\(', blk)
    if tm:
        text = psd_decode(psd_string(blk, tm.end()))

    # 每个 /StyleRun 字典里同时有 /RunArray（字体下标，按 run 顺序）与
    # /RunLengthArray（各 run 的字符数，下标一一对应）。
    # 段落级的 /ParagraphRun 也有这两个键，但里面没有 /Font，且在 /StyleRun 之外，故不受影响。
    runs, exact = [], False
    for m in re.finditer(rb'/StyleRun\b', blk):
        sr = _scan_block(blk, rb'/StyleRun', b'<<', b'>>', m.start())
        if not sr:
            continue
        ra = _scan_block(sr[0], rb'/RunArray', b'[', b']')
        if not ra:
            continue
        fonts = [int(x) for x in re.findall(rb'/Font\s+(\d+)', ra[0])]
        rl = _scan_block(sr[0], rb'/RunLengthArray', b'[', b']')
        lens = [int(x) for x in re.findall(rb'-?\d+', rl[0])] if rl else []
        if not fonts:
            continue
        if len(lens) == len(fonts):
            runs.extend(zip(fonts, lens))
            exact = True
        else:
            # 对不齐就按能对齐的部分算，剩下的记为「字数未知」（不装作对齐了）
            runs.extend(zip(fonts, lens))
            runs.extend((i, None) for i in fonts[len(lens):])

    counts = {}
    for i, ln in runs:
        if not 0 <= i < len(fontset):
            continue
        if ln is None:
            counts.setdefault(i, None)          # 字数未知
        elif i not in counts or counts[i] is not None:
            counts[i] = (counts.get(i) or 0) + max(ln, 0)

    used, unknown_names = {}, set()
    for i, n in enumerate(fontset):
        if i not in counts:
            continue
        c = counts[i]
        if c is None or n in unknown_names:
            unknown_names.add(n)
            used[n] = None                      # 用到了，但字数没算出来
        else:
            used[n] = (used.get(n) or 0) + c    # FontSet 里同款字可能重复登记，按名字累加
    # 「声明未用」必须按**名字**判重：FontSet 里同一款字常重复登记（下标不同、名字相同），
    # 只用下标去判，会把已经用到的字体也列进"未用"里，自相矛盾。
    unused = [n for i, n in enumerate(fontset)
              if n not in used and (i not in counts or counts[i] == 0)]

    total = sum(c for c in counts.values() if c)
    matched = (not text) or (total == len(text))
    return {'text': text, 'fontset': fontset, 'runs': runs, 'used': used,
            'unused': unused, 'exact': exact, 'matched': matched}


def from_psd(path, names, notes, layers=None):
    data = open(path, 'rb').read()
    marks = [m.start() for m in re.finditer(rb'TySh', data)]
    if marks:
        windows = [(s, marks[i + 1] if i + 1 < len(marks) else len(data))
                   for i, s in enumerate(marks)]
    else:
        windows = [(0, len(data))]

    hits, unused_all, n_inexact, n_mismatch = [], [], 0, 0
    for s, e in windows:
        info = parse_psd_text_block(data[s:e])
        exact = True
        picked = info['used']
        if not picked and info['fontset']:
            # 只有在拿不到 RunLengthArray 时才退化，并明确标注「不确定」
            picked = {n: None for n in info['fontset']}
            exact = False
            n_inexact += 1
        if not picked:
            continue
        if exact and not info['matched']:
            n_mismatch += 1
        if exact:
            for n in info['unused']:
                if not _psd_noise(n):
                    unused_all.append(n)
        tag = 'psd:实际用字' if exact else 'psd:FontSet(未定位到用字)'
        layer = {'text': info['text'].replace('\r', ' / ').strip()[:40],
                 'fonts': {}, 'unused': [], 'exact': exact}
        for n, c in picked.items():
            n = clean(n)
            if _psd_noise(n):
                continue
            hits.append((n, tag))
            layer['fonts'][n] = c
        for n in info['unused']:
            n = clean(n)
            if not _psd_noise(n):
                layer['unused'].append(n)
        if layers is not None and layer['fonts']:
            layers.append(layer)

    uniq = []
    for n, tag in hits:
        if n not in [x[0] for x in uniq]:
            uniq.append((n, tag))
    for n, tag in uniq:
        add(names, n, tag)

    uniq_unused = []
    for n in unused_all:
        if n not in uniq_unused:
            uniq_unused.append(n)
    if uniq_unused:
        notes.append('PSD 字表里**声明过、但一个字都没分到**的字体（僵尸 run，未计入比对）：%s'
                     % '、'.join(uniq_unused))
    if n_inexact:
        notes.append('PSD 有 %d 层未取到 /RunLengthArray，只能按整份 FontSet 报（**不确定**，'
                     '该层可能含未实际使用的字体）' % n_inexact)
    if n_mismatch:
        notes.append('PSD 有 %d 层的 run 字符数与 /Text 长度不一致，逐层字数仅供参考' % n_mismatch)
    return len(uniq)


# ------------------------------------------------- PSD 智能对象（可选，需 psd_tools）
# 智能对象（logo / 素材图 / 内嵌设计稿）内的字体，光学 PSD 是读不到的 ——
# 内嵌的是独立文件（PSB / PDF / AI / PNG），必须**拆出来单独解析**。
# 这一步能把「智能对象」从盲区变成结论：内嵌 PSB 里的文字层照样有 TySh，
# 内嵌 AI/PDF 里有 /BaseFont。
SO_EXTS = ('.psb', '.psd', '.pdf', '.ai', '.tif', '.tiff', '.png', '.jpg', '.jpeg')
SO_MAX_BYTES = 64 * 1024 * 1024
SO_MAX_OBJECTS = 16


def from_psd_smart_objects(path, names, notes):
    """拆出 PSD 里内嵌的智能对象，逐个当独立文件解析。需要 psd_tools（可选依赖）。"""
    try:
        from psd_tools import PSDImage
    except Exception:
        notes.append('本机未装 psd_tools，**智能对象（logo / 素材图）未拆解**，'
                     '内部字体读不到（报告里要单列此盲区；装上后重跑即可：pip install psd-tools）')
        return 0

    tmp = tempfile.mkdtemp(prefix='so_')
    seen, n, out = set(), 0, 0
    try:
        psd = PSDImage.open(path)
        for layer in psd.descendants():
            if layer.kind != 'smartobject' or n >= SO_MAX_OBJECTS:
                continue
            try:
                so = layer.smart_object
                fn, data = so.filename or '', bytes(so.data or b'')
            except Exception as e:
                notes.append('智能对象「%s」拆解失败：%s' % (getattr(layer, 'name', '?'), e))
                continue
            # 同一个素材常被复用多次（实测一份推图里同一个 png 放了 7 遍），按内容去重
            key = (fn, len(data))
            if not data or key in seen or len(data) > SO_MAX_BYTES:
                continue
            seen.add(key)
            n += 1
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SO_EXTS:
                ext = '.pdf' if data[:4] == b'%PDF' else (
                    '.psb' if data[:4] == b'8BPS' else '.bin')
            # 落盘时用安全名：内嵌名可能是中文/含空格/含路径分隔符，也可能在别处被当成 GBK 解
            local = os.path.join(tmp, 'so%02d%s' % (n, ext))
            with open(local, 'wb') as f:
                f.write(data)
            sub, subnotes = {}, []
            process(local, sub, subnotes, no_so=True)
            inner = sorted(sub.keys())
            if inner:
                out += 1
                for k in inner:
                    add(names, k, 'psd:智能对象(%s)' % fn)
            notes.append('智能对象「%s」（%.1f MB）→ %s'
                         % (fn, len(data) / 1048576.0,
                            ('拆出字体：' + '、'.join(inner)) if inner
                            else '内部没有可读字体（多为位图或文字已转曲，属盲区）'))
            for sn in subnotes[:3]:
                if sn.endswith('no-font-metadata'):
                    continue                    # 与上一句重复，不必再报
                notes.append('   └ 智能对象内部：%s' % sn)
    finally:
        try:
            for f in os.listdir(tmp):
                os.remove(os.path.join(tmp, f))
            os.rmdir(tmp)
        except OSError:
            pass
    return out


# ---------------------------------------------------------------- 图片
def from_image(path, names):
    """图片容器里偶尔会有设计工具写入的字体信息，命中率低但值得一看。"""
    ext = os.path.splitext(path)[1].lower()
    found = False
    try:
        from PIL import Image
        im = Image.open(path)
        info = getattr(im, 'info', {}) or {}
        for k, v in info.items():
            if not isinstance(v, str):
                continue
            if re.search(r'font|typeface|family', k, re.I) or \
               re.search(r'font|typeface', v, re.I):
                add(names, v[:60], 'image:%s' % k)
                found = True
        ex = im.getexif() if hasattr(im, 'getexif') else {}
        for k, v in (ex or {}).items():
            if isinstance(v, str) and re.search(r'font|typeface', v, re.I):
                add(names, v[:60], 'image:exif')
                found = True
    except Exception as e:
        add(names, '', '')  # noop
        return ('无法读取图片元数据：%s' % e)
    return None if found else 'no-font-metadata'


# ---------------------------------------------------------------- 调度
def sniff(path):
    with open(path, 'rb') as f:
        head = f.read(8)
    if head[:4] == b'PK\x03\x04':
        return 'ooxml'
    if head[:4] == b'%PDF':
        return 'pdf'
    if head[:4] == b'8BPS':
        return 'psd'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image'
    if head[:2] == b'\xff\xd8':
        return 'image'
    return 'unknown'


def process(path, names, notes, layers=None, no_so=False):
    # 目录要被明确拒绝：静默返回"文件不存在"会让人误以为只是路径写错了。
    # 传目录往往是"顺手把整个素材文件夹丢进来"的信号，而审查范围只应是用户点名的那一个文件。
    if os.path.isdir(path):
        notes.append('%s : 这是【目录】，本工具只接受文件，不会递归扫描。\n'
                     '      请只传用户在本次消息里点名的那一个文件；要审多个就逐个列出。' % path)
        return 'dir'
    if not os.path.isfile(path):
        notes.append('%s : 文件不存在' % path)
        return 'missing'
    kind = sniff(path)
    try:
        if kind == 'ooxml':
            from_ooxml(path, names)
        elif kind == 'pdf':
            from_pdf(path, names)
        elif kind == 'psd':
            from_psd(path, names, notes, layers)
            if not no_so:
                from_psd_smart_objects(path, names, notes)
        elif kind == 'image':
            msg = from_image(path, names)
            if msg:
                notes.append('%s : %s' % (os.path.basename(path), msg))
        else:
            # .ai 之类先按 PDF 试一次
            data = open(path, 'rb').read(4096)
            if b'/BaseFont' in data or b'%PDF' in data:
                from_pdf(path, names)
            else:
                notes.append('%s : 无法识别的格式（魔数 %r）'
                             % (os.path.basename(path), head4(path)))
    except Exception as e:
        notes.append('%s : 解析失败 %s: %s'
                     % (os.path.basename(path), type(e).__name__, e))


def head4(path):
    with open(path, 'rb') as f:
        return f.read(4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+')
    ap.add_argument('--json', dest='json_out', default=None)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--psd-layers', action='store_true',
                    help='打印 PSD 逐层用字映射（图层 → 字体 → 字数），排查误报时用')
    ap.add_argument('--no-smart-objects', action='store_true',
                    help='不拆解 PSD 里的智能对象（内嵌 logo / 素材图），默认拆')
    a = ap.parse_args()

    names, notes, layers = {}, [], []
    n_dir = 0
    for p in a.files:
        if process(p, names, notes, layers, no_so=a.no_smart_objects) == 'dir':
            n_dir += 1

    result = {
        'files': a.files,
        'fonts': sorted(
            ({'name': k, 'seen_in': sorted(v)} for k, v in names.items()),
            key=lambda x: x['name'].lower()),
        'notes': notes,
    }
    if layers:
        # 逐层映射（图层文字 → 字体 → 字数）。坑四的误报就靠它复查：
        # 「FontSet 里声明过」与「真的分到字符」是两件事，这里只列后者。
        result['psd_layers'] = layers

    if not a.quiet:
        if result['fonts']:
            print('从 %d 个文件中提取到 %d 个字体名：\n'
                  % (len(a.files), len(result['fonts'])))
            for f in result['fonts']:
                print('  - %-38s  <- %s' % (f['name'], ', '.join(f['seen_in'])[:90]))
        else:
            print('未提取到任何字体名。这批素材可能只有成图，需走第二层字形比对。')
        for n in notes:
            print('  ! %s' % n)
        if a.psd_layers and layers:
            print('\nPSD 逐层用字（按 RunLengthArray 逐段归属，0 字的不列）：')
            for L in layers:
                desc = ' / '.join('%s %s' % (k, ('%d字' % v) if v else '字数未知')
                                  for k, v in L['fonts'].items())
                print('  · %-42s → %s%s'
                      % (L['text'] or '（无文字）', desc,
                         ('   [声明未用：%s]' % '、'.join(L['unused'])) if L['unused'] else ''))

    if a.json_out:
        with open(a.json_out, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        if not a.quiet:
            print('\n结果已写入: %s' % a.json_out)

    if n_dir:
        # 退出码 2 = 参数里有目录（区别于 1 = 有文件但没提取到字体名）
        print('\n⚠️ 有 %d 个参数是【目录】。本工具只接受文件，不会替你扫描整个文件夹 ——\n'
              '   审查范围只应是用户在本次消息里点名的那一个文件。' % n_dir)
        return 2

    return 0 if result['fonts'] else 1


if __name__ == '__main__':
    sys.exit(main())
