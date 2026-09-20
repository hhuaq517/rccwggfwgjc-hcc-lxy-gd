#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
读字体文件（ttf / otf / ttc）name 表里的真实名称，用来填《已购字体清单》的 aliases。

为什么需要它：靠记忆猜 PostScript 名（FZCuTanHeiJW ？ FZCTHJW ？）会漏匹配，
漏匹配的后果是合规字体被误判成「不在清单内」。字体文件里写死的名字才是准的。

坑（已在代码里处理）：同一款字体在 name 表里有多条记录，不同平台/语言各一份。
很多中文字体的英文（0x0409）记录是乱码，正确的名称在简中（0x0804）记录里，
所以必须按「语言优先 + 解码质量」打分选最优，不能取第一条。

用法：
  python font_info.py <字体文件或目录> [更多路径...] [--json out.json]
  python font_info.py "C:/Users/x/AppData/Local/Microsoft/Windows/Fonts" --json fonts.json
  python font_info.py "xxx.ttf" --all-names     # 打印全部 nameID（版权、厂商、设计者等）
"""
import argparse
import glob
import json
import os
import struct
import sys

# name 表 nameID 含义
NAME_IDS = {
    0: 'copyright', 1: 'family', 2: 'subfamily', 3: 'unique_id', 4: 'full_name',
    5: 'version', 6: 'postscript', 7: 'trademark', 8: 'manufacturer', 9: 'designer',
    11: 'vendor_url', 12: 'designer_url', 13: 'license_desc', 14: 'license_url',
    16: 'typo_family', 17: 'typo_subfamily', 18: 'compatible_full', 19: 'sample',
}
# 主要字段（会写进 json 的 top-level 键）
MAIN_IDS = (1, 2, 4, 6, 16, 17)

FONT_EXT = ('.ttf', '.otf', '.ttc', '.otc', '.ttf2')


def _decode(raw, platform_id):
    """按平台解码 name 记录。Windows/Unicode 用 UTF-16BE，Mac 用 latin-1。"""
    if platform_id in (0, 3):
        try:
            return raw.decode('utf-16-be', errors='replace').strip('\x00').strip()
        except Exception:
            return ''
    if platform_id == 1:
        try:
            return raw.decode('latin-1', errors='replace').strip()
        except Exception:
            return ''
    return raw.decode('utf-8', errors='replace').strip()


def _lang_score(platform_id, language_id):
    """给一条 name 记录定语言优先级：简中 > 繁中 > 英文 > 其他 Windows > Mac。"""
    if platform_id == 3:
        if language_id == 0x0804:          # zh-CN
            return 50
        if language_id in (0x0404, 0x0C04, 0x1404):   # zh-TW / zh-HK / zh-MO
            return 45
        if language_id == 0x0409:          # en-US
            return 40
        return 30
    if platform_id == 0:
        return 20
    if platform_id == 1:
        return 10
    return 5


def _quality_penalty(value):
    """乱码惩罚：含替换字符、含控制字符的候选要被打下去。"""
    p = 0
    if '\ufffd' in value:
        p += 100
    if any(ord(c) < 32 and c not in '\t\n' for c in value):
        p += 50
    return p


def _parse_name_table(data, off):
    """解析 name 表，返回 {nameID: [值...]}（已按语言优先级 + 解码质量排序）。"""
    out = {}
    if len(data) < off + 6:
        return out
    try:
        fmt, count, str_off = struct.unpack('>HHH', data[off:off + 6])
    except struct.error:
        return out
    if fmt not in (0, 1):
        return out
    base = off + 6
    scored = {}
    for i in range(count):
        rec = base + i * 12
        try:
            pid, eid, lid, nid, ln, o = struct.unpack('>HHHHHH', data[rec:rec + 12])
        except struct.error:
            break
        if nid not in NAME_IDS:
            continue
        raw = data[off + str_off + o: off + str_off + o + ln]
        val = _decode(raw, pid)
        if not val:
            continue
        sc = _lang_score(pid, lid) - _quality_penalty(val)
        scored.setdefault(nid, []).append((sc, val))
    for nid, lst in scored.items():
        # 去重后按分数降序
        seen, ordered = set(), []
        for sc, v in sorted(lst, key=lambda t: -t[0]):
            if v not in seen:
                seen.add(v)
                ordered.append(v)
        out[nid] = ordered
    return out


def _sfnt_offsets(data):
    """返回文件里每个字体（face）的起始偏移。ttc 会有多个。"""
    if data[:4] == b'ttcf':
        try:
            n = struct.unpack('>I', data[8:12])[0]
            return list(struct.unpack('>%dI' % n, data[12:12 + 4 * n]))
        except struct.error:
            return []
    return [0]


def read_font(path, all_names=False):
    """返回该文件里每个 face 的名称字典列表。"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        return [{'error': str(e), 'file': os.path.basename(path), 'path': path}]
    faces = []
    for fi, off in enumerate(_sfnt_offsets(data)):
        try:
            num_tables = struct.unpack('>H', data[off + 4:off + 6])[0]
        except struct.error:
            faces.append({'error': 'not a sfnt font', 'file': os.path.basename(path)})
            continue
        tables, ok = {}, True
        for i in range(num_tables):
            rec = off + 12 + i * 16
            try:
                tag, _chk, toff, tlen = struct.unpack('>4sIII', data[rec:rec + 16])
            except struct.error:
                ok = False
                break
            tables[tag] = toff
        if not ok or b'name' not in tables:
            faces.append({'error': 'no name table', 'file': os.path.basename(path)})
            continue
        names = _parse_name_table(data, tables[b'name'])
        info = {'face_index': fi, 'file': os.path.basename(path), 'path': path}
        for nid, key in NAME_IDS.items():
            vals = names.get(nid)
            if not vals:
                continue
            if nid in MAIN_IDS or all_names:
                info[key] = vals[0]
                info[key + '_all'] = vals
        faces.append(info)
    return faces


def _iter_font_files(paths):
    seen = set()
    for p in paths:
        if os.path.isdir(p):
            for ext in ('*.ttf', '*.otf', '*.ttc', '*.otc', '*.TTF', '*.OTF', '*.TTC'):
                for f in sorted(glob.glob(os.path.join(p, ext))):
                    rp = os.path.realpath(f)
                    if rp not in seen:
                        seen.add(rp)
                        yield f
        elif os.path.isfile(p):
            rp = os.path.realpath(p)
            if rp not in seen:
                seen.add(rp)
                yield p
        else:
            yield None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='+', help='字体文件或目录')
    ap.add_argument('--json', default=None, help='结果写入 json')
    ap.add_argument('--filter', default=None,
                    help='只输出 family/full_name/postscript 里含该关键词的字体')
    ap.add_argument('--all-names', action='store_true',
                    help='打印全部 nameID（版权、厂商、设计者等），用于追溯来源')
    a = ap.parse_args()

    results, missing = [], []
    for p in _iter_font_files(a.paths):
        if p is None:
            missing.append(p)
            continue
        results.extend(read_font(p, all_names=a.all_names))

    if a.filter:
        kw = a.filter.lower()
        results = [r for r in results
                   if any(kw in str(r.get(k, '')).lower()
                          for k in ('family', 'full_name', 'postscript', 'typo_family'))]

    for r in results:
        if r.get('error'):
            print('!! %s : %s' % (r.get('file'), r['error']))
            continue
        print('%-40s | family=%-26s | sub=%-12s | PS=%s'
              % (r['file'], r.get('family', ''), r.get('subfamily', ''),
                 r.get('postscript', '')))
        if r.get('full_name') and r.get('full_name') != r.get('family'):
            print('%40s   full=%s' % ('', r['full_name']))
        if a.all_names:
            for key in ('copyright', 'trademark', 'manufacturer', 'designer',
                        'license_desc', 'version', 'unique_id'):
                if r.get(key):
                    print('%40s   %-13s %s' % ('', key + ':', r[key]))

    print('\n共读取 %d 个 face' % len(results))

    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump({'faces': results}, f, ensure_ascii=False, indent=2)
        print('已写入: %s' % a.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
