"""物理关系算子与执行器。

算子集合（去关联的物理表达，全部一次性构建、逐行探测，不为每个外行重扫内表）：
- PxHashSemiJoin / PxHashAntiJoin：等值半/反连接，空键不匹配；
- PxHashMarkJoin：mark 连接，mode='exists' 给确定布尔，mode='in' 给 TRUE/FALSE/UNKNOWN 三态；
- PxAggJoin：右侧先按关联键分组聚合再左探测，外行 1:1 保留，不产生连接扇出；
- PxScalarJoin：裸标量连接，零行→NULL、一行→值、多行→CardinalityError；
- PxAggGlobal：无关联整表聚合（COUNT 空表为 0，SUM/MIN 空表为 NULL）。

空值规则统一在 _key_of / 构建端处理：关联键含 NULL 的内行进入空桶，
任何探测都不命中（SQL 中 NULL=NULL 不是 TRUE）。

构建是惰性的：左侧零行时不物化右侧，与基准解释器“没有外行就不执行子查询”
的扫描时机一致；构建结果通过 BuildCache 按 (节点, 快照, 参数指纹) 复用，
快照或参数变化必然换键，杜绝旧结果误用。
"""
from dataclasses import dataclass, field

from .ast import Agg
from .values import eval_scalar, compare, not3, CardinalityError, RelError

RID = '$rid'


# -- 计划节点 ---------------------------------------------------------------

@dataclass
class PxTable:
    table: str
    alias: str
    bid: int
    schema: tuple
    node_id: int = -1


@dataclass
class PxFilter:
    child: object
    expr: object
    node_id: int = -1


@dataclass
class PxProject:
    child: object
    outputs: tuple          # ((out_ref, expr), ...)
    node_id: int = -1


@dataclass
class PxRename:
    child: object
    names: tuple            # ((源ref, 目标键), ...)
    to_user: bool = False   # True：目标键是用户列名字符串（仅顶层输出）；False：ref->ref
    node_id: int = -1


@dataclass
class PxLimit:
    child: object
    n: int
    node_id: int = -1


@dataclass
class PxAggGlobal:
    child: object
    agg: Agg
    out_ref: tuple
    node_id: int = -1


@dataclass
class PxHashSemiJoin:
    left: object
    right: object
    left_keys: tuple        # Col 元组
    right_keys: tuple
    node_id: int = -1


@dataclass
class PxHashAntiJoin:
    left: object
    right: object
    left_keys: tuple
    right_keys: tuple
    node_id: int = -1


@dataclass
class PxHashMarkJoin:
    left: object
    right: object
    left_keys: tuple
    right_keys: tuple
    mark_ref: tuple
    mode: str                       # 'exists' | 'in'
    value_col: object = None        # mode='in'：左侧 IN 值表达式
    candidate_ref: tuple = None     # mode='in'：右侧候选列 ref
    negated: bool = False
    node_id: int = -1


@dataclass
class PxAggJoin:
    left: object
    right: object
    left_keys: tuple
    right_keys: tuple
    agg: Agg
    out_ref: tuple
    node_id: int = -1


@dataclass
class PxScalarJoin:
    left: object
    right: object
    left_keys: tuple
    right_keys: tuple
    value_ref: tuple
    out_ref: tuple
    node_id: int = -1


# -- 统计与缓存 -------------------------------------------------------------

@dataclass
class ExecStats:
    table_scans: dict = field(default_factory=dict)
    rows_scanned: dict = field(default_factory=dict)
    builds: list = field(default_factory=list)
    cache_hits: list = field(default_factory=list)
    cache_misses: list = field(default_factory=list)

    def record_scan(self, table, row_count):
        self.table_scans[table] = self.table_scans.get(table, 0) + 1
        self.rows_scanned[table] = self.rows_scanned.get(table, 0) + row_count

    def as_dict(self):
        return {
            'table_scans': dict(self.table_scans),
            'rows_scanned': dict(self.rows_scanned),
            'builds': list(self.builds),
            'cache_hits': list(self.cache_hits),
            'cache_misses': list(self.cache_misses),
        }


class BuildCache:
    """按 (计划实例, 节点身份, 输入快照令牌, 参数指纹) 缓存构建侧行集。

    计划实例号隔离不同查询的节点编号空间，杜绝 node#3 这类跨计划撞键；
    快照或参数变化必然换键，旧结果不可能误用。
    """

    def __init__(self):
        self._entries = {}

    def get(self, plan_id, node_id, snapshot_token, params_fp):
        key = (plan_id, node_id, snapshot_token, params_fp)
        return self._entries.get(key)

    def put(self, plan_id, node_id, snapshot_token, params_fp, payload):
        self._entries[(plan_id, node_id, snapshot_token, params_fp)] = payload

    def clear(self):
        self._entries.clear()


# -- 执行上下文 -------------------------------------------------------------

class ExecContext:
    def __init__(self, catalog, tables, params, stats, cache, snapshot_token,
                 plan_id=0):
        self.catalog = catalog
        self.tables = tables
        self.params = params or {}
        self.stats = stats
        self.cache = cache
        self.snapshot_token = snapshot_token
        self.plan_id = plan_id
        self.params_fp = _params_fingerprint(self.params)


def _params_fingerprint(params):
    return tuple(sorted((k, repr(v)) for k, v in params.items()))


def _key_of(row, key_cols):
    values = tuple(eval_scalar(col, row, None) for col in key_cols)
    if any(value is None for value in values):
        return None  # 含 NULL 的键进入空桶，永不命中
    return values


def _apply_agg(agg, rows, ctx):
    if agg.func == 'count' and agg.expr is None:
        return len(rows)
    values = [eval_scalar(agg.expr, row, ctx.params) for row in rows]
    non_null = [value for value in values if value is not None]
    if agg.func == 'count':
        return len(non_null)
    if not non_null:
        return None  # SUM/MIN 空输入为 NULL
    if agg.func == 'sum':
        return sum(non_null)
    if agg.func == 'min':
        return min(non_null)
    raise RelError(f'未知聚合: {agg.func}')


def run(node, ctx):
    kind = type(node).__name__
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise RelError(f'未知物理节点: {kind}')
    return handler(node, ctx)


def _run_table(node, ctx):
    stored = ctx.tables[node.table]
    ctx.stats.record_scan(node.table, len(stored))
    rows = []
    for position, stored_row in enumerate(stored):
        row = {RID: (node.table, position)}
        for column in node.schema:
            row[(node.bid, node.alias, column)] = stored_row[column]
        rows.append(row)
    return rows


def _run_filter(node, ctx):
    return [row for row in run(node.child, ctx)
            if eval_scalar(node.expr, row, ctx.params) is True]


def _run_project(node, ctx):
    out = []
    for row in run(node.child, ctx):
        new_row = dict(row)
        for out_ref, expr in node.outputs:
            new_row[out_ref] = eval_scalar(expr, row, ctx.params)
        out.append(new_row)
    return out


def _run_rename(node, ctx):
    out = []
    for row in run(node.child, ctx):
        if node.to_user:
            out.append({target: row[source] for source, target in node.names})
        else:
            new_row = dict(row)
            for source, target in node.names:
                new_row[target] = row[source]
            out.append(new_row)
    return out


def _run_limit(node, ctx):
    return run(node.child, ctx)[:node.n]


def _run_agg_global(node, ctx):
    rows = run(node.child, ctx)
    return [{node.out_ref: _apply_agg(node.agg, rows, ctx)}]


# -- 连接算子 ---------------------------------------------------------------

def _build_side(node, ctx):
    """惰性构建：返回 (桶字典, 原始行)。缓存命中不产生新的表扫描。"""
    cached = ctx.cache.get(ctx.plan_id, node.node_id,
                           ctx.snapshot_token, ctx.params_fp)
    if cached is not None:
        ctx.stats.cache_hits.append(node.node_id)
        return cached
    ctx.stats.cache_misses.append(node.node_id)
    right_rows = run(node.right, ctx)
    buckets = {}
    for row in right_rows:
        key = _key_of(row, node.right_keys)
        if key is None:
            continue  # 空键桶：任何探测都不匹配
        buckets.setdefault(key, []).append(row)
    ctx.stats.builds.append({
        'node_id': node.node_id,
        'kind': type(node).__name__,
        'build_keys': [col.ref for col in node.right_keys],
        'rows_built': len(right_rows),
        'buckets': len(buckets),
    })
    payload = (buckets, right_rows)
    ctx.cache.put(ctx.plan_id, node.node_id,
                  ctx.snapshot_token, ctx.params_fp, payload)
    return payload


def _run_semi(node, ctx):
    left_rows = run(node.left, ctx)
    if not left_rows:
        return left_rows  # 惰性：无外行则不构建、不扫描内表
    buckets, _ = _build_side(node, ctx)
    return [row for row in left_rows if _key_of(row, node.left_keys) in buckets]


def _run_anti(node, ctx):
    left_rows = run(node.left, ctx)
    if not left_rows:
        return left_rows
    buckets, _ = _build_side(node, ctx)
    return [row for row in left_rows if _key_of(row, node.left_keys) not in buckets]


def _run_mark(node, ctx):
    left_rows = run(node.left, ctx)
    if not left_rows:
        return left_rows
    buckets, _ = _build_side(node, ctx)
    out = []
    for row in left_rows:
        key = _key_of(row, node.left_keys)
        matched = buckets.get(key, ()) if key is not None else ()
        if node.mode == 'exists':
            mark = bool(matched)  # EXISTS 永不 UNKNOWN
        else:
            mark = _in_mark(node, row, matched, ctx)
        if node.negated:
            mark = not3(mark)
        new_row = dict(row)
        new_row[node.mark_ref] = mark
        out.append(new_row)
    return out


def _in_mark(node, outer_row, matched_inner_rows, ctx):
    value = eval_scalar(node.value_col, outer_row, ctx.params)
    if value is None:
        return None  # NULL IN (...) 恒 UNKNOWN，投影必须保留
    has_null = False
    for inner_row in matched_inner_rows:
        candidate = inner_row[node.candidate_ref]
        if candidate is None:
            has_null = True
        elif compare('=', value, candidate) is True:
            return True
    # 无真匹配：候选含 NULL 则 UNKNOWN，否则 FALSE。NOT IN 绝不能化简成 anti join
    return None if has_null else False


def _run_agg_join(node, ctx):
    left_rows = run(node.left, ctx)
    if not left_rows:
        return left_rows
    buckets, _ = _build_side(node, ctx)
    # 先分组聚合（构建端每个键只算一次），再探测：外行 1:1，绝不扇出
    aggregated = {
        key: _apply_agg(node.agg, group, ctx)
        for key, group in buckets.items()
    }
    out = []
    for row in left_rows:
        key = _key_of(row, node.left_keys)
        new_row = dict(row)
        if key is None or key not in aggregated:
            # 关联无匹配等价于内表空集：COUNT=0，SUM/MIN=NULL
            new_row[node.out_ref] = 0 if node.agg.func == 'count' else None
        else:
            new_row[node.out_ref] = aggregated[key]
        out.append(new_row)
    return out


def _run_scalar_join(node, ctx):
    left_rows = run(node.left, ctx)
    if not left_rows:
        return left_rows
    buckets, _ = _build_side(node, ctx)
    out = []
    for row in left_rows:
        key = _key_of(row, node.left_keys)
        group = buckets.get(key, ()) if key is not None else ()
        new_row = dict(row)
        if len(group) == 0:
            new_row[node.out_ref] = None       # 零行 -> NULL
        elif len(group) > 1:
            raise CardinalityError('裸标量子查询返回多行，不能取第一条')
        else:
            new_row[node.out_ref] = group[0][node.value_ref]
        out.append(new_row)
    return out


_HANDLERS = {
    'PxTable': _run_table,
    'PxFilter': _run_filter,
    'PxProject': _run_project,
    'PxRename': _run_rename,
    'PxLimit': _run_limit,
    'PxAggGlobal': _run_agg_global,
    'PxHashSemiJoin': _run_semi,
    'PxHashAntiJoin': _run_anti,
    'PxHashMarkJoin': _run_mark,
    'PxAggJoin': _run_agg_join,
    'PxScalarJoin': _run_scalar_join,
}


def explain(node, indent=0):
    """渲染物理计划，标注构建键与节点身份（缓存键的组成部分）。"""
    prefix = '  ' * indent
    kind = type(node).__name__
    details = [f'node#{node.node_id}']
    if isinstance(node, PxTable):
        details.append(f'table={node.table} alias={node.alias}')
    elif isinstance(node, (PxHashSemiJoin, PxHashAntiJoin, PxHashMarkJoin,
                           PxAggJoin, PxScalarJoin)):
        details.append('build_keys=' + repr([c.ref for c in node.right_keys]))
        details.append('probe_keys=' + repr([c.ref for c in node.left_keys]))
    if isinstance(node, PxHashMarkJoin):
        details.append(f"mode={node.mode} negated={node.negated}")
    if isinstance(node, (PxAggJoin, PxAggGlobal)):
        details.append(f"agg={node.agg.func}")
    lines = [prefix + kind + '(' + ', '.join(details) + ')']
    for child_name in ('child', 'left', 'right'):
        child = getattr(node, child_name, None)
        if child is not None and hasattr(child, 'node_id'):
            lines.append(explain(child, indent + 1))
    return '\n'.join(lines)
