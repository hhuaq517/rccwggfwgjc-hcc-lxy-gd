# -*- coding: utf-8 -*-
"""在图层 bbox 窗口内做"墨迹收紧"，必要时按行拆框，生成 annotate.py 用的 boxes.json。

为什么要它：图层 bbox 是**文本框**，常常比文字本身大一圈（含右侧留白、多行块）。
多行图层更麻烦——同一个图层里第一行属问题 A、第二行属问题 B，必须拆成两个框、两个编号。

用法：
  python build_boxes.py <composite.png> <spec.json> <boxes.json>

spec.json 结构：
{
  "boxes": [
    {"num": 1,  "level": 1, "window": [163,1001,1846,1117]},          # 整块，不拆行
    {"num": 5,  "level": 2, "window": [333,2204,1036,2282],
                            "line": [1, 2]},                          # 该窗口的第 2 行，共 2 行
    {"num": 9,  "level": 3, "window": [1856,1002,2337,1396]}
  ],
  "pad": 6
}

level: 1=红 2=橙 3=黄（与 annotate.py 一致）。同一条问题多处出现时给相同的 num。

⚠️ 窗口必须取自**实际出图**的图层：先 `psd_layers.py` 看 visible 与 bbox，
   再用裁图 `Read` 复核框里确实是目标文字（隐藏层/被遮挡层会画出"幽灵框"）。
依赖：Pillow + numpy
"""
import argparse
import json
import sys
from collections import Counter

import numpy as np
from PIL import Image


def ink_bbox(A, win, lines=None):
    x1, y1, x2, y2 = win
    sub = A[y1:y2, x1:x2]
    h, w, _ = sub.shape
    if h <= 0 or w <= 0:
        return list(win)
    # 背景 = 出现最多的颜色（量化到 8 阶，抗渐变/抗锯齿噪声）
    q = sub.reshape(-1, 3) // 8 * 8
    modal = np.array(Counter(map(tuple, q)).most_common(1)[0][0])
    mask = np.abs(q - modal).max(axis=1).reshape(h, w) > 60

    rows = np.where(mask.sum(axis=1) > max(1, w // 200))[0]
    if len(rows) == 0:
        return list(win)

    if lines:
        i, n = lines
        groups, cur = [], [rows[0]]
        for r in rows[1:]:
            if r - cur[-1] <= 2:
                cur.append(r)
            else:
                groups.append((cur[0], cur[-1]))
                cur = [r]
        groups.append((cur[0], cur[-1]))
        if len(groups) != n:            # 行数对不上（字距/描边粘连）→ 等分
            step = (y2 - y1) / float(n)
            groups = [(int(k * step), int((k + 1) * step) - 1) for k in range(n)]
        r0, r1 = groups[i]
    else:
        r0, r1 = rows.min(), rows.max()

    band = mask[r0:r1 + 1]
    cols = np.where(band.sum(axis=0) > 0)[0]
    if len(cols) == 0:
        return list(win)
    return [x1 + int(cols.min()), y1 + int(r0), x1 + int(cols.max()), y1 + int(r1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("spec")
    ap.add_argument("out")
    a = ap.parse_args()

    im = Image.open(a.image).convert("RGB")
    A = np.array(im).astype(int)
    W, H = im.size

    spec = json.load(open(a.spec, encoding="utf-8"))
    pad = int(spec.get("pad", 6))

    boxes = []
    for item in spec["boxes"]:
        win = item["window"]
        b = ink_bbox(A, win, tuple(item["line"]) if item.get("line") else None)
        x1 = max(0, b[0] - pad); y1 = max(0, b[1] - pad)
        x2 = min(W, b[2] + pad); y2 = min(H, b[3] + pad)
        r = [round(x1 / W, 4), round(y1 / H, 4), round(x2 / W, 4), round(y2 / H, 4)]
        boxes.append(r + [int(item["level"]), int(item["num"])])
        print("num %-3d lvl%d  px %-28s -> %s" % (
            item["num"], item["level"], str(b), r))

    json.dump({"boxes": boxes, "legend": True},
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n共 %d 框 -> %s" % (len(boxes), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
