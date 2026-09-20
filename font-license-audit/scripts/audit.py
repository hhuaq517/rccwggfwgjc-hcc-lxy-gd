#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
第三段：把提取到的字体名与《已购字体清单》比对，输出分档结论。

关键认知：买过 ≠ 什么场景都能用。字体授权通常限定用途、主体、期限、地域，
所以要分档，而不是简单一句"未授权"：
  ✅ 在清单内且在授权范围
  ⚠️ 已购，但用途 / 期限 / 主体需核授权书
  ⛔ 已在「排除清单」内（来源存疑 / 改名冒用 / 明确禁用）—— 比普通 🚫 更严重
  🚫 当前清单中未见（如实列为风险项并给替代方案）

⛔ 与 🚫 的区别很重要：⛔ 是有据可查的禁用项，直接替换；
🚫 只是**当前清单里没有**，措辞写"当前清单中未见此款"，**不要写成"确定侵权"**。
（清单曾两次漏登：站酷酷黑、方正兰亭大黑。）
**但不要向用户追问"这款在不在公司授权包里"** —— 物料所属公司 2026-09-19 指定：
《已购字体清单》即唯一口径，新增已购字体由用户主动告知，届时补录重跑即可。
排除清单写在 licensed-fonts.json 的 `_excluded` 里。

用法：
  python audit.py --extracted extracted.json --licensed licensed-fonts.json \
                  --out 报告.md [--glyph glyph_result.json] [--media 线上线下全平台]
"""
import argparse
import datetime as dt
import json
import re
import sys

# 投放媒体口径：**本批素材固定为「线上线下全平台」**（物料所属公司 2026-09-19 指定），
# 因此不再逐次询问 --media，默认值即全平台。判授权时要求**线上与线下同时覆盖**。
# 「全媒体」视为线上线下通吃；只写单侧的（如仅"网络"或仅"印刷"）不算覆盖全平台。
ONLINE_KEYS = ('网络', '线上', '互联网', '新媒体', '公众号', '电商', '详情页', '朋友圈')
OFFLINE_KEYS = ('印刷', '线下', '纸质', '户外', '海报', '易拉宝', '展架', 'KT板', '灯箱', '传单', '校园')
ALL_MEDIA_KEYS = ('全媒体', '全平台', '不限媒体', '各媒体', '线上线下')
DEFAULT_MEDIA = '线上线下全平台'
# 这些写法表示「无到期日」，不必逐条提醒人工确认（否则报告里每行都挂一句噪声）
OPEN_ENDED = ('长期', '永久', '不限', '无期限', '无限期', '长期有效', 'n/a', 'na', '-', 'none')


def norm(s):
    """归一化：小写、去掉一切非字母数字与汉字。"""
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]', '', (s or '').lower())


def match_font(name, entry):
    """返回 (匹配方式, 命中的别名)。exact 优先，其次子串模糊匹配。"""
    n = norm(name)
    if not n:
        return None, None
    cands = [entry.get('family', '')] + list(entry.get('aliases') or [])
    for c in cands:
        if norm(c) and norm(c) == n:
            return 'exact', c
    for c in cands:
        cn = norm(c)
        if len(cn) >= 4 and (cn in n or n in cn):
            return 'fuzzy', c
    return None, None


def match_excluded(name, excluded):
    """命中「排除清单」则返回该条目，否则 None。

    排除清单是**禁用项**（改名冒用 / 来源存疑），不是"没登记"。
    模糊匹配阈值比 match_font 更严（长名 >= 8），避免把清单内的正规字体误判成禁用。
    """
    n = norm(name)
    if not n:
        return None
    for e in excluded or []:
        for c in e.get('names') or []:
            cn = norm(c)
            if not cn:
                continue
            if cn == n or (len(cn) >= 8 and (cn in n or n in cn)):
                return e
    return None


def parse_date(s):
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y-%m', '%Y'):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def media_scope(*texts):
    """把媒体写法归纳为 (是否覆盖线上, 是否覆盖线下)。

    「全媒体 / 全平台 / 线上线下」这类写法两边都算覆盖；
    只写单侧的（如仅"网络"、仅"印刷"）只覆盖对应一侧。
    """
    joined = ''.join(t or '' for t in texts)
    online = any(k in joined for k in ONLINE_KEYS)
    offline = any(k in joined for k in OFFLINE_KEYS)
    if any(k in joined for k in ALL_MEDIA_KEYS):
        online = offline = True
    return online, offline


def check_license(lic, asset_media):
    """返回 (档位, 降档原因, 提示信息)。

    档位：ok / warn。
    只有「用途不覆盖」和「已过期」才降档；note 与"长期"这类表述只作提示，
    否则会把本来合规的字体全部误报成需核。
    """
    warn, notes = [], []
    media = lic.get('media') or []
    if media:
        cap_on, cap_off = media_scope(*media)
        need_on, need_off = media_scope(asset_media)
        missing = []
        if need_on and not cap_on:
            missing.append('线上')
        if need_off and not cap_off:
            missing.append('线下')
        if missing:
            warn.append('授权媒体为 %s，未覆盖本批素材的%s投放'
                        % ('/'.join(media), '+'.join(missing)))
    else:
        warn.append('授权书未登记媒体范围')

    vu_raw = lic.get('valid_until')
    vu = parse_date(vu_raw)
    if not vu_raw:
        warn.append('授权期限未登记')
    elif str(vu_raw).strip().lower() in OPEN_ENDED:
        pass                      # 登记为「长期」，不产生噪声提示
    elif vu is None:
        notes.append('授权期限记为「%s」，无法自动判定，请人工确认' % vu_raw)
    elif vu < dt.datetime.now():
        warn.append('授权已于 %s 到期' % vu_raw)

    if lic.get('note'):
        notes.append(lic['note'])
    if lic.get('subject'):
        notes.append('授权主体：%s' % lic['subject'])

    return ('ok' if not warn else 'warn'), warn, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--extracted', required=True, help='extract_fonts.py 输出的 json')
    ap.add_argument('--licensed', required=True, help='已购字体清单 json')
    ap.add_argument('--glyph', default=None, help='glyph_match.py 输出的 json（可选）')
    ap.add_argument('--media', default=DEFAULT_MEDIA,
                    help='本批物料的传播媒体，默认「线上线下全平台」（已按此固定，无需逐次指定）')
    ap.add_argument('--out', default=None, help='输出 markdown 报告路径')
    a = ap.parse_args()

    ex = json.load(open(a.extracted, encoding='utf-8'))
    lic = json.load(open(a.licensed, encoding='utf-8'))
    fonts = ex.get('fonts', [])
    entries = lic.get('fonts', [])
    excluded = lic.get('_excluded', [])

    used_alias = set()
    ok, warn, bad, blocked = [], [], [], []
    for f in fonts:
        name = f['name']
        line = {'name': name, 'seen_in': f.get('seen_in', [])}
        # 先查排除清单：命中的直接判禁用，不再进已购匹配（排除项永远不该被"命中已购"洗白）
        hx = match_excluded(name, excluded)
        if hx:
            line['excluded'] = hx
            blocked.append(line)
            continue
        # 收集全部命中，再按「exact 优先 → 命中的别名更长（更具体）优先 → 清单里靠前优先」选。
        # 不能取「最后一条命中」：兰亭黑 / 兰亭粗黑 / 兰亭特黑这类同族字体，别名互为子串，
        # 取最后一条会随机落到错的那一款（例如把粗黑判成普通黑）。
        cands = []
        for idx, e in enumerate(entries):
            how, alias = match_font(name, e)
            if how:
                cands.append((0 if how == 'exact' else 1,
                              -len(norm(alias or '')), idx, e, how, alias))
        if not cands:
            bad.append(line)
            continue
        cands.sort(key=lambda t: (t[0], t[1], t[2]))
        _, _, _, e, how, alias = cands[0]
        others = sorted({c[3].get('family') for c in cands[1:]
                         if c[3].get('family') and c[3].get('family') != e.get('family')})
        used_alias.add(e.get('family'))
        line['matched'] = e.get('family')
        line['how'] = how
        line['alias'] = alias
        line['license'] = e.get('license', {})
        level, reasons, notes = check_license(line['license'], a.media)
        if others and how != 'exact':
            # 只有「没命中精确名、靠模糊子串才匹配上」时才提示歧义；
            # 精确命中不存在疑问，提示了只是噪声。
            notes.append('另有 %d 款清单字体被同名模糊命中（%s），结论存疑时请人工确认'
                         % (len(others), '、'.join(others[:3])))
        line['reasons'] = reasons
        line['notes'] = notes
        (ok if level == 'ok' else warn).append(line)

    # 报告
    L = []
    L.append('# 字体版权比对报告')
    L.append('')
    L.append('- **公司**：%s' % lic.get('company', '（未填）'))
    L.append('- **物料媒体**：%s' % a.media)
    L.append('- **清单版本**：%s' % lic.get('updated', '（未填）'))
    L.append('- **生成时间**：%s' % dt.datetime.now().strftime('%Y-%m-%d %H:%M'))
    L.append('- **比对素材**：%d 个文件，提取到 %d 个字体名' % (len(ex.get('files', [])), len(fonts)))
    L.append('')
    L.append('| 档位 | 数量 |')
    L.append('|---|---|')
    L.append('| ✅ 在清单内且在授权范围 | %d |' % len(ok))
    L.append('| ⚠️ 已购，但需核授权书 | %d |' % len(warn))
    L.append('| 🚫 当前清单中未见 | %d |' % (len(bad) + len(blocked)))
    L.append('')
    if blocked:
        L.append('> ⛔ 其中 **%d** 个命中「已知排除清单」（改名冒用 / 来源存疑），'
                 '属有据可查的禁用项，**应立即替换**，见下方专节。' % len(blocked))
        L.append('')
    if bad:
        L.append('> 🚫 剩余 **%d** 个只是**当前清单里没有登记**。'
                 '**按风险项如实列出并给替代方案即可，不必回头向用户确认"是否已购"** —— '
                 '《已购字体清单》即唯一口径，新增已购字体由用户主动告知，'
                 '届时补录清单、重跑比对即可刷新结论。' % len(bad))
        L.append('')
    L.append('> 清单覆盖：本次命中 **%d / %d** 款已购字体。'
             '清单里未出现的 %d 款，说明本批物料没用到，不在本报告中列出。'
             % (len(used_alias), len(entries), max(0, len(entries) - len(used_alias))))
    L.append('')

    n_open = sum(1 for e in entries
                 if str((e.get('license') or {}).get('valid_until', '')).strip().lower()
                 in OPEN_ENDED)
    if n_open:
        L.append('> 期限提示：清单中 **%d 款**登记为「长期」（无到期日），'
                 '故本报告不对其作期限风险标注；无到期日的授权仍以授权书原文为准。' % n_open)
        L.append('')

    if blocked:
        L.append('## ⛔ 已知排除字体（高风险 · 立即替换）')
        L.append('')
        L.append('这些字体已确认不在授权范围内或来源存疑，**不得用于任何物料**：')
        L.append('')
        for x in blocked:
            e = x['excluded']
            L.append('- **%s**' % x['name'])
            if e.get('recorded_as'):
                L.append('  - 标称：%s' % e['recorded_as'])
            if e.get('actual_font'):
                L.append('  - 实际字体：%s' % e['actual_font'])
            if e.get('vendor'):
                L.append('  - 版权方：%s' % e['vendor'])
            if e.get('reason'):
                L.append('  - 排除原因：%s' % e['reason'])
            if x.get('seen_in'):
                L.append('  - 出现位置：%s' % ', '.join(x['seen_in'])[:160])
            if e.get('status'):
                L.append('  - 状态：%s' % e['status'])
            L.append('  - 处置：**立即替换为清单内字体**，并同步修改设计源文件')
        L.append('')

    if bad:
        L.append('## 🚫 当前清单中未见此款（处置：替换 / 补购）')
        L.append('')
        for x in bad:
            L.append('- **%s**' % x['name'])
            L.append('  - 出现位置：%s' % ', '.join(x['seen_in'])[:160])
            L.append('  - 处置建议：替换为清单内的同类字体；若确需保留，补购授权后再用')
        L.append('')

    if warn:
        L.append('## ⚠️ 已购，但需核授权书')
        L.append('')
        for x in warn:
            L.append('- **%s** → 命中清单项「%s」(%s匹配)' % (x['name'], x['matched'], x['how']))
            for r in x['reasons']:
                L.append('  - 需核原因：%s' % r)
            for n in x.get('notes', []):
                L.append('  - %s' % n)
        L.append('')

    if ok:
        L.append('## ✅ 在清单内且在授权范围')
        L.append('')
        for x in ok:
            L.append('- **%s** → 「%s」(%s匹配)' % (x['name'], x['matched'], x['how']))
            for n in x.get('notes', []):
                L.append('  - %s' % n)
        L.append('')

    if ex.get('psd_layers'):
        # 逐层用字是坑四的复核依据：把「图层 → 字体 → 字数」随报告一起交付，
        # 结论才可核查（只写"用了某款字"、不写在哪一层用了多少字，没法复核）。
        L.append('## 🧩 PSD 逐层用字（按 `/RunLengthArray` 逐段归属，0 字的 run 不计）')
        L.append('')
        L.append('| 图层文字 | 实际用字（字数） | 字表里声明但未用 |')
        L.append('|---|---|---|')
        for x in ex['psd_layers']:
            used = '、'.join('%s %s' % (k, ('%d 字' % v) if v else '字数未知')
                            for k, v in (x.get('fonts') or {}).items())
            unused = '、'.join(x.get('unused') or []) or '—'
            txt = (x.get('text') or '（无文字）').replace('|', '\\|')
            L.append('| %s | %s | %s |' % (txt, used, unused))
        L.append('')
        L.append('> 同一层出现多款字是**正常现象**：run 边界切在中英混排处，'
                 '数字/标点常留在原字体上（实测「全款大额优惠叠加…」里的数字 `1` 就指向另一款字）。'
                 '「声明但未用」是 Photoshop 留下的僵尸 run，**不参与比对**，列出来便于人工复核。')
        L.append('')

    if ex.get('notes'):
        L.append('## 📄 提取备注')
        L.append('')
        for n in ex['notes']:
            L.append('- %s' % n)
        L.append('')

    if a.glyph:
        g = json.load(open(a.glyph, encoding='utf-8'))
        L.append('## 🔬 字形比对记录（第二层）')
        L.append('')
        # 候选池健康状况必须随报告一起走 —— 缺字体时排名会失真，
        # 只把名次抄进报告、不说明候选池缺了多少，等于给出一个无法复核的结论。
        pool = g.get('candidate_pool') or {}
        if pool:
            lt = pool.get('licensed_total', 0)
            lf = pool.get('licensed_found', 0)
            cov = (lf / lt * 100) if lt else 0.0
            L.append('> **候选池**：已购字体在本机解析到 **%d / %d** 款（覆盖率 %.1f%%），'
                     '未购对照 %d / %d 款。'
                     % (lf, lt, cov, pool.get('control_found', 0),
                        pool.get('control_total', 0)))
            miss = pool.get('licensed_missing') or []
            if miss:
                L.append('>')
                L.append('> ⚠️ 有 **%d 款**已购字体未在本机找到：%s。'
                         '这些字体没有参与打分，**排名会向已装字体偏移，本次结果不可用于定款**；'
                         '补齐字体后重跑本报告。'
                         % (len(miss), '、'.join(m.get('label', '') for m in miss)))
            L.append('')
        for s in g.get('samples', []):
            L.append('**%s**「%s」' % (s.get('label', ''), s.get('text', '')))
            L.append('')
            L.append('| 排名 | 候选字体 | 相似度 |')
            L.append('|---|---|---|')
            for i, r in enumerate(s.get('ranking', [])[:5], 1):
                sc = r.get('score')
                L.append('| %d | %s | %s |' % (i, r.get('label', ''),
                                               ('%.1f' % sc) if sc is not None else (r.get('error') or '-')))
            L.append('')

    L.append('---')
    L.append('')
    L.append('**说明**：本报告基于源文件元数据（确定性）与字形比对（量化打分）得出，')
    L.append('用于风险筛查与定位。**结论以本次比对所用的《已购字体清单》为准**，'
             '清单更新后重跑比对即可刷新。')
    L.append('最终授权结论以字体权利方出具的授权书为准。')
    text = '\n'.join(L)

    print('比对完成：✅ %d  /  ⚠️ %d  /  🚫 %d  （其中 ⛔ 已知排除 %d）'
          % (len(ok), len(warn), len(bad) + len(blocked), len(blocked)))
    if blocked:
        print('\n⛔ 已知排除字体（高风险，立即替换）：')
        for x in blocked:
            print('  - %s  →  实际是 %s' % (x['name'], x['excluded'].get('actual_font', '?')))
    if bad:
        print('\n当前清单中未见此款（处置：替换为清单内同类字体）：')
        for x in bad:
            print('  - %s' % x['name'])
    if warn:
        print('\n已购但需核授权书：')
        for x in warn:
            print('  - %s  (%s)' % (x['name'], '；'.join(x['reasons'])[:100]))
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(text)
        print('\n报告已写入: %s' % a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
