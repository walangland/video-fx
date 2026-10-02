#!/usr/bin/env python3
"""video_fx: JSON timeline based video compositor powered by FFmpeg, Pillow and NumPy.

Legacy mode:
  python video_fx.py input.mp4 output.mp4 --preset cinematic --text "안녕하세요"
Project mode:
  python video_fx.py render project.json -o out.mp4
  python video_fx.py render project.json --preview-frame 1.25 -o preview.png

The project format follows the v1 schema described by AE.md. This single-file build
implements deterministic frame rendering, common layer types, transforms,
keyframes, effects, audio mixing and H.264/ProRes/PNG output.
"""
from __future__ import annotations

import argparse
import copy
import functools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

try:
    import numpy as np
    from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont
except ImportError as exc:
    raise SystemExit("필수 패키지가 없습니다. 실행: pip install numpy pillow jsonschema") from exc

try:
    import jsonschema
except ImportError as exc:
    raise SystemExit("필수 패키지가 없습니다. 실행: pip install jsonschema") from exc

RESAMPLING = getattr(Image, "Resampling", Image)

PROJECT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version", "compositions"],
    "properties": {
        "version": {"const": 1},
        "compositions": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "required": ["id", "width", "height", "fps", "duration", "layers"],
                "properties": {
                    "id": {"type": "string"},
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                    "fps": {"type": "number", "exclusiveMinimum": 0},
                    "duration": {"type": "number", "exclusiveMinimum": 0},
                    "background": {"type": "string"},
                    "layers": {"type": "array", "items": {"type": "object", "required": ["type"]}},
                }, "additionalProperties": True,
            },
        },
        "audio": {"type": "array", "items": {"type": "object", "required": ["source"]}},
    },
    "additionalProperties": True,
}

PRESETS: dict[str, list[dict[str, Any]]] = {
    "cinematic": [{"type": "color", "contrast": 1.12, "saturation": 0.88, "brightness": -3}, {"type": "vignette", "amount": 0.32}],
    "vintage": [{"type": "color", "contrast": 0.94, "saturation": 0.72, "temperature": 0.10}, {"type": "grain", "amount": 0.035}],
    "bw-film": [{"type": "color", "saturation": 0.0, "contrast": 1.12}, {"type": "grain", "amount": 0.04}],
    "vivid": [{"type": "color", "contrast": 1.08, "saturation": 1.28}],
    "cold": [{"type": "color", "temperature": -0.12}],
    "warm": [{"type": "color", "temperature": 0.12}],
}

LAYER_TYPES = {"video", "image", "text", "shape", "solid", "null", "comp", "adjustment", "audio"}
EFFECT_TYPES = {"color", "blur", "sharpen", "glow", "vignette", "grain", "noise", "invert", "sepia", "edge", "flip", "drop_shadow", "wiggle"}
LAYER_KEYS = {
    "id", "type", "name", "source", "composition", "start", "end", "in", "out", "speed", "mute",
    "volume_db", "fade_in", "fade_out", "hidden", "fit", "text", "style", "animator", "counter", "shape",
    "color", "size", "transform", "keyframes", "effects", "blend", "parent", "mask", "matte", "presets",
    "motion_blur", "shutter_angle", "samples", "duration", "anchor", "position", "scale", "rotation", "opacity",
    "width", "height", "fill", "stroke", "kind", "radius", "points", "inner_radius", "trim", "tracking",
    "tail", "inherit_opacity",
}
TRANSFORM_KEYS = {"anchor", "position", "scale", "rotation", "opacity"}
KEYFRAME_KEYS = TRANSFORM_KEYS | {"width", "height", "color", "trim", "tracking", "size"}
COMPOSITION_KEYS = {"id", "name", "width", "height", "fps", "duration", "background", "layers"}
AUDIO_KEYS = {"id", "source", "start", "in", "out", "end", "duration", "speed", "volume_db", "fade_in", "fade_out", "loop", "mute"}
SHAPE_KINDS = {"rectangle", "circle", "ellipse", "polygon", "star", "burst", "line", "arrow"}
FONT_CACHE_SIZE = 96
_FFPROBE_CACHE: dict[Path, dict[str, Any]] = {}

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf",
    r"C:\Windows\Fonts\NotoSansKR-Regular.ttf", r"C:\Windows\Fonts\NotoSansKR-Black.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]


def die(message: str, code: int = 2) -> None:
    print(f"오류: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        die("ffmpeg와 ffprobe가 PATH에 있어야 합니다.")


def parse_color(value: str | list[int] | tuple[int, ...], alpha: int = 255) -> tuple[int, int, int, int]:
    if isinstance(value, (list, tuple)):
        vals = list(value)
        if len(vals) == 3:
            vals.append(alpha)
        return tuple(int(max(0, min(255, x))) for x in vals[:4])  # type: ignore[return-value]
    if str(value).strip().lower() == "transparent":
        return (0, 0, 0, 0)
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) == 6:
        text += f"{alpha:02x}"
    if len(text) != 8 or not re.fullmatch(r"[0-9a-fA-F]{8}", text):
        die(f"잘못된 색상 값: {value}")
    return tuple(int(text[i:i+2], 16) for i in range(0, 8, 2))  # type: ignore[return-value]


def ffprobe(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if path in _FFPROBE_CACHE:
        return _FFPROBE_CACHE[path]
    p = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ], text=True, capture_output=True)
    if p.returncode:
        die(f"미디어 정보를 읽을 수 없습니다: {path}\n{p.stderr.strip()}")
    _FFPROBE_CACHE[path] = json.loads(p.stdout)
    return _FFPROBE_CACHE[path]


def has_stream(path: Path, kind: str) -> bool:
    return any(s.get("codec_type") == kind for s in ffprobe(path).get("streams", []))


def media_duration(path: Path) -> float:
    info = ffprobe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def resolve_path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def layer_end(layer: dict[str, Any], comp_duration: float) -> float:
    if "end" in layer:
        return float(layer["end"])
    if layer.get("type") == "video" and layer.get("out") is not None:
        speed = float(layer.get("speed", 1))
        if speed <= 0:
            die(f"레이어 {layer.get('id', '?')}의 speed는 0보다 커야 합니다.")
        return float(layer.get("start", 0)) + (float(layer["out"]) - float(layer.get("in", 0))) / speed
    return float(comp_duration)


def _bezier_coord(t: float, a: float, b: float) -> float:
    u = 1.0 - t
    return 3*u*u*t*a + 3*u*t*t*b + t*t*t


def ease_value(name: str, x: float) -> float:
    x = min(1.0, max(0.0, x))
    if name == "hold": return 0.0
    if name == "ease-in": return x * x
    if name == "ease-out": return 1 - (1 - x) ** 2
    if name == "ease-in-out": return 3*x*x - 2*x*x*x
    if name == "back": return x*x*(2.70158*x - 1.70158)
    if name == "bounce":
        n, d = 7.5625, 2.75
        if x < 1/d: return n*x*x
        if x < 2/d: x -= 1.5/d; return n*x*x + .75
        if x < 2.5/d: x -= 2.25/d; return n*x*x + .9375
        x -= 2.625/d; return n*x*x + .984375
    if name == "elastic":
        if x in (0, 1): return x
        return 2 ** (-10*x) * math.sin((x*10-.75) * (2*math.pi/3)) + 1
    if name.startswith("cubic-bezier"):
        nums = [float(n) for n in re.findall(r"[-+]?\d*\.?\d+", name)]
        if len(nums) == 4:
            x1, y1, x2, y2 = nums
            # CSS timing curves map x to y; solve the x polynomial first.
            lo, hi = 0.0, 1.0
            for _ in range(40):
                t = (lo+hi)/2
                if _bezier_coord(t, x1, x2) < x: lo = t
                else: hi = t
            return _bezier_coord((lo+hi)/2, y1, y2)
    return x


def lerp(a: Any, b: Any, x: float) -> Any:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return [lerp(i, j, x) for i, j in zip(a, b)]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + (b-a)*x
    return a if x < .5 else b


def animated(layer: dict[str, Any], prop: str, default: Any, local_t: float) -> Any:
    frames = layer.get("keyframes", {}).get(prop)
    if not frames:
        return layer.get("transform", {}).get(prop, layer.get(prop, default))
    frames = sorted(frames, key=lambda k: float(k["t"]))
    if local_t <= float(frames[0]["t"]): return frames[0]["v"]
    if local_t >= float(frames[-1]["t"]): return frames[-1]["v"]
    for a, b in zip(frames, frames[1:]):
        ta, tb = float(a["t"]), float(b["t"])
        if ta <= local_t <= tb:
            x = ease_value(str(a.get("ease", "linear")), (local_t-ta)/(tb-ta))
            return lerp(a["v"], b["v"], x)
    return default


@functools.lru_cache(maxsize=FONT_CACHE_SIZE)
def load_font(path: str | None, size: int, text: str = "") -> ImageFont.FreeTypeFont:
    candidates = ([path] if path else []) + FONT_CANDIDATES
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    if any(ord(ch) > 127 for ch in text):
        die("한글 글꼴을 찾지 못했습니다. 텍스트 style.font 또는 --font로 글꼴 파일을 지정하세요.")
    return ImageFont.load_default(size=size)


def contain_cover(image: Image.Image, size: tuple[int, int], fit: str = "contain") -> Image.Image:
    tw, th = size
    if fit == "stretch": return image.resize(size, RESAMPLING.LANCZOS)
    ratio = min(tw/image.width, th/image.height) if fit == "contain" else max(tw/image.width, th/image.height)
    nw, nh = max(1, round(image.width*ratio)), max(1, round(image.height*ratio))
    resized = image.resize((nw, nh), RESAMPLING.LANCZOS)
    canvas = Image.new("RGBA", size, (0,0,0,0))
    canvas.alpha_composite(resized, ((tw-nw)//2, (th-nh)//2))
    return canvas


def color_effect(img: Image.Image, fx: dict[str, Any]) -> Image.Image:
    arr = np.asarray(img.convert("RGBA"), dtype=np.float32)
    rgb, alpha = arr[..., :3], arr[..., 3:4]
    contrast = float(fx.get("contrast", 1.0)); brightness = float(fx.get("brightness", 0.0))
    rgb = (rgb - 128.0) * contrast + 128.0 + brightness
    saturation = float(fx.get("saturation", 1.0))
    lum = rgb[..., 0:1]*.2126 + rgb[..., 1:2]*.7152 + rgb[..., 2:3]*.0722
    rgb = lum + (rgb-lum)*saturation
    gamma = max(.01, float(fx.get("gamma", 1.0)))
    rgb = 255*np.power(np.clip(rgb, 0, 255)/255, 1/gamma)
    temp = float(fx.get("temperature", 0.0))
    rgb[..., 0] *= 1 + max(-.8, min(.8, temp))*.65
    rgb[..., 2] *= 1 - max(-.8, min(.8, temp))*.65
    return Image.fromarray(np.uint8(np.clip(np.concatenate([rgb, alpha], axis=2), 0, 255)), "RGBA")


def vignette(img: Image.Image, amount: float) -> Image.Image:
    arr = np.asarray(img, dtype=np.float32)
    h, w = arr.shape[:2]
    y, x = np.ogrid[-1:1:complex(h), -1:1:complex(w)]
    d = np.sqrt(x*x+y*y) / math.sqrt(2)
    edge = np.clip((d-.35)/.65, 0, 1)
    smooth = edge*edge*(3-2*edge)
    arr[..., :3] *= (1 - np.clip(amount,0,1)*smooth[...,None])
    return Image.fromarray(np.uint8(np.clip(arr,0,255)), "RGBA")


def apply_effects(img: Image.Image, effects: Iterable[dict[str, Any]], frame: int = 0) -> Image.Image:
    out = img
    for fx in effects:
        kind = fx.get("type")
        if kind == "color": out = color_effect(out, fx)
        elif kind == "blur": out = out.filter(ImageFilter.GaussianBlur(float(fx.get("radius", fx.get("amount", 2)))))
        elif kind == "sharpen":
            pct = int(max(0, float(fx.get("amount", .5))) * 180)
            out = out.filter(ImageFilter.UnsharpMask(radius=2, percent=pct, threshold=3))
        elif kind == "glow":
            glow = out.filter(ImageFilter.GaussianBlur(float(fx.get("radius", 12))))
            out = ImageChops.screen(out, glow)
        elif kind == "vignette": out = vignette(out, float(fx.get("amount", .35)))
        elif kind in ("grain", "noise"):
            arr = np.asarray(out, dtype=np.int16)
            rng = np.random.default_rng(int(fx.get("seed", 1)) + frame)
            amp = float(fx.get("amount", .03))*255
            arr[..., :3] += rng.normal(0, amp, arr[..., :3].shape).astype(np.int16)
            out = Image.fromarray(np.uint8(np.clip(arr,0,255)), "RGBA")
        elif kind == "invert":
            rgb = ImageChops.invert(out.convert("RGB")); rgb.putalpha(out.getchannel("A")); out = rgb
        elif kind == "sepia":
            arr = np.asarray(out, dtype=np.float32); rgb = arr[..., :3]
            mat = np.array([[.393,.769,.189],[.349,.686,.168],[.272,.534,.131]], dtype=np.float32)
            arr[..., :3] = np.clip(rgb @ mat.T, 0, 255); out = Image.fromarray(arr.astype(np.uint8), "RGBA")
        elif kind == "edge":
            edge = out.convert("RGB").filter(ImageFilter.FIND_EDGES); edge.putalpha(out.getchannel("A")); out = edge
        elif kind == "flip":
            direction = str(fx.get("direction", "horizontal"))
            if direction in ("hv", "both"):
                out = out.transpose(Image.Transpose.FLIP_LEFT_RIGHT).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            elif direction in ("v", "vertical"):
                out = out.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            else:
                out = out.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        elif kind == "drop_shadow":
            off = fx.get("offset", [8, 8]); radius = float(fx.get("blur", 8))
            shadow = Image.new("RGBA", out.size, parse_color(fx.get("color", "#00000080")))
            shadow.putalpha(out.getchannel("A").filter(ImageFilter.GaussianBlur(radius)))
            canvas = Image.new("RGBA", (out.width+abs(int(off[0]))+radius.__ceil__()*2, out.height+abs(int(off[1]))+radius.__ceil__()*2))
            origin = (radius.__ceil__()+max(0,-int(off[0])), radius.__ceil__()+max(0,-int(off[1])))
            canvas.alpha_composite(shadow, (origin[0]+int(off[0]), origin[1]+int(off[1]))); canvas.alpha_composite(out, origin); out = canvas
    return out


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        tokens = list(paragraph) if " " not in paragraph else paragraph.split(" ")
        sep = "" if " " not in paragraph else " "
        for token in tokens:
            trial = token if not current else current + sep + token
            if current and draw.textbbox((0,0), trial, font=font)[2] > max_width:
                lines.append(current); current = token
            else: current = trial
        lines.append(current)
    return "\n".join(lines)


def make_text_layer(layer: dict[str, Any], local_t: float) -> Image.Image:
    style = layer.get("style", {})
    text = str(layer.get("text", ""))
    animator = layer.get("animator", {})
    if "typewriter" in animator:
        rate = float(animator["typewriter"].get("chars_per_second", 15))
        text = text[:max(0, int(local_t*rate))]
    if "counter" in layer:
        c = layer["counter"]; dur = max(.001, float(c.get("duration", layer.get("end",1)-layer.get("start",0))))
        x = ease_value(str(c.get("ease","linear")), min(1,local_t/dur))
        value = round(lerp(float(c.get("from",0)), float(c.get("to",0)), x))
        fmt = str(c.get("format", "#,##0"))
        text = f"{value:,}" if "," in fmt else str(value)
        text += fmt.split("0",1)[1] if "0" in fmt else ""
    size = max(1, int(animated(layer, "size", style.get("size", 72), local_t)))
    # Cache by font path/size. Text is only used for the missing-Korean-font diagnostic.
    font = load_font(style.get("font"), size, "한" if any(ord(ch)>127 for ch in text) else "")
    max_width = int(style.get("max_width", 2000)); tracking = float(animated(layer, "tracking", style.get("tracking", 0), local_t))
    stroke = style.get("stroke", {}); sw = int(stroke.get("width", 0)); spacing = int(size*(float(style.get("line_height",1.15))-1))
    scratch = Image.new("RGBA", (max_width+200, max(size*4,1000))); d = ImageDraw.Draw(scratch)
    spans = style.get("spans", []); stagger = animator.get("stagger")
    if not spans and not tracking and not stagger:
        wrapped = wrap_text(d, text, font, max_width)
        bbox = d.multiline_textbbox((0,0), wrapped, font=font, stroke_width=sw, spacing=spacing)
        w, h = max(1,bbox[2]-bbox[0]+40), max(1,bbox[3]-bbox[1]+40)
        img = Image.new("RGBA", (w,h), (0,0,0,0)); draw = ImageDraw.Draw(img)
        align = style.get("align", "center"); anchor_x = {"left":20, "center":w/2, "right":w-20}.get(align,w/2)
        anchor = {"left":"la", "center":"ma", "right":"ra"}.get(align,"ma")
        shadow = style.get("shadow")
        if shadow:
            off = shadow.get("offset", [0,4]); sh = Image.new("RGBA", img.size, (0,0,0,0)); sd = ImageDraw.Draw(sh)
            sd.multiline_text((anchor_x+off[0],20+off[1]), wrapped, font=font, fill=parse_color(shadow.get("color","#00000080")), anchor=anchor, align=align, spacing=spacing, stroke_width=sw, stroke_fill=parse_color(shadow.get("color","#00000080")))
            sh = sh.filter(ImageFilter.GaussianBlur(float(shadow.get("blur",4)))); img.alpha_composite(sh)
        draw.multiline_text((anchor_x,20), wrapped, font=font, fill=parse_color(animated(layer, "color", style.get("color","#ffffff"), local_t)), anchor=anchor, align=align, spacing=spacing, stroke_width=sw, stroke_fill=parse_color(stroke.get("color","#000000")))
        return img

    # Lay out the complete string once. Stagger changes each glyph, never line geometry.
    lines: list[list[tuple[str,int,float,int]]] = [[]]; widths = [0.0]
    by = str((stagger or {}).get("by", "character"))
    if by == "line": unit_for = {}; unit=0
    elif by == "word":
        unit_for={}; unit=0; in_word=False
        for i,ch in enumerate(text):
            if ch.isspace(): in_word=False
            else:
                if not in_word: unit += 1; in_word=True
                unit_for[i]=unit-1
    else: unit_for={i:i for i,ch in enumerate(text) if ch!='\n'}
    line_unit=0
    for idx, ch in enumerate(text):
        if ch == "\n": lines.append([]); widths.append(0.0); line_unit += 1; continue
        adv = float(d.textlength(ch, font=font)) + tracking
        if lines[-1] and widths[-1]+adv > max_width:
            lines.append([]); widths.append(0.0); line_unit += 1
        u = line_unit if by=="line" else unit_for.get(idx, 0)
        lines[-1].append((ch, idx, adv, u)); widths[-1] += adv
    line_h = int(size*float(style.get("line_height",1.15)))
    w = max(1, int(max(widths, default=1)+40)); h = max(1, line_h*len(lines)+40)
    img = Image.new("RGBA", (w,h)); align = style.get("align", "center")
    base_color = animated(layer, "color", style.get("color", "#ffffff"), local_t)
    def char_color(index: int) -> Any:
        for span in spans:
            lo, hi = span.get("range", [0,0])
            if int(lo) <= index < int(hi): return span.get("color", base_color)
        return base_color
    max_unit=max((u for row in lines for *_,u in row),default=0)
    for row, chars in enumerate(lines):
        lw = widths[row]; x = 20.0 if align == "left" else (w-lw-20 if align == "right" else (w-lw)/2); y = 20+row*line_h
        for ch, idx, adv, unit_index in chars:
            opacity=100.0; oy=0.0; glyph_scale=100.0
            if stagger:
                order=max_unit-unit_index if str(stagger.get("direction","forward"))=="reverse" else unit_index
                interval=max(.0001,float(stagger.get("interval",stagger.get("stagger",.04))))
                transition=max(.0001,float(stagger.get("duration",interval)))
                q=(local_t-float(stagger.get("delay",0))-order*interval)/transition
                q=ease_value(str(stagger.get("ease","linear")),min(1,max(0,q)))
                op_pair=stagger.get("opacity",[0,100]); off_pair=stagger.get("offset_y",[0,0]); sc_pair=stagger.get("scale",[100,100])
                opacity=float(lerp(op_pair[0],op_pair[-1],q)); oy=float(lerp(off_pair[0],off_pair[-1],q)); glyph_scale=float(lerp(sc_pair[0],sc_pair[-1],q))
            color=list(parse_color(char_color(idx))); color[3]=round(color[3]*max(0,min(100,opacity))/100)
            if glyph_scale==100:
                ImageDraw.Draw(img).text((x,y+oy),ch,font=font,fill=tuple(color),stroke_width=sw,stroke_fill=parse_color(stroke.get("color","#000000")))
            elif opacity>0:
                bb=d.textbbox((0,0),ch,font=font,stroke_width=sw); gw=max(1,bb[2]-bb[0]+2*sw+4); gh=max(1,bb[3]-bb[1]+2*sw+4)
                glyph=Image.new("RGBA",(gw,gh)); gd=ImageDraw.Draw(glyph)
                gd.text((sw+2-bb[0],sw+2-bb[1]),ch,font=font,fill=tuple(color),stroke_width=sw,stroke_fill=parse_color(stroke.get("color","#000000")))
                factor=max(.01,glyph_scale/100); glyph=glyph.resize((max(1,round(gw*factor)),max(1,round(gh*factor))),RESAMPLING.LANCZOS)
                img.alpha_composite(glyph,(round(x+(adv-glyph.width)/2),round(y+oy+(size-glyph.height)/2)))
            x += adv
    return img

def make_shape_layer(layer: dict[str, Any], local_t: float = 0.0) -> Image.Image:
    shape = layer.get("shape", layer)
    w = max(1, int(animated(layer, "width", shape.get("width",400), local_t)))
    h = max(1, int(animated(layer, "height", shape.get("height",200), local_t)))
    stroke = shape.get("stroke", {}); sw = int(stroke.get("width",0)); pad = sw+2 if sw else 0
    img = Image.new("RGBA", (w+2*pad,h+2*pad), (0,0,0,0)); mask = Image.new("L", img.size); d=ImageDraw.Draw(mask)
    kind=shape.get("kind",shape.get("shape","rectangle")); box=(pad,pad,pad+w,pad+h)
    verts: list[tuple[float,float]] = []
    if kind in ("circle","ellipse"): d.ellipse(box, fill=255)
    elif kind in ("polygon","star","burst"):
        points=max(2,int(shape.get("points",5))); outer=min(w,h)/2; inner=float(shape.get("inner_radius",outer*.45)); cx,cy=pad+w/2,pad+h/2
        for i in range(points*2):
            r=outer if i%2==0 else inner; a=-math.pi/2+i*math.pi/points; verts.append((cx+math.cos(a)*r,cy+math.sin(a)*r))
        d.polygon(verts, fill=255)
    elif kind in ("line", "arrow"):
        pts=shape.get("points_xy", [[0,h/2],[w,h/2]]); trim=float(animated(layer,"trim",shape.get("trim",100),local_t))/100
        if len(pts)>=2:
            x1,y1=pts[0]; x2,y2=pts[-1]; end=(pad+x1+(x2-x1)*trim,pad+y1+(y2-y1)*trim)
            d.line([(pad+x1,pad+y1),end],fill=255,width=max(1,sw or int(shape.get("line_width",8))))
            if kind=="arrow" and trim>0:
                ang=math.atan2(y2-y1,x2-x1); sz=float(shape.get("arrow_size",24)); tip=end
                d.polygon([tip,(tip[0]-sz*math.cos(ang-.5),tip[1]-sz*math.sin(ang-.5)),(tip[0]-sz*math.cos(ang+.5),tip[1]-sz*math.sin(ang+.5))],fill=255)
    else: d.rounded_rectangle(box, radius=int(shape.get("radius",0)), fill=255)
    fill_value=animated(layer,"color",shape.get("fill","#ffffff"),local_t)
    if isinstance(fill_value,dict):
        colors=fill_value.get("colors",["#ffffff","#000000"]); stops=fill_value.get("stops",[0,1]); radial=fill_value.get("type")=="radial"
        yy,xx=np.mgrid[0:img.height,0:img.width]; q=np.sqrt(((xx-img.width/2)/(img.width/2))**2+((yy-img.height/2)/(img.height/2))**2) if radial else xx/max(1,img.width-1)
        c0=np.array(parse_color(colors[0]),dtype=float); c1=np.array(parse_color(colors[-1]),dtype=float); arr=c0[None,None,:]+(c1-c0)[None,None,:]*np.clip(q[...,None],float(stops[0]),float(stops[-1])); fill_img=Image.fromarray(np.uint8(np.clip(arr,0,255)),"RGBA")
    else: fill_img=Image.new("RGBA",img.size,parse_color(fill_value))
    img.paste(fill_img,(0,0),mask)
    if sw:
        od=ImageDraw.Draw(img); outline=parse_color(stroke.get("color","#000000"))
        if kind in ("circle","ellipse"): od.ellipse(box,outline=outline,width=sw)
        elif verts: od.line(verts+[verts[0]],fill=outline,width=sw,joint="curve")
        elif kind not in ("line","arrow"): od.rounded_rectangle(box,radius=int(shape.get("radius",0)),outline=outline,width=sw)
    return img

def blend(base: Image.Image, over: Image.Image, pos: tuple[int,int], mode: str) -> Image.Image:
    if mode == "normal":
        base.alpha_composite(over, pos); return base
    layer = Image.new("RGBA", base.size, (0,0,0,0)); layer.alpha_composite(over,pos)
    if mode == "multiply": mixed=ImageChops.multiply(base,layer)
    elif mode == "screen": mixed=ImageChops.screen(base,layer)
    elif mode == "add": mixed=ImageChops.add(base,layer,scale=1.0,offset=0)
    elif mode in ("overlay", "soft-light"):
        b=np.asarray(base,dtype=np.float32)/255; o=np.asarray(layer,dtype=np.float32)/255
        if mode=="overlay": rgb=np.where(b[...,:3]<=.5,2*b[...,:3]*o[...,:3],1-2*(1-b[...,:3])*(1-o[...,:3]))
        else: rgb=(1-2*o[...,:3])*b[...,:3]**2+2*o[...,:3]*b[...,:3]
        arr=np.concatenate([np.clip(rgb,0,1),b[...,3:4]],axis=2); mixed=Image.fromarray(np.uint8(arr*255),"RGBA")
    else: die(f"지원하지 않는 합성 모드: {mode}"); return base
    mask=layer.getchannel("A"); return Image.composite(mixed,base,mask)


def _unknown(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    bad=set(obj)-allowed
    if bad: die(f"프로젝트 값이 잘못되었습니다 ({where}): 알 수 없는 속성 {sorted(bad)[0]!r}")


def validate_project_data(data: dict[str, Any]) -> None:
    _unknown(data,{"version","main","compositions","audio"},"root")
    for ai,audio in enumerate(data.get("audio",[])):
        _unknown(audio,AUDIO_KEYS,f"audio.{ai}")
        if not audio.get("source"): die(f"프로젝트 값이 잘못되었습니다 (audio.{ai}.source): 필수 값입니다")
    effect_keys={
        "color":{"type","contrast","saturation","brightness","gamma","temperature"}, "blur":{"type","radius","amount"},
        "sharpen":{"type","amount"}, "glow":{"type","radius","amount"}, "vignette":{"type","amount"},
        "grain":{"type","amount","seed"}, "noise":{"type","amount","seed"}, "invert":{"type"}, "sepia":{"type"},
        "edge":{"type"}, "flip":{"type","direction"}, "drop_shadow":{"type","offset","blur","color"},
        "wiggle":{"type","freq","amp","seed","until"},
    }
    for ci, comp in enumerate(data.get("compositions", [])):
        _unknown(comp,COMPOSITION_KEYS,f"compositions.{ci}")
        ids: set[str] = set()
        for li, layer in enumerate(comp.get("layers", [])):
            where=f"compositions.{ci}.layers.{li}"; kind=layer.get("type")
            if kind not in LAYER_TYPES: die(f"프로젝트 값이 잘못되었습니다 ({where}.type): 허용되지 않은 레이어 종류 {kind!r}")
            _unknown(layer,LAYER_KEYS,where)
            if kind in ("video","image","audio") and not layer.get("source"): die(f"프로젝트 값이 잘못되었습니다 ({where}.source): 필수 값입니다")
            if layer.get("tail","hold") not in {"hold","loop","error"}: die(f"프로젝트 값이 잘못되었습니다 ({where}.tail): hold, loop, error 중 하나여야 합니다")
            transform=layer.get("transform",{}); _unknown(transform,TRANSFORM_KEYS,f"{where}.transform")
            for prop, frames in layer.get("keyframes",{}).items():
                if prop not in KEYFRAME_KEYS: die(f"프로젝트 값이 잘못되었습니다 ({where}.keyframes): 구현되지 않았거나 알 수 없는 속성 {prop!r}")
                if not isinstance(frames,list) or any(not isinstance(k,dict) or "t" not in k or "v" not in k or set(k)-{"t","v","ease"} for k in frames):
                    die(f"프로젝트 값이 잘못되었습니다 ({where}.keyframes.{prop}): 각 키프레임에는 t, v, 선택적 ease만 허용됩니다")
            for ei, fx in enumerate(layer.get("effects",[])):
                ft=fx.get("type")
                if ft not in EFFECT_TYPES: die(f"프로젝트 값이 잘못되었습니다 ({where}.effects.{ei}.type): 지원하지 않는 효과 {ft!r}")
                _unknown(fx,effect_keys[ft],f"{where}.effects.{ei}")
            if layer.get("blend","normal") not in {"normal","multiply","screen","overlay","add","soft-light"}: die(f"프로젝트 값이 잘못되었습니다 ({where}.blend): 지원하지 않는 합성 모드")
            shape=layer.get("shape",{}) if isinstance(layer.get("shape",{}),dict) else {}
            if kind=="shape":
                _unknown(shape,{"width","height","fill","color","stroke","kind","shape","radius","points","inner_radius","trim","points_xy","line_width","arrow_size"},f"{where}.shape")
                sk=shape.get("kind",shape.get("shape",layer.get("kind","rectangle")))
                if sk not in SHAPE_KINDS:die(f"프로젝트 값이 잘못되었습니다 ({where}.shape.kind): 지원하지 않는 도형 {sk!r}")
            for owner,name in ((layer.get("style",{}),"style"),(shape.get("stroke",{}),"shape.stroke")):
                if isinstance(owner,dict):
                    allowed={"font","size","color","stroke","shadow","align","max_width","line_height","tracking","spans"} if name=="style" else {"width","color"}
                    _unknown(owner,allowed,f"{where}.{name}")
            style=layer.get("style",{})
            if isinstance(style,dict):
                if isinstance(style.get("stroke"),dict):_unknown(style["stroke"],{"width","color"},f"{where}.style.stroke")
                if isinstance(style.get("shadow"),dict):_unknown(style["shadow"],{"offset","color","blur"},f"{where}.style.shadow")
                for si,span in enumerate(style.get("spans",[])):_unknown(span,{"range","color","font","size","weight"},f"{where}.style.spans.{si}")
            if isinstance(layer.get("counter"),dict):_unknown(layer["counter"],{"from","to","duration","ease","format"},f"{where}.counter")
            if isinstance(layer.get("animator"),dict):
                _unknown(layer["animator"],{"typewriter","stagger"},f"{where}.animator")
                if isinstance(layer["animator"].get("typewriter"),dict):_unknown(layer["animator"]["typewriter"],{"chars_per_second"},f"{where}.animator.typewriter")
                if isinstance(layer["animator"].get("stagger"),dict):_unknown(layer["animator"]["stagger"],{"by","interval","stagger","delay","direction","duration","opacity","offset_y","scale","ease"},f"{where}.animator.stagger")
            masks=layer.get("mask",[]);masks=masks if isinstance(masks,list) else ([masks] if masks else [])
            for mi,mask in enumerate(masks):_unknown(mask,{"type","box","points","feather","invert","progress_keyframes"},f"{where}.mask.{mi}")
            lid=str(layer.get("id",f"layer_{li}"))
            if lid in ids:die(f"프로젝트 값이 잘못되었습니다 ({where}.id): 중복 id {lid!r}")
            ids.add(lid)



def apply_layer_presets(layer: dict[str, Any], comp_duration: float, base: Path|None = None) -> dict[str, Any]:
    out=copy.deepcopy(layer); transform=out.setdefault("transform",{}); modifiers=[]
    start=float(out.get("start",0)); dur=max(0.001,layer_end(out,comp_duration)-start)
    def mod(prop:str,frames:list[dict[str,Any]],operation:str)->None:modifiers.append({"prop":prop,"frames":frames,"operation":operation})
    for item in out.get("presets",[]):
        name=str(item.get("name")); t=float(item.get("t",0)); amount=float(item.get("amount",100))
        if name=="zoom_punch":mod("scale",[{"t":t,"v":[110,110],"ease":"ease-out"},{"t":t+.22,"v":[100,100]}],"multiply")
        elif name=="slow_push":mod("scale",[{"t":t,"v":[100,100]},{"t":t+dur,"v":[105,105],"ease":"linear"}],"multiply")
        elif name=="pop_in":
            mod("scale",[{"t":t,"v":[0,0],"ease":"back"},{"t":t+.16,"v":[115,115],"ease":"ease-out"},{"t":t+.28,"v":[100,100]}],"multiply")
            mod("opacity",[{"t":t,"v":0},{"t":t+.1,"v":100}],"multiply")
        elif name=="stamp":
            mod("scale",[{"t":t,"v":[150,150],"ease":"ease-out"},{"t":t+.18,"v":[100,100]}],"multiply");out.setdefault("effects",[]).append({"type":"wiggle","freq":28,"amp":8,"seed":17,"until":t+.22})
        elif name=="bar_fill":transform.setdefault("anchor",[0,.5]);mod("scale",[{"t":t,"v":[0,100],"ease":"ease-out"},{"t":t+.35,"v":[amount,100]}],"multiply")
        elif name=="crossfade":mod("opacity",[{"t":t,"v":0},{"t":t+.25,"v":100}],"multiply")
        elif name=="slide_up":mod("position",[{"t":t,"v":[0,80],"ease":"ease-out"},{"t":t+.3,"v":[0,0]}],"add")
        elif name=="flash":mod("opacity",[{"t":t,"v":100},{"t":t+.1,"v":0}],"multiply")
        elif name=="shake":out.setdefault("effects",[]).append({"type":"wiggle","freq":18,"amp":float(item.get("amp",10)),"seed":int(item.get("seed",1))})
        elif name=="wipe":out["mask"]={"type":"rectangle","progress_keyframes":[{"t":t,"v":0},{"t":t+.35,"v":100}],"feather":item.get("feather",0)}
        else:
            preset_path=(base/"presets"/f"{name}.json") if base else None
            if not preset_path or not preset_path.exists():die(f"알 수 없는 프리셋: {name}")
            try:spec=json.loads(preset_path.read_text(encoding="utf-8"))
            except (OSError,json.JSONDecodeError) as exc:die(f"프리셋을 읽을 수 없습니다: {preset_path}: {exc}")
            out.setdefault("effects",[]).extend(copy.deepcopy(spec.get("effects",[])))
            for prop,value in spec.get("transform",{}).items():
                neutral=[100,100] if prop=="scale" else (100 if prop=="opacity" else ([0,0] if prop=="position" else 0))
                mod(prop,[{"t":t,"v":neutral},{"t":t,"v":value}],"multiply" if prop in ("scale","opacity") else "add")
            for prop,frames in spec.get("keyframes",{}).items():
                shifted=copy.deepcopy(frames)
                for frame in shifted:frame["t"]=float(frame["t"])+t
                mod(prop,shifted,"multiply" if prop in ("scale","opacity") else "add")
    out["_preset_modifiers"]=modifiers
    return out


@dataclass
class Project:
    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "Project":
        try: data=json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError,json.JSONDecodeError) as exc: die(f"프로젝트 JSON을 읽을 수 없습니다: {exc}")
        try: jsonschema.validate(data,PROJECT_SCHEMA)
        except jsonschema.ValidationError as exc:
            location=".".join(str(x) for x in exc.absolute_path) or "root"
            die(f"프로젝트 값이 잘못되었습니다 ({location}): {exc.message}")
        validate_project_data(data)
        return cls(path.resolve(),data)

    def comp(self, comp_id: str | None=None) -> dict[str,Any]:
        cid=comp_id or self.data.get("main") or self.data["compositions"][0]["id"]
        for c in self.data["compositions"]:
            if c["id"]==cid:return c
        die(f"컴포지션을 찾을 수 없습니다: {cid}"); return {}


class VideoDecoder:
    """Sequential raw-video decoder with accurate hold/loop/error tail policies."""
    def __init__(self,path:Path,fps:float,speed:float,start_in:float,output_size:tuple[int,int],fit:str|None,tail:str="hold"):
        self.path=path;self.fps=fps;self.speed=speed;self.start_in=start_in;self.output_size=output_size;self.fit=fit;self.tail=tail
        info=ffprobe(path);stream=next((x for x in info.get("streams",[]) if x.get("codec_type")=="video"),None)
        if not stream:die(f"영상 스트림이 없습니다: {path}")
        sw,sh=int(stream["width"]),int(stream["height"]);tw,th=output_size;self.w,self.h=((tw,th) if fit else (sw,sh))
        self.proc:subprocess.Popen[bytes]|None=None;self.next_index=0
        self.last_frame:Image.Image|None=None;self.last_index:int|None=None;self.tail_frame:Image.Image|None=None
        self.loop_frames=max(1,math.ceil(max(0,media_duration(path)-start_in)*fps/max(speed,.0001)))
        # A short loop is cheaper and more reliable when its first pass is retained.
        self.loop_cache:dict[int,Image.Image]={}
        self.cache_loop=tail=="loop" and self.loop_frames*self.w*self.h*4<=256*1024*1024
    def _filters(self)->str:
        rate=self.fps/max(.0001,self.speed);filters=[f"fps={rate:.12g}"];tw,th=self.output_size
        if self.fit=="stretch":filters.append(f"scale={tw}:{th}")
        elif self.fit=="contain":filters += [f"scale={tw}:{th}:force_original_aspect_ratio=decrease",f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=0x00000000"]
        elif self.fit=="cover":filters += [f"scale={tw}:{th}:force_original_aspect_ratio=increase",f"crop={tw}:{th}"]
        return ",".join(filters)
    def restart(self,index:int)->None:
        self.close();seek=self.start_in+index*self.speed/self.fps
        cmd=["ffmpeg","-v","error","-ss",f"{seek:.9f}","-i",str(self.path),"-an","-sn","-vf",self._filters(),"-f","rawvideo","-pix_fmt","rgba","-"]
        self.proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE);self.next_index=index
    def _read_source(self,index:int)->Image.Image|None:
        if self.cache_loop and index in self.loop_cache:return self.loop_cache[index].copy()
        if self.proc is None or index!=self.next_index:self.restart(index)
        assert self.proc and self.proc.stdout;need=self.w*self.h*4;data=bytearray()
        while len(data)<need:
            chunk=self.proc.stdout.read(need-len(data))
            if not chunk:break
            data.extend(chunk)
        if len(data)!=need:
            self.close();return None
        frame=Image.frombytes("RGBA",(self.w,self.h),bytes(data));self.next_index+=1
        self.last_frame=frame;self.last_index=index
        if index==self.loop_frames-1:self.tail_frame=frame.copy()
        if self.cache_loop:self.loop_cache[index]=frame.copy()
        return frame.copy()
    def _read_tail(self)->Image.Image:
        if self.tail_frame is not None:return self.tail_frame.copy()
        # Do not use last_frame here: a preview/contact-sheet may have last read
        # any arbitrary earlier time. Seek specifically to the source's tail.
        frame=self._read_source(self.loop_frames-1)
        if frame is not None:self.tail_frame=frame.copy();return frame
        # Duration metadata can round one frame high. Walk back only until an
        # actual final frame is found and make that the canonical loop length.
        for index in range(self.loop_frames-2,max(-1,self.loop_frames-7),-1):
            frame=self._read_source(index)
            if frame is not None:
                self.loop_frames=index+1;self.tail_frame=frame.copy();return frame
        die(f"영상 프레임을 읽지 못했습니다(원본 끝): {self.path}");return Image.new("RGBA",(self.w,self.h))
    def read(self,index:int)->Image.Image:
        if index<0:index=0
        if index>=self.loop_frames:
            if self.tail=="hold":return self._read_tail()
            if self.tail=="error":die(f"영상 프레임을 읽지 못했습니다(원본 끝): {self.path}")
            index%=self.loop_frames
        frame=self._read_source(index)
        if frame is not None:return frame
        # If sequential decoding exposed slightly shorter media than metadata,
        # correct the canonical length once and apply the requested policy.
        if self.last_frame is not None and self.last_index==index-1:
            self.loop_frames=max(1,index);self.tail_frame=self.last_frame.copy()
            if self.tail=="hold":return self.tail_frame.copy()
            if self.tail=="loop":return self.read(index%self.loop_frames)
        if self.tail=="hold":return self._read_tail()
        die(f"영상 프레임을 읽지 못했습니다(원본 끝): {self.path}");return Image.new("RGBA",(self.w,self.h))
    def close(self)->None:
        if self.proc:
            if self.proc.stdout:self.proc.stdout.close()
            if self.proc.stderr:self.proc.stderr.close()
            if self.proc.poll() is None:self.proc.terminate()
            try:self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait()
        self.proc=None


class Renderer:
    def __init__(self, project: Project, comp_id: str|None=None, scale: float=1.0, shared: dict[str,"Renderer"]|None=None):
        self.project=project; self.comp=copy.deepcopy(project.comp(comp_id)); self.base=project.path.parent; self.scale=scale
        self.w=max(1,round(self.comp["width"]*scale)); self.h=max(1,round(self.comp["height"]*scale)); self.fps=float(self.comp["fps"])
        self.comp["layers"]=[apply_layer_presets(x,float(self.comp["duration"]),self.base) for x in self.comp.get("layers",[])]
        self.layers_by_id={str(x.get("id",f"layer_{i}")):x for i,x in enumerate(self.comp["layers"])}
        self.image_cache: dict[Path,Image.Image]={}; self.layer_cache: dict[str,Image.Image]={}; self.video_decoders: dict[str,VideoDecoder]={}; self.precomps=shared if shared is not None else {}
        self.matte_ids={str(m.get("layer")) if isinstance(m,dict) else str(m) for x in self.comp["layers"] for m in [x.get("matte")] if m}
        self.precomps[self.comp["id"]]=self

    def close(self)->None:
        for decoder in self.video_decoders.values(): decoder.close()
        self.video_decoders.clear()
        for renderer in set(self.precomps.values()):
            if renderer is not self:
                for decoder in renderer.video_decoders.values(): decoder.close()

    def source_image(self, layer: dict[str,Any], t: float) -> Image.Image:
        kind=layer["type"]
        if kind=="video":
            path=resolve_path(self.base,layer["source"]); key=str(layer.get("id",id(layer)))
            if not path.exists(): die(f"영상 파일을 찾을 수 없습니다: {path}")
            if key not in self.video_decoders:
                self.video_decoders[key]=VideoDecoder(path,self.fps,float(layer.get("speed",1)),float(layer.get("in",0)),(self.w,self.h),layer.get("fit"),str(layer.get("tail","hold")))
            return self.video_decoders[key].read(max(0,round(t*self.fps)))
        if kind=="image":
            path=resolve_path(self.base,layer["source"])
            if not path.exists(): die(f"이미지 파일을 찾을 수 없습니다: {path}")
            if path not in self.image_cache:self.image_cache[path]=Image.open(path).convert("RGBA")
            return self.image_cache[path].copy()
        if kind in ("text","shape"):
            dynamic=bool(layer.get("keyframes") or layer.get("animator") or layer.get("counter"))
            key=str(layer.get("id",id(layer)))
            if not dynamic and key in self.layer_cache:return self.layer_cache[key].copy()
            image=make_text_layer(layer,t) if kind=="text" else make_shape_layer(layer,t)
            if not dynamic:self.layer_cache[key]=image.copy()
            return image
        if kind=="solid":
            size=layer.get("size",[self.comp["width"],self.comp["height"]]); return Image.new("RGBA",(max(1,round(float(size[0])*self.scale)),max(1,round(float(size[1])*self.scale))),parse_color(animated(layer,"color",layer.get("color","#000000"),t)))
        if kind=="comp":
            cid=layer.get("composition")
            if cid not in self.precomps:self.precomps[cid]=Renderer(self.project,cid,self.scale,self.precomps)
            return self.precomps[cid].render_frame(t*float(layer.get("speed",1)))
        return Image.new("RGBA",(1,1),(0,0,0,0))

    def transform_values(self,layer:dict[str,Any],local:float,seen:set[str]|None=None)->tuple[list[float],list[float],float,float]:
        pos=list(animated(layer,"position",[self.comp["width"]/2,self.comp["height"]/2],local));sc=list(animated(layer,"scale",[100,100],local));rot=float(animated(layer,"rotation",0,local));op=float(animated(layer,"opacity",100,local))
        for modifier in layer.get("_preset_modifiers",[]):
            prop=modifier["prop"];holder={"keyframes":{prop:modifier["frames"]}};neutral=[100,100] if prop=="scale" else (100 if prop=="opacity" else ([0,0] if prop=="position" else 0));value=animated(holder,prop,neutral,local)
            if prop=="scale":sc=[sc[0]*float(value[0])/100,sc[1]*float(value[1])/100]
            elif prop=="opacity":op*=float(value)/100
            elif prop=="position":pos=[pos[0]+float(value[0]),pos[1]+float(value[1])]
            elif prop=="rotation":rot+=float(value)
        for fx in layer.get("effects",[]):
            if fx.get("type")=="wiggle" and local<=float(fx.get("until",1e20)):
                seed=float(fx.get("seed",1));freq=float(fx.get("freq",12));amp=float(fx.get("amp",8));pos[0]+=math.sin((local*freq+seed)*6.2831853)*amp;pos[1]+=math.sin((local*freq*1.173+seed*2.31)*6.2831853)*amp
        parent_id=layer.get("parent")
        if parent_id:
            seen=set() if seen is None else seen
            if parent_id in seen:die(f"부모 레이어 순환 참조: {parent_id}")
            seen.add(parent_id);parent=self.layers_by_id.get(str(parent_id))
            if not parent:die(f"부모 레이어를 찾을 수 없습니다: {parent_id}")
            plocal=local+float(layer.get("start",0))-float(parent.get("start",0));pp,ps,pr,po=self.transform_values(parent,plocal,seen)
            x,y=pos[0]*ps[0]/100,pos[1]*ps[1]/100;rad=math.radians(pr);pos=[pp[0]+x*math.cos(rad)-y*math.sin(rad),pp[1]+x*math.sin(rad)+y*math.cos(rad)];sc=[sc[0]*ps[0]/100,sc[1]*ps[1]/100];rot+=pr
            if layer.get("inherit_opacity"):op*=po/100
        return pos,sc,rot,op

    def apply_mask(self,img:Image.Image,layer:dict[str,Any],local:float)->Image.Image:
        spec=layer.get("mask")
        if not spec:return img
        masks=spec if isinstance(spec,list) else [spec]; alpha=img.getchannel("A")
        for mask in masks:
            m=Image.new("L",img.size); d=ImageDraw.Draw(m); kind=mask.get("type","rectangle")
            progress=100.0
            frames=mask.get("progress_keyframes")
            if frames:
                holder={"keyframes":{"progress":frames}};progress=float(animated(holder,"progress",100,local))
            box=mask.get("box",[0,0,img.width*progress/100,img.height])
            if kind=="ellipse":d.ellipse(tuple(box),fill=255)
            elif kind=="polygon":d.polygon([tuple(p) for p in mask.get("points",[])],fill=255)
            else:d.rectangle(tuple(box),fill=255)
            feather=float(mask.get("feather",0))
            if feather:m=m.filter(ImageFilter.GaussianBlur(feather))
            if mask.get("invert"):m=ImageChops.invert(m)
            alpha=ImageChops.multiply(alpha,m)
        img.putalpha(alpha);return img

    def prepare_layer(self,img:Image.Image,layer:dict[str,Any],local:float)->tuple[Image.Image,tuple[int,int]]:
        img=self.apply_mask(img,layer,local);pos,scale,rotation,opacity=self.transform_values(layer,local)
        preview_scale=1.0 if layer.get("type")=="video" and layer.get("fit") else self.scale
        sx,sy=float(scale[0])/100*preview_scale,float(scale[1])/100*preview_scale
        target=(max(1,round(img.width*sx)),max(1,round(img.height*sy)))
        if target!=img.size:img=img.resize(target,RESAMPLING.LANCZOS)
        anchor=animated(layer,"anchor",[.5,.5],local);ax=float(anchor[0])*img.width;ay=float(anchor[1])*img.height
        if not rotation:
            if opacity<100:img.putalpha(img.getchannel("A").point(lambda a:int(a*max(0,opacity)/100)))
            return img,(round(float(pos[0])*self.scale-ax),round(float(pos[1])*self.scale-ay))
        halfx=math.ceil(max(ax,img.width-ax))+2;halfy=math.ceil(max(ay,img.height-ay))+2
        centered=Image.new("RGBA",(halfx*2,halfy*2));centered.alpha_composite(img,(round(halfx-ax),round(halfy-ay)))
        centered=centered.rotate(-rotation,expand=True,resample=RESAMPLING.BICUBIC)
        if opacity<100:centered.putalpha(centered.getchannel("A").point(lambda a:int(a*max(0,opacity)/100)))
        return centered,(round(float(pos[0])*self.scale-centered.width/2),round(float(pos[1])*self.scale-centered.height/2))

    def full_surface(self,small:Image.Image,pos:tuple[int,int])->Image.Image:
        full=Image.new("RGBA",(self.w,self.h));full.alpha_composite(small,pos);return full

    def place_layer(self,img:Image.Image,layer:dict[str,Any],local:float)->Image.Image:
        small,pos=self.prepare_layer(img,layer,local);return self.full_surface(small,pos)

    def motion_blur_surface(self,img:Image.Image,layer:dict[str,Any],local:float)->Image.Image|None:
        if not layer.get("motion_blur"):return None
        shutter=max(0,float(layer.get("shutter_angle",180)))/360/self.fps;earlier=max(0,local-shutter)
        a=self.transform_values(layer,earlier)[:3];b=self.transform_values(layer,local)[:3]
        if all(abs(float(x)-float(y))<1e-6 for av,bv in zip(a,b) for x,y in zip(av if isinstance(av,list) else [av],bv if isinstance(bv,list) else [bv])):return None
        samples=max(2,int(layer.get("samples",8)));acc=np.zeros((self.h,self.w,4),dtype=np.float32)
        for i in range(samples):
            st=earlier+(local-earlier)*(i/(samples-1));small,pos=self.prepare_layer(img,layer,st);acc+=np.asarray(self.full_surface(small,pos),dtype=np.float32)
        return Image.fromarray(np.uint8(np.clip(acc/samples,0,255)),"RGBA")

    def render_frame(self,t:float,comp_id:str|None=None)->Image.Image:
        if comp_id and comp_id!=self.comp["id"]:
            if comp_id not in self.precomps:self.precomps[comp_id]=Renderer(self.project,comp_id,self.scale,self.precomps)
            return self.precomps[comp_id].render_frame(t)
        canvas=Image.new("RGBA",(self.w,self.h),parse_color(self.comp.get("background","#000000")));surfaces:dict[str,Image.Image]={}
        for i,layer in enumerate(self.comp.get("layers",[])):
            lid=str(layer.get("id",f"layer_{i}"));start=float(layer.get("start",0));end=layer_end(layer,float(self.comp["duration"]))
            if layer.get("type")=="video" and t>=end and lid in self.video_decoders:
                self.video_decoders.pop(lid).close()
            matte_source=lid in self.matte_ids
            if (layer.get("hidden") and not matte_source) or layer.get("type") in ("null","audio"):continue
            if not(start<=t<end):continue
            local=t-start
            if layer["type"]=="adjustment":
                if not layer.get("hidden"):canvas=apply_effects(canvas,layer.get("effects",[]),round(t*self.fps))
                continue
            img=self.source_image(layer,local)
            if layer.get("fit") and layer["type"]!="video":img=contain_cover(img,(self.w,self.h),layer["fit"])
            img=apply_effects(img,[x for x in layer.get("effects",[]) if x.get("type")!="wiggle"],round(t*self.fps))
            motion=self.motion_blur_surface(img,layer,local)
            need_full=motion is not None or matte_source or bool(layer.get("matte")) or layer.get("blend","normal")!="normal"
            if motion is not None:surface=motion;small=None;pos=(0,0)
            else:
                small,pos=self.prepare_layer(img,layer,local);surface=self.full_surface(small,pos) if need_full else None
            matte=layer.get("matte")
            if matte:
                if isinstance(matte,str):mid,mtype=matte,"alpha"
                else:mid,mtype=str(matte.get("layer")),str(matte.get("type","alpha"))
                if mid not in surfaces:die(f"매트 레이어가 대상보다 먼저 렌더되어야 합니다: {mid}")
                assert surface is not None;mask=surfaces[mid].convert("L") if mtype.startswith("luma") else surfaces[mid].getchannel("A")
                if "invert" in mtype:mask=ImageChops.invert(mask)
                surface.putalpha(ImageChops.multiply(surface.getchannel("A"),mask))
            if matte_source:
                if surface is None:surface=self.full_surface(small,pos) # type: ignore[arg-type]
                surfaces[lid]=surface
                continue
            if layer.get("hidden"):continue
            if surface is not None:canvas=blend(canvas,surface,(0,0),str(layer.get("blend","normal")))
            else:canvas.alpha_composite(small,pos) # type: ignore[arg-type]
        return canvas

    def audio_inputs_and_filter(self,start:float,duration:float)->tuple[list[str],str,str|None]:
        args:list[str]=[];chains:list[str]=[];labels:list[str]=[];entries:list[tuple[dict[str,Any],bool]]=[]
        entries += [(x,True) for x in self.project.data.get("audio",[]) if not x.get("mute")]
        for layer in self.comp.get("layers",[]):
            if layer.get("type")=="audio" and layer.get("source") and not layer.get("mute"):entries.append((layer,True))
            if layer.get("type")=="video" and not layer.get("mute",False) and layer.get("source"):
                entries.append(({"source":layer["source"],"start":layer.get("start",0),"in":layer.get("in",0),"out":layer.get("out"),"volume_db":layer.get("volume_db",0),"speed":layer.get("speed",1),"end":layer_end(layer,float(self.comp["duration"])),"fade_in":layer.get("fade_in"),"fade_out":layer.get("fade_out")},False))
        input_index=1
        for a,explicit in entries:
            path=resolve_path(self.base,a["source"])
            if not path.exists():die(f"오디오 파일을 찾을 수 없습니다: {path}")
            if not has_stream(path,"audio"):
                if explicit:die(f"프로젝트 audio 항목에 오디오 스트림이 없습니다: {path}")
                continue
            args += ["-i",str(path)];delay=max(0,float(a.get("start",0))-start);trim=max(0,start-float(a.get("start",0)))+float(a.get("in",0));speed=float(a.get("speed",1))
            if speed<=0:die("오디오 speed는 0보다 커야 합니다.")
            pieces=[f"atrim=start={trim:.9f}"]
            out_value=a.get("out");clip_source=float(media_duration(path) if out_value is None else out_value)-float(a.get("in",0));clip_duration=max(0,clip_source/speed)
            if a.get("duration") is not None:clip_duration=min(clip_duration,float(a["duration"]))
            if a.get("end") is not None:clip_duration=min(clip_duration,max(0,float(a["end"])-float(a.get("start",0))))
            if speed!=1:
                remain=speed
                while remain>2:pieces.append("atempo=2");remain/=2
                while remain<.5:pieces.append("atempo=.5");remain*=2
                if abs(remain-1)>.000001:pieces.append(f"atempo={remain:.9f}")
            pieces.append(f"atrim=duration={clip_duration:.9f}")
            pieces.append(f"volume={float(a.get('volume_db',0))}dB")
            if a.get("fade_in"):pieces.append(f"afade=t=in:st=0:d={float(a['fade_in']):.9f}")
            if a.get("fade_out"):pieces.append(f"afade=t=out:st={max(0,clip_duration-float(a['fade_out'])):.9f}:d={float(a['fade_out']):.9f}")
            pieces += [f"adelay={round(delay*1000)}|{round(delay*1000)}",f"atrim=duration={duration:.9f}","asetpts=N/SR/TB"]
            label=f"a{len(labels)}";chains.append(f"[{input_index}:a]{','.join(pieces)}[{label}]");labels.append(f"[{label}]");input_index+=1
        if not labels:return args,"",None
        chains.append("".join(labels)+f"amix=inputs={len(labels)}:normalize=0:duration=longest,apad,atrim=duration={duration:.9f}[aout]")
        return args,";".join(chains),"[aout]"

    def render_contact_sheet(self,times:list[float],out:Path,columns:int=3)->None:
        frames=[self.render_frame(t) for t in times];thumb_w=max(1,self.w//max(1,columns));thumb_h=max(1,round(self.h*thumb_w/self.w));rows=math.ceil(len(frames)/columns)
        sheet=Image.new("RGBA",(thumb_w*columns,thumb_h*rows),(24,24,24,255))
        for i,frame in enumerate(frames):sheet.alpha_composite(frame.resize((thumb_w,thumb_h),RESAMPLING.LANCZOS),((i%columns)*thumb_w,(i//columns)*thumb_h))
        out.parent.mkdir(parents=True,exist_ok=True);sheet.save(out)

    def fast_video_layer(self)->dict[str,Any]|None:
        visible=[x for x in self.comp.get("layers",[]) if not x.get("hidden") and x.get("type") not in ("null","audio")]
        if len(visible)!=1 or visible[0].get("type")!="video":return None
        layer=visible[0];tr=layer.get("transform",{})
        if layer.get("effects") or layer.get("keyframes") or layer.get("presets") or layer.get("mask") or layer.get("matte") or layer.get("parent") or layer.get("motion_blur"):return None
        if layer.get("tail")=="loop" and float(layer.get("in",0))!=0:return None
        if layer.get("blend","normal")!="normal" or float(layer.get("start",0))!=0:return None
        if abs(layer_end(layer,float(self.comp["duration"]))-float(self.comp["duration"]))>1e-6:return None
        if tr.get("anchor",[.5,.5])!=[.5,.5] or tr.get("scale",[100,100])!=[100,100] or float(tr.get("rotation",0))!=0 or float(tr.get("opacity",100))!=100:return None
        if tr.get("position",[self.comp["width"]/2,self.comp["height"]/2])!=[self.comp["width"]/2,self.comp["height"]/2]:return None
        return layer

    def render_fast(self,layer:dict[str,Any],out:Path,start:float,end:float)->None:
        path=resolve_path(self.base,layer["source"]);speed=float(layer.get("speed",1));start_in=float(layer.get("in",0));tail=str(layer.get("tail","hold"));needed=end-start
        source_duration=media_duration(path);source_span=max(0,source_duration-start_in);seek=start_in+start*speed;loop_input: list[str]=[]
        if tail=="loop":
            if source_span<=0:die(f"반복할 영상 프레임이 없습니다: {path}")
            seek=start_in+(start*speed)%source_span
            if start_in==0:loop_input=["-stream_loop","-1"]
        elif tail=="hold" and seek>=source_duration:
            # A range that starts after EOF still needs one real tail frame for tpad.
            seek=max(start_in,source_duration-speed/self.fps)
        audio_args,afilter,alabel=self.audio_inputs_and_filter(start,needed);fps_arg=str(Fraction(str(self.comp["fps"])).limit_denominator(1001));fit=layer.get("fit","contain")
        if fit=="stretch":vf=f"scale={self.w}:{self.h}"
        elif fit=="cover":vf=f"scale={self.w}:{self.h}:force_original_aspect_ratio=increase,crop={self.w}:{self.h}"
        else:vf=f"scale={self.w}:{self.h}:force_original_aspect_ratio=decrease,pad={self.w}:{self.h}:(ow-iw)/2:(oh-ih)/2:color=black"
        vf+=f",setpts=PTS/{speed:.9f},fps={fps_arg}"
        available=max(0,(source_duration-seek)/speed)
        if tail=="loop" and start_in==0:available=needed
        if needed>available and tail=="hold":vf+=f",tpad=stop_mode=clone:stop_duration={needed-available+.1:.9f}"
        elif needed>available and tail=="error":die(f"영상 원본이 레이어보다 짧습니다: {path}")
        cmd=["ffmpeg","-y","-v","error"]+loop_input+["-ss",f"{seek:.9f}","-i",str(path)]+audio_args+["-vf",vf]
        if afilter:cmd += ["-filter_complex",afilter]
        cmd += ["-map","0:v:0"]
        if alabel:cmd += ["-map",alabel]
        if out.suffix.lower()==".mov":cmd += ["-c:v","prores_ks","-profile:v","4","-pix_fmt","yuva444p10le"]
        else:cmd += ["-c:v","libx264","-crf","18","-preset","fast","-pix_fmt","yuv420p","-movflags","+faststart"]
        if alabel:cmd += ["-c:a","aac","-b:a","192k"]
        cmd += ["-t",f"{end-start:.9f}",str(out)];p=subprocess.run(cmd,capture_output=True)
        if p.returncode:die(f"ffmpeg 렌더 실패:\n{p.stderr.decode(errors='replace').strip()}")

    def render(self,out:Path,start:float=0,end:float|None=None)->None:
        require_ffmpeg();end=float(self.comp["duration"]) if end is None else end
        if start<0 or end<=start or end>float(self.comp["duration"])+1e-6:die("--range 값이 컴포지션 범위를 벗어났습니다.")
        frames=round((end-start)*self.fps);out.parent.mkdir(parents=True,exist_ok=True)
        try:
            fast=self.fast_video_layer()
            if fast and out.suffix.lower() in (".mp4",".mov"):
                self.render_fast(fast,out,start,end);return
            if out.suffix.lower()==".png":
                if "%" not in out.name and frames!=1:die("PNG 시퀀스 출력은 폴더 경로 또는 frame_%06d.png 패턴을 사용하세요.")
                if frames==1:self.render_frame(start).save(out);return
                for n in range(frames):self.render_frame(start+n/self.fps).save(Path(str(out)%n))
                return
            if out.suffix.lower() not in (".mp4",".mov"):
                out.mkdir(parents=True,exist_ok=True)
                for n in range(frames):
                    self.render_frame(start+n/self.fps).save(out/f"frame_{n:06d}.png")
                    if n%max(1,round(self.fps))==0:print(f"\r{n+1}/{frames}",end="",file=sys.stderr)
                print(file=sys.stderr);return
            audio_args,afilter,alabel=self.audio_inputs_and_filter(start,end-start);fps_arg=str(Fraction(str(self.comp["fps"])).limit_denominator(1001))
            cmd=["ffmpeg","-y","-v","error","-f","rawvideo","-pix_fmt","rgba","-s",f"{self.w}x{self.h}","-r",fps_arg,"-i","-"]+audio_args
            if afilter:cmd += ["-filter_complex",afilter]
            cmd += ["-map","0:v:0"]
            if alabel:cmd += ["-map",alabel]
            if out.suffix.lower()==".mov":cmd += ["-c:v","prores_ks","-profile:v","4","-pix_fmt","yuva444p10le"]
            else:cmd += ["-c:v","libx264","-crf","18","-preset","veryfast","-pix_fmt","yuv420p","-movflags","+faststart"]
            if alabel:cmd += ["-c:a","aac","-b:a","192k"]
            cmd += ["-t",f"{end-start:.9f}",str(out)];proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stderr=subprocess.PIPE);assert proc.stdin is not None
            try:
                for n in range(frames):
                    proc.stdin.write(self.render_frame(start+n/self.fps).tobytes())
                    if n%max(1,round(self.fps))==0:print(f"\r{n+1}/{frames}",end="",file=sys.stderr)
                proc.stdin.close();err=proc.stderr.read().decode(errors="replace") if proc.stderr else "";code=proc.wait()
            except BrokenPipeError:err=proc.stderr.read().decode(errors="replace") if proc.stderr else "";code=proc.wait()
            print(file=sys.stderr)
            if code:die(f"ffmpeg 렌더 실패:\n{err.strip()}")
        finally:self.close()


def parse_range(text: str|None,duration: float)->tuple[float,float]:
    if not text:return 0,duration
    try:a,b=text.split("-",1); return float(a),float(b)
    except ValueError:die("--range는 시작-끝 초 형식이어야 합니다 (예: 10-20)."); return 0,duration


def legacy_project(args: argparse.Namespace,tmp: Path)->Path:
    require_ffmpeg(); src=Path(args.legacy_input).resolve()
    if not src.exists():die(f"입력 파일을 찾을 수 없습니다: {src}")
    info=ffprobe(src); video=next((s for s in info["streams"] if s.get("codec_type")=="video"),None)
    if not video:die("입력 파일에 영상 스트림이 없습니다.")
    w,h=int(video["width"]),int(video["height"])
    if args.size:
        try:w,h=map(int,args.size.lower().split("x"))
        except ValueError:die("--size는 WIDTHxHEIGHT 형식이어야 합니다.")
    fps_text=video.get("avg_frame_rate","30/1"); num,den=map(float,fps_text.split("/")); fps=num/den if den else 30
    speed=float(args.speed)
    if not .1<=speed<=10:die("--speed는 0.1 이상 10 이하이어야 합니다.")
    dur=media_duration(src)/speed; effects=[]
    if args.preset:effects += PRESETS[args.preset]
    if args.blur:effects.append({"type":"blur","radius":args.blur})
    if args.vignette:effects.append({"type":"vignette","amount":args.vignette})
    if args.grayscale:effects.append({"type":"color","saturation":0})
    if args.sepia:effects.append({"type":"sepia"})
    if args.invert:effects.append({"type":"invert"})
    if args.edge:effects.append({"type":"edge"})
    if args.flip:effects.append({"type":"flip","direction":args.flip})
    if args.brightness or args.contrast!=1 or args.saturation!=1:effects.append({"type":"color","brightness":args.brightness,"contrast":args.contrast,"saturation":args.saturation})
    layers=[{"id":"video","type":"video","source":str(src),"start":0,"end":dur,"speed":speed,"fit":args.fit,"position":[w/2,h/2],"effects":effects}]
    if args.text:
        layers.append({"id":"text","type":"text","start":0,"end":dur,"text":args.text,"style":{"font":args.font,"size":args.text_size,"color":args.text_color,"stroke":{"width":args.stroke_width,"color":args.stroke_color},"align":"center","max_width":int(w*.9)},"position":[w/2,h*.82]})
    if args.fade_in or args.fade_out:
        k=[]
        if args.fade_in:k += [{"t":0,"v":0,"ease":"linear"},{"t":args.fade_in,"v":100}]
        else:k.append({"t":0,"v":100})
        if args.fade_out:k += [{"t":max(0,dur-args.fade_out),"v":100},{"t":dur,"v":0}]
        layers.append({"id":"fade","type":"adjustment","start":0,"end":dur,"effects":[]})
        for layer in layers[:-1]:layer.setdefault("keyframes",{})["opacity"]=k
    data={"version":1,"compositions":[{"id":"main","width":w,"height":h,"fps":fps,"duration":dur,"background":"#000000","layers":layers}]}
    p=tmp/"legacy.json";p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8");return p


def _srt_time(text:str)->float:
    h,m,rest=text.strip().replace('.',',').split(':');s,ms=rest.split(',');return int(h)*3600+int(m)*60+int(s)+int(ms)/1000


def import_srt(project_path:Path,srt_path:Path,out:Path,font:str|None=None)->None:
    project=Project.load(project_path);text=srt_path.read_text(encoding="utf-8-sig");blocks=re.split(r"\r?\n\s*\r?\n",text.strip());comp=project.data["compositions"][0]
    for i,block in enumerate(blocks):
        lines=block.splitlines()
        if len(lines)<2:continue
        timing=next((x for x in lines if "-->" in x),None)
        if not timing:continue
        a,b=[_srt_time(x) for x in timing.split("-->")];idx=lines.index(timing);caption="\n".join(lines[idx+1:])
        comp["layers"].append({"id":f"srt_{i+1}","type":"text","start":a,"end":b,"text":caption,"style":{"font":font,"size":72,"color":"#ffffff","stroke":{"width":5,"color":"#000000"},"align":"center","max_width":int(comp["width"]*.9)},"transform":{"position":[comp["width"]/2,comp["height"]*.82]}})
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(project.data,ensure_ascii=False,indent=2),encoding="utf-8")


def import_timing(project_path:Path,timing_path:Path,out:Path,base:Path|None=None,scenes_path:Path|None=None)->None:
    project=Project.load(project_path)
    try:raw=json.loads(timing_path.read_text(encoding="utf-8-sig"))
    except (OSError,json.JSONDecodeError) as exc:die(f"timing JSON을 읽을 수 없습니다: {exc}")
    comp=project.data["compositions"][0];root=(base.resolve() if base else timing_path.resolve().parent)
    def source_value(value:str)->str:
        q=Path(value).expanduser();return str(q if q.is_absolute() else (root/q).resolve())
    if isinstance(raw,dict) and ("chunks" in raw or "lines" in raw):
        if "total" in raw:comp["duration"]=float(raw["total"])
        chunks=raw.get("chunks",[]);lines=raw.get("lines",[])
        if not isinstance(chunks,list) or not isinstance(lines,list):die("timing JSON의 chunks와 lines는 배열이어야 합니다.")
        audio=project.data.setdefault("audio",[])
        for i,chunk in enumerate(chunks):
            if not isinstance(chunk,dict) or "file" not in chunk or "start" not in chunk:die(f"timing chunks.{i}에 file과 start가 필요합니다.")
            item={"id":f"timing_audio_{i+1}","source":source_value(str(chunk["file"])),"start":float(chunk["start"])}
            if "duration" in chunk:item["duration"]=float(chunk["duration"]);item["end"]=float(chunk["start"])+float(chunk["duration"])
            audio.append(item)
        scenes=None
        if scenes_path:
            try:scenes=json.loads(scenes_path.read_text(encoding="utf-8-sig"))
            except (OSError,json.JSONDecodeError) as exc:die(f"scenes JSON을 읽을 수 없습니다: {exc}")
            if not isinstance(scenes,dict):die("scenes JSON은 줄 id를 파일 경로에 대응한 객체여야 합니다.")
        for i,line in enumerate(lines):
            if not isinstance(line,dict) or "start" not in line or "end" not in line:die(f"timing lines.{i}에 start와 end가 필요합니다.")
            lid=str(line.get("id",i+1));a=float(line["start"]);b=float(line["end"])
            if scenes is not None:
                src=scenes.get(lid,scenes.get(str(i+1)))
                if src is None:die(f"scenes JSON에 줄 {lid!r}의 영상이 없습니다.")
                comp["layers"].append({"id":f"scene_{lid}","type":"video","source":source_value(str(src)),"start":a,"end":b,"fit":"cover","tail":"hold"})
            else:
                comp["layers"].append({"id":f"line_{lid}","type":"text","start":a,"end":b,"text":str(line.get("text","")),"style":{"size":72,"color":"#ffffff","stroke":{"width":5,"color":"#000000"},"align":"center","max_width":int(comp["width"]*.9)},"transform":{"position":[comp["width"]/2,comp["height"]*.82]}})
    else:
        cuts=raw.get("cuts",raw) if isinstance(raw,dict) else raw
        if not isinstance(cuts,list):die("timing JSON은 배열, cuts 배열 또는 total/chunks/lines 형식이어야 합니다.")
        for i,cut in enumerate(cuts):
            if not isinstance(cut,dict) or "source" not in cut or "start" not in cut:die(f"timing 항목 {i}에 source와 start가 필요합니다.")
            layer={"id":str(cut.get("id",f"cut_{i+1}")),"type":"video","source":cut["source"],"start":cut["start"],"in":cut.get("in",0),"speed":cut.get("speed",1),"fit":cut.get("fit","cover")}
            if "out" in cut:layer["out"]=cut["out"]
            if "end" in cut:layer["end"]=cut["end"]
            comp["layers"].append(layer)
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(project.data,ensure_ascii=False,indent=2),encoding="utf-8")


def build_render_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="video_fx render",description="JSON 프로젝트 렌더")
    p.add_argument("project");p.add_argument("-o","--output",required=True);p.add_argument("--comp");p.add_argument("--range");p.add_argument("--preview-frame",type=float);p.add_argument("--scale",type=float,default=1.0);p.add_argument("--contact-sheet",help="쉼표로 구분한 초 목록");p.add_argument("--columns",type=int,default=3)
    return p


def build_legacy_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="video_fx",description="레이어·키프레임 기반 영상 합성 엔진\n프로젝트 모드: video_fx render project.json -o out.mp4")
    p.add_argument("legacy_input",metavar="input",nargs="?");p.add_argument("legacy_output",metavar="output",nargs="?");p.add_argument("--preset",choices=sorted(PRESETS));p.add_argument("--list-presets",action="store_true");p.add_argument("--text");p.add_argument("--font");p.add_argument("--text-size",type=int,default=72);p.add_argument("--text-color",default="#ffffff");p.add_argument("--stroke-width",type=int,default=4);p.add_argument("--stroke-color",default="#000000");p.add_argument("--fade-in",type=float,default=0);p.add_argument("--fade-out",type=float,default=0);p.add_argument("--speed",type=float,default=1);p.add_argument("--fit",choices=["contain","cover","stretch"],default="contain");p.add_argument("--size","--resize",dest="size");p.add_argument("--blur",type=float,default=0);p.add_argument("--vignette",type=float,default=0);p.add_argument("--brightness",type=float,default=0);p.add_argument("--contrast",type=float,default=1);p.add_argument("--saturation",type=float,default=1);p.add_argument("--grayscale",action="store_true");p.add_argument("--sepia",action="store_true");p.add_argument("--invert",action="store_true");p.add_argument("--edge",action="store_true");p.add_argument("--flip",choices=["h","v","hv","horizontal","vertical"])
    return p


def main(argv:list[str]|None=None)->int:
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("import-srt","import_srt"):
        p=argparse.ArgumentParser(prog="video_fx import-srt");p.add_argument("project");p.add_argument("srt");p.add_argument("-o","--output",required=True);p.add_argument("--font");a=p.parse_args(argv[1:]);import_srt(Path(a.project),Path(a.srt),Path(a.output),a.font);return 0
    if argv and argv[0] in ("import-timing","import_timing"):
        p=argparse.ArgumentParser(prog="video_fx import-timing");p.add_argument("project");p.add_argument("timing");p.add_argument("-o","--output",required=True);p.add_argument("--base",type=Path);p.add_argument("--scenes",type=Path);a=p.parse_args(argv[1:]);import_timing(Path(a.project),Path(a.timing),Path(a.output),a.base,a.scenes);return 0
    if argv and argv[0]=="render":
        args=build_render_parser().parse_args(argv[1:])
        project=Project.load(Path(args.project)); renderer=Renderer(project,args.comp,args.scale); out=Path(args.output)
        if args.contact_sheet:
            try:times=[float(x) for x in args.contact_sheet.split(",")]
            except ValueError:die("--contact-sheet는 0,5,10처럼 초를 쉼표로 구분하세요.")
            renderer.render_contact_sheet(times,out,args.columns);renderer.close()
        elif args.preview_frame is not None:
            if out.suffix.lower()!=".png":die("--preview-frame 출력은 .png 파일이어야 합니다.")
            out.parent.mkdir(parents=True,exist_ok=True);renderer.render_frame(args.preview_frame).save(out);renderer.close()
        else:
            start,end=parse_range(args.range,float(renderer.comp["duration"]));renderer.render(out,start,end)
        return 0
    args=build_legacy_parser().parse_args(argv)
    if args.list_presets:
        print("\n".join(sorted(PRESETS)));return 0
    if not args.legacy_input or not args.legacy_output:die("input과 output 파일을 지정하세요.")
    with tempfile.TemporaryDirectory(prefix="video_fx_") as td:
        project=Project.load(legacy_project(args,Path(td)));Renderer(project).render(Path(args.legacy_output))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
