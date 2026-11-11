"""清分样本卡号脱敏：连接键血缘追溯与换号。

输入：含样本行的模型 JSON（source_tables 带 columns 与 rows，models 带 SQL）。
从选中的源表卡号列出发，沿各模型的直接选列与改名把等值 INNER JOIN 的
连接键解析回源列（含跨模型引用），与所选列通过连接键直接或间接相连的
源列全部换号；其余字段、所有行与模型 SQL 原样保留。

仅解析：带限定名的直接选列、改名（AS）、等值 INNER JOIN（多条等式用 AND）。
不执行 SQL；遇到不支持的语法、缺列或无效样本即抛出 MaskingError 并指出位置。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import NamedTuple

import sqlglot
from sqlglot import exp

DIALECT = "duckdb"

MASK_PREFIX = "MASK"


class MaskingError(Exception):
    """无法生成脱敏下载时抛出，message 为可直接展示的中文说明（含位置）。"""


class Leaf(NamedTuple):
    """源列叶子：源表名 + 列名。"""
    table: str
    column: str


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ColumnRef:
    """模型 SQL 中一处带限定名的列引用。"""
    qualifier: str
    column: str


@dataclass
class RawModel:
    """单条模型 SQL 的结构化解析结果（尚未做跨模型列解析）。"""
    name: str
    sql: str
    # 输出列名 -> 列引用
    projections: dict[str, ColumnRef]
    # 连接等式：(左引用, 右引用)
    equalities: list[tuple[ColumnRef, ColumnRef]]
    # 别名/表名 -> ("table" | "model", 实际名称)
    relations: dict[str, tuple[str, str]]


@dataclass
class ModelInfo:
    name: str
    sql: str
    upstream: set[str]
    # 输出列名 -> 最终源列叶子（拓扑序解析后填充）
    outputs: dict[str, Leaf] = field(default_factory=dict)


@dataclass
class Package:
    source_schemas: dict[str, list[str]]
    source_rows: dict[str, list[dict]]
    models: dict[str, ModelInfo]
    order: list[str]
    # 全部模型连接等式解析出的源列无向边
    edges: list[tuple[Leaf, Leaf]]


# ---------------------------------------------------------------------------
# JSON 结构校验
# ---------------------------------------------------------------------------

def _validate_payload(payload: dict) -> tuple[dict[str, list[str]], dict[str, list[dict]], list[dict]]:
    if not isinstance(payload, dict):
        raise MaskingError("JSON 顶层必须是对象，包含 source_tables 和 models 两个数组。")
    source_tables = payload.get("source_tables")
    models = payload.get("models")
    if not isinstance(source_tables, list):
        raise MaskingError("JSON 缺少数组字段 source_tables（源表结构与样本行）。")
    if not isinstance(models, list):
        raise MaskingError("JSON 缺少数组字段 models（模型 SQL 清单）。")

    source_schemas: dict[str, list[str]] = {}
    source_rows: dict[str, list[dict]] = {}
    for i, tbl in enumerate(source_tables):
        where = f"source_tables[{i}]"
        if not isinstance(tbl, dict):
            raise MaskingError(f"{where} 必须是对象，包含 table、columns、rows。")
        tname = tbl.get("table")
        if not isinstance(tname, str) or not tname.strip():
            raise MaskingError(f"{where} 缺少非空字符串 table。")
        tname = tname.strip()
        if tname in source_schemas:
            raise MaskingError(f"源表「{tname}」在 source_tables 中重复出现。")
        cols = tbl.get("columns")
        if not isinstance(cols, list) or not cols or not all(
            isinstance(c, str) and c.strip() for c in cols
        ):
            raise MaskingError(f"源表「{tname}」的 columns 必须是非空字符串数组。")
        cols = [c.strip() for c in cols]
        if len(set(cols)) != len(cols):
            dup = next(c for c in cols if cols.count(c) > 1)
            raise MaskingError(f"源表「{tname}」的列「{dup}」在 columns 中重复。")
        rows = tbl.get("rows")
        if not isinstance(rows, list):
            raise MaskingError(f"源表「{tname}」缺少样本行数组 rows。")
        for j, row in enumerate(rows):
            if not isinstance(row, dict):
                raise MaskingError(
                    f"源表「{tname}」第 {j + 1} 行不是 JSON 对象，样本行必须是对象。"
                )
        source_schemas[tname] = cols
        source_rows[tname] = rows

    model_names: list[str] = []
    for i, m in enumerate(models):
        where = f"models[{i}]"
        if not isinstance(m, dict):
            raise MaskingError(f"{where} 必须是对象，包含 name 和 sql。")
        name = m.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MaskingError(f"{where} 缺少非空字符串 name。")
        name = name.strip()
        if name in model_names:
            raise MaskingError(f"模型「{name}」在 models 中重复出现。")
        sql = m.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise MaskingError(f"模型「{name}」缺少非空 sql。")
        if name in source_schemas:
            raise MaskingError(f"模型「{name}」与源表重名，请改名以消除歧义。")
        model_names.append(name)

    return source_schemas, source_rows, [
        {"name": m["name"].strip(), "sql": m["sql"]} for m in models
    ]


# ---------------------------------------------------------------------------
# 单条模型 SQL 解析（严格子集）
# ---------------------------------------------------------------------------

_UNSUPPORTED_CLAUSES = [
    ("where", "WHERE 筛选"),
    ("group", "GROUP BY 分组"),
    ("having", "HAVING 筛选"),
    ("qualify", "QUALIFY 筛选"),
    ("order", "ORDER BY 排序"),
    ("limit", "LIMIT 截断"),
    ("offset", "OFFSET 偏移"),
    ("distinct", "DISTINCT 去重"),
]

_SUBSET_HINT = "本工具仅解析带限定名的直接选列、改名（AS）与等值 INNER JOIN（多条等式用 AND）。"


def _unwrap_parens(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _check_qualified_column(col: exp.Column, model_name: str) -> ColumnRef:
    """校验 表别名.列名 形式的列引用，返回 ColumnRef。"""
    if isinstance(col.this, exp.Star):
        raise MaskingError(
            f"模型「{model_name}」不允许使用星号 {col.sql()}，请显式列出列。"
        )
    if col.args.get("db") is not None or col.args.get("catalog") is not None:
        raise MaskingError(
            f"模型「{model_name}」的列「{col.sql()}」限定名只能是「表别名.列名」两级。"
        )
    if not col.table:
        raise MaskingError(
            f"模型「{model_name}」的列「{col.name}」未带表名或别名，"
            "请为所有列引用补充限定名。"
        )
    return ColumnRef(qualifier=col.table, column=col.name)


def _parse_projection(proj: exp.Expression, model_name: str) -> tuple[str, ColumnRef]:
    if isinstance(proj, exp.Star):
        raise MaskingError(
            f"模型「{model_name}」不允许使用星号 SELECT *，请显式列出列。"
        )
    if isinstance(proj, exp.Column):
        ref = _check_qualified_column(proj, model_name)
        return ref.column, ref
    if isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Column):
        ref = _check_qualified_column(proj.this, model_name)
        alias = proj.alias
        if not alias:
            raise MaskingError(
                f"模型「{model_name}」的投影「{proj.sql()}」缺少有效别名。"
            )
        return alias, ref
    raise MaskingError(
        f"模型「{model_name}」的 SELECT 投影「{proj.sql()}」不是直接选列或改名，"
        f"{_SUBSET_HINT}"
    )


def _split_equalities(on: exp.Expression, model_name: str) -> list[exp.EQ]:
    on = _unwrap_parens(on)
    parts = [_unwrap_parens(p) for p in on.flatten()] if isinstance(on, exp.And) else [on]
    equalities: list[exp.EQ] = []
    for part in parts:
        if isinstance(part, exp.Or):
            raise MaskingError(
                f"模型「{model_name}」的 JOIN ON 条件「{part.sql()}」使用了 OR，"
                "仅支持 AND 连接的等值条件。"
            )
        if not isinstance(part, exp.EQ):
            raise MaskingError(
                f"模型「{model_name}」的 JOIN ON 条件「{part.sql()}」不是「列 = 列」等值条件，"
                f"{_SUBSET_HINT}"
            )
        equalities.append(part)
    return equalities


def _describe_join(join: exp.Join) -> str:
    side = (join.side or "").upper()
    kind = (join.kind or "").upper()
    method = (join.method or "").upper()
    if method:
        return f"{method} JOIN"
    if side or kind:
        return f"{(side + ' ' + kind).strip()} JOIN"
    return "未标 INNER 的连接（含逗号隐式连接 / 裸 JOIN）"


def _register_relation(relations: dict[str, tuple[str, str]],
                       table: exp.Table,
                       model_name: str,
                       source_schemas: dict[str, list[str]],
                       model_names: set[str]) -> None:
    if table.args.get("db") is not None or table.args.get("catalog") is not None:
        raise MaskingError(
            f"模型「{model_name}」的表「{table.sql()}」不支持带库名/目录名限定，"
            "请直接使用源表或模型名。"
        )
    actual = table.name
    key = table.alias_or_name
    if key in relations:
        raise MaskingError(f"模型「{model_name}」中表名/别名「{key}」重复。")
    if actual in model_names:
        relations[key] = ("model", actual)
    elif actual in source_schemas:
        relations[key] = ("table", actual)
    else:
        raise MaskingError(
            f"模型「{model_name}」读取了未知表「{actual}」："
            "它既不在 source_tables 中，也不是已定义的模型。"
        )


def _parse_model(model_name: str, sql: str,
                 source_schemas: dict[str, list[str]],
                 model_names: set[str]) -> RawModel:
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise MaskingError(f"模型「{model_name}」SQL 解析失败：{exc}") from exc
    if len(statements) != 1:
        raise MaskingError(
            f"模型「{model_name}」必须只包含一条 SELECT 语句，当前解析出 {len(statements)} 条。"
        )
    tree = statements[0]
    if isinstance(tree, (exp.Union, exp.Except, exp.Intersect)):
        raise MaskingError(
            f"模型「{model_name}」不支持集合运算（UNION / EXCEPT / INTERSECT）。{_SUBSET_HINT}"
        )
    if not isinstance(tree, exp.Select):
        raise MaskingError(
            f"模型「{model_name}」只允许单条 SELECT 语句，不支持 {type(tree).__name__}。"
        )
    if tree.args.get("with_") is not None:
        raise MaskingError(
            f"模型「{model_name}」不支持 CTE（WITH），请把子查询展开为独立模型。{_SUBSET_HINT}"
        )
    for arg_key, label in _UNSUPPORTED_CLAUSES:
        if tree.args.get(arg_key) is not None:
            raise MaskingError(f"模型「{model_name}」包含不支持的 {label}。{_SUBSET_HINT}")

    # SELECT 投影：仅直接选列与改名
    projections: dict[str, ColumnRef] = {}
    for proj in tree.selects:
        out_name, ref = _parse_projection(proj, model_name)
        if out_name in projections:
            raise MaskingError(f"模型「{model_name}」的输出列「{out_name}」重复。")
        projections[out_name] = ref

    # FROM 与 INNER JOIN 关系
    relations: dict[str, tuple[str, str]] = {}
    from_node = tree.args.get("from_")
    if from_node is None or not isinstance(from_node.this, exp.Table):
        found = "子查询" if from_node is not None and isinstance(
            from_node.this, exp.Subquery) else "缺失或不是表引用"
        raise MaskingError(
            f"模型「{model_name}」的 FROM {found}，仅支持直接引用源表或模型。{_SUBSET_HINT}"
        )
    _register_relation(relations, from_node.this, model_name, source_schemas, model_names)

    equalities: list[tuple[ColumnRef, ColumnRef]] = []
    for join in tree.args.get("joins", []):
        if (join.kind or "").upper() != "INNER" or join.side or join.method:
            raise MaskingError(
                f"模型「{model_name}」仅允许显式 INNER JOIN，"
                f"发现{_describe_join(join)}，请改写为 INNER JOIN ... ON ...。"
            )
        if not isinstance(join.this, exp.Table):
            raise MaskingError(
                f"模型「{model_name}」的 JOIN 目标不是表引用（不支持子查询）。{_SUBSET_HINT}"
            )
        _register_relation(relations, join.this, model_name, source_schemas, model_names)
        if join.args.get("using"):
            raise MaskingError(
                f"模型「{model_name}」不支持 JOIN ... USING，请改写为 ON 等值条件。"
            )
        on = join.args.get("on")
        if on is None:
            raise MaskingError(
                f"模型「{model_name}」的 INNER JOIN 缺少 ON 等值条件。{_SUBSET_HINT}"
            )
        for eq in _split_equalities(on, model_name):
            left = _unwrap_parens(eq.this)
            right = _unwrap_parens(eq.expression)
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                raise MaskingError(
                    f"模型「{model_name}」的 JOIN ON 等式「{eq.sql()}」两侧都必须是"
                    "带限定名的列，不允许常量或表达式。"
                )
            equalities.append((
                _check_qualified_column(left, model_name),
                _check_qualified_column(right, model_name),
            ))

    return RawModel(
        name=model_name,
        sql=sql,
        projections=projections,
        equalities=equalities,
        relations=relations,
    )


# ---------------------------------------------------------------------------
# 跨模型解析与包构建
# ---------------------------------------------------------------------------

def _topological_order(models: dict[str, RawModel],
                       upstream: dict[str, set[str]]) -> list[str]:
    order: list[str] = []
    state: dict[str, int] = {}  # 0=未访问 1=访问中 2=完成

    def visit(name: str, stack: list[str]) -> None:
        s = state.get(name, 0)
        if s == 2:
            return
        if s == 1:
            cycle = " -> ".join(stack[stack.index(name):] + [name])
            raise MaskingError(f"模型依赖存在循环：{cycle}，仅支持有向无环的模型引用。")
        state[name] = 1
        for up in sorted(upstream[name]):
            visit(up, stack + [name])
        state[name] = 2
        order.append(name)

    for name in sorted(models):
        visit(name, [])
    return order


def build_package(payload: dict) -> Package:
    """校验并解析整个样本包，返回源表结构、样本行、模型血缘与连接等式边。"""
    source_schemas, source_rows, model_defs = _validate_payload(payload)
    model_names = {m["name"] for m in model_defs}

    raw: dict[str, RawModel] = {}
    upstream: dict[str, set[str]] = {}
    for m in model_defs:
        rm = _parse_model(m["name"], m["sql"], source_schemas, model_names)
        raw[m["name"]] = rm
        ups = {actual for kind, actual in rm.relations.values() if kind == "model"}
        if rm.name in ups:
            raise MaskingError(f"模型「{rm.name}」不能读取自身。")
        upstream[rm.name] = ups

    order = _topological_order(raw, upstream)

    # 按拓扑序把每个引用解析到源列叶子
    infos: dict[str, ModelInfo] = {}
    edges: list[tuple[Leaf, Leaf]] = []

    def resolve(model_name: str, ref: ColumnRef) -> Leaf:
        rel = raw[model_name].relations.get(ref.qualifier)
        if rel is None:
            raise MaskingError(
                f"模型「{model_name}」引用了未定义的表名/别名「{ref.qualifier}」"
                f"（列「{ref.qualifier}.{ref.column}」），无法解析连接键。"
            )
        kind, actual = rel
        if kind == "table":
            if ref.column not in source_schemas[actual]:
                raise MaskingError(
                    f"模型「{model_name}」引用了源表「{actual}」上不存在的列"
                    f"「{ref.column}」，请检查列名拼写。"
                )
            return Leaf(actual, ref.column)
        up_outputs = infos[actual].outputs
        if ref.column not in up_outputs:
            raise MaskingError(
                f"模型「{model_name}」引用了上游模型「{actual}」上不存在的输出列"
                f"「{ref.column}」，请检查列名拼写。"
            )
        return up_outputs[ref.column]

    for name in order:
        rm = raw[name]
        info = ModelInfo(name=name, sql=rm.sql, upstream=upstream[name])
        for out_name, ref in rm.projections.items():
            info.outputs[out_name] = resolve(name, ref)
        for left, right in rm.equalities:
            edges.append((resolve(name, left), resolve(name, right)))
        infos[name] = info

    return Package(
        source_schemas=source_schemas,
        source_rows=source_rows,
        models=infos,
        order=order,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# 并查集：连接键连通的源列集合
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[Leaf, Leaf] = {}

    def add(self, node: Leaf) -> None:
        self._parent.setdefault(node, node)

    def find(self, node: Leaf) -> Leaf:
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, a: Leaf, b: Leaf) -> None:
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def component(self, node: Leaf) -> set[Leaf]:
        root = self.find(node)
        return {n for n in self._parent if self.find(n) == root}


def connected_columns(package: Package, table: str, column: str) -> list[Leaf]:
    """与所选列通过连接键直接或间接相连的源列（含所选列本身），按名称排序。"""
    if table not in package.source_schemas:
        raise MaskingError(f"源表「{table}」不在 source_tables 清单中。")
    if column not in package.source_schemas[table]:
        raise MaskingError(f"源表「{table}」上不存在列「{column}」。")
    uf = _UnionFind()
    uf.add(Leaf(table, column))
    for left, right in package.edges:
        uf.union(left, right)
    return sorted(uf.component(Leaf(table, column)))


# ---------------------------------------------------------------------------
# 换号
# ---------------------------------------------------------------------------

def _json_type_name(value) -> str:
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, (int, float)):
        return "数字"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, dict):
        return "对象"
    return type(value).__name__


def _collect_originals(package: Package, columns: list[Leaf]) -> set[str]:
    """校验待换号列的样本值并收集全部不同原号。"""
    originals: set[str] = set()
    for leaf in columns:
        rows = package.source_rows[leaf.table]
        for i, row in enumerate(rows):
            if leaf.column not in row:
                raise MaskingError(
                    f"源表「{leaf.table}」第 {i + 1} 行缺少待换号列「{leaf.column}」，"
                    "样本无效，未生成下载。"
                )
            value = row[leaf.column]
            if value is None:
                continue
            if not isinstance(value, str):
                raise MaskingError(
                    f"源表「{leaf.table}」第 {i + 1} 行待换号列「{leaf.column}」的值"
                    f"（{value!r}）类型为{_json_type_name(value)}，仅允许字符串或 null，"
                    "样本无效，未生成下载。"
                )
            originals.add(value)
    return originals


def _build_mapping(originals: set[str]) -> dict[str, str]:
    """同一原号 -> 同一新号；不同原号不合并；新号不等于任何待换原号。"""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    seq = 0
    for value in sorted(originals):
        while True:
            seq += 1
            candidate = f"{MASK_PREFIX}{seq:08d}"
            if candidate not in originals and candidate not in used:
                break
        mapping[value] = candidate
        used.add(candidate)
    return mapping


def mask_payload(payload: dict, table: str, column: str) -> tuple[dict, dict]:
    """生成脱敏样本包。

    返回 (脱敏后的完整 JSON, 换号摘要)。脱敏 JSON 与输入同构，
    不含原号与新号的对照关系。
    """
    package = build_package(payload)
    columns = connected_columns(package, table, column)
    originals = _collect_originals(package, columns)
    mapping = _build_mapping(originals)

    masked = copy.deepcopy(payload)
    tables_by_name = {t["table"]: t for t in masked["source_tables"]}

    summary_columns = []
    for leaf in columns:
        rows = tables_by_name[leaf.table]["rows"]
        replaced = 0
        nulls = 0
        for row in rows:
            value = row[leaf.column]
            if value is None:
                nulls += 1
            else:
                row[leaf.column] = mapping[value]
                replaced += 1
        summary_columns.append({
            "table": leaf.table,
            "column": leaf.column,
            "rows": len(rows),
            "replaced": replaced,
            "nulls": nulls,
        })

    summary = {
        "target": {"table": table, "column": column},
        "renumbered_columns": summary_columns,
        "column_count": len(summary_columns),
        "distinct_values": len(mapping),
        "total_replaced": sum(c["replaced"] for c in summary_columns),
    }
    return masked, summary
