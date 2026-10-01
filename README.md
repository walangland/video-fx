# video_fx — AE-style Video Compositing Engine

After Effects 스타일의 영상 합성 엔진 (Python 단일 파일).
레이어, 키프레임, 모션 그래픽, 오디오 믹싱을 지원하고 FFmpeg로 렌더링합니다.

## 요구 사항

- Python 3.9+
- FFmpeg (시스템에 설치)
- `pip install numpy pillow jsonschema`

## 사용법

### 레거시 모드 (단일 영상에 필터)

```bash
python video_fx.py input.mp4 output.mp4 --preset cinematic --text "안녕하세요"
python video_fx.py input.mp4 output.mp4 --grayscale --blur 5 --fade-in 1 --fade-out 1
```

주요 옵션: `--preset` (cinematic/vintage/bw-film/vivid/cold/warm),
`--text`, `--font`, `--text-size`, `--fade-in`, `--fade-out`,
`--speed`, `--fit` (contain/cover/stretch), `--blur`, `--vignette`,
`--brightness`, `--contrast`, `--saturation`

### 프로젝트 모드 (타임라인 합성)

```bash
python video_fx.py render project.json -o out.mp4
python video_fx.py render project.json --preview-frame 1.25 -o preview.png
```

`project.json` v1 스키마: compositions (width/height/fps/duration/background),
layers (video/image/text/shape/solid/null/comp/adjustment),
transform + keyframes (linear/hold/ease-in/ease-out/ease-in-out/back/bounce/elastic),
effects, audio 믹싱을 지원합니다. 잘못된 값은 렌더 전에 어떤 레이어의
어떤 값이 틀렸는지 알려주고 종료됩니다.

### 출력 형식

- mp4 (H.264 + AAC, `-crf 18 -pix_fmt yuv420p -movflags +faststart`)
- mov (ProRes 4444, 알파 지원)
- png 시퀀스 / 단일 프레임 / 콘택트 시트

## 스펙

상세 스펙은 `AE.md` (업그레이드 요구사항 문서) 참조.
