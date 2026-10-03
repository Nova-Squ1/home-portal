#!/usr/bin/env python3
"""重生成门户图标：等轴测 MC 镶金黑石，材质取自 Faithful 32x（https://faithfulpack.net，署名见 app-icons/src/CREDITS.txt）。
用法：python3 make_icons.py [--fetch]   # --fetch 重新下载材质"""
import base64, io, sys, urllib.request
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "web/assets/app-icons"
SRC = OUT / "src"
RAW = "https://raw.githubusercontent.com/Faithful-Resource-Pack/Faithful-Java-32x/java-latest/assets/minecraft/textures/block/"
NAMES = ("gilded_blackstone",)  # 六面同一张材质

def tex(name):
    return Image.open(SRC / f"{name}.png").convert("RGBA")

def cube(size):
    """size×size 透明底上的等轴测方块（2:1 菱形顶面，左亮右暗），4 倍超采样后缩小。"""
    n = size * 4
    side_im = tex(NAMES[0])
    top = side = side_im.load()
    T = side_im.width
    w = n * .465                      # 半宽
    hgt = w * 1.15                    # 侧面高
    cx, ty = n / 2, (n - (w + hgt)) / 2 + w / 2   # 顶面菱形中心
    im = Image.new("RGBA", (n, n))
    out = im.load()
    for y in range(n):
        for x in range(n):
            dx, dy = (x + .5 - cx) / w, (y + .5 - ty) / (w / 2)
            u, v = (dx + dy) / 2 + .5, (dy - dx) / 2 + .5
            if 0 <= u < 1 and 0 <= v < 1:
                src, k = top, 1.0
            else:
                u = dx + 1 if dx < 0 else dx
                edge = u if dx < 0 else 1 - u            # 该列侧面顶边相对 ty 的下移量（单位 w/2）
                v = (y + .5 - ty - edge * w / 2) / hgt
                if not (0 <= u < 1 and 0 <= v < 1):
                    continue
                src, k = side, (.8 if dx < 0 else .6)
            r, g, b, a = src[int(u * T), int(v * T)]
            out[x, y] = (int(r * k), int(g * k), int(b * k), a)
    return im.resize((size, size), Image.LANCZOS)

def icon(size, scale, radius):
    """天蓝渐变底 + 居中方块；scale 为方块占边长比例。"""
    bg = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(bg)
    for y in range(size):
        k = y / size
        d.line([(0, y), (size, y)], fill=(int(120 + 80 * k), int(200 + 35 * k), 255, 255))
    c = cube(int(size * scale))
    bg.alpha_composite(c, ((size - c.width) // 2, (size - c.height) // 2))
    if radius:
        m = Image.new("L", (size, size), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], int(size * radius), fill=255)
        bg.putalpha(m)
    return bg

if __name__ == "__main__":
    SRC.mkdir(exist_ok=True)
    for name in NAMES:
        if "--fetch" in sys.argv or not (SRC / f"{name}.png").exists():
            (SRC / f"{name}.png").write_bytes(urllib.request.urlopen(RAW + name + ".png").read())
    cube(32).save(OUT / "favicon-32.png")           # 小图标不要底，方块占满
    for s in (192, 512):
        icon(s, .78, .22).save(OUT / f"icon-{s}.png")
        icon(s, .58, 0).save(OUT / f"icon-maskable-{s}.png")  # 安全区内
    buf = io.BytesIO()
    icon(512, .78, .22).save(buf, "PNG")
    (OUT / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><image width="512" height="512" '
        f'href="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"/></svg>')
