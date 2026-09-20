#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
第二层：字形比对（闭集问题）。

思路：公司已购的字体文件就在手里，所以不用去猜"这是什么字体"，
而是问一个更容易的问题 —— "图上这段字，最像我们已购清单里的哪一款？"

做法：把同一串文字用每个候选字体渲染出来，与图片上裁出的文字做像素级比对，
输出相似度排名。这是量化打分，不是肉眼判断。

用法（推荐：候选池直接从清单生成，不用手工维护路径）：
  python glyph_match.py <spec.json> --manifest assets/licensed-fonts.json [--out result.json]

  python glyph_match.py <spec.json> [--font-dir DIR]... [--min-coverage 0.6] [--allow-partial]

spec.json:
{
  "image": "C:/path/poster.png",
  "target_h": 64,
  "samples": [
    {"label": "主标题", "text": "全年无限学", "box": [0.05, 0.18, 0.62, 0.28]}
  ],
  "candidates": [
    {"label": "方正粗谭黑", "path": "FZCTHJW.TTF"},
    {"label": "微软雅黑",   "path": "msyh.ttc", "index": 0, "group": "control"}
  ],
  "top": 5
}

候选字体的 path 可以只写**文件名**（推荐）或写绝对路径，脚本都会自动定位；
给 --manifest 时，已购候选直接由清单生成，spec 里的 candidates 作补充（此时默认算对照组）。

⚠️ 候选池自检：本脚本会把「要把候选字体渲染成像素」这件事当成硬前提 ——
已购字体解析不到足够数量时**直接拒跑**（退出码 3），而不是照常输出一份排名。
因为缺字体不会报错，只会让排名字段 quietly 变成「矮子里拔将军」，
历史实测出现过把未购对照字体（微软雅黑）推上第 1 名的情况。

box 用 0-1 比例坐标 [x1, y1, x2, y2]，与图片分辨率无关。
依赖 Pillow。
"""
import argparse
import json
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from font_lookup import ENV_KEY, FontIndex, default_dirs, pad  # noqa: E402

# 未购对照字体：**故意放的"反例"**。
# 只拿已购字体当候选，等于让已购选手在自家赛道上跑 —— 万一图上用的是没买的字体，
# 它也会在已购里「矮子里拔将军」给出看似合理的答案。必须混入没买的通用字体当对照，
# 才可能发现「未购字体冲到了第一名」这个真信号。
#
# ⚠️ 对照池要**跨平台**：早期只列了 Windows 自带的 6 款，在 macOS / Linux 上
# 一个都找不到 → 对照池为空、脚本却照常出排名，等于把上面这条设计前提悄悄作废
# （实测 macOS：`未购对照 0/6 款`）。下面按 Windows → macOS → Linux 依次列候选，
# 缺的会被跳过，都缺时 report_pool() 会显式告警。
CONTROL_FONTS = [
    # Windows
    ('黑体',         'simhei.ttf'),
    ('微软雅黑',     'msyh.ttc'),
    ('微软雅黑粗体', 'msyhbd.ttc'),
    ('等线',         'Deng.ttf'),
    ('宋体',         'simsun.ttc'),
    ('楷体',         'simkai.ttf'),
    # macOS（系统自带中文字体；Hiragino Sans GB 为 CFF 轮廓，Pillow 可渲染）
    ('华文黑体',     'STHeiti Medium.ttc'),
    ('华文细黑',     'STHeiti Light.ttc'),
    ('华文宋体',     'Songti.ttc'),
    ('冬青黑体简',   'Hiragino Sans GB.ttc'),
    # Linux
    ('Noto Sans CJK', 'NotoSansCJK-Regular.ttc'),
    ('文泉驿正黑',    'wqy-zenhei.ttc'),
]

# 已购字体在本机的解析率低于这个值就拒跑。
# 不做成 1.0 是因为个别字体可能只在设计机上装过（如方正兰亭大黑），
# 缺一两款不影响排名可信度；但缺掉大半时结论会完全失真，必须拦住。
DEFAULT_MIN_COVERAGE = 0.6

PAD_X = 8   # 水平平移搜索范围（像素）
PAD_Y = 4   # 垂直平移搜索范围（像素）
NORM_H = 64  # 归一化墨迹高度
INK_T = 128  # 墨迹阈值


# ---------------------------------------------------------------- 基础工具
def otsu(gray):
    """Otsu 自动阈值，比固定阈值抗光照/背景干扰。"""
    h = gray.histogram()[:256]
    total = sum(h)
    if total == 0:
        return 128
    sum_all = sum(i * h[i] for i in range(256))
    sum_b = 0.0
    w_b = 0
    best, thr = -1.0, 128
    for i in range(256):
        w_b += h[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * h[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best:
            best, thr = var, i
    # 兜底：输入是纯二值图（只有 0 / 255 两个灰阶）时，Otsu 的最优点会落在**最小灰阶**上，
    # 于是 thr 返回 0，"v < thr" 取不到任何像素 —— 表现为"裁切区域内找不到文字，跳过"。
    # 实测：把样张先二值化（消除彩色字与黑色渲染之间的灰度偏差）就必踩这个坑。
    # 修法：阈值落在最小灰阶上时，取实际存在的最小/最大灰阶中点为阈值。
    nz = [i for i in range(256) if h[i] > 0]
    if len(nz) >= 2 and thr <= nz[0]:
        thr = (nz[0] + nz[-1]) // 2
    return thr


def border_is_light(gray):
    """判断底色明暗 —— 决定哪个方向算"墨迹"。深色底海报必须反相，否则全错。"""
    w, h = gray.size
    px = gray.load()
    pts = []
    sx = max(1, w // 40)
    sy = max(1, h // 40)
    for x in range(0, w, sx):
        pts.append(px[x, 0])
        pts.append(px[x, h - 1])
    for y in range(0, h, sy):
        pts.append(px[0, y])
        pts.append(px[w - 1, y])
    if not pts:
        return True
    return (sum(pts) / len(pts)) > 127


def ink_bbox(gray):
    """返回墨迹（文字）的 bbox。"""
    thr = otsu(gray)
    light = border_is_light(gray)
    if light:
        m = gray.point(lambda v: 255 if v < thr else 0)
    else:
        m = gray.point(lambda v: 255 if v >= thr else 0)
    m = m.convert('L')
    return m.getbbox()


def prep(gray):
    """裁到墨迹 bbox → 统一极性（墨迹=深，底=白）→ 按高度归一化（保长宽比）。

    返回 (归一化后的 L 图, 宽度)。极性统一很关键：深色底海报不反相会得到全错的结果。
    """
    bb = ink_bbox(gray)
    if not bb:
        return None, 0
    light = border_is_light(gray)
    g = gray.crop(bb)
    if not light:
        g = ImageChops.invert(g)
    w, h = g.size
    nw = max(4, int(round(w * NORM_H / max(h, 1))))
    return g.resize((nw, NORM_H), Image.LANCZOS), nw


def count_on(mask_l):
    """统计二值 L 图（0/255）里"开"的像素数。

    用 ImageStat 求和，不用 mode '1' 的 histogram —— 那个索引约定容易踩坑。
    """
    return int(round(ImageStat.Stat(mask_l).sum[0] / 255.0))


def bin_of(canvas_l):
    """从灰度图取墨迹掩膜（墨迹=255）。"""
    return canvas_l.point(lambda v: 255 if v < INK_T else 0).convert('L')


# ---------------------------------------------------------------- 载入字体
def load_font(path, size, index=0):
    """载入字体。.ttc 需要 index 参数，失败时回退不带 index 的调用。"""
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return None


def render_sample(path, index, text, target_h):
    """把 text 用指定字体渲染，缩放到墨迹高度 ≈ target_h，裁到墨迹 bbox。"""
    probe = 160
    f = load_font(path, probe, index)
    if f is None:
        return None, '无法载入字体文件'
    w = int(probe * (len(text) + 3) * 1.4) + 200
    im = Image.new('L', (max(w, 200), probe * 4), 255)
    ImageDraw.Draw(im).text((80, probe), text, font=f, fill=0)
    bb = ink_bbox(im)
    if not bb:
        return None, '该字体渲染不出这批字符（可能缺字）'
    h = bb[3] - bb[1]
    size = max(8, int(round(probe * target_h / max(h, 1))))
    f2 = load_font(path, size, index)
    if f2 is None:
        return None, '缩放后无法载入字体文件'
    w2 = int(size * (len(text) + 3) * 1.4) + 200
    im2 = Image.new('L', (max(w2, 200), size * 4), 255)
    ImageDraw.Draw(im2).text((80, size), text, font=f2, fill=0)
    bb2 = ink_bbox(im2)
    if not bb2:
        return None, '缩放后渲染失败'
    return im2.crop(bb2), None


# ---------------------------------------------------------------- 打分
def score_pair(ref, ref_w, cand, cand_w):
    """墨迹 IoU + 灰度平均绝对差，外加宽度比例惩罚（宽度差体现字面率差异）。"""
    W = max(ref_w, cand_w) + 2 * PAD_X
    H = NORM_H + 2 * PAD_Y

    base = Image.new('L', (W, H), 255)
    base.paste(ref, (PAD_X, PAD_Y))
    ref_bin = bin_of(base)
    ref_on = count_on(ref_bin)

    best = None
    for dx in range(-PAD_X, PAD_X + 1):
        for dy in range(-PAD_Y, PAD_Y + 1):
            c = Image.new('L', (W, H), 255)
            c.paste(cand, (PAD_X + dx, PAD_Y + dy))
            c_bin = bin_of(c)
            inter = count_on(ImageChops.multiply(ref_bin, c_bin))
            union = count_on(ImageChops.lighter(ref_bin, c_bin))
            iou = inter / union if union else 0.0
            if best is None or iou > best[0]:
                mad = ImageStat.Stat(ImageChops.difference(base, c)).mean[0]
                best = (iou, mad)

    iou, mad = best
    # 相似度 = 形状重叠 + 灰度接近，再乘宽度比例惩罚
    sim = 0.65 * iou + 0.35 * (1.0 - mad / 255.0)
    ratio = min(ref_w, cand_w) / max(ref_w, cand_w, 1)
    return sim * ratio, iou, mad, ratio, ref_on


# ---------------------------------------------------------------- 候选池构建
def _norm_entry(raw, default_group, default_label=None):
    """把 candidates 里的一项规整成 dict。允许写成纯字符串（向后兼容）。"""
    if isinstance(raw, str):
        e = {'path': raw}
    else:
        e = dict(raw)
    path = e.get('path') or ''
    e['path'] = path
    e.setdefault('index', 0)
    e.setdefault('label', default_label or os.path.basename(str(path).replace('\\', '/')))
    e.setdefault('group', default_group)
    return e


def build_candidates(spec, manifest, index, add_control=True):
    """构建候选池，返回 (可用候选, 统计)。

    候选有两个来源：
      1. --manifest 指定的《已购字体清单》→ 每款一条，group='licensed'
      2. spec['candidates'] → 手工补充；给了 manifest 时默认算对照组，否则默认算已购

    未购对照（CONTROL_FONTS）在两种情况下都会自动附加，它们不参与覆盖率计算。
    """
    licensed, control = [], []

    if manifest:
        for e in manifest.get('fonts', []):
            licensed.append({'label': e.get('family') or e.get('font_file', ''),
                             'target': e.get('font_file') or '',
                             'index': e.get('font_index', 0),
                             'group': 'licensed'})
        # 清单已覆盖已购，spec 里手工写的默认按对照组处理，避免重复计数
        spec_default_group = 'control'
    else:
        spec_default_group = 'licensed'

    for raw in (spec.get('candidates') or []):
        e = _norm_entry(raw, spec_default_group)
        (control if e['group'] == 'control' else licensed).append(e)

    if add_control:
        for label, fn in CONTROL_FONTS:
            control.append({'label': label, 'target': fn, 'index': 0, 'group': 'control'})

    cands, missing_l, missing_c = [], [], []
    seen = set()

    def _take(items, missing):
        for it in items:
            p = index.find(it.get('target') or it.get('path') or '')
            if not p:
                missing.append(it)
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            c = dict(it)
            c['path'] = p
            c.pop('target', None)
            cands.append(c)

    _take(licensed, missing_l)
    _take(control, missing_c)

    stats = {
        'licensed_total': len(licensed),
        'licensed_found': len(licensed) - len(missing_l),
        'control_total': len(control),
        'control_found': len(control) - len(missing_c),
        'licensed_missing': [{'label': m.get('label'), 'target': m.get('target') or m.get('path')}
                             for m in missing_l],
        'control_missing': [m.get('label') for m in missing_c],
    }
    stats['coverage'] = (stats['licensed_found'] / stats['licensed_total']
                         if stats['licensed_total'] else 0.0)
    return cands, stats


def report_pool(stats, min_coverage, allow_partial):
    """打印候选池体检结果。返回 True 表示可以继续比对。"""
    lt, lf = stats['licensed_total'], stats['licensed_found']
    print('候选池：已购 %d/%d 款（覆盖率 %.1f%%，门槛 %.0f%%）'
          % (lf, lt, stats['coverage'] * 100, min_coverage * 100))
    print('        未购对照 %d/%d 款（用于发现「图上用的是没买的字体」）'
          % (stats['control_found'], stats['control_total']))
    miss = stats['licensed_missing']

    if stats['control_found'] > lf:
        print('        ⚠️ 未购对照比已购还多 —— 已购字体基本没装上，排名没有意义。')

    if stats['control_total'] and stats['control_found'] == 0:
        # 对照池为空 = 「必须加未购对照字体」这条设计前提被悄悄作废：
        # 已购字体在自家赛道上跑，未购字体用上了也看不出来。
        # 不拒跑（已购侧结论仍可用），但必须显式说出来，不能让报告看着一切正常。
        print('        ⚠️ 未购对照池为空 —— 本机没有找到任何对照字体，'
              '「未购字体冲上第一」这个信号本次**无法发现**。')
        print('           处置：装任意一款未购通用中文字体（如思源黑体 / 微软雅黑 / 苹方），'
              '或用 spec 的 candidates 手工指定对照组。')

    if lt == 0 and stats['control_found'] == 0:
        print('\n没有任何可用的候选字体文件。')
        return False

    if lt == 0:
        print('\n⚠️ 候选池里一款已购字体都没有，无法判断素材是否用了已购字体。')
        if not allow_partial:
            print('   如确要看未购对照之间的相对排名，加 --allow-partial。')
            return False
        return True

    if stats['coverage'] >= min_coverage:
        return True

    # 逃生口：确实要在候选不全的情况下看排名（例如只想缩小范围）。
    # 这里必须**显式放行**，不能把上面那段拒跑提示照打一遍 —— 否则用户加了参数
    # 还是看到「自检未通过」，会以为参数没起作用。
    if allow_partial:
        print('\n⚠️ 候选池不全：已购字体只解析到 %d / %d 款（%.1f%% < 门槛 %.0f%%），'
              % (lf, lt, stats['coverage'] * 100, min_coverage * 100))
        print('   已按 --allow-partial 继续比对。结果**只能用于缩小范围，不可用于定款** ——')
        print('   缺席的 %d 款字体没有参与打分，排名天然偏向已装字体。' % len(miss))
        return True

    print('\n' + '=' * 60)
    print('❌ 候选池自检未通过：已购字体只解析到 %d / %d 款，低于门槛 %.0f%%。'
          % (lf, lt, min_coverage * 100))
    print('=' * 60)
    print('第二层的原理是「把候选字体渲染成像素再比」，缺字体就等于缺选手。')
    print('缺的时候**不会报错**，但排名会变成矮子里拔将军 ——')
    print('历史实测：未购对照的微软雅黑被推上第 1 名，看上去像一份正常报告。')
    print('\n本机缺的 %d 款：' % len(miss))
    for m in miss:
        print('  - %-24s %s' % (m['label'], m['target'] or '（清单未登记文件名）'))
    print('\n处置：把上列字体装进本机字体目录（装哪个目录都行），然后重跑；')
    print('      先跑一次体检可以看到完整清单与安装指引：')
    print('        python scripts/check_fonts.py <licensed-fonts.json>')
    print('      若字体装在非标准目录，用 --font-dir 指定，或设环境变量 %s。' % ENV_KEY)
    print('\n确要在候选不全的情况下查看排名（结果不可用于定款）：加 --allow-partial')
    return False


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('spec')
    ap.add_argument('--out', default=None)
    ap.add_argument('--manifest', default=None,
                    help='《已购字体清单》json；给了就自动生成已购候选，不必手工维护路径')
    ap.add_argument('--font-dir', action='append', default=None,
                    help='追加字体搜索目录，可重复；也可用环境变量 %s' % ENV_KEY)
    ap.add_argument('--min-coverage', type=float, default=DEFAULT_MIN_COVERAGE,
                    help='已购字体最低解析率，低于则拒跑（默认 %g）' % DEFAULT_MIN_COVERAGE)
    ap.add_argument('--allow-partial', action='store_true',
                    help='候选不全也照跑（结果不可用于定款，仅作参考）')
    a = ap.parse_args()

    with open(a.spec, encoding='utf-8') as f:
        spec = json.load(f)

    manifest = None
    if a.manifest:
        if not os.path.isfile(a.manifest):
            print('清单不存在: %s' % a.manifest)
            return 2
        with open(a.manifest, encoding='utf-8') as f:
            manifest = json.load(f)

    img_path = spec['image']
    if not os.path.isfile(img_path):
        print('图片不存在: %s' % img_path)
        return 2
    base = Image.open(img_path).convert('L')
    W, H = base.size
    target_h = int(spec.get('target_h', 64))
    top = int(spec.get('top', 5))

    index = FontIndex(default_dirs(a.font_dir))
    cands, stats = build_candidates(spec, manifest, index)
    if not report_pool(stats, a.min_coverage, a.allow_partial):
        return 3
    if not cands:
        return 2

    print('\n原图 %dx%d，候选字体 %d 款，样张 %d 处\n'
          % (W, H, len(cands), len(spec['samples'])))

    out = {'image': img_path, 'candidate_pool': stats, 'samples': []}
    for s in spec['samples']:
        x1, y1, x2, y2 = s['box']
        box = (int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H))
        label = s.get('label') or s['text']
        ref, ref_w = prep(base.crop(box))
        if ref is None or ref_w == 0:
            print('【%s】「%s」裁切区域内找不到文字，跳过。' % (label, s['text']))
            out['samples'].append({'label': label, 'text': s['text'],
                                   'error': 'no-ink', 'ranking': []})
            continue

        rows = []
        for c in cands:
            img, err = render_sample(c['path'], c['index'], s['text'], target_h)
            if img is None:
                rows.append({'label': c['label'], 'score': None, 'error': err})
                continue
            pc, cw = prep(img)
            if pc is None:
                rows.append({'label': c['label'], 'score': None, 'error': '渲染失败'})
                continue
            sc, iou, mad, ratio, _ = score_pair(ref, ref_w, pc, cw)
            rows.append({'label': c['label'], 'score': round(sc * 100, 1),
                         'iou': round(iou, 3), 'mad': round(mad, 1),
                         'w_ratio': round(ratio, 3), 'path': c['path']})

        rows.sort(key=lambda r: (r.get('score') is None, -(r.get('score') or 0)))
        print('【%s】「%s」  裁切 %dx%d'
              % (label, s['text'], box[2] - box[0], box[3] - box[1]))
        for i, r in enumerate(rows[:top], 1):
            if r.get('score') is None:
                print('  %d. %s %s' % (i, pad(r['label'], 26), r.get('error')))
            else:
                print('  %d. %s 相似度 %5.1f  (IoU %.3f / 灰度差 %5.1f / 宽比 %.2f)'
                      % (i, pad(r['label'], 26), r['score'], r['iou'], r['mad'], r['w_ratio']))
        print()

        out['samples'].append({'label': label, 'text': s['text'],
                               'box': s['box'], 'ranking': rows})

    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print('结果已写入: %s' % a.out)

    print('提示：相似度是量化的相对排名，用于缩小范围与交叉验证，')
    print('      不构成字体版权或商用授权的结论 —— 最终以授权书为准。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
