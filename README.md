# video-fx

JSON 타임라인 기반 영상 합성기. Python 3.11+, FFmpeg, Pillow, NumPy, jsonschema가 필요합니다.

```bash
python video_fx.py input.mp4 output.mp4 --grayscale --blur 5 --fade-in 1 --fade-out 1
python video_fx.py render project.json -o output.mp4
python video_fx.py import-srt project.json captions.srt -o with-captions.json
python video_fx.py import-timing project.json timing.json --base ./sh02 --scenes scenes.json -o sh02.json
```

`tail`은 영상 원본 종료 뒤 동작을 `hold`(기본), `loop`, `error` 중에서 고릅니다. `hidden` 매트 레이어는 매트 표면만 만들며 최종 캔버스에는 나타나지 않습니다. 프리셋은 사용자 변환 위에 scale 곱셈, position/rotation 덧셈, opacity 곱셈으로 순서대로 합성됩니다.

뒤집기는 `--flip h`, `--flip v`, `--flip hv` 및 긴 이름을 지원합니다. 프로젝트 스키마와 효과별 옵션은 엄격히 검사합니다. 전체 설계는 `docs/AE.md`, 회귀 테스트는 `tests/test_regression.py`에 있습니다.

## 성능 기준

같은 Linux 검증 환경의 1080×1920, 30fps, 2초 테스트에서:

- 영상 1개 빠른 경로: 3.82초
- 영상 1 + 도형 2 + 텍스트 2 일반 경로: 5.87초

두 수치는 서로 다른 레이어 구성입니다. 실제 렌더 시간은 입력 코덱, 효과, 해상도, CPU에 따라 달라집니다.
