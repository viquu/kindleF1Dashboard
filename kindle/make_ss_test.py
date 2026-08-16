#!/mnt/us/python3/bin/python3.9
# -*- coding: utf-8 -*-
"""生成 758x1024 屏保定位测试图,确定可见窗口(600x800)的偏移。

用法:
    /mnt/us/python3/bin/python3.9 /mnt/us/f1dash/make_ss_test.py
    (输出到 /mnt/us/linkss/screensavers/bg_ss00.png)
"""

from PIL import Image, ImageDraw, ImageFont

W, H = 758, 1024
OUT = "/mnt/us/linkss/screensavers/bg_ss00.png"
FONT_BOLD = "/usr/java/lib/fonts/Futura-Bold.ttf"


def font(size):
    return ImageFont.truetype(FONT_BOLD, size)


def main():
    img = Image.new("L", (W, H), 255)  # 白底 8 位灰度
    d = ImageDraw.Draw(img)

    # 四角 30x30 黑块:标记完整 758x1024 画布的四角
    for (x, y) in [(0, 0), (W - 30, 0), (0, H - 30), (W - 30, H - 30)]:
        d.rectangle([x, y, x + 29, y + 29], fill=0)

    # 外边框
    d.rectangle([0, 0, W - 1, H - 1], outline=0, width=4)

    # 中心十字(辅助判断偏移)
    d.line([0, H // 2, W - 1, H // 2], fill=0, width=2)
    d.line([W // 2, 0, W // 2, H - 1], fill=0, width=2)

    # 假定可见窗口右边界 x=600 / 下边界 y=800 参考线
    d.line([600, 0, 600, H - 1], fill=64, width=2)
    d.line([0, 800, W - 1, 800], fill=64, width=2)

    # 边角/位置标签
    d.text((40, 40), "TOP-LEFT", font=font(40), fill=0)
    d.text((W - 300, 40), "TOP-RIGHT", font=font(40), fill=0)
    d.text((40, H - 80), "BOTTOM-LEFT", font=font(40), fill=0)
    d.text((W - 340, H - 80), "BOTTOM-RIGHT", font=font(40), fill=0)
    d.text((W // 2 - 140, H // 2 - 40), "CENTER", font=font(40), fill=0)
    d.text((40, 760), "LEFT y=800", font=font(32), fill=0)
    d.text((620, 400), "RIGHT x=600", font=font(32), fill=0)
    d.text((W // 2 - 110, 40), "758x1024", font=font(36), fill=0)

    img.save(OUT)
    print("saved:", OUT, img.size, img.mode)


if __name__ == "__main__":
    main()
