# -*- coding: utf-8 -*-
"""
广告法合规标注脚本：在原图上按风险等级画标注框。

用法:
    python annotate.py <图片路径> <boxes.json 路径> <输出png路径>

boxes.json 结构（坐标均为 0-1 比例，与图片分辨率无关）:
{
    "boxes": [
        [x1, y1, x2, y2, level],          # level: 1=红(高风险) 2=橙(中风险) 3=黄(提示)
        [x1, y1, x2, y2, level, num],     # 可选第 6 位 num：指定编号
        ...
    ],
    "legend": true                   # 可选，默认 true，底部追加图例条
}

关于第 6 位 num（同一问题在图上多处出现时用）:
    不写 num 时按顺序自动编号 1..N（与结论文档条目一一对应）。
    同一条问题若在图上 3 个位置都要标，就给这 3 个框都写同一个 num，
    这样图上多处共用同一个编号，不会把结论文档的条目数撑大。
    ⚠️ 混用时注意：**只要数组里出现过 num，就必须所有框都显式写 num**，
    否则自动编号会与显式编号撞号。

依赖: Pillow
"""
import json
import sys

from PIL import Image, ImageDraw, ImageFont

COLORS = {1: (230, 30, 30), 2: (245, 130, 20), 3: (230, 180, 0)}
LEGEND = {1: "红=高风险", 2: "橙=中风险", 3: "黄=提示"}

# 编号徽标 / 图例条用的字体：**跨平台候选表逐个探测**。
# 早期版本把路径写死成 C:/Windows/Fonts/... —— 在 macOS / Linux 上会被 except 静默吞掉、
# 回退到 ImageFont.load_default()，而默认位图字体**不含中文字形**：
# 图例「红=高风险 / 橙=中风险 / 黄=提示」会变成空白或一排方框，编号数字也会变粗糙。
# 这类"不报错、只是画得不对"的降级最难察觉，所以改成候选表 + 显式告警。
NUM_FONT_CANDIDATES = (
    r"C:/Windows/Fonts/arialbd.ttf",                            # Windows
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",        # macOS
    "/System/Library/Fonts/Helvetica.ttc",                      # macOS（兜底）
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",     # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
CJK_FONT_CANDIDATES = (
    r"C:/Windows/Fonts/msyh.ttc",                               # Windows
    r"C:/Windows/Fonts/msyhbd.ttc",
    r"C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/STHeiti Medium.ttc",                 # macOS
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Linux
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


def _font(candidates, size, what="字体"):
    """按候选表逐个探测，返回第一个能加载的字体；全失败才回退默认字体（并告警）。

    candidates 可传字符串（单个路径）或路径序列。
    """
    if isinstance(candidates, str):
        candidates = (candidates,)
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    print("⚠️ 未找到可用的%s文件（候选 %d 个都不存在），本次回退默认位图字体，"
          "编号/图例可能显示异常。" % (what, len(candidates)), file=sys.stderr)
    return ImageFont.load_default()


def annotate(image_path, boxes, out_path, legend=True):
    im = Image.open(image_path).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)

    # 编号徽标尺寸自适应：按「最矮的那个框」反推半径。
    # 密集标注（例如同一块的上下两行各一个框、行距只有 30px）时，
    # 固定 r=34 的徽标会互相压住，导致数字叠在一起看不清。
    min_h = min(int((b[3] - b[1]) * H) for b in boxes) if boxes else H
    r = max(11, min(34, int(min_h * 0.42)))
    fnum = _font(NUM_FONT_CANDIDATES, max(12, int(r * 1.3)), "编号字体")
    fleg = _font(CJK_FONT_CANDIDATES, 36, "图例中文字体")

    for i, box in enumerate(boxes, 1):
        x1, y1, x2, y2, lvl = box[:5]
        num = box[5] if len(box) > 5 else i   # 第 6 位可显式指定编号
        c = COLORS.get(lvl, COLORS[1])
        p1 = (int(x1 * W), int(y1 * H))
        p2 = (int(x2 * W), int(y2 * H))
        for k in range(7):  # 7px 粗边框
            d.rectangle([p1[0] + k, p1[1] + k, p2[0] - k, p2[1] - k], outline=c)
        cx, cy = p1[0] - 2, p1[1] + 4
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
        tb = d.textbbox((0, 0), str(num), font=fnum)
        d.text((cx - (tb[2] - tb[0]) / 2 - tb[0], cy - (tb[3] - tb[1]) / 2 - tb[1]),
               str(num), font=fnum, fill=(255, 255, 255))

    if legend:
        lh = 64
        bar = Image.new("RGB", (W, lh + 8), (255, 255, 255))
        bd = ImageDraw.Draw(bar)
        x = 20
        for lvl in (1, 2, 3):
            c = COLORS[lvl]
            bd.rectangle([x, 14, x + 46, 50], outline=c, width=5)
            bd.text((x + 60, 20), LEGEND[lvl], font=fleg, fill=(60, 60, 60))
            x += 60 + bd.textlength(LEGEND[lvl], font=fleg) + 70
        out = Image.new("RGB", (W, H + lh + 8), (255, 255, 255))
        out.paste(im, (0, 0))
        out.paste(bar, (0, H))
    else:
        out = im

    out.save(out_path)
    print("saved:", out_path, out.size)


def main():
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(sys.argv) != 4:
        print("用法: python annotate.py <图片路径> <boxes.json 路径> <输出png路径>")
        print()
        print("示例: python annotate.py poster.png boxes.json poster_标注.png")
        print()
        print("boxes.json 结构（坐标 0-1 比例）:")
        print('  {"boxes": [[x1, y1, x2, y2, level], ...], "legend": true}')
        print("  level: 1=红(高风险) 2=橙(中风险) 3=黄(提示)；可选第 6 位 num 指定编号")
        print()
        print("完整说明见 --help")
        return 2

    img, cfg, out = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(cfg, encoding="utf-8") as f:
        data = json.load(f)
    annotate(img, data.get("boxes", []), out, data.get("legend", True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
