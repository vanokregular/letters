#!/usr/bin/env python3
"""word2svg — набирает слова выбранным шрифтом и экспортирует каждое в SVG.

Раскладка букв делается через HarfBuzz (учитывается кернинг и лигатуры),
контуры берутся из шрифта через fonttools, результат — один <path> на слово.

Пример:
    python3 word2svg.py MyFont.ttf СЛОВО ДРУГОЕ --color "#000000" --out svg_words/
"""

import argparse
import re
import sys
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont


def parse_color(value):
    value = value.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise argparse.ArgumentTypeError("цвет задаётся как RRGGBB")
    return "#" + value.upper()


def rnd(x):
    v = round(x, 2)
    return int(v) if v == int(v) else v


class SvgPathPen:
    """Пишет контуры глифа в SVG path data со сдвигом (dx, dy) в единицах шрифта."""

    def __init__(self, dx, dy):
        self.dx, self.dy = dx, dy
        self.parts = []

    def _pt(self, p):
        return f"{rnd(p[0] + self.dx)},{rnd(-(p[1] + self.dy))}"

    def moveTo(self, p):
        self.parts.append("M" + self._pt(p))

    def lineTo(self, p):
        self.parts.append("L" + self._pt(p))

    def curveTo(self, c1, c2, p):
        self.parts.append("C" + " ".join(self._pt(q) for q in (c1, c2, p)))

    def qCurveTo(self, *points):
        # Разворачиваем цепочку TrueType-квадратик с подразумеваемыми on-curve точками
        if points[-1] is None:
            points = points[:-1] + (points[0],)
        for i, q in enumerate(points[:-1]):
            if i + 1 < len(points) - 1:
                on = ((q[0] + points[i + 1][0]) / 2, (q[1] + points[i + 1][1]) / 2)
            else:
                on = points[-1]
            self.parts.append("Q" + self._pt(q) + " " + self._pt(on))

    def closePath(self):
        self.parts.append("Z")

    def endPath(self):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("font", help="путь к .ttf/.otf")
    ap.add_argument("words", nargs="+", help="слова, каждое станет отдельным SVG")
    ap.add_argument("--color", type=parse_color, default="#1E1E1E", help="цвет заливки, RRGGBB")
    ap.add_argument("--height", type=int, default=512, help="высота канваса SVG")
    ap.add_argument("--margin", type=float, default=0.06, help="отступ, доля высоты")
    ap.add_argument("--out", default="svg_words", help="куда складывать файлы")
    args = ap.parse_args()

    blob = hb.Blob.from_file_path(args.font)
    face = hb.Face(blob)
    hb_font = hb.Font(face)
    upem = face.upem

    tt = TTFont(args.font)
    glyph_set = tt.getGlyphSet()
    glyph_order = tt.getGlyphOrder()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for word in args.words:
        buf = hb.Buffer()
        buf.add_str(word)
        buf.guess_segment_properties()
        hb.shape(hb_font, buf)

        pen_parts = []
        x = y = 0
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            glyph_name = glyph_order[info.codepoint]
            rec = DecomposingRecordingPen(glyph_set)
            glyph_set[glyph_name].draw(rec)
            pen = SvgPathPen(x + pos.x_offset, y + pos.y_offset)
            for op, op_args in rec.value:
                getattr(pen, op)(*op_args)
            pen_parts.extend(pen.parts)
            x += pos.x_advance
            y += pos.y_advance

        d = " ".join(pen_parts)
        if not d:
            print(f"⚠ пустое слово, пропускаю: {word!r}", file=sys.stderr)
            continue

        # Точный bbox берём по фактическому пути через отрисовку в bounds-пен
        from fontTools.misc.bezierTools import calcQuadraticBounds, calcCubicBounds

        nums = [float(v) for v in re.findall(r"-?\d+\.?\d*", d)]
        cmds = re.findall(r"[MLCQZ]", d)
        xs, ys = [], []
        i = 0
        cur = start = (0, 0)
        for cmd in cmds:
            if cmd == "M":
                cur = start = (nums[i], nums[i + 1]); i += 2
                xs.append(cur[0]); ys.append(cur[1])
            elif cmd == "L":
                cur = (nums[i], nums[i + 1]); i += 2
                xs.append(cur[0]); ys.append(cur[1])
            elif cmd == "C":
                p1, p2, p3 = (nums[i], nums[i+1]), (nums[i+2], nums[i+3]), (nums[i+4], nums[i+5]); i += 6
                bx0, by0, bx1, by1 = calcCubicBounds(cur, p1, p2, p3)
                xs += [bx0, bx1]; ys += [by0, by1]
                cur = p3
            elif cmd == "Q":
                p1, p2 = (nums[i], nums[i+1]), (nums[i+2], nums[i+3]); i += 4
                bx0, by0, bx1, by1 = calcQuadraticBounds(cur, p1, p2)
                xs += [bx0, bx1]; ys += [by0, by1]
                cur = p2
            elif cmd == "Z":
                cur = start

        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        pad = (ymax - ymin) * args.margin
        vb = (rnd(xmin - pad), rnd(ymin - pad), rnd(xmax - xmin + 2 * pad), rnd(ymax - ymin + 2 * pad))
        scale = args.height / vb[3]
        width = rnd(vb[2] * scale)

        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{args.height}" '
            f'viewBox="{vb[0]} {vb[1]} {vb[2]} {vb[3]}">'
            f'<path d="{d}" fill="{args.color}"/></svg>'
        )
        path = out_dir / f"{word}.svg"
        path.write_text(svg)
        print(f"{word}  ->  {path.name}  ({width}x{args.height})")

    print(f"\nГотово, файлы в {out_dir}/")


if __name__ == "__main__":
    main()
