# AE-FX → After Effects 기능 구현 수정사항

> 작성: 2026-10-01 · 대상: `D:\SW\Muse\Coding\After_Effect\ae_fx.py`
> 목표: 영상 한 개에 필터를 씌우는 CLI를 **레이어·키프레임·모션 그래픽이 있는 합성 엔진**으로 바꾼다.
> 기준 결과물: 유튜브 쇼츠 sh02 v3 — 1080×1920 · 30fps · 59.5초 · 영상 34컷 + TTS + 효과음 + 모션 그래픽
> (`D:\01. 유튜브\12. 1분만\맛도리혼밥러\02_편별기획\sh02_쿠팡치즈스틱TOP3\v3_자극형\`)

---

## 0. 먼저 알아둘 것

- **같은 목표의 문서가 하나 더 있다:** `C:\Users\iamkc\Documents\Codex\2026-10-01\new-chat\outputs\motion-studio-mcp\업그레이드_요구사항.md`. 둘 다 끝까지 만들면 같은 일을 두 번 하게 된다. **둘 중 하나만 본 개발 대상으로 정한다.** 이 문서의 §1(버그 수정)만은 어느 쪽을 택하든 해 둘 가치가 있다.
- 현재 구조(OpenCV로 프레임을 읽어 필터 적용 → OpenCV로 저장)로는 오디오·여러 클립·레이어 합성을 할 수 없다. **§2 구조 교체가 이 문서의 핵심**이고, §3 이후 기능은 전부 새 구조 위에 올린다.

---

## 1단계 — 확인된 버그 수정 (2026-10-01 실측)

1080×1920 · 30fps · 2초(영상+오디오) 테스트 파일로 직접 실행해 확인한 문제다.

| # | 문제 | 실측 | 원인 (줄 번호) | 수정 |
|---|---|---|---|---|
| B1 | **오디오 사라짐** | 입력 h264+aac → 출력 mpeg4 영상만 | `cv2.VideoWriter`는 오디오 미지원 (224) | 영상 출력을 ffmpeg 파이프로 교체하고 원본 오디오를 `-map 1:a` 로 합침. 속도 변경 시 `atempo` |
| B2 | **Windows에서 한글 자막 □□** | 폰트를 못 찾아 `load_default()` → 네모 글자, 크기(90) 무시 | 폰트 후보가 Linux 경로뿐 (110~113) | `--font` 옵션 추가. 기본 후보에 `C:\Windows\Fonts\malgun.ttf`, `malgunbd.ttf`, `NotoSansKR-*.ttf` 추가. **한글 폰트를 못 찾으면 조용히 넘어가지 말고 오류로 종료** |
| B3 | **비네팅 극단적으로 느림** | 2초 영상 122.8초 (효과 없음 1.5초) | 매 프레임 같은 마스크를 σ=w/4 거대 블러로 재계산 (64~75) | 마스크를 **한 번만** 만들어 재사용. 블러 대신 부드러운 반경 함수(`smoothstep`)로 직접 계산 |
| B4 | `--speed 0.5` 동작 안 함 | 60프레임 → 60프레임 | 스킵만 있고 중복 로직 없음 (250~255) | 출력 프레임 i → 원본 프레임 `round(i * speed)` 매핑으로 교체 (느리게·빠르게 모두 처리) |
| B5 | `--speed 0` 이면 종료 | `ZeroDivisionError` | 254줄 가드 없음 | 인자 검증: `0.1 ≤ speed ≤ 10`, 벗어나면 안내 후 종료 |
| B6 | 대비가 밝기처럼 동작 | 회색 128에 대비 1.3 → **163** | `x*contrast + brightness` (21) | `(x-128)*contrast + 128 + brightness`. 수정 후 128 → 128 |
| B7 | `cold`·`warm` 프리셋이 색온도를 안 바꿈 | — | 색 채널 조정 없음 (146~147) | R/B 채널 게인(예: cold R×0.92 B×1.08)으로 실제 색온도 조정 |
| B8 | 해상도 변경 시 찌그러짐 | 1080×1920 → 1280×720 늘어남 | 비율 무시 `cv2.resize` (260) | `--fit contain|cover|stretch` (기본 contain, 남는 영역 검정) |
| B9 | 화질 낮음 | 출력 코덱 mpeg4(Part 2) | `mp4v` (224) | H.264 `libx264 -crf 18 -preset medium -pix_fmt yuv420p`, `-movflags +faststart` |
| B10 | 자막이 페이드와 따로 놂 | 화면은 어두워지는데 글자는 그대로 | 페이드 후 자막 그림 (285~289) | 합성 순서: 레이어 합성 → 전체 페이드 |
| B11 | 오류 숨김 | — | `except:` 전부 무시 (117, 215) | 구체적 예외만 잡고 메시지 출력. 미사용 코드(`math`, `frame_pos`, `out_fps` 계산) 삭제 |

**1단계 완료 기준** (테스트 스크립트 `tests/test_regression.py`로 자동 확인)
- [ ] 영상+오디오 입력 → 출력에 오디오 스트림 존재, 길이 오차 ≤ 1프레임
- [ ] Windows에서 `--text "야식 또 시키세요?" --text-size 90` 결과 프레임에 네모 글자 없음 (결과 이미지 첨부)
- [ ] 1080×1920 60프레임 비네팅 처리 시간 ≤ 효과 없음 대비 2배
- [ ] `--speed 0.5` → 프레임 수 2배, `--speed 2` → 절반, 오디오 길이도 동일 비율
- [ ] `--speed 0` → 안내 메시지와 함께 정상 종료 (예외 스택 없음)
- [ ] 회색 128 + 대비 1.3 → 128 ± 1
- [ ] ffprobe 결과 코덱 h264, pix_fmt yuv420p

---

## 2단계 — 구조 교체: 필터 CLI → 합성 엔진

### 2-1. 새 구조

```
project.json ─▶ 로더/검증 ─▶ 타임라인 평가(시간 t의 레이어 상태 계산)
                                   │
                          프레임 렌더러 (numpy/Pillow 또는 skia-python)
                                   │  RGBA 프레임을 stdout 파이프로
                                   ▼
                        ffmpeg (영상 인코딩 + 오디오 믹스) ─▶ out.mp4 / .mov / png 시퀀스
```

- **프레임 단위 확정 렌더:** 시간 t = frame / fps 에서 모든 레이어 상태를 계산해 그린다. 실시간 재생·녹화 방식 금지.
- **영상 입력 디코딩은 ffmpeg**로 한다 (정확한 프레임 seek, VFR 대응). OpenCV `CAP_PROP_FRAME_COUNT` 추정에 의존하지 않는다.
- **오디오는 프레임 루프와 분리:** 오디오 레이어 목록으로 ffmpeg `filter_complex`(adelay · atrim · volume · afade · amix)를 만들어 한 번에 믹스.
- 렌더러 후보: 기본은 numpy + Pillow. 도형 안티앨리어싱·그라데이션·블러 품질이 부족하면 `skia-python`으로 교체 (인터페이스는 같게).

### 2-2. 기존 CLI 호환

- `python ae_fx.py input.mp4 output.mp4 --옵션` 형식은 계속 동작해야 한다. 내부적으로 "영상 레이어 1개 + 효과" 프로젝트를 만들어 새 엔진으로 렌더.
- 새 사용법: `python ae_fx.py render project.json -o out.mp4 [--range 10-20] [--preview-frame 12.5]`

### 2-3. 프로젝트 JSON 스키마 (v1)

```json
{
  "version": 1,
  "compositions": [{
    "id": "main", "width": 1080, "height": 1920, "fps": 30, "duration": 59.5,
    "background": "#000000",
    "layers": [
      {
        "id": "v1", "type": "video", "name": "scene_01",
        "source": "D:\\...\\outputs\\scene_01.mp4",
        "start": 0.0, "in": 0.3, "out": 1.8, "speed": 1.0,
        "transform": {
          "anchor": [0.5, 0.5], "position": [540, 960], "scale": [100, 100],
          "rotation": 0, "opacity": 100
        },
        "keyframes": {
          "scale": [
            {"t": 0.0, "v": [110, 110], "ease": "ease-out"},
            {"t": 0.22, "v": [100, 100]}
          ]
        },
        "effects": [{"type": "color", "saturation": 1.22, "contrast": 1.07}, {"type": "sharpen", "amount": 0.5}],
        "blend": "normal", "parent": null, "mask": null, "matte": null
      },
      {
        "id": "t1", "type": "text", "start": 0.0, "end": 2.0,
        "text": "야식 또 시키세요?",
        "style": {
          "font": "C:\\Windows\\Fonts\\NotoSansKR-Black.ttf", "size": 96, "color": "#ffffff",
          "stroke": {"width": 10, "color": "#000000"},
          "shadow": {"color": "#00000080", "blur": 8, "offset": [0, 4]},
          "align": "center", "max_width": 960, "line_height": 1.15,
          "spans": [{"range": [0, 2], "color": "#ffd400"}]
        },
        "transform": {"position": [540, 1500]},
        "presets": [{"name": "pop_in", "t": 0.0}]
      }
    ]
  }],
  "audio": [
    {"source": "D:\\...\\tts\\audio\\TTS_01_05.wav", "start": 0.0, "volume_db": 0},
    {"source": "D:\\...\\sfx\\pop.wav", "start": 1.2, "volume_db": -11}
  ]
}
```

- 로딩 시 **스키마 검증**(jsonschema). 잘못된 값이면 렌더 전에 어떤 레이어의 어떤 값이 틀렸는지 알려 주고 종료.
- 경로는 절대 경로 또는 프로젝트 파일 기준 상대 경로 허용.

---

## 3단계 — After Effects 핵심 기능

### 3-1. 레이어

| 종류 | 요구사항 |
|---|---|
| `video` | in/out 자르기, 속도, 타임라인 시작 위치, 음소거 여부 (영상 속 오디오도 믹스 대상) |
| `image` | png/jpg, 알파 유지 |
| `text` | 폰트 파일, 크기, 색, 테두리, 그림자, 자간·행간, 최대 폭 자동 줄바꿈, 정렬, **글자 일부만 다른 색(spans)** |
| `shape` | 사각형(둥근 모서리), 원, 다각형·별·버스트(`points`, `inner_radius`), 선·화살표, 채우기(단색·선형·원형 그라데이션), 테두리 |
| `solid` | 단색 판 (배경, 플래시용) |
| `null` | 보이지 않는 부모 컨트롤러 |
| `comp` | 다른 컴포지션을 레이어로 사용 (프리컴프). 시간 이동·속도 적용 |
| `adjustment` | 아래 레이어 전체에 효과 적용 |

- 모든 레이어: `start`/`end` 표시 구간, 순서(z-order), 숨김, `parent`(부모의 위치·회전·배율 상속)

### 3-2. 트랜스폼과 키프레임

- 속성: `anchor`, `position`, `scale`(가로·세로 분리), `rotation`, `opacity`
- **효과·도형·텍스트 속성도 키프레임 가능**: `width`, `height`, `color`, `blur`, 도형 `trim`(0~100%, 선 그리기용), 텍스트 `tracking` 등
- 키프레임마다 가속 곡선: `linear` · `hold` · `ease-in` · `ease-out` · `ease-in-out` · `back`(오버슈트) · `bounce` · `elastic` · `cubic-bezier(x1,y1,x2,y2)`
- 위치 경로는 직선 기본, 선택으로 곡선(베지어 핸들)

### 3-3. 텍스트 애니메이터

- 글자·단어·줄 단위로 순차 등장 (`stagger` 간격, 방향)
- 속성: 투명도, 위치 오프셋, 배율, 회전, 흐림
- **숫자 카운트업:** `counter: {from: 12250, to: 22000, format: "#,##0원", ease: "ease-out"}`
- 타자기 효과(글자 수만큼 표시)

### 3-4. 마스크 · 매트 · 합성 모드

- 마스크: 사각형·타원·다각형, 페더(가장자리 부드럽게), 반전, 마스크 키프레임
- 트랙 매트: 알파 / 알파 반전 / 루마
- 합성 모드: normal, multiply, screen, overlay, add, soft-light
- 모션 블러: 레이어별 on/off, 셔터 각도, 샘플 수

### 3-5. 효과

| 효과 | 요구사항 |
|---|---|
| 색 보정 | 밝기·대비(§1 B6 공식)·채도·감마·색온도·틴트, LUT(.cube) 적용 |
| 블러 | 가우시안, 방향성(모션), 방사형(줌) |
| 샤픈 | 언샤프 마스크 |
| 글로우 | 밝은 부분 번짐 |
| 드롭 섀도 | 레이어 알파 기준 |
| 비네팅 | §1 B3 방식 (마스크 캐시) |
| 흔들림 | `wiggle(freq, amp)` — 시드 고정으로 매번 같은 결과 |
| 노이즈·그레인 | 필름 질감 |

### 3-6. 전환·프리셋 (sh02 v3에서 실제로 쓰는 것 우선)

| 프리셋 | sh02 v3 사용처 |
|---|---|
| `zoom_punch` — 110%→100%, 0.22초 | 컷마다 |
| `slow_push` — 100%→105% 컷 길이 동안 | 컷마다 |
| `shake` | 강조 줄 (2·8·10·16·21·27·31줄) |
| `flash` — 흰 화면 0.1초 | 구간 시작 (5·11·17·24·28·34줄) |
| `pop_in` — 배율 0→115→100 + 투명도 | 팝업 자막 |
| `stamp` — 150%→100% + 흔들림 + 쿵 효과음 | 개당 260원 도장 |
| `bar_fill` — 가로 배율 0→값 | 개당 가격 막대 |
| `slide_up`, `crossfade`, `wipe` | 일반 |

- 프리셋은 JSON 파일(`presets/*.json`)로 정의 → 사용자가 추가 가능

### 3-7. 오디오

- 오디오 레이어: 시작 위치, 자르기, 볼륨(dB), 페이드 인/아웃
- 덕킹: 낭독 구간에서 배경음 자동 감소 (선택)
- 프리셋에 효과음 연결 (예: `stamp` → `sfx/thud.wav`)

### 3-8. 렌더 출력

| 형식 | 용도 |
|---|---|
| mp4 (H.264 + AAC) | 업로드용 완성본 |
| mov (ProRes 4444, 알파) | Premiere에서 영상 위에 얹는 그래픽 |
| png 시퀀스 (RGBA) | 알파 그래픽, 검수 |
| 단일 프레임 png / 콘택트 시트 | 미리보기·검수 |

- 부분 렌더(`--range`), 저해상도 미리보기(`--scale 0.5`), 진행률 표시
- **멀티프로세스 렌더:** 프레임 구간을 나눠 병렬 처리 후 이어 붙이기

---

## 4단계 — sh02 파이프라인 연결

| 기능 | 내용 |
|---|---|
| `import_srt` | `edit/sh02_자막.srt` → 텍스트 레이어 자동 생성 (스타일 프리셋 지정) |
| `import_timing` | `edit/timing.json` → 영상 컷 자동 배치 (파일 형식 확인 후 맞춤) |
| `import_premiere_xml` (선택) | `edit/sh02_편집.xml` 컷 정보 읽기 |
| 템플릿 | 쇼츠 공통 요소(상단 칩, 기준일 표시, 진행 바)를 컴포지션 템플릿으로 저장 |

## 5단계 — Claude 연동 (MCP, 선택)

엔진이 안정된 뒤 `mcp_server.py`로 감싼다. 기존 Motion Studio MCP의 보안 방식을 따른다.

- stdio MCP, stdout은 MCP 전용 (로그는 stderr)
- 도구: `create_project`, `add_layer`, `set_keyframes`, `apply_preset`, `batch`(전부 성공 or 전부 취소), `import_srt`, `import_timing`, `render`, `render_status`, **`preview_frame`(이미지를 MCP 응답으로 반환)**
- 파일 읽기는 허용 폴더(`AEFX_MEDIA_ROOTS`) 안만, 경로 정규화 후 검사. 저장은 출력 폴더 안만, 기본 덮어쓰기 금지

---

## 6. 완료 기준 — sh02 v3 재현 테스트

- [ ] 1단계 회귀 테스트 전부 통과
- [ ] sh02 34컷 + TTS + 효과음을 `project.json` 하나로 렌더 → 1080×1920 · 30fps · 59.5초 ± 1프레임, 오디오 싱크 어긋남 없음
- [ ] v3 그래픽 중 **개당 가격 막대 · 순위 메달 1·2·3위 · 개당 260원 도장 · `12,250원 → 22,000원` 카운트업 · -44% 버스트** 5개 재현
- [ ] v3 원본(`sh02_v3_자극형.mp4`)과 같은 시점 프레임 나란히 비교 이미지 제출 (각 그래픽당 3시점)
- [ ] 알파 mov로 뽑은 그래픽을 Premiere에 얹었을 때 가장자리 검은 테두리(프리멀티플라이 오류) 없음
- [ ] 60초 1080×1920 렌더 시간 ≤ 10분 (일반 PC 기준, 측정값 기록)

## 7. 작업 순서

1. §0 — Motion Studio와 둘 중 본 개발 대상 결정
2. §1 버그 수정 + 회귀 테스트 (구조 교체 전이라도 즉시 사용 가능해짐)
3. §2 구조 교체 (ffmpeg 파이프, JSON 스키마, 타임라인 평가기)
4. **단일 프레임 미리보기(§3-8)를 먼저** — 이후 모든 기능을 프레임 이미지로 검증
5. §3-1 · 3-2 → 3-6 프리셋 → 3-3 텍스트 애니메이터 → 3-4 · 3-5 → 3-7 → 3-8 나머지
6. §4 → §5
7. 각 단계 끝에 README 갱신 + 완료 기준 체크 결과 첨부
