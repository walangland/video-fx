import json, runpy, subprocess, sys, time
from pathlib import Path
import pytest
from PIL import Image

ENGINE=Path(__file__).parents[2]/"video_fx.py"
M=runpy.run_path(str(ENGINE))

def run(*args,check=True):
    return subprocess.run([sys.executable,str(ENGINE),*map(str,args)],text=True,capture_output=True,check=check)
def probe(path):
    return json.loads(subprocess.check_output(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(path)]))
def base_project(w=320,h=240,fps=30,duration=2,layers=None):
    return {"version":1,"compositions":[{"id":"main","width":w,"height":h,"fps":fps,"duration":duration,"background":"transparent","layers":layers or []}]}
def write_project(tmp_path,data,name="p.json"):
    p=tmp_path/name;p.write_text(json.dumps(data));return p
def media(tmp_path,duration=2,size="320x240",rate=30):
    silent=tmp_path/"silent.mp4";audio=tmp_path/"tone.wav";av=tmp_path/"av.mp4"
    subprocess.run(["ffmpeg","-y","-v","error","-f","lavfi","-i",f"testsrc2=size={size}:rate={rate}","-t",str(duration),"-c:v","libx264","-pix_fmt","yuv420p",str(silent)],check=True)
    subprocess.run(["ffmpeg","-y","-v","error","-f","lavfi","-i","sine=440:sample_rate=48000","-t",str(duration),str(audio)],check=True)
    subprocess.run(["ffmpeg","-y","-v","error","-i",str(silent),"-i",str(audio),"-c:v","copy","-c:a","aac","-shortest",str(av)],check=True)
    return silent,audio,av

def test_contrast_and_bezier():
    im=Image.new("RGBA",(2,2),(128,128,128,255));out=M["color_effect"](im,{"contrast":1.3})
    assert abs(out.getpixel((0,0))[0]-128)<=1
    for x,y in zip([.25,.5,.75],[.4085,.8024,.9605]):assert abs(M["ease_value"]("cubic-bezier(0.25,0.1,0.25,1)",x)-y)<.01

def test_silent_av_speed_and_invalid_speed(tmp_path):
    silent,audio,av=media(tmp_path);out=tmp_path/"silent_out.mp4";run(silent,out)
    assert [s["codec_type"] for s in probe(out)["streams"]]==["video"]
    for speed,want in [(.5,4),(2,1)]:
        dst=tmp_path/f"s{speed}.mp4";run(av,dst,"--speed",speed);info=probe(dst)
        assert {s["codec_name"] for s in info["streams"]}>={"h264","aac"};assert abs(float(info["format"]["duration"])-want)<=1/30
    bad=run(av,tmp_path/"bad.mp4","--speed",0,check=False);assert bad.returncode and "speed" in bad.stderr

def test_layer_end_anchor_and_transparent(tmp_path):
    silent,_,_=media(tmp_path)
    layers=[{"id":"v","type":"video","source":str(silent),"start":1,"in":.3,"out":1.8,"fit":"contain"},{"id":"bar","type":"shape","start":0,"end":1,"shape":{"width":400,"height":50,"fill":"#f00"},"transform":{"anchor":[0,.5],"position":[100,100]},"keyframes":{"scale":[{"t":0,"v":[0,100]},{"t":.5,"v":[100,100]}]}}]
    p=write_project(tmp_path,base_project(600,300,30,2.6,layers))
    before=tmp_path/"before.png";after=tmp_path/"after.png";anchor=tmp_path/"anchor.png"
    run("render",p,"--preview-frame",2.4666667,"-o",before);run("render",p,"--preview-frame",2.5,"-o",after);run("render",p,"--preview-frame",.5,"-o",anchor)
    assert Image.open(before).getbbox() and Image.open(after).getbbox() is None
    assert Image.open(anchor).getchannel("A").getbbox()[0:3:2]==(100,500)
    mov=tmp_path/"alpha.mov";run("render",p,"--range","0-0.1","-o",mov);assert probe(mov)["streams"][0]["pix_fmt"]=="yuva444p12le" or "yuva" in probe(mov)["streams"][0]["pix_fmt"]

def test_decoder_release_and_tail_hold(tmp_path):
    silent,_,_=media(tmp_path,duration=.3,size="80x60",rate=10)
    layers=[{"id":f"v{i}","type":"video","source":str(silent),"start":i*.3,"end":i*.3+.3,"fit":"contain"} for i in range(34)]
    p=write_project(tmp_path,base_project(80,60,10,10.2,layers));project=M["Project"].load(p);r=M["Renderer"](project);peak=0
    for n in range(102):r.render_frame(n/10);peak=max(peak,len(r.video_decoders))
    r.close();assert peak<=2
    hold=base_project(80,60,10,1,[{"id":"v","type":"video","source":str(silent),"start":0,"end":1,"fit":"contain","tail":"hold"},{"id":"dot","type":"shape","start":0,"end":1,"shape":{"width":1,"height":1,"fill":"#fff"},"position":[0,0]}])
    hp=write_project(tmp_path,hold,"hold.json");out=tmp_path/"hold.mp4";run("render",hp,"-o",out);assert abs(float(probe(out)["format"]["duration"])-1)<=.1

def test_audio_stops_at_layer_end(tmp_path):
    _,_,av=media(tmp_path,duration=2,size="80x60",rate=10)
    p=write_project(tmp_path,base_project(80,60,10,2,[{"id":"v","type":"video","source":str(av),"start":0,"end":.5,"fit":"contain"}]))
    out=tmp_path/"cut.mp4";run("render",p,"-o",out)
    raw=subprocess.check_output(["ffmpeg","-v","error","-ss","0.8","-t","0.3","-i",str(out),"-f","s16le","-ac","1","-"])
    import array;a=array.array("h",raw);assert max(map(abs,a),default=0)<=2

def test_overlapping_presets_and_parent_opacity(tmp_path):
    layers=[{"id":"parent","type":"null","opacity":20,"scale":[100,100],"position":[0,0]},{"id":"x","type":"solid","parent":"parent","scale":[100,100],"opacity":80,"presets":[{"name":"zoom_punch","t":0},{"name":"slow_push","t":0}]}]
    p=write_project(tmp_path,base_project(100,100,30,2,layers));r=M["Renderer"](M["Project"].load(p));layer=r.comp["layers"][1]
    assert r.transform_values(layer,0)[1][0]==pytest.approx(110)
    assert r.transform_values(layer,1.5)[1][0]==pytest.approx(103.75,.01)
    assert r.transform_values(layer,1)[3]==pytest.approx(80)
    layer["inherit_opacity"]=True;assert r.transform_values(layer,1)[3]==pytest.approx(16)

def test_timing_chunks_lines_and_scenes(tmp_path):
    base=write_project(tmp_path,base_project(1080,1920,30,1,[]),"base.json")
    timing={"total":61.022,"chunks":[{"file":f"tts/{i}.wav","start":i,"duration":1} for i in range(7)],"lines":[{"id":i+1,"start":i,"end":i+1,"text":str(i)} for i in range(34)]}
    tp=tmp_path/"timing.json";tp.write_text(json.dumps(timing));out=tmp_path/"captions.json";run("import-timing",base,tp,"--base",tmp_path,"-o",out)
    data=json.loads(out.read_text());assert data["compositions"][0]["duration"]==61.022 and len(data["audio"])==7 and len(data["compositions"][0]["layers"])==34
    scenes=tmp_path/"scenes.json";scenes.write_text(json.dumps({str(i+1):f"scene_{i+1}.mp4" for i in range(34)}));out2=tmp_path/"scenes_out.json";run("import-timing",base,tp,"--base",tmp_path,"--scenes",scenes,"-o",out2)
    assert all(x["type"]=="video" for x in json.loads(out2.read_text())["compositions"][0]["layers"])

def test_hidden_inverted_matte(tmp_path):
    layers=[{"id":"m","type":"shape","hidden":True,"shape":{"kind":"circle","width":100,"height":100,"fill":"#fff"},"position":[100,100]},{"id":"red","type":"solid","size":[200,200],"color":"#f00","position":[100,100],"matte":{"layer":"m","type":"alpha-invert"}}]
    p=write_project(tmp_path,base_project(200,200,30,1,layers));out=tmp_path/"m.png";run("render",p,"--preview-frame",0,"-o",out);im=Image.open(out)
    assert im.getpixel((100,100))[:3]==(0,0,0) and im.getpixel((10,10))[:3]==(255,0,0)

def test_stagger_layout_is_stable(tmp_path):
    layer={"type":"text","text":"CHEESE TOP3","style":{"size":60,"align":"center"},"animator":{"stagger":{"by":"character","interval":.1,"duration":.1,"opacity":[0,100],"offset_y":[30,0],"scale":[80,100]}}}
    a=M["make_text_layer"](layer,.05);b=M["make_text_layer"](layer,2)
    assert a.size==b.size and abs(a.getchannel("A").getbbox()[0]-b.getchannel("A").getbbox()[0])<=2

def test_validation_typos_and_flip_aliases(tmp_path):
    cases=[];base=base_project(10,10,1,1,[])
    x=json.loads(json.dumps(base));x["compositions"][0]["backgroud"]="#f00";cases.append(x)
    x=json.loads(json.dumps(base));x["compositions"][0]["layers"]=[{"type":"shape","shape":{"kind":"circel"}}];cases.append(x)
    x=json.loads(json.dumps(base));x["compositions"][0]["layers"]=[{"type":"solid","effects":[{"type":"color","saturaton":3}]}];cases.append(x)
    x=json.loads(json.dumps(base));x["compositions"][0]["layers"]=[{"type":"solid","keyframes":{"blur":[{"t":0,"v":1}]}}];cases.append(x)
    for i,data in enumerate(cases):
        p=write_project(tmp_path,data,f"bad{i}.json");r=run("render",p,"--preview-frame",0,"-o",tmp_path/f"x{i}.png",check=False);assert r.returncode and f"compositions.0" in r.stderr
    silent,_,_=media(tmp_path,duration=.2,size="80x60",rate=10)
    for alias in ("h","v","hv"):run(silent,tmp_path/f"{alias}.mp4","--flip",alias)

def test_motion_blur_static_is_skipped(tmp_path):
    layer={"id":"s","type":"shape","shape":{"width":40,"height":40,"fill":"#fff"},"position":[50,50],"motion_blur":True,"samples":5}
    p=write_project(tmp_path,base_project(100,100,30,1,[layer]));r=M["Renderer"](M["Project"].load(p));img=r.source_image(r.comp["layers"][0],.5)
    assert r.motion_blur_surface(img,r.comp["layers"][0],.5) is None

def test_lossless_source_frame_mapping(tmp_path):
    source=tmp_path/"numbered.mkv"
    subprocess.run(["ffmpeg","-y","-v","error","-f","lavfi","-i",r"color=s=320x240:r=30,format=rgb24,geq=r='mod(N\,32)*8':g='floor(N/32)*64':b=0","-t","2","-c:v","libx264rgb","-qp","0",str(source)],check=True)
    layer={"id":"v","type":"video","source":str(source),"start":1,"in":.3,"out":1.8}
    p=write_project(tmp_path,base_project(320,240,30,2.6,[layer]));r=M["Renderer"](M["Project"].load(p))
    def number(t):
        px=r.render_frame(t).getpixel((160,120));return px[0]//8+(px[1]//64)*32
    assert number(1)==9 and number(2.4666667)==53 and r.render_frame(2.5).getbbox() is None
    r.close()

def test_srt_34_lines(tmp_path):
    base=write_project(tmp_path,base_project(1080,1920,30,40,[]),"base_srt.json")
    blocks=[]
    for i in range(34):blocks.append(f"{i+1}\n00:00:{i:02},000 --> 00:00:{i:02},900\ncaption {i+1}")
    srt=tmp_path/"captions.srt";srt.write_text("\n\n".join(blocks));out=tmp_path/"srt.json";run("import-srt",base,srt,"-o",out)
    assert len(json.loads(out.read_text())["compositions"][0]["layers"])==34
