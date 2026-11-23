"""`python -m playtest_telemetry` — `ptel` 명령과 같다. ptel 이 PATH 에 없을 때 쓴다."""

import sys

from .cli import main

sys.exit(main())
