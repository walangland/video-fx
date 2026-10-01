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
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
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
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) == 6:
        text += f"{alpha:02x}"
    if len(text) != 8 or not re.fullmatch(r"[0-9a-fA-F]{8}", text):
        die(f"잘못된 색상 값: {value}")
    return tuple(int(text[i:i+2], 16) for i in range(0, 8, 2))  # type: ignore[return-value]


def ffprobe(path: Path) -> dict[str, Any]:
    p = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ], text=True, capture_output=True)
    if p.returncode:
        die(f"미디어 정보를 읽을 수 없습니다: {path}\n{p.stderr.strip()}")
    return json.loads(p.stdout)


def media_duration(path: Path) -> float:
    info = ffprobe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def resolve_path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


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
            _, y1, _, y2 = nums
            u = 1-x
            return 3*u*u*x*y1 + 3*u*x*x*y2 + x**3
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


def decode_video_frame(path: Path, t: float) -> Image.Image:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0,t):.6f}", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode or not p.stdout:
        die(f"영상 프레임을 읽지 못했습니다: {path}\n{p.stderr.decode(errors='replace').strip()}")
    import io
    return Image.open(io.BytesIO(p.stdout)).convert("RGBA")


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
    size = int(style.get("size", 72)); font = load_font(style.get("font"), size, text)
    max_width = int(style.get("max_width", 2000)); scratch = Image.new("RGBA", (max_width+200, 1000)); d = ImageDraw.Draw(scratch)
    text = wrap_text(d, text, font, max_width)
    stroke = style.get("stroke", {}); sw = int(stroke.get("width", 0))
    bbox = d.multiline_textbbox((0,0), text, font=font, stroke_width=sw, spacing=int(size*(float(style.get("line_height",1.15))-1)))
    w, h = max(1,bbox[2]-bbox[0]+40), max(1,bbox[3]-bbox[1]+40)
    img = Image.new("RGBA", (w,h), (0,0,0,0)); draw = ImageDraw.Draw(img)
    align = style.get("align", "center"); anchor_x = {"left":20, "center":w/2, "right":w-20}.get(align,w/2)
    anchor = {"left":"la", "center":"ma", "right":"ra"}.get(align,"ma")
    shadow = style.get("shadow")
    if shadow:
        off = shadow.get("offset", [0,4]); sh = Image.new("RGBA", img.size, (0,0,0,0)); sd = ImageDraw.Draw(sh)
        sd.multiline_text((anchor_x+off[0],20+off[1]), text, font=font, fill=parse_color(shadow.get("color","#00000080")), anchor=anchor, align=align, spacing=int(size*(float(style.get("line_height",1.15))-1)), stroke_width=sw, stroke_fill=parse_color(shadow.get("color","#00000080")))
        sh = sh.filter(ImageFilter.GaussianBlur(float(shadow.get("blur",4)))); img.alpha_composite(sh)
    draw.multiline_text((anchor_x,20), text, font=font, fill=parse_color(style.get("color","#ffffff")), anchor=anchor, align=align, spacing=int(size*(float(style.get("line_height",1.15))-1)), stroke_width=sw, stroke_fill=parse_color(stroke.get("color","#000000")))
    return img


def make_shape_layer(layer: dict[str, Any]) -> Image.Image:
    shape = layer.get("shape", layer)
    w, h = int(shape.get("width",400)), int(shape.get("height",200))
    pad = int(shape.get("stroke",{}).get("width",0))+8
    img = Image.new("RGBA", (w+2*pad,h+2*pad), (0,0,0,0)); d=ImageDraw.Draw(img)
    fill=parse_color(shape.get("fill","#ffffff")); stroke=shape.get("stroke",{}); outline=parse_color(stroke.get("color","#000000")); sw=int(stroke.get("width",0))
    kind=shape.get("kind",shape.get("shape","rectangle")); box=(pad,pad,pad+w,pad+h)
    if kind in ("circle","ellipse"): d.ellipse(box, fill=fill, outline=outline if sw else None, width=sw)
    elif kind in ("polygon","star","burst"):
        points=int(shape.get("points",5)); outer=min(w,h)/2; inner=float(shape.get("inner_radius",outer*.45)); cx,cy=pad+w/2,pad+h/2
        verts=[]
        for i in range(points*2):
            r=outer if i%2==0 else inner; a=-math.pi/2+i*math.pi/points; verts.append((cx+math.cos(a)*r,cy+math.sin(a)*r))
        d.polygon(verts, fill=fill); d.line(verts+[verts[0]], fill=outline, width=sw, joint="curve") if sw else None
    else: d.rounded_rectangle(box, radius=int(shape.get("radius",0)), fill=fill, outline=outline if sw else None, width=sw)
    return img


def blend(base: Image.Image, over: Image.Image, pos: tuple[int,int], mode: str) -> Image.Image:
    if mode == "normal":
        base.alpha_composite(over, pos); return base
    layer = Image.new("RGBA", base.size, (0,0,0,0)); layer.alpha_composite(over,pos)
    if mode == "multiply": mixed=ImageChops.multiply(base,layer)
    elif mode == "screen": mixed=ImageChops.screen(base,layer)
    elif mode == "add": mixed=ImageChops.add(base,layer,scale=1.0,offset=0)
    else: base.alpha_composite(over,pos); return base
    mask=layer.getchannel("A"); return Image.composite(mixed,base,mask)


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
        return cls(path.resolve(),data)

    def comp(self, comp_id: str | None=None) -> dict[str,Any]:
        cid=comp_id or self.data.get("main") or self.data["compositions"][0]["id"]
        for c in self.data["compositions"]:
            if c["id"]==cid:return c
        die(f"컴포지션을 찾을 수 없습니다: {cid}"); return {}


class Renderer:
    def __init__(self, project: Project, comp_id: str|None=None, scale: float=1.0):
        self.project=project; self.comp=project.comp(comp_id); self.base=project.path.parent; self.scale=scale
        self.w=max(1,round(self.comp["width"]*scale)); self.h=max(1,round(self.comp["height"]*scale)); self.fps=float(self.comp["fps"])
        self.image_cache: dict[Path,Image.Image]={}
        self.video_meta: dict[Path,tuple[float,float]]={}

    def source_image(self, layer: dict[str,Any], t: float) -> Image.Image:
        kind=layer["type"]
        if kind=="video":
            path=resolve_path(self.base,layer["source"]); source_t=float(layer.get("in",0))+t*float(layer.get("speed",1))
            if path not in self.video_meta:
                info=ffprobe(path); stream=next((s for s in info.get("streams",[]) if s.get("codec_type")=="video"),{})
                rate=str(stream.get("avg_frame_rate","30/1")).split("/")
                source_fps=float(rate[0])/max(float(rate[1]),1) if len(rate)==2 else 30.0
                self.video_meta[path]=(media_duration(path),source_fps)
            source_duration,source_fps=self.video_meta[path]
            source_t=min(source_t,max(0,source_duration-1/source_fps))
            return decode_video_frame(path,source_t)
        if kind=="image":
            path=resolve_path(self.base,layer["source"])
            if path not in self.image_cache: self.image_cache[path]=Image.open(path).convert("RGBA")
            return self.image_cache[path].copy()
        if kind=="text": return make_text_layer(layer,t)
        if kind=="shape": return make_shape_layer(layer)
        if kind=="solid":
            size=layer.get("size",[self.w,self.h]); return Image.new("RGBA",(int(size[0]),int(size[1])),parse_color(layer.get("color","#000000")))
        if kind=="comp": return self.render_frame(t*float(layer.get("speed",1)),layer.get("composition"))
        return Image.new("RGBA",(1,1),(0,0,0,0))

    def render_frame(self, t: float, comp_id: str|None=None) -> Image.Image:
        if comp_id and comp_id!=self.comp["id"]:
            return Renderer(self.project,comp_id,self.scale).render_frame(t)
        canvas=Image.new("RGBA",(self.w,self.h),parse_color(self.comp.get("background","#000000")))
        layers=self.comp.get("layers",[])
        for layer in layers:
            if layer.get("hidden") or layer.get("type") in ("null","audio"): continue
            start=float(layer.get("start",0)); end=float(layer.get("end",layer.get("out",self.comp["duration"])))
            if not(start<=t<end): continue
            local=t-start
            if layer["type"]=="adjustment":
                canvas=apply_effects(canvas,layer.get("effects",[]),round(t*self.fps)); continue
            img=self.source_image(layer,local)
            fit=layer.get("fit")
            if fit: img=contain_cover(img,(self.w,self.h),fit)
            img=apply_effects(img,layer.get("effects",[]),round(t*self.fps))
            scale=animated(layer,"scale",[100,100],local); sx,sy=float(scale[0])/100*self.scale,float(scale[1])/100*self.scale
            img=img.resize((max(1,round(img.width*sx)),max(1,round(img.height*sy))),RESAMPLING.LANCZOS)
            rotation=float(animated(layer,"rotation",0,local));
            if rotation: img=img.rotate(-rotation,expand=True,resample=RESAMPLING.BICUBIC)
            opacity=float(animated(layer,"opacity",100,local));
            if opacity<100: img.putalpha(img.getchannel("A").point(lambda a:int(a*max(0,opacity)/100)))
            pos=animated(layer,"position",[self.comp["width"]/2,self.comp["height"]/2],local)
            x=round(float(pos[0])*self.scale-img.width/2); y=round(float(pos[1])*self.scale-img.height/2)
            canvas=blend(canvas,img,(x,y),str(layer.get("blend","normal")))
        return canvas

    def audio_inputs_and_filter(self, start: float, duration: float) -> tuple[list[str],str,str|None]:
        args: list[str]=[]; chains: list[str]=[]; labels: list[str]=[]
        entries=list(self.project.data.get("audio",[]))
        for layer in self.comp.get("layers",[]):
            if layer.get("type")=="video" and not layer.get("mute",False) and layer.get("source"):
                entries.append({"source":layer["source"],"start":layer.get("start",0),"in":layer.get("in",0),"out":layer.get("out"),"volume_db":layer.get("volume_db",0),"speed":layer.get("speed",1)})
        for i,a in enumerate(entries,1):
            path=resolve_path(self.base,a["source"])
            if not path.exists(): die(f"오디오 파일을 찾을 수 없습니다: {path}")
            args += ["-i",str(path)]; delay=max(0,float(a.get("start",0))-start); trim=max(0,start-float(a.get("start",0)))+float(a.get("in",0))
            pieces=[f"atrim=start={trim:.6f}"]
            if a.get("out") is not None: pieces[0]+=f":end={float(a['out']):.6f}"
            speed=float(a.get("speed",1));
            if speed!=1:
                remain=speed
                while remain>2: pieces.append("atempo=2"); remain/=2
                while remain<.5: pieces.append("atempo=.5"); remain*=2
                pieces.append(f"atempo={remain:.8f}")
            pieces += [f"volume={float(a.get('volume_db',0))}dB"]
            if a.get("fade_in"): pieces.append(f"afade=t=in:st=0:d={float(a['fade_in'])}")
            if a.get("fade_out"):
                adur=float(a.get("duration",duration)); pieces.append(f"afade=t=out:st={max(0,adur-float(a['fade_out']))}:d={float(a['fade_out'])}")
            pieces += [f"adelay={round(delay*1000)}|{round(delay*1000)}",f"atrim=duration={duration:.6f}","asetpts=N/SR/TB"]
            label=f"a{i}"; chains.append(f"[{i}:a]{','.join(pieces)}[{label}]"); labels.append(f"[{label}]")
        if not labels:return args,"",None
        chains.append("".join(labels)+f"amix=inputs={len(labels)}:normalize=0:duration=longest[aout]")
        return args,";".join(chains),"[aout]"

    def render(self,out: Path,start: float=0,end: float|None=None) -> None:
        require_ffmpeg(); end=float(self.comp["duration"]) if end is None else end
        if start<0 or end<=start or end>float(self.comp["duration"])+1e-6: die("--range 값이 컴포지션 범위를 벗어났습니다.")
        frames=round((end-start)*self.fps); out.parent.mkdir(parents=True,exist_ok=True)
        if out.suffix.lower()==".png" or out.name.lower().endswith(".png"):
            if frames==1:self.render_frame(start).save(out); return
        if out.suffix.lower() not in (".mp4",".mov"):
            out.mkdir(parents=True,exist_ok=True)
            for n in range(frames):
                self.render_frame(start+n/self.fps).save(out/f"frame_{n:06d}.png")
                if n%max(1,round(self.fps))==0: print(f"\r{n+1}/{frames}",end="",file=sys.stderr)
            print(file=sys.stderr); return
        audio_args,afilter,alabel=self.audio_inputs_and_filter(start,end-start)
        cmd=["ffmpeg","-y","-v","error","-f","rawvideo","-pix_fmt","rgba","-s",f"{self.w}x{self.h}","-r",str(self.fps),"-i","-"]+audio_args
        if afilter:cmd += ["-filter_complex",afilter]
        cmd += ["-map","0:v:0"]
        if alabel:cmd += ["-map",alabel]
        if out.suffix.lower()==".mov":cmd += ["-c:v","prores_ks","-profile:v","4","-pix_fmt","yuva444p10le"]
        else:cmd += ["-c:v","libx264","-crf","18","-preset","medium","-pix_fmt","yuv420p","-movflags","+faststart"]
        if alabel:cmd += ["-c:a","aac","-b:a","192k"]
        cmd += ["-t",f"{end-start:.6f}",str(out)]
        proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
        assert proc.stdin is not None
        try:
            for n in range(frames):
                proc.stdin.write(self.render_frame(start+n/self.fps).tobytes())
                if n%max(1,round(self.fps))==0:print(f"\r{n+1}/{frames}",end="",file=sys.stderr)
            proc.stdin.close(); err=proc.stderr.read().decode(errors="replace") if proc.stderr else ""; code=proc.wait()
        except BrokenPipeError:
            err=proc.stderr.read().decode(errors="replace") if proc.stderr else ""; code=proc.wait()
        print(file=sys.stderr)
        if code: die(f"ffmpeg 렌더 실패:\n{err.strip()}")


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


def build_render_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="video_fx render",description="JSON 프로젝트 렌더")
    p.add_argument("project");p.add_argument("-o","--output",required=True);p.add_argument("--comp");p.add_argument("--range");p.add_argument("--preview-frame",type=float);p.add_argument("--scale",type=float,default=1.0)
    return p


def build_legacy_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="video_fx",description="레이어·키프레임 기반 영상 합성 엔진\n프로젝트 모드: video_fx render project.json -o out.mp4")
    p.add_argument("legacy_input",metavar="input");p.add_argument("legacy_output",metavar="output");p.add_argument("--preset",choices=sorted(PRESETS));p.add_argument("--text");p.add_argument("--font");p.add_argument("--text-size",type=int,default=72);p.add_argument("--text-color",default="#ffffff");p.add_argument("--stroke-width",type=int,default=4);p.add_argument("--stroke-color",default="#000000");p.add_argument("--fade-in",type=float,default=0);p.add_argument("--fade-out",type=float,default=0);p.add_argument("--speed",type=float,default=1);p.add_argument("--fit",choices=["contain","cover","stretch"],default="contain");p.add_argument("--size");p.add_argument("--blur",type=float,default=0);p.add_argument("--vignette",type=float,default=0);p.add_argument("--brightness",type=float,default=0);p.add_argument("--contrast",type=float,default=1);p.add_argument("--saturation",type=float,default=1)
    return p


def main(argv:list[str]|None=None)->int:
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0]=="render":
        args=build_render_parser().parse_args(argv[1:])
        project=Project.load(Path(args.project)); renderer=Renderer(project,args.comp,args.scale); out=Path(args.output)
        if args.preview_frame is not None:
            if out.suffix.lower()!=".png":die("--preview-frame 출력은 .png 파일이어야 합니다.")
            out.parent.mkdir(parents=True,exist_ok=True);renderer.render_frame(args.preview_frame).save(out)
        else:
            start,end=parse_range(args.range,float(renderer.comp["duration"]));renderer.render(out,start,end)
        return 0
    args=build_legacy_parser().parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="video_fx_") as td:
        project=Project.load(legacy_project(args,Path(td)));Renderer(project).render(Path(args.legacy_output))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
