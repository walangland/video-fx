# video_fx — AE-style Video Compositing Engine

After Effects 스타일의 영상 합성 엔진 (Python 단일 파일).
JSON 타임라인의 레이어, 키프레임, 모션 그래픽, 오디오 믹싱을 지원하고
FFmpeg로 렌더링합니다.

## 요구 사항

- Python 3.9+
- FFmpeg (시스템에 설치, ffprobe 포함)
- `pip install numpy pillow jsonschema`

## 사용법

### 레거시 모드 (단일 영상에 필터)

```bash
python video_fx.py input.mp4 output.mp4 --preset cinematic --text "안녕하세요"
python video_fx.py input.mp4 output.mp4 --grayscale --blur 5 --fade-in 1 --fade-out 1
python video_fx.py --list-presets
```

주요 옵션: `--preset` (cinematic/vintage/bw-film/vivid/cold/warm),
`--text`, `--font`, `--text-size`, `--fade-in`, `--fade-out`,
`--speed`, `--fit` (contain/cover/stretch), `--size` (`--resize` 별칭),
`--blur`, `--vignette`, `--brightness`, `--contrast`, `--saturation`,
`--grayscale`, `--sepia`, `--invert`, `--edge`, `--flip` (horizontal/vertical)

### 프로젝트 모드 (타임라인 합성)

```bash
python video_fx.py render project.json -o out.mp4
python video_fx.py render project.json --preview-frame 1.25 -o preview.png
python video_fx.py render project.json --contact-sheet 0,5,10,20 -o sheet.png
```

`project.json` v1 스키마 — compositions (width/height/fps/duration/background),
layers (video/image/text/shape/solid/null/comp/adjustment):

- **transform + keyframes**: linear/hold/ease-in/ease-out/ease-in-out/back/bounce/elastic/cubic-bezier
- **anchor · parent**: 기준점(0~1 비율) 기준 배율·회전, 부모 레이어 변환 상속 (null 레이어 포함)
- **presets**: 내장 sh02 프리셋 (zoom_punch, slow_push, shake, flash, pop_in, stamp, bar_fill, slide_up, crossfade, wipe).
  레이어에 `"presets":[{"name":"pop_in","t":0}]` 형태로 적용.
  `project.json` 옆 `presets/*.json`에 사용자 정의 프리셋 추가 가능
- **텍스트**: `spans` (부분 색상), `stagger` (글자/단어/줄 순차 등장), 카운트업, 타자기
- **마스크 · 매트**: 사각형·타원·다각형 마스크 + 페더 + 키프레임, 알파/루마 매트
- **합성 모드**: normal, multiply, screen, overlay, soft-light 등
- **도형**: width/height/color/trim 키프레임, 선형·원형 그라데이션
- **모션 블러**: 셔터 각도·샘플 수 (가우시안 근사)
- **SRT/타이밍 가져오기**: 자막 파일에서 레이어 자동 생성
- **검증**: 잘못된 레이어 타입·속성·키프레임은 렌더 전에 어느 레이어의 어느 값이 틀렸는지 알려주고 종료

### 오디오

- 영상에 오디오 스트림이 없어도 렌더 성공 (ffprobe로 자동 감지)
- `"mute": true` 레이어의 오디오는 제외
- 오디오 트랙/클립의 fade_in/fade_out, 볼륨, atempo 속도 보정

### 출력 형식

- mp4 (H.264 + AAC, `-crf 18 -pix_fmt yuv420p -movflags +faststart`)
- mov (ProRes 4444, 알파 지원 — 투명 배경은 `"transparent"`)
- png 시퀀스: 폴더 경로 또는 `frame_%06d.png` 패턴 지정
- 콘택트 시트: `--contact-sheet 0,5,10,...`

## 성능

- 영상 레이어별 순차 FFmpeg 디코더 사용 (프레임마다 프로세스 재실행 안 함)
- 측정: 1080×1920 · 30fps · 2초 · 레이어 1개 → 약 3.8초
- 목표: 60초 영상 ≤ 10분

## 스펙

상세 스펙은 `docs/AE.md` (업그레이드 요구사항 문서) 참조.
