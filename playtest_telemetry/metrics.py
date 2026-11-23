"""
정의 파일(YAML)을 읽어 파생지표를 계산하고 metric_results 에 적재한다.

이 파일이 설계의 중심 주장을 구현한다 — **지표 정의는 수집 이후에 바뀐다.**
게임은 원시 이벤트만 남기고, 무엇을 지표로 볼지는 여기서 결정한다. 정의를
고치고 다시 돌리면 새 지표가 나온다. 게임을 다시 빌드할 필요가 없다.

지표 종류
---------
  count    조건에 맞는 이벤트 수
  sum      이벤트의 특정 파라미터 합
  avg      이벤트의 특정 파라미터 평균
  min/max  이벤트의 특정 파라미터 최소/최대
  formula  이미 계산된 다른 지표들로 만드는 수식

`by` 를 주면 그 파라미터를 축으로 쪼개 계산한다(아이템별, 몬스터별 등).
축을 추가해도 테이블 스키마는 바뀌지 않는다 — dimension_key/dimension_value 에
들어간다.

사용법
------
    python -m playtest_telemetry.metrics <정의파일.yaml> [--db playtest.sqlite] [--show]
"""

from __future__ import annotations

import argparse
import ast
import difflib
import pathlib
import sqlite3
import sys
from typing import Any

import yaml

from .db import SCHEMA, connect


AGGREGATES = {
    "count": "COUNT(*)",
    "sum": "SUM({v})",
    "avg": "AVG({v})",
    "min": "MIN({v})",
    "max": "MAX({v})",
}

OPERATORS = {"eq": "=", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}

# 파라미터 값을 문자로 통일해 꺼내는 식. 축 값으로 쓴다.
PARAM_AS_TEXT = (
    "COALESCE("
    "  CASE WHEN {p}.num_value = CAST({p}.num_value AS INTEGER)"
    "       THEN CAST(CAST({p}.num_value AS INTEGER) AS TEXT)"
    "       ELSE CAST({p}.num_value AS TEXT) END,"
    "  {p}.text_value,"
    "  CAST({p}.bool_value AS TEXT)"
    ")"
)


class DefinitionError(Exception):
    """정의 파일이 잘못된 경우. 어느 지표가 왜 틀렸는지 알려준다."""


# --------------------------------------------------------------------- 수식

class FormulaEvaluator:
    """
    다른 지표의 값으로 사칙연산을 하는 작은 평가기.

    eval() 을 쓰지 않는다. 정의 파일은 사람이 손으로 쓰는 설정이고, 거기에
    임의의 파이썬이 실행되면 안 된다. 허용한 노드만 직접 처리한다.

    "이름을 잘못 적었다" 와 "그 지표에 해당 데이터가 없었다" 를 구분한다.
    전자는 정의 파일의 잘못이므로 멈추고 알려야 하고, 후자는 흔한 일이다 —
    전투를 한 번도 안 한 세션에 승률이 없는 것은 오류가 아니다. 이 경우
    수식은 값 없음(None)이 되고 그 행은 적재되지 않는다.

    "범위가 달라 원래 값이 있을 수 없다" 도 전자로 본다. 세션 수식에서 파티
    지표를 이름만으로 부르면 세션 범위에는 그 값이 영영 없다. 데이터 없음으로
    넘기면 "건너뜀 N개" 만 찍히고 지표가 통째로 빈다.

    declared  {지표 이름: (범위, by)} — 지금까지 선언된 지표
    scope     이 수식을 평가하는 범위. global 이면 모든 지표의 전역값을 이름으로
              부를 수 있다.
    """

    ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)

    def __init__(self, values: dict[str, float],
                 declared: dict[str, tuple[str, str | None]], scope: str):
        self.values = values
        self.declared = declared
        self.scope = scope

    def eval(self, expression: str) -> float | None:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as e:
            raise DefinitionError(f"수식을 해석할 수 없다: {expression!r} ({e.msg})")
        return self._walk(tree.body)

    def _walk(self, node: ast.AST) -> float | None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise DefinitionError(f"수식에 쓸 수 없는 상수: {node.value!r}")
            return float(node.value)

        if isinstance(node, ast.Name):
            if node.id in self.values:
                return self.values[node.id]
            if node.id not in self.declared:
                raise DefinitionError(
                    f"수식이 참조한 지표가 정의 파일에 없다: {node.id!r}. "
                    f"이름을 확인하고, 이 지표보다 앞에 선언되어 있는지 보라."
                )
            if self.scope != "global":
                self._check_reachable(node.id)
            return None           # 선언은 되어 있으나 이 범위에 데이터가 없다

        # party.어떤지표 — 같은 세션의 파티 값을 가져온다.
        # "내 지출 ÷ 파티 지출" 같은 기여도 지표가 이 모양이라, 범위를
        # 넘나드는 참조가 필요하다. ast 에서는 Attribute 노드로 들어온다.
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or node.value.id != "party":
                raise DefinitionError(
                    f"수식에 쓸 수 없는 참조다: {ast.unparse(node)!r}. "
                    f"범위를 넘나드는 참조는 party.지표이름 만 된다."
                )
            key = f"party.{node.attr}"
            if key in self.values:
                return self.values[key]
            ref_scope, ref_by = self.declared.get(node.attr, (None, None))
            if ref_scope == "party":
                if ref_by and self.scope != "global":
                    raise DefinitionError(
                        f"party.{node.attr} 는 by: {ref_by} 로 쪼갠 지표라 수식에서 "
                        f"한 값으로 쓸 수 없다. by 없는 지표를 따로 선언해 참조하라."
                    )
                return None       # 파티 지표로 선언됐으나 이 세션에 값이 없다
            if ref_scope is not None:
                raise DefinitionError(
                    f"{node.attr!r} 는 party 범위로 선언되지 않았다. "
                    f"party.{node.attr} 로 참조하려면 그 지표에 "
                    f"scope: party 를 적어야 한다."
                )
            raise DefinitionError(
                f"수식이 참조한 지표가 정의 파일에 없다: party.{node.attr}. "
                f"이름을 확인하고, 이 지표보다 앞에 선언되어 있는지 보라."
            )

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = self._walk(node.operand)
            if operand is None:
                return None
            return operand if isinstance(node.op, ast.UAdd) else -operand

        if isinstance(node, ast.BinOp) and isinstance(node.op, self.ALLOWED_BINOPS):
            left, right = self._walk(node.left), self._walk(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            # 나눗셈. 0 으로 나누면 값이 없는 것으로 둔다.
            # 0 을 돌려주면 "전투를 안 했다" 와 "승률이 0" 이 구분되지 않는다.
            return None if right == 0 else left / right

        raise DefinitionError(
            f"수식에 허용되지 않은 문법이 있다: {type(node).__name__}. "
            f"사칙연산과 지표 이름만 쓸 수 있다."
        )

    def _check_reachable(self, name: str) -> None:
        """이 범위의 수식에서 이름만으로 부를 수 있는 지표인지 본다."""
        ref_scope, ref_by = self.declared[name]
        if ref_by:
            raise DefinitionError(
                f"{name!r} 는 by: {ref_by} 로 쪼갠 지표라 수식에서 한 값으로 쓸 수 없다. "
                f"by 없는 지표를 따로 선언해 참조하라."
            )
        if ref_scope == self.scope:
            return
        label = {"session": "세션", "party": "파티", "global": "전역"}
        hint = ""
        if ref_scope == "party" and self.scope == "session":
            hint = f" 같은 세션의 파티 값이면 party.{name} 로 쓴다."
        elif ref_scope == "global":
            hint = " 전역 지표는 scope: global 수식에서만 참조할 수 있다."
        elif ref_scope == "session" and self.scope == "party":
            hint = (" 파티 수식에는 파티 지표만 쓸 수 있다 — 같은 정의로 "
                    "scope: party 지표를 하나 더 선언해 참조하라.")
        raise DefinitionError(
            f"{name!r} 는 {label[ref_scope]} 범위 지표라 {label[self.scope]} 범위 "
            f"수식에서 이름만으로 참조할 수 없다 — 값이 항상 비게 된다.{hint}"
        )


# ----------------------------------------------------------------- SQL 생성

def build_where(conditions: dict[str, Any], params: list[Any]) -> list[str]:
    """파라미터 조건을 EXISTS 절들로 바꾼다."""
    clauses = []
    for i, (key, condition) in enumerate(conditions.items()):
        alias = f"w{i}"

        if isinstance(condition, dict):
            if len(condition) != 1:
                raise DefinitionError(
                    f"조건 {key!r} 에 연산자가 하나만 있어야 한다: {condition!r}"
                )
            (op_name, want), = condition.items()
            if op_name not in OPERATORS:
                raise DefinitionError(
                    f"모르는 연산자 {op_name!r}. 쓸 수 있는 것: "
                    f"{', '.join(OPERATORS)}"
                )
            op = OPERATORS[op_name]
        else:
            op, want = "=", condition

        if isinstance(want, bool):
            value_expr = f"{alias}.bool_value {op} ?"
        elif isinstance(want, (int, float)):
            value_expr = f"{alias}.num_value {op} ?"
        else:
            value_expr = f"{alias}.text_value {op} ?"

        clauses.append(
            f"EXISTS (SELECT 1 FROM event_params_merged {alias}"
            f" WHERE {alias}.session_id = e.session_id"
            f"   AND {alias}.player_id  = e.player_id"
            f"   AND {alias}.seq        = e.seq"
            f"   AND {alias}.key        = ?"
            f"   AND {value_expr})"
        )
        params.extend([key, want])

    return clauses


def build_query(spec: dict[str, Any], grouping: str = "session") -> tuple[str, list[Any]]:
    """
    지표 정의 하나를 SQL 로 바꾼다. grouping 이 묶는 단위를 정한다.

      session  세션 × 플레이어          "내가 쓴 골드"
      party    세션 (참가자를 합침)      "파티가 쓴 골드"
      global   전부를 하나로

    party 와 global 은 session 결과를 합치지 않고 원본에서 다시 집계한다.
    합계는 어느 쪽으로 구해도 같지만 평균은 다르다 — 참가자마다 표본 수가
    달라서, 세션 평균을 다시 평균내면 적게 산 사람과 많이 산 사람이 같은
    무게를 갖는다. 실제 로그에서 재집계 146.4 vs 평균의 평균 147.7 로
    갈렸다. 어느 지표가 들어올지 모르므로 항상 원본에서 다시 센다.
    """
    if grouping not in ("session", "party", "global"):
        raise DefinitionError(f"모르는 집계 단위 {grouping!r}")
    name = spec["name"]
    kind = spec["type"]

    if kind not in AGGREGATES:
        raise DefinitionError(f"[{name}] 모르는 지표 종류 {kind!r}")

    event = spec.get("event")
    if not event:
        raise DefinitionError(f"[{name}] event 가 필요하다")

    param = spec.get("param")
    if kind != "count" and not param:
        raise DefinitionError(f"[{name}] {kind} 은 param 이 필요하다")

    sql_params: list[Any] = []
    joins: list[str] = []
    if grouping == "session":
        select_cols = ["e.session_id", "e.player_id"]
        group_cols = ["e.session_id", "e.player_id"]
    elif grouping == "party":
        # player_id 자리에 NULL 을 넣는다. 적재하는 쪽이 session 범위와
        # 똑같은 모양(session_id, player_id, dim, value)으로 받게 하려는 것이고,
        # NULL 자체가 "이 값은 특정 플레이어의 것이 아니다" 라는 표시가 된다.
        select_cols = ["e.session_id", "NULL AS player_id"]
        group_cols = ["e.session_id"]
    else:
        select_cols, group_cols = [], []

    # 집계 대상 파라미터
    if param:
        joins.append(
            "JOIN event_params_merged v"
            " ON v.session_id = e.session_id"
            " AND v.player_id = e.player_id"
            " AND v.seq = e.seq"
            " AND v.key = ?"
        )
        sql_params.append(param)
        agg = AGGREGATES[kind].format(v="v.num_value")
    else:
        agg = AGGREGATES[kind]

    # 쪼갤 축
    by = spec.get("by")
    if by:
        joins.append(
            "JOIN event_params_merged d"
            " ON d.session_id = e.session_id"
            " AND d.player_id = e.player_id"
            " AND d.seq = e.seq"
            " AND d.key = ?"
        )
        sql_params.append(by)
        dim_expr = PARAM_AS_TEXT.format(p="d")
        select_cols.append(f"{dim_expr} AS dimension_value")
        group_cols.append(dim_expr)
    else:
        select_cols.append("'' AS dimension_value")

    select_cols.append(f"{agg} AS value")

    # canonical = 0 은 다른 참가자 파일의 중복 복사본이다. 파티 집계에서 빼야
    # 공유 자원이 참가자 수만큼 부풀려지지 않는다. 1인 세션은 전부 1 이라 영향이 없다.
    where = ["e.canonical = 1", "e.name = ?"]
    sql_params_where: list[Any] = [event]
    where.extend(build_where(spec.get("where") or {}, sql_params_where))

    sql = (
        f"SELECT {', '.join(select_cols)}\n"
        f"FROM events e\n"
        + ("".join(f"{j}\n" for j in joins))
        + f"WHERE {' AND '.join(where)}"
    )
    if group_cols:
        sql += f"\nGROUP BY {', '.join(group_cols)}"
    return sql, sql_params + sql_params_where


# ------------------------------------------------------------------- 실행

# 지표 정의에서 쓸 수 있는 키. 여기 없는 키가 오면 멈춘다.
#
# 예전에는 모르는 키를 조용히 무시했다. 그래서 `scope: party` 를 적어도
# 오류 없이 세션 범위로 계산됐고, 사람 눈에는 파티 합계처럼 보이는 숫자가
# 대시보드에 떴다. `wehre:` 같은 오타도 마찬가지로 조건 없이 전부 집계했다.
# 정의 파일을 손으로 고치는 도구에서 "고친 게 안 먹었다" 를 알려주지 않으면
# 도구가 거짓말을 하는 셈이다.
KNOWN_KEYS = {"name", "type", "scope", "event", "param",
              "by", "where", "default", "formula"}


def describe_unknown(got, known: set[str]) -> str | None:
    """모르는 키를 비슷한 이름 힌트와 함께 설명한다. 없으면 None.

    정의 파일의 지표 키(여기)와 merge: 섹션의 키(merge.py)가 같이 쓴다.
    """
    unknown = set(got) - known
    if not unknown:
        return None
    hints = []
    for key in sorted(map(str, unknown)):
        near = difflib.get_close_matches(key, known, n=1, cutoff=0.6)
        hints.append(f"{key!r}" + (f" (혹시 {near[0]!r}?)" if near else ""))
    return (f"{', '.join(hints)}\n"
            f"  쓸 수 있는 키: {', '.join(sorted(known))}")


def load_definition(path: str | pathlib.Path) -> dict[str, Any]:
    """정의 파일을 읽어 맨 위 모양까지 확인한다.

    맨 위는 `metrics:` (와 선택으로 `merge:`)를 가진 모음이어야 한다.
    목록만 있는 파일, 빈 파일, YAML 문법 오류는 여기서 사람이 읽을 수 있는
    오류로 바꾼다. 그대로 두면 AttributeError 같은 traceback 으로 끝난다.
    """
    try:
        data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise DefinitionError(f"{path} 를 YAML 로 읽지 못했다: {e}") from e
    check_definition(data)
    return data


def check_definition(data: Any) -> None:
    if isinstance(data, list):
        raise DefinitionError(
            "정의 파일의 맨 위가 목록이다. 지표 목록은 `metrics:` 아래에 둔다.\n"
            "    metrics:\n"
            "      - name: battle_count\n"
            "        ...")
    if not isinstance(data, dict):
        raise DefinitionError(
            "정의 파일이 비어 있거나 모양이 틀렸다. 맨 위에 `metrics:` 가 있어야 한다.")
    metrics = data.get("metrics")
    if not metrics:
        raise DefinitionError("metrics 가 비어 있다")
    if not isinstance(metrics, list):
        raise DefinitionError("metrics 는 지표의 목록이어야 한다 (각 항목 앞에 '- ')")
    for i, spec in enumerate(metrics):
        if not isinstance(spec, dict):
            raise DefinitionError(
                f"metrics 의 {i + 1}번째 항목이 `name: ...` 꼴이 아니다: {spec!r}")


def check_keys(spec: dict[str, Any], name: str) -> None:
    problem = describe_unknown(spec, KNOWN_KEYS)
    if problem:
        raise DefinitionError(f"[{name}] 모르는 키: {problem}")


def list_scopes(con: sqlite3.Connection, scope: str) -> list[tuple[str, Any]]:
    """그 범위에 존재하는 단위를 모두 돌려준다.

    지표가 하나도 안 걸린 세션도 포함해야 한다. default 를 채우거나
    "데이터 없어 건너뜀" 을 세려면 분모가 필요하기 때문이다.
    """
    if scope == "party":
        return [(r[0], None) for r in con.execute(
            "SELECT DISTINCT session_id FROM sessions")]
    return con.execute("SELECT session_id, player_id FROM sessions").fetchall()


def scope_values(con: sqlite3.Connection, scope: str,
                 session_id: str, player_id: Any) -> dict[str, float]:
    """이 범위에서 수식이 참조할 수 있는 값들.

    자기 범위의 지표는 이름 그대로, 같은 세션의 파티 값은 party. 를 붙여
    담는다. session 범위의 수식에서도 party 값을 쓸 수 있어야 한다 —
    "내 지출 ÷ 파티 지출" 같은 기여도 지표가 그 모양이고, 분모가 없으면
    협동 게임에서 묻고 싶은 질문이 성립하지 않는다.
    """
    if scope == "party":
        rows = con.execute(
            "SELECT metric_name, value FROM metric_results"
            " WHERE scope = 'party' AND session_id = ? AND dimension_key = ''",
            (session_id,))
    else:
        rows = con.execute(
            "SELECT metric_name, value FROM metric_results"
            " WHERE scope = 'session' AND session_id = ? AND player_id = ?"
            "   AND dimension_key = ''",
            (session_id, player_id))
    values = dict(rows)

    for metric_name, value in con.execute(
            "SELECT metric_name, value FROM metric_results"
            " WHERE scope = 'party' AND session_id = ? AND dimension_key = ''",
            (session_id,)):
        values[f"party.{metric_name}"] = value
    return values


def compute(con: sqlite3.Connection, definition: dict[str, Any]) -> int:
    check_definition(definition)
    metrics = definition["metrics"]

    # SCHEMA 는 여러 문장이라 executescript 가 필요하다.
    con.executescript(SCHEMA)
    con.execute("DELETE FROM metric_results")

    # formula 가 참조할 전역 합계를 모아둔다.
    global_values: dict[str, float] = {}
    rows_written = 0

    # 이미 선언된 지표 {이름: (범위, by)}. 오타와 "데이터 없음" 을 구분하고,
    # 범위가 달라 값이 있을 수 없는 참조를 잡는 데 쓴다.
    declared: dict[str, tuple[str, str | None]] = {}

    for spec in metrics:
        name = spec.get("name")
        if not name:
            raise DefinitionError(f"name 이 없는 지표가 있다: {spec!r}")
        check_keys(spec, name)
        kind = spec.get("type")
        if not kind:
            raise DefinitionError(f"[{name}] type 이 없다")

        scope = spec.get("scope", "session")
        if scope not in ("session", "party", "global"):
            raise DefinitionError(
                f"[{name}] 모르는 scope {scope!r}. session · party · global 중 하나다."
            )

        # ---------------------------------------------------- formula
        if kind == "formula":
            expression = spec.get("formula")
            if not expression:
                raise DefinitionError(f"[{name}] formula 가 필요하다")

            # 범위별로 계산한다. 범위마다 참조 지표 값이 다르다.
            # 지표가 하나도 안 걸린 세션도 포함해야 하므로 sessions 를 기준으로 돈다.
            # scope: global 은 전역값 하나만 낸다 — 세션마다 돌면 scope='global'
            # 인데 session_id 가 붙은 행이 생겨 v_metrics_global 에 여러 줄이 뜬다.
            scopes = list_scopes(con, scope) if scope != "global" else []

            # 참조가 맞는지는 데이터와 상관없이 먼저 본다. 세션이 하나도 없는 DB
            # 에서도 범위가 틀린 참조는 잡아야 한다. 값이 비어 있으니 결과는 None.
            FormulaEvaluator({}, declared, scope).eval(expression)

            computed = 0
            for session_id, player_id in scopes:
                values = scope_values(con, scope, session_id, player_id)
                result = FormulaEvaluator(values, declared, scope).eval(expression)
                if result is None:
                    continue
                con.execute(
                    "INSERT INTO metric_results VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    (name, scope, session_id, player_id, "", "", result),
                )
                rows_written += 1
                computed += 1

            # 전역값도 같은 수식으로 계산한다.
            result = FormulaEvaluator(global_values, declared, "global").eval(expression)
            if result is not None:
                con.execute(
                    "INSERT INTO metric_results VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    (name, "global", None, None, "", "", result),
                )
                global_values[name] = result
                rows_written += 1

            declared[name] = (scope, None)
            skipped = len(scopes) - computed
            note = f", 데이터 없어 건너뜀 {skipped}개" if skipped else ""
            label = {"session": "세션", "party": "파티", "global": "전역"}[scope]
            if scope == "global":
                count = "1개" if result is not None else "값 없음"
            else:
                count = f"{computed}개{note}"
            print(f"  {name} (formula, {label}) — {count}")
            continue

        # ------------------------------------------------ 집계 지표
        dimension_key = spec.get("by") or ""
        seen_scopes: set[tuple[str, str]] = set()
        rows: list[Any] = []

        # scope: global 은 전역값만 낸다. 아래 전역 집계가 그 일을 하므로
        # 여기서는 범위별 행을 만들지 않는다.
        if scope != "global":
            sql, sql_params = build_query(spec, scope)
            try:
                rows = con.execute(sql, sql_params).fetchall()
            except sqlite3.Error as e:
                raise DefinitionError(f"[{name}] 쿼리 실행 실패: {e}") from e

            for session_id, player_id, dim_value, value in rows:
                if value is None:
                    continue
                seen_scopes.add((session_id, player_id))
                con.execute(
                    "INSERT INTO metric_results VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    (name, scope, session_id, player_id,
                     dimension_key, dim_value or "", float(value)),
                )
                rows_written += 1

        # default 가 선언된 지표는 해당 이벤트가 없는 세션에도 그 값을 채운다.
        #
        # "해당 이벤트가 0건" 이 0 을 뜻하는지는 지표마다 다르다. 획득한 골드
        # 합계는 0 이 맞지만, 평균 전투 시간은 전투를 안 했으면 0 이 아니라
        # 값이 없는 것이다. 어느 쪽인지는 지표를 정의한 사람만 알 수 있으므로
        # 정의 파일에서 선언하게 한다.
        if "default" in spec and not dimension_key and scope != "global":
            default = float(spec["default"])
            for session_id, player_id in list_scopes(con, scope):
                if (session_id, player_id) in seen_scopes:
                    continue
                con.execute(
                    "INSERT INTO metric_results VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    (name, scope, session_id, player_id, "", "", default),
                )
                rows_written += 1

        # 전역 집계. 세션별 결과를 합치지 않고 원본에서 다시 계산한다.
        global_spec = dict(spec)
        global_spec.pop("by", None)
        gsql, gparams = build_query(global_spec, "global")
        try:
            grow = con.execute(gsql, gparams).fetchone()
        except sqlite3.Error as e:
            raise DefinitionError(f"[{name}] 전역 집계 실패: {e}") from e

        gvalue = None
        if grow and grow[-1] is not None:
            gvalue = float(grow[-1])
        elif "default" in spec:
            gvalue = float(spec["default"])

        if gvalue is not None:
            con.execute(
                "INSERT INTO metric_results VALUES (?,?,?,?,?,?,?,datetime('now'))",
                (name, "global", None, None, "", "", gvalue),
            )
            global_values[name] = gvalue
            rows_written += 1

        # 축별 전역값. 이것도 원본에서 다시 센다 — 세션·플레이어별 avg 를
        # 더하거나 평균내면 틀린다(README "전역 평균은 세션 평균의 평균이 아니다").
        # 카드와 --show 가 축별 합계를 낼 때 이 행을 읽는다.
        if dimension_key:
            gsql, gparams = build_query(spec, "global")
            try:
                grows = con.execute(gsql, gparams).fetchall()
            except sqlite3.Error as e:
                raise DefinitionError(f"[{name}] 축별 전역 집계 실패: {e}") from e
            for dim_value, value in grows:
                if value is None:
                    continue
                con.execute(
                    "INSERT INTO metric_results VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    (name, "global", None, None, dimension_key, dim_value or "",
                     float(value)),
                )
                rows_written += 1

        declared[name] = (scope, dimension_key or None)
        dim_note = f", 축 {dimension_key}" if dimension_key else ""
        scope_note = f", {scope}" if scope != "session" else ""
        empty = "  ※ 해당 이벤트가 로그에 없다" if not rows else ""
        print(f"  {name} ({kind}{scope_note}) — {len(rows)}행{dim_note}{empty}")

    return rows_written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m playtest_telemetry.metrics",
        description="정의 파일로 파생지표를 계산한다")
    ap.add_argument("definition", help="지표 정의 YAML")
    ap.add_argument("--db", default="playtest.sqlite")
    ap.add_argument("--show", action="store_true", help="계산 결과를 출력한다")
    args = ap.parse_args(argv)

    def_path = pathlib.Path(args.definition)
    if not def_path.exists():
        print(f"[오류] 정의 파일이 없다: {def_path}", file=sys.stderr)
        return 1
    if not pathlib.Path(args.db).exists():
        # 그대로 열면 빈 DB 가 새로 생기고 "해당 이벤트가 없다" 만 줄줄이 뜬다.
        print(f"[오류] DB 가 없다: {args.db} — ptel run 으로 먼저 적재하라",
              file=sys.stderr)
        return 1
    try:
        definition = load_definition(def_path)
    except DefinitionError as e:
        print(f"[정의 오류] {e}", file=sys.stderr)
        return 1
    con = connect(args.db)

    print(f"지표 계산 — {def_path.name}")
    try:
        written = compute(con, definition)
    except DefinitionError as e:
        # 정의가 틀렸으면 아무것도 쓰지 않는다. 절반만 계산된 결과를
        # 남기면 그래프가 조용히 틀린 값을 그린다.
        con.rollback()
        print(f"\n[정의 오류] {e}", file=sys.stderr)
        return 1

    con.commit()
    print(f"\n완료 — metric_results {written}행")

    if args.show:
        print("\n전역 지표")
        print("-" * 52)
        for row in con.execute(
            "SELECT metric_name, value FROM metric_results"
            " WHERE scope = 'global' AND dimension_key = ''"
            " ORDER BY metric_name"
        ).fetchall():
            print(f"  {row[0]:<34} {row[1]:>14,.2f}")

        dim_rows = con.execute(
            "SELECT metric_name, dimension_key, dimension_value, value"
            " FROM metric_results WHERE scope = 'global' AND dimension_key != ''"
            " ORDER BY 1, 4 DESC"
        ).fetchall()
        if dim_rows:
            print("\n축별 지표 (전역 — 원본에서 다시 계산)")
            print("-" * 52)
            for name, dkey, dval, value in dim_rows:
                print(f"  {name:<24} {dkey}={dval:<8} {value:>10,.2f}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
