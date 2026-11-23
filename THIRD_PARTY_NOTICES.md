# 외부 구성요소

이 저장소의 코드는 MIT 라이선스다([LICENSE](LICENSE)). 아래는 이 도구가 함께 쓰거나
연동하는 외부 구성요소와 그 라이선스다.

## 설치할 때 함께 받는 것

| 구성요소 | 쓰는 곳 | 라이선스 |
|---|---|---|
| [PyYAML](https://github.com/yaml/pyyaml) | 분석 도구가 정의 파일(`game.yaml`)을 읽는다. `pip install` 때 PyPI 에서 함께 설치된다. 이 저장소에 들어 있지 않다 | MIT |

## 이미 있는 것을 쓰는 것

| 구성요소 | 쓰는 곳 | 라이선스 |
|---|---|---|
| SQLite (Python 표준 라이브러리 `sqlite3`) | 결과 DB | Public Domain |
| Unity | 수집기가 동작하는 엔진. 사용자의 Unity 라이선스를 따른다 | Unity 이용 약관 |

## 포함하지 않는 것

**Metabase** 는 이 도구에 포함되어 있지 않다. 대시보드가 필요한 사용자가 따로 설치하며,
Metabase 의 라이선스를 따른다. 오픈소스 에디션은 AGPL-3.0, 그 외 에디션은 Metabase 의
상용 라이선스다. 이 도구는 Metabase 의 HTTP API 로 카드를 만들 뿐 Metabase 의 코드를
담거나 고치지 않는다.
