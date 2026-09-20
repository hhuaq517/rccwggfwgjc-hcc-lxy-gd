#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
换电脑后的**第一步**：体检本机装没装清单里的已购字体。

为什么要有这个脚本：
第二层（字形比对）必须把候选字体**渲染成像素**才能和成图比对，所以它硬依赖字体文件。
换了电脑、字体没跟过去时，会出现两种后果：

  - 候选一款都解析不到 → 报错退出，能察觉；
  - **只缺已购、系统自带字体还在 → 不报错，照常排出名次**，
    把本该当「未购对照」的微软雅黑推到第 1 名 —— 这个才危险。

所以「装上字体」和「确认装上了」是两件事，本脚本负责后者：
告诉你在本机找到了哪几款、还缺哪几款、该装进哪个目录。

用法：
  python check_fonts.py <licensed-fonts.json> [--font-dir DIR] [--json out.json]

退出码：0 = 覆盖率达标（第二层可用）；1 = 覆盖率不足（应补齐字体后重跑）；2 = 清单读不了
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from font_lookup import ENV_KEY, FontIndex, basename_of, default_dirs, pad  # noqa: E402

# 默认门槛：清单里的已购字体至少要有这么大比例能在本机找到，第二层才可信。
# 定 0.6 而不是 1.0，是因为个别字体确实可能只在设计机上装过（例如方正兰亭大黑），
# 缺一两款不影响整体排名的可信度；但缺掉大半时，排名会变成「矮子里拔将军」，必须拦住。
DEFAULT_MIN_COVERAGE = 0.6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('manifest', help='已购字体清单 json（assets/licensed-fonts.json）')
    ap.add_argument('--font-dir', action='append', default=None,
                    help='追加字体搜索目录，可重复；也可用环境变量 %s' % ENV_KEY)
    ap.add_argument('--min-coverage', type=float, default=DEFAULT_MIN_COVERAGE,
                    help='已购字体的最低覆盖率，低于则判为不可用（默认 %g）' % DEFAULT_MIN_COVERAGE)
    ap.add_argument('--json', dest='json_out', default=None, help='结果写入 json')
    a = ap.parse_args()

    if not os.path.isfile(a.manifest):
        print('清单不存在: %s' % a.manifest)
        return 2
    with io.open(a.manifest, encoding='utf-8') as f:
        lic = json.load(f)

    dirs = default_dirs(a.font_dir)
    idx = FontIndex(dirs)

    print('字体版权比对 · 本机字体体检')
    print('=' * 56)
    print('清单：%s（更新于 %s）' % (os.path.basename(a.manifest), lic.get('updated', '未填')))
    print('\n扫描目录（%d 个，存在 %d 个）：' % (len(dirs), len(idx.existing_dirs())))
    for d in dirs:
        print('  %s %s' % ('[有]' if os.path.isdir(d) else '[无]', d))
    print('  索引到字体文件 %d 个' % idx.count())

    entries = lic.get('fonts', [])
    found, missing = [], []
    for e in entries:
        fam = e.get('family', '（未命名）')
        tgt = e.get('font_file') or ''
        p = idx.find(tgt)
        (found if p else missing).append((fam, tgt, p, e.get('vendor', '')))

    total = len(entries)
    cov = (len(found) / total) if total else 0.0

    # 模板体检：清单还是「示例数据」时先说清楚，否则使用者会照着"缺失的 N 款"
    # 去找根本不存在的 `示例字体甲`，白跑一趟。
    # 判据只看**明确是占位符**的写法（示例/请替换/填公司全称），不做模糊猜测。
    def _is_placeholder(s):
        s = str(s or '')
        return ('示例' in s) or ('请替换' in s) or ('填公司' in s) or \
               s.upper().startswith('EXAMPLE') or ('（填' in s)
    placeholder = [e.get('family') for e in entries if _is_placeholder(e.get('family'))] + \
                  [e.get('font_file') for e in entries
                   if str(e.get('font_file') or '').upper().startswith('EXAMPLE')]
    if placeholder or _is_placeholder(lic.get('company')):
        print('\n' + '=' * 56)
        print('⚠️ 本清单看起来还是**示例模板**，不是本单位真实清单。')
        print('   下面的「缺失 N 款」是模板里的占位条目，**不用去找、也装不上**。')
        print('   正确做法：先用 font_info.py 读出本机字体的真实名称，'
              '替换 assets/licensed-fonts.json 里的 fonts 数组，再重跑本脚本。')
        print('     python scripts/font_info.py <字体目录或文件> --json fonts.json')
        print('=' * 56)

    print('\n' + '-' * 56)
    print('已购字体 %d 款，本机找到 %d 款，覆盖率 %.1f%%（门槛 %.0f%%）'
          % (total, len(found), cov * 100, a.min_coverage * 100))

    if cov >= a.min_coverage:
        print('结论：第二层字形比对【可用】。')
    else:
        print('结论：第二层字形比对【不可用】—— 候选池太不完整，')
        print('      强行比对会把系统自带字体排到前面，给出看似正常但错误的结论。')

    if missing:
        print('\n缺失的 %d 款（把它们装到上面任意一个「[有]」的目录即可）：' % len(missing))
        for fam, tgt, _p, vendor in missing:
            print('  - %s %s  %s' % (pad(fam, 26), pad(tgt or '（清单未登记文件名）', 38), vendor))
        print('\n  说明：装好后重跑本脚本确认；文件名需与上表一致（大小写在 Windows 上不敏感）。')
    else:
        print('\n清单内所有字体均已在本机找到。')

    # 排除项：排除 ≠ 卸载。文件还在时提醒一句，避免误以为已经删干净。
    # 按条目归并 —— 同一条排除项常有多个文件（01/02），逐文件打印会重复同一行标题。
    excluded = lic.get('_excluded', [])
    still = []
    for e in excluded:
        hits = [basename_of(p) for p in (e.get('files') or []) if idx.find(p)]
        if hits:
            still.append((e.get('label', '（未命名）'), hits))
    if still:
        n_files = sum(len(h) for _l, h in still)
        print('\n⚠️ 排除清单里的字体文件仍有 %d 个装在本机（清单层面已禁用，未卸载）：' % n_files)
        for label, hits in still:
            print('  - %s → %s' % (label, '、'.join(hits)))
        print('  设计软件的字体列表里仍能选到它们，注意不要误用。')

    if a.json_out:
        with io.open(a.json_out, 'w', encoding='utf-8') as f:
            json.dump({
                'manifest': a.manifest,
                'total': total,
                'found': [{'family': f, 'target': t, 'path': p, 'vendor': v}
                          for f, t, p, v in found],
                'missing': [{'family': f, 'target': t, 'vendor': v}
                            for f, t, _p, v in missing],
                'coverage': round(cov, 4),
                'min_coverage': a.min_coverage,
                'usable': cov >= a.min_coverage,
                'font_dirs': dirs,
            }, f, ensure_ascii=False, indent=2)
        print('\n结果已写入: %s' % a.json_out)

    return 0 if cov >= a.min_coverage else 1


if __name__ == '__main__':
    sys.exit(main())
