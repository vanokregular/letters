#!/usr/bin/env python3
"""font2tgs — превращает буквы шрифта в .tgs-файлы для кастомных эмодзи Telegram.

Каждый символ становится отдельным .tgs (Lottie JSON, сжатый gzip'ом):
канвас 512x512 (Telegram требует ровно 512 для .tgs — и для стикеров,
и для кастомных эмодзи; клиент сам уменьшает эмодзи), 60 fps, лимит 64 КБ.

Примеры:
    python3 font2tgs.py MyFont.ttf
    python3 font2tgs.py MyFont.ttf --chars "АБВГД" --color "#FF3366" --animate pop
    python3 font2tgs.py MyFont.ttf --size 512 --out stickers/
"""

import argparse
import gzip
import json
import re
import sys
import unicodedata
from pathlib import Path

from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont

RU_UPPER = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
EN_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"
DEFAULT_CHARS = RU_UPPER + EN_UPPER + DIGITS

TGS_MAX_BYTES = 64 * 1024


def parse_color(value):
    value = value.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise argparse.ArgumentTypeError("цвет задаётся как RRGGBB, например #1E90FF")
    return [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]


def rnd(x):
    v = round(x, 2)
    return int(v) if v == int(v) else v


class ContourBuilder:
    """Собирает контуры глифа в формат Lottie-путей (v/i/o, замкнутость)."""

    def __init__(self):
        self.contours = []
        self._cur = None

    def moveTo(self, p):
        self._cur = {"v": [p], "i": [(0, 0)], "o": [(0, 0)]}

    def lineTo(self, p):
        c = self._cur
        c["o"][-1] = (0, 0)
        c["v"].append(p)
        c["i"].append((0, 0))
        c["o"].append((0, 0))

    def curveTo(self, c1, c2, p):
        c = self._cur
        p0 = c["v"][-1]
        c["o"][-1] = (c1[0] - p0[0], c1[1] - p0[1])
        c["v"].append(p)
        c["i"].append((c2[0] - p[0], c2[1] - p[1]))
        c["o"].append((0, 0))

    def qCurveTo(self, *points):
        # TrueType: все точки off-curve, последняя on-curve; между соседними
        # off-curve точками подразумевается on-curve середина. Последняя None —
        # контур целиком из off-curve точек.
        if points[-1] is None:
            # Замкнутый контур без on-curve точек: стартуем с середины
            # последнего сегмента (RecordingPen уже дал moveTo в таком случае
            # только если контур декомпозирован; сюда попадаем редко).
            points = points[:-1]
            start = mid(points[-1], points[0])
            self.moveTo(start)
            offs = list(points) + [points[0]]
            prev_on = start
            for a, b in zip(offs, offs[1:]):
                on = mid(a, b)
                self._quad(prev_on, a, on)
                prev_on = on
            self.closePath()
            return
        offs, last_on = points[:-1], points[-1]
        prev_on = self._cur["v"][-1]
        for j, q in enumerate(offs):
            on = mid(q, offs[j + 1]) if j + 1 < len(offs) else last_on
            self._quad(prev_on, q, on)
            prev_on = on
        if not offs:
            self.lineTo(last_on)

    def _quad(self, p0, q, p1):
        c1 = (p0[0] + 2 / 3 * (q[0] - p0[0]), p0[1] + 2 / 3 * (q[1] - p0[1]))
        c2 = (p1[0] + 2 / 3 * (q[0] - p1[0]), p1[1] + 2 / 3 * (q[1] - p1[1]))
        self.curveTo(c1, c2, p1)

    def closePath(self):
        c = self._cur
        if c is None:
            return
        # Если контур вернулся в стартовую точку — сливаем последнюю вершину с первой
        if len(c["v"]) > 1 and _close(c["v"][0], c["v"][-1]):
            c["i"][0] = c["i"][-1]
            for k in ("v", "i", "o"):
                c[k].pop()
        self.contours.append(c)
        self._cur = None

    endPath = closePath


def mid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _close(a, b, eps=1e-6):
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def replay(pen_value, builder):
    for op, args in pen_value:
        getattr(builder, op)(*args)


def glyph_contours(glyph_set, glyph_name):
    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    b = ContourBuilder()
    replay(pen.value, b)
    if b._cur is not None:
        b.closePath()
    return b.contours


def contours_bbox(contours):
    xs, ys = [], []
    for c in contours:
        for (x, y), (ix, iy), (ox, oy) in zip(c["v"], c["i"], c["o"]):
            xs += [x, x + ix, x + ox]
            ys += [y, y + iy, y + oy]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def to_lottie_shape(contours, transform):
    shapes = []
    for c in contours:
        v = [[rnd(x) for x in transform(p)] for p in c["v"]]
        # Тангенсы — относительные, масштабируем без сдвига; ось Y переворачивается
        sx, sy = transform.scale
        i_t = [[rnd(t[0] * sx), rnd(-t[1] * sy)] for t in c["i"]]
        o_t = [[rnd(t[0] * sx), rnd(-t[1] * sy)] for t in c["o"]]
        shapes.append(
            {
                "ty": "sh",
                "ks": {"a": 0, "k": {"i": i_t, "o": o_t, "v": v, "c": True}},
            }
        )
    return shapes


class Transform:
    def __init__(self, scale, dx, dy):
        self.scale = (scale, scale)
        self.dx, self.dy = dx, dy

    def __call__(self, p):
        return (p[0] * self.scale[0] + self.dx, -p[1] * self.scale[1] + self.dy)


def build_lottie(name, shapes, color, size, animate):
    transform = {
        "o": {"a": 0, "k": 100},
        "r": {"a": 0, "k": 0},
        "p": {"a": 0, "k": [size / 2, size / 2, 0]},
        "a": {"a": 0, "k": [size / 2, size / 2, 0]},
        "s": {"a": 0, "k": [100, 100, 100]},
    }
    op = 60
    if animate == "pop":
        ease = {"i": {"x": [0.2], "y": [1]}, "o": {"x": [0.6], "y": [0]}}
        transform["s"] = {
            "a": 1,
            "k": [
                {"t": 0, "s": [0, 0, 100], **ease},
                {"t": 10, "s": [112, 112, 100], **ease},
                {"t": 16, "s": [100, 100, 100]},
            ],
        }
    group = {
        "ty": "gr",
        "it": shapes
        + [
            {"ty": "fl", "c": {"a": 0, "k": color + [1]}, "o": {"a": 0, "k": 100}, "r": 1},
            {
                "ty": "tr",
                "p": {"a": 0, "k": [0, 0]},
                "a": {"a": 0, "k": [0, 0]},
                "s": {"a": 0, "k": [100, 100]},
                "r": {"a": 0, "k": 0},
                "o": {"a": 0, "k": 100},
            },
        ],
    }
    return {
        "tgs": 1,
        "v": "5.5.2",
        "fr": 60,
        "ip": 0,
        "op": op,
        "w": size,
        "h": size,
        "nm": name,
        "ddd": 0,
        "assets": [],
        "layers": [
            {
                "ddd": 0,
                "ind": 1,
                "ty": 4,
                "nm": name,
                "sr": 1,
                "ks": transform,
                "ao": 0,
                "shapes": [group],
                "ip": 0,
                "op": op,
                "st": 0,
            }
        ],
    }


def write_tgs(path, lottie):
    data = json.dumps(lottie, separators=(",", ":"), ensure_ascii=False).encode()
    with open(path, "wb") as f:
        # filename="" — без поля FNAME в заголовке, как у родных TGS Телеграма
        with gzip.GzipFile(filename="", fileobj=f, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(data)
    return path.stat().st_size


def contours_to_svg_path(contours, transform):
    parts = []
    for c in contours:
        v = [transform(p) for p in c["v"]]
        sx, sy = transform.scale
        i_t = [(t[0] * sx, -t[1] * sy) for t in c["i"]]
        o_t = [(t[0] * sx, -t[1] * sy) for t in c["o"]]
        n = len(v)
        parts.append(f"M{rnd(v[0][0])},{rnd(v[0][1])}")
        for j in range(n):
            k = (j + 1) % n
            c1 = (v[j][0] + o_t[j][0], v[j][1] + o_t[j][1])
            c2 = (v[k][0] + i_t[k][0], v[k][1] + i_t[k][1])
            parts.append(
                f"C{rnd(c1[0])},{rnd(c1[1])} {rnd(c2[0])},{rnd(c2[1])} {rnd(v[k][0])},{rnd(v[k][1])}"
            )
        parts.append("Z")
    return " ".join(parts)


def safe_name(ch):
    try:
        label = unicodedata.name(ch).replace(" ", "_")
    except ValueError:
        label = "CHAR"
    return f"{ord(ch):04X}_{label[:40]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("font", help="путь к .ttf/.otf")
    ap.add_argument("--chars", default=DEFAULT_CHARS, help="какие символы генерировать")
    ap.add_argument("--color", type=parse_color, default=parse_color("1E1E1E"), help="цвет заливки, RRGGBB")
    ap.add_argument(
        "--size",
        type=int,
        default=512,
        help="размер канваса; Telegram требует ровно 512 для .tgs (и стикеров, и эмодзи)",
    )
    ap.add_argument("--margin", type=float, default=0.08, help="отступ от края, доля канваса")
    ap.add_argument("--animate", choices=["none", "pop"], default="none", help="pop = появление с отскоком")
    ap.add_argument("--out", default="tgs_out", help="куда складывать файлы")
    ap.add_argument("--svg", action="store_true", help="дополнительно писать SVG-превью")
    args = ap.parse_args()

    font = TTFont(args.font)
    cmap = font.getBestCmap()
    glyph_set = font.getGlyphSet()

    chars = list(dict.fromkeys(args.chars))
    missing = [c for c in chars if ord(c) not in cmap]
    chars = [c for c in chars if ord(c) in cmap]
    if missing:
        print(f"⚠ в шрифте нет: {' '.join(missing)}", file=sys.stderr)
    if not chars:
        sys.exit("ни одного запрошенного символа в шрифте нет")

    all_contours = {c: glyph_contours(glyph_set, cmap[ord(c)]) for c in chars}
    empty = [c for c, ct in all_contours.items() if not ct]
    for c in empty:
        print(f"⚠ пустой глиф, пропускаю: {c!r}", file=sys.stderr)
        del all_contours[c]

    # Общий вертикальный диапазон — чтобы буквы сидели на одной линии
    # и сохраняли относительные размеры; по горизонтали каждая центрируется сама.
    boxes = {c: contours_bbox(ct) for c, ct in all_contours.items()}
    ymin = min(b[1] for b in boxes.values())
    ymax = max(b[3] for b in boxes.values())
    wmax = max(b[2] - b[0] for b in boxes.values())
    inner = args.size * (1 - 2 * args.margin)
    scale = min(inner / (ymax - ymin), inner / wmax)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    y_center_font = (ymin + ymax) / 2
    oversize = []
    for ch, contours in all_contours.items():
        b = boxes[ch]
        x_center = (b[0] + b[2]) / 2
        tr = Transform(
            scale,
            args.size / 2 - x_center * scale,
            args.size / 2 + y_center_font * scale,
        )
        shapes = to_lottie_shape(contours, tr)
        lottie = build_lottie(ch, shapes, args.color, args.size, args.animate)
        path = out_dir / f"{safe_name(ch)}.tgs"
        n = write_tgs(path, lottie)
        if n > TGS_MAX_BYTES:
            oversize.append((ch, n))
        if args.svg:
            d = contours_to_svg_path(contours, tr)
            svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {args.size} {args.size}">'
                f'<path d="{d}" fill="rgb({",".join(str(round(v * 255)) for v in args.color)})"/></svg>'
            )
            path.with_suffix(".svg").write_text(svg)
        print(f"{ch}  ->  {path.name}  ({n} байт)")

    for ch, n in oversize:
        print(f"⚠ {ch!r}: {n} байт — больше лимита Telegram в 64 КБ", file=sys.stderr)
    print(f"\nГотово: {len(all_contours)} файлов в {out_dir}/")


if __name__ == "__main__":
    main()
