"""逐外行基准解释器（嵌套循环，小资料对照路径）。

语义以本模块为准，物理重写必须与它逐行等价：
- WHERE 只保留 TRUE，FALSE/UNKNOWN 一律剔除；投影原样保留 UNKNOWN(NULL)；
- IN 三值逻辑：无真匹配但候选含 NULL 时为 UNKNOWN；NOT IN 不做 anti join 化简；
- EXISTS/NOT EXISTS 只产生确定布尔，不产生 UNKNOWN；
- COUNT 空输入为 0，SUM/MIN 空输入为 NULL；
- 裸标量子查询：零行→NULL，一行→该值，多行→CardinalityError，绝不取第一条；
- 空键不与空键相等（SQL 等值比较，NULL=NULL 为 UNKNOWN）；
- 每次子查询执行都完整扫描自己的 FROM，扫描次数随外行数线性增长，
  这正是去关联要消除的工作量，也是统计对照的基准。
"""
from dataclasses import dataclass, field

from .ast import (
    Table, Query,
    ProjStar, ProjExpr, ProjAgg,
)
from .values import (
    eval_scalar, compare, is_true, not3, and3 as _and3, or3 as _or3,
    CardinalityError, RelError,
)

RID = '$rid'  # 内部物理行标识，绝不进入用户输出


@dataclass
class ScanStats:
    table_scans: dict = field(default_factory=dict)
    rows_scanned: dict = field(default_factory=dict)

    def record_scan(self, table, row_count):
        self.table_scans[table] = self.table_scans.get(table, 0) + 1
        self.rows_scanned[table] = self.rows_scanned.get(table, 0) + row_count

    def as_dict(self):
        return {
            'table_scans': dict(self.table_scans),
            'rows_scanned': dict(self.rows_scanned),
        }


class BaselineRunner:
    def __init__(self, catalog, tables, params=None):
        self.catalog = catalog
        self.tables = tables
        self.params = params or {}
        self.stats = ScanStats()

    # -- 入口 ---------------------------------------------------------------

    def run(self, query):
        # _execute_block 已按投影别名产出用户列名字典，RID 是内部键不进入投影
        return self._execute_block(query, outer_row={})

    # -- FROM 物化（每次调用都是一次真实扫描） -------------------------------

    def _materialize_from(self, block, outer_row):
        from_ = block.from_
        if isinstance(from_, Table):
            stored = self.tables[from_.name]
            self.stats.record_scan(from_.name, len(stored))
            rows = []
            for position, stored_row in enumerate(stored):
                row = dict(outer_row)  # 合并外层作用域，ref 带 block_id 不会撞键
                for column in self.catalog[from_.name]:
                    row[(from_.bid, from_.alias, column)] = stored_row[column]
                row[RID] = (from_.name, position)
                rows.append(row)
            return rows
        if isinstance(from_, Query):
            child_rows = self._execute_block(from_, outer_row)
            parent_bid = block.bid
            parent_alias = from_.sub_alias
            remapped = []
            for child_row in child_rows:
                row = dict(outer_row)
                for name, value in child_row.items():
                    row[(parent_bid, parent_alias, name)] = value
                remapped.append(row)
            return remapped
        raise RelError('未知 FROM 节点')

    # -- 块执行 -------------------------------------------------------------

    def _execute_block(self, block, outer_row):
        rows = self._materialize_from(block, outer_row)
        if block.where is not None:
            survivors = []
            for row in rows:
                predicate = self._eval_expr(block.where, row)
                if is_true(predicate):
                    survivors.append(row)
            rows = survivors

        is_agg_block = any(isinstance(item, ProjAgg) for item in block.select)
        if is_agg_block:
            rows = self._aggregate(block, rows)
        else:
            projected = []
            for row in rows:
                out = {}
                for item in block.select:
                    if isinstance(item, ProjStar):
                        frame = self._frame_of(block)
                        for column in frame['schema']:
                            out[column] = row[(frame['bid'], frame['alias'], column)]
                    elif isinstance(item, ProjExpr):
                        out[item.name] = self._eval_expr(item.expr, row)
                    else:
                        raise RelError('非聚合块出现聚合投影')
                projected.append(out)
            rows = projected

        if block.limit is not None:
            rows = rows[:block.limit]
        return rows

    def _frame_of(self, block):
        from_ = block.from_
        if isinstance(from_, Table):
            return {'bid': from_.bid, 'alias': from_.alias,
                    'schema': self.catalog[from_.name]}
        return {'bid': block.bid, 'alias': from_.sub_alias,
                'schema': [item.name for item in from_.select if hasattr(item, 'name')]}

    # -- 聚合 ---------------------------------------------------------------

    def _aggregate(self, block, rows):
        item = block.select[0]
        agg = item.agg
        if block.group_by:
            groups = {}
            order = []
            for row in rows:
                key = tuple(self._eval_expr(g, row) for g in block.group_by)
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(row)
            return [{item.name: self._apply_agg(agg, group)} for key in order for group in [groups[key]]]
        return [{item.name: self._apply_agg(agg, rows)}]

    def _apply_agg(self, agg, rows):
        if agg.func == 'count' and agg.expr is None:
            return len(rows)
        values = [self._eval_expr(agg.expr, row) for row in rows]
        non_null = [value for value in values if value is not None]
        if agg.func == 'count':
            return len(non_null)
        if not non_null:
            return None  # SUM/MIN 空输入（或全 NULL）为 NULL，不被占位行多算
        if agg.func == 'sum':
            return sum(non_null)
        if agg.func == 'min':
            return min(non_null)
        raise RelError(f'未知聚合: {agg.func}')

    # -- 表达式（含子查询节点） ----------------------------------------------

    def _eval_expr(self, expr, row):
        kind = type(expr).__name__
        if kind in ('Lit', 'Param', 'Col', 'Bin', 'IsNull'):
            return eval_scalar(expr, row, self.params)
        if kind == 'And':
            outcome = True
            for sub in expr.exprs:
                outcome = _and3(outcome, self._eval_expr(sub, row))
            return outcome
        if kind == 'Or':
            outcome = False
            for sub in expr.exprs:
                outcome = _or3(outcome, self._eval_expr(sub, row))
            return outcome
        if kind == 'Not':
            return not3(self._eval_expr(expr.expr, row))
        if kind == 'Exists':
            inner_rows = self._execute_block(expr.sub, outer_row=row)
            return len(inner_rows) > 0
        if kind == 'InSub':
            return self._eval_in(expr, row)
        if kind == 'Scalar':
            return self._eval_scalar_subquery(expr, row)
        raise RelError(f'基准路径出现未支持节点: {kind}')

    def _eval_in(self, expr, row):
        value = self._eval_expr(expr.value, row)
        candidates = [
            inner_row[expr.sub.select[0].name]
            for inner_row in self._execute_block(expr.sub, outer_row=row)
        ]
        if value is None:
            return None  # NULL IN (...) 恒为 UNKNOWN
        has_null = False
        for candidate in candidates:
            if candidate is None:
                has_null = True
            elif compare('=', value, candidate) is True:
                result = True
                break
        else:
            result = None if has_null else False
        return not3(result) if expr.negated else result

    def _eval_scalar_subquery(self, expr, row):
        block = expr.sub
        rows = self._execute_block(block, outer_row=row)
        if any(isinstance(item, ProjAgg) for item in block.select):
            # 聚合块自身保证恰好一行（无 GROUP BY）或每组一行（有 GROUP BY）
            if len(rows) == 0:
                return None  # 关联 GROUP BY 无匹配组：与聚合空集区分开
            if len(rows) > 1:
                raise CardinalityError('标量子查询返回多行')
            return rows[0][block.select[0].name]
        if len(rows) == 0:
            return None
        if len(rows) > 1:
            raise CardinalityError('标量子查询返回多行，不能取第一条')
        return rows[0][block.select[0].name]
