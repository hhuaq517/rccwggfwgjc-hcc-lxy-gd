# -*- coding: utf-8 -*-
"""PSD 图层导出（给广告法标注用）：列出所有图层的 kind / 可见性 / bbox / 文案。

用法：
  python psd_layers.py <psd>                 # 打印表格
  python psd_layers.py <psd> --json out.json # 同时写出 json（供 build_boxes.py 取框位）

要点：
  - 只列 **真正出图** 的层：visible == 0 的隐藏层单独标出来，不参与标注（见 SKILL.md「幽灵框」一节）
  - bbox 是像素坐标，除以画布尺寸即得 0–1 比例坐标
  - kind: type=文字层，shape/pixel=装饰块，smartobject=智能对象（内部字体读不到）
依赖：psd-tools（`python -m pip install psd-tools`）
"""
import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("psd")
    ap.add_argument("--json")
    a = ap.parse_args()

    from psd_tools import PSDImage

    psd = PSDImage.open(a.psd)
    W, H = psd.width, psd.height
    print("画布: %d x %d" % (W, H))

    items = []

    def walk(group, depth=0):
        for L in group:
            try:
                bbox = [int(v) for v in L.bbox]
            except Exception:
                bbox = [0, 0, 0, 0]
            rec = {"name": str(L.name), "kind": L.kind,
                   "visible": bool(L.visible), "bbox": bbox, "depth": depth}
            if L.kind == "type":
                try:
                    rec["text"] = L.text
                except Exception as e:
                    rec["text"] = "<err %s>" % e
            items.append(rec)
            if L.is_group():
                walk(L, depth + 1)

    walk(psd)

    n_type = sum(1 for r in items if r["kind"] == "type")
    n_hidden = sum(1 for r in items if not r["visible"])
    print("图层 %d 个（文字层 %d，隐藏 %d）\n" % (len(items), n_type, n_hidden))

    print("%-12s %-4s %-30s %s" % ("kind", "vis", "bbox", "name / text"))
    for r in items:
        b = r["bbox"]
        if b[2] - b[0] <= 0 or b[3] - b[1] <= 0:
            continue
        label = r["name"]
        if r.get("text"):
            label = "TEXT: " + r["text"].replace("\r", "|")[:44]
        mark = "" if r["visible"] else "   <<< 隐藏，不出图"
        print("%-12s %-4d [%5d,%5d,%5d,%5d] %s%s%s" % (
            r["kind"], r["visible"], b[0], b[1], b[2], b[3],
            "  " * r["depth"], label, mark))

    if a.json:
        json.dump({"canvas": [W, H], "items": items},
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n已写出: %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
