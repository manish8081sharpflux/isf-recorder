"""Jomi app icon: white microphone + bold 'ISF' on an emerald-teal background.
Produces Android adaptive-icon layers (foreground/background) and legacy square/round PNGs for every
density, plus a TEST variant with an orange 'TEST' band. Output goes into the given res folders."""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

FONT = r"C:\Windows\Fonts\segoeuib.ttf"
TOP, BOTTOM = (16, 185, 129), (13, 148, 136)        # emerald -> teal (app theme)
WHITE = (255, 255, 255, 255)
ORANGE = (249, 115, 22, 255)


def gradient(size):
    img = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)) + (255,)
        d.line([(0, y), (size, y)], fill=c)
    return img


def draw_mark(d, size, cx, top, scale, test=False):
    """Mic (capsule + stand) and 'ISF' text, centred on cx, starting at y=top, scaled by `scale` (px per unit)."""
    u = scale
    cap_w, cap_h = 26 * u, 40 * u
    x0, y0 = cx - cap_w / 2, top
    d.rounded_rectangle([x0, y0, x0 + cap_w, y0 + cap_h], radius=cap_w / 2, fill=WHITE)
    # grille lines (teal) on the capsule
    for k in (0.38, 0.52, 0.66):
        yy = y0 + cap_h * k
        d.line([(x0 + cap_w * 0.28, yy), (x0 + cap_w * 0.72, yy)], fill=BOTTOM + (255,), width=max(1, int(2.2 * u)))
    # stand arc
    arc_w, stroke = 40 * u, max(2, int(4.2 * u))
    ax0, ay0 = cx - arc_w / 2, y0 + cap_h * 0.35
    d.arc([ax0, ay0, ax0 + arc_w, ay0 + arc_w * 0.95], start=0, end=180, fill=WHITE, width=stroke)
    stem_top = ay0 + arc_w * 0.95
    d.line([(cx, stem_top - stroke / 2), (cx, stem_top + 8 * u)], fill=WHITE, width=stroke)
    d.line([(cx - 11 * u, stem_top + 8 * u), (cx + 11 * u, stem_top + 8 * u)], fill=WHITE, width=stroke)
    text_top = stem_top + 15 * u
    if test:  # TEST build: an orange "TEST" pill where "ISF" is, so the two apps are never confused
        font = ImageFont.truetype(FONT, int(22 * u))
        w = d.textlength("TEST", font=font)
        pad_x, h = 8 * u, 32 * u
        d.rounded_rectangle([cx - w / 2 - pad_x, text_top, cx + w / 2 + pad_x, text_top + h], radius=h / 2, fill=ORANGE)
        d.text((cx - w / 2, text_top + 2.5 * u), "TEST", font=font, fill=WHITE)
        return
    font = ImageFont.truetype(FONT, int(30 * u))
    w = d.textlength("ISF", font=font)
    d.text((cx - w / 2, text_top), "ISF", font=font, fill=WHITE)


def foreground(size, test=False):
    """Adaptive foreground: 108dp canvas, artwork inside the 66dp safe circle."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / 108 * 0.62
    draw_mark(d, size, size / 2, size * 0.215, u, test=test)
    if False:
        band_h = size * 0.13
        y = size * 0.66
        d.rectangle([size * 0.17, y, size * 0.83, y + band_h], fill=ORANGE)
        f = ImageFont.truetype(FONT, int(band_h * 0.72))
        w = d.textlength("TEST", font=f)
        d.text((size / 2 - w / 2, y + band_h * 0.08), "TEST", font=f, fill=WHITE)
    return img


def legacy(size, round_=False, test=False):
    """Pre-Android-8 icon: full square (rounded) or circle with the artwork."""
    big = size * 4
    bg = gradient(big)
    fg = foreground(big, test).resize((big, big), Image.LANCZOS)
    # the foreground is drawn for a 108dp canvas; legacy icons show ~the central 72dp -> zoom 1.5x
    zoom = int(big * 1.5)
    fgz = fg.resize((zoom, zoom), Image.LANCZOS).crop(((zoom - big) // 2,) * 2 + ((zoom + big) // 2,) * 2)
    img = Image.alpha_composite(bg, fgz)
    mask = Image.new("L", (big, big), 0)
    md = ImageDraw.Draw(mask)
    if round_:
        md.ellipse([0, 0, big, big], fill=255)
    else:
        md.rounded_rectangle([0, 0, big, big], radius=big * 0.18, fill=255)
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


DENSITIES = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}


def write_res(res_dir, test=False):
    for name, f in DENSITIES.items():
        folder = os.path.join(res_dir, f"mipmap-{name}")
        os.makedirs(folder, exist_ok=True)
        legacy(int(48 * f), test=test).save(os.path.join(folder, "ic_launcher.png"))
        legacy(int(48 * f), round_=True, test=test).save(os.path.join(folder, "ic_launcher_round.png"))
        fg = foreground(int(108 * f) * 4, test).resize((int(108 * f),) * 2, Image.LANCZOS)
        fg.save(os.path.join(folder, "ic_launcher_foreground.png"))
        gradient(int(108 * f)).save(os.path.join(folder, "ic_launcher_background.png"))
    anydpi = os.path.join(res_dir, "mipmap-anydpi-v26")
    os.makedirs(anydpi, exist_ok=True)
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
           '    <background android:drawable="@mipmap/ic_launcher_background" />\n'
           '    <foreground android:drawable="@mipmap/ic_launcher_foreground" />\n'
           '    <monochrome android:drawable="@mipmap/ic_launcher_foreground" />\n'
           '</adaptive-icon>\n')
    for n in ("ic_launcher.xml", "ic_launcher_round.xml"):
        open(os.path.join(anydpi, n), "w", encoding="utf-8").write(xml)


if __name__ == "__main__":
    main_res, staging_res, preview = sys.argv[1], sys.argv[2], sys.argv[3]
    write_res(main_res, test=False)
    write_res(staging_res, test=True)
    # preview: real icon + TEST icon + how a round launcher crops them
    tiles = [legacy(256), legacy(256, round_=True), legacy(256, test=True), legacy(256, round_=True, test=True)]
    sheet = Image.new("RGBA", (256 * 4 + 50, 296), (245, 247, 250, 255))
    for i, t in enumerate(tiles):
        sheet.paste(t, (10 + i * 266, 10), t)
    sheet.save(preview)
    print("done")
