#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
第二层的入口脚本：从**成图**里把样张抠干净，供 glyph_match.py 比对。

为什么需要这一步（实测踩过）：
推图上的文字通常是彩色的（红字/黄字/白字），而候选字体渲染出来是纯黑。
直接比对时，灰度差（mad）会被"颜色不同"系统性拉大 —— 实测把所有候选的
相似度压在 45~64 之间、第一名与第二名只差 1~5 分，区分度被完全吃掉。
做法：先把样张**二值化**（墨迹=黑 0 / 底=白 255），颜色差异归零，再用
glyph_match.py 比对，此时比的就真的是字形。

用法：
  python extract_samples.py <spec.json> [--out-dir DIR] [--scale 4]

输入 spec.json 与 glyph_match.py 同格式（image / samples / candidates），
其中 samples[].box 是**大致**框住文字的区域即可，脚本会自动二值化并裁到墨迹。

输出：
  <out-dir>/bw_samples.png   二值化后的干净样张拼图
  <out-dir>/spec_bw.json     指向拼图的新 spec，可直接喂给 glyph_match.py

两个关键处理：
1. **4x 超采样后再二值化** —— 原图字号常常只有 40~100px，直接二值化边缘锯齿重、
   字形失真。先放大再取阈值，边缘更平滑（等价于抗锯齿后再硬化）。
2. **极性自动判断** —— 白底红字 与 红底白字 都要能处理，用 border_is_light()
   看区域边缘明暗决定"哪一侧算墨迹"，否则深色底样张会整个反相。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from glyph_match import otsu, border_is_light  # noqa: E402

from PIL import Image  # noqa: E402


def extract_tile(im, box_px, scale):
    """裁 → 超采样 → Otsu 二值化（自动极性）→ 裁到墨迹。返回 L 图。"""
    g = im.crop(box_px)
    if g.width < 4 or g.height < 4:
        return None, None
    thr = otsu(g)
    light = border_is_light(g)
    big = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    if light:
        bw = big.point(lambda v: 0 if v < thr else 255)      # 底亮 → 暗的算墨迹
    else:
        bw = big.point(lambda v: 255 if v < thr else 0)      # 底暗 → 亮的算墨迹
    mask = bw.point(lambda v: 255 if v < 128 else 0)
    bb = mask.getbbox()
    if not bb:
        return None, None
    return bw.crop(bb), {'thr': thr, 'light': light}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('spec')
    ap.add_argument('--out-dir', default=None)
    ap.add_argument('--scale', type=int, default=4)
    ap.add_argument('--gap', type=int, default=40)
    a = ap.parse_args()

    with open(a.spec, encoding='utf-8') as f:
        spec = json.load(f)

    img_path = spec['image']
    if not os.path.isfile(img_path):
        print('图片不存在: %s' % img_path)
        return 2
    im = Image.open(img_path).convert('L')
    W, H = im.size
    out_dir = a.out_dir or os.path.dirname(os.path.abspath(a.spec))
    os.makedirs(out_dir, exist_ok=True)

    tiles = []
    for s in spec['samples']:
        x1, y1, x2, y2 = s['box']
        px = (int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H))
        bw, meta = extract_tile(im, px, a.scale)
        if bw is None:
            print('【%s】「%s」区域内找不到文字' % (s.get('label'), s.get('text')))
            continue
        tiles.append((s.get('label') or s['text'], s['text'], bw))
        print('%-24s 阈值%3d 底亮=%-5s -> %dx%d'
              % (s.get('label'), meta['thr'], meta['light'], bw.width, bw.height))
    if not tiles:
        print('没有任何可用样张。')
        return 2

    gap = a.gap
    maxw = max(t[2].width for t in tiles)
    totalh = sum(t[2].height for t in tiles) + gap * (len(tiles) + 1)
    canvas = Image.new('L', (maxw + 2 * gap, totalh), 255)
    y = gap
    new_samples = []
    for label, text, bw in tiles:
        canvas.paste(bw, (gap, y))
        new_samples.append({'label': label, 'text': text,
                            'box': [gap / canvas.width, y / canvas.height,
                                    (gap + bw.width) / canvas.width,
                                    (y + bw.height) / canvas.height]})
        y += bw.height + gap

    png = os.path.join(out_dir, 'bw_samples.png')
    canvas.save(png)
    out_spec = os.path.join(out_dir, 'spec_bw.json')
    # candidates 允许缺省 —— 交给 glyph_match.py --manifest 从清单自动生成即可，
    # 这样换电脑时不用改任何路径（原来是 spec['candidates']，缺 key 会直接 KeyError）。
    json.dump({'image': png, 'target_h': spec.get('target_h', 64),
               'samples': new_samples, 'candidates': spec.get('candidates', []),
               'top': spec.get('top', 8)},
              open(out_spec, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\n二值样张拼图: %s  %s' % (png, canvas.size))
    print('新 spec      : %s' % out_spec)
    print('下一步: python glyph_match.py "%s" --manifest assets/licensed-fonts.json' % out_spec)
    print('      （候选池由清单自动生成，无需手工填字体路径）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
