"""作用域绑定与受限结构校验。

绑定器负责：
1. 为每个 Query 块分配 block_id，为每个 FROM 登记作用域帧；
2. 按“最近作用域优先”解析 Col(alias, column)。允许内外层使用相同别名，
   内层块内对该别名的引用解析到内层帧——同名列被内层别名遮蔽，绝不外逃；
3. 把解析结果写入 Col.ref=(block_id, alias, column) 不透明键，
   后续基准/物理路径只认 ref，不再按字符串猜作用域；
4. 校验关联深度：关联只允许指向紧邻外层（深度1），跨层捕获直接 ScopeError；
   FROM 子查询（派生表）必须不关联外层；
5. 聚合子查询的 group_by 形态留给重写器做“恰好等于关联内层列”的等价判定。
"""
from dataclasses import replace

from .ast import (
    And, Or, Not, Bin, IsNull, Col, Param, Lit, Agg,
    Exists, InSub, Scalar, Query, Table,
    ProjStar, ProjExpr, ProjAgg,
    walk_expr, AGG_FUNCS,
)
from .values import ScopeError, UnsupportedError


class ScopeFrame:
    def __init__(self, bid, alias, schema, origin):
        self.bid = bid
        self.alias = alias
        self.schema = tuple(schema)
        self.origin = origin  # 'table' | 'subquery'

    def resolve(self, alias, column):
        if self.alias != alias:
            return None
        if column not in self.schema:
            raise ScopeError(f'别名 {alias} 下不存在列 {column}')
        return (self.bid, alias, column)


class Binder:
    def __init__(self, catalog):
        self.catalog = catalog          # {table_name: [column,...]}
        self._next_bid = 0

    def bind(self, query):
        return self._bind_block(query, parent_frames=[], is_top=True)

    def _new_bid(self):
        bid = self._next_bid
        self._next_bid += 1
        return bid

    def _bind_block(self, query, parent_frames, is_top):
        bid = self._new_bid()
        from_ = query.from_
        if isinstance(from_, Table):
            if from_.name not in self.catalog:
                raise ScopeError(f'未登记的表: {from_.name}')
            frame = ScopeFrame(bid, from_.alias, self.catalog[from_.name], 'table')
            bound_from = replace(from_, bid=bid)
        elif isinstance(from_, Query):
            if not from_.sub_alias:
                raise UnsupportedError('子查询 FROM 必须显式登记别名')
            bound_from = self._bind_block(from_, parent_frames, is_top=False)
            schema = [item.name for item in bound_from.select if hasattr(item, 'name')]
            if any(isinstance(item, ProjStar) for item in bound_from.select):
                raise UnsupportedError('作为 FROM 的子查询必须显式命名投影，不允许 *')
            # 派生表不允许关联外层：其内部所有 Col 必须落在自身块集合内
            inner_bids = self._collect_bids(bound_from)
            for ref in self._collect_col_refs(bound_from):
                if ref[0] not in inner_bids:
                    raise ScopeError('FROM 派生表不允许关联外层作用域')
            frame = ScopeFrame(bid, from_.sub_alias, schema, 'subquery')
            bound_from = replace(bound_from, sub_alias=from_.sub_alias)
        else:
            raise UnsupportedError('FROM 仅支持登记过的表或受限子查询')

        # 注意：这里刻意允许 frame.alias 与父帧同名。最近作用域优先的解析顺序
        # 使内层帧自然遮蔽外层同名别名，这是两层别名遮蔽用例所要求的语义。
        frames = parent_frames + [frame]

        bound_where = self._bind_expr(query.where, frames) if query.where else None
        bound_group = tuple(self._bind_expr(key, frames) for key in query.group_by)

        bound_select = []
        has_proj_agg = False
        star_columns = set()
        explicit_names = set()
        for item in query.select:
            if isinstance(item, ProjStar):
                bound_select.append(item)
                if isinstance(query.from_, Table):
                    star_columns.update(self.catalog[query.from_.name])
                else:
                    star_columns.update(
                        i.name for i in query.from_.select if hasattr(i, 'name'))
            elif isinstance(item, ProjAgg):
                has_proj_agg = True
                if is_top and query.group_by:
                    raise UnsupportedError('顶层整表聚合不允许 GROUP BY')
                bound_agg = self._bind_agg(item.agg, frames)
                bound_select.append(replace(item, agg=bound_agg))
            elif isinstance(item, ProjExpr):
                if item.name in star_columns or item.name in explicit_names:
                    raise UnsupportedError(f'投影别名与既有列撞名: {item.name}')
                explicit_names.add(item.name)
                for node in walk_expr(item.expr):
                    if isinstance(node, Agg):
                        raise UnsupportedError('投影中的聚合必须包装为标量聚合子查询或 ProjAgg')
                bound_select.append(replace(item, expr=self._bind_expr(item.expr, frames)))
            else:
                raise UnsupportedError(f'不支持的投影项: {type(item).__name__}')
        if has_proj_agg and len(bound_select) != 1:
            raise UnsupportedError('聚合查询只能有一个聚合投影')

        return replace(
            query,
            from_=bound_from,
            where=bound_where,
            select=tuple(bound_select),
            group_by=bound_group,
            bid=bid,
        )

    # -- 表达式 -------------------------------------------------------------

    def _resolve_col(self, col, frames):
        # 先按别名筛出同名帧，再按显式层级取第 level 个（0=最近作用域）。
        # 两层别名遮蔽时，内层 level=0 命中内层帧；要引用外层同名别名必须
        # 显式 level=1，杜绝同名列被错误捕获。
        same_alias = [frame for frame in frames if frame.alias == col.alias]
        if not same_alias:
            raise ScopeError(f'列 {col.alias}.{col.column} 无法在当前作用域解析')
        if col.level >= len(same_alias):
            raise ScopeError(
                f'列 {col.alias}.{col.column} 请求层级 {col.level}，'
                f'但同名作用域只有 {len(same_alias)} 层'
            )
        frame = same_alias[-(col.level + 1)]
        ref = frame.resolve(col.alias, col.column)
        if ref is None:
            raise ScopeError(f'别名 {col.alias} 下不存在列 {col.column}')
        return replace(col, ref=ref)

    def _bind_expr(self, expr, frames):
        if expr is None:
            return None
        if isinstance(expr, (Lit, Param)):
            return expr
        if isinstance(expr, Col):
            return self._resolve_col(expr, frames)
        if isinstance(expr, Bin):
            return replace(
                expr,
                left=self._bind_expr(expr.left, frames),
                right=self._bind_expr(expr.right, frames),
            )
        if isinstance(expr, IsNull):
            return replace(expr, expr=self._bind_expr(expr.expr, frames))
        if isinstance(expr, And):
            return replace(expr, exprs=tuple(self._bind_expr(e, frames) for e in expr.exprs))
        if isinstance(expr, Or):
            return replace(expr, exprs=tuple(self._bind_expr(e, frames) for e in expr.exprs))
        if isinstance(expr, Not):
            return replace(expr, expr=self._bind_expr(expr.expr, frames))
        if isinstance(expr, Agg):
            return self._bind_agg(expr, frames)
        if isinstance(expr, Exists):
            return Exists(self._bind_subquery(expr.sub, frames, kind='exists'))
        if isinstance(expr, InSub):
            bound_value = self._bind_expr(expr.value, frames)
            bound_sub = self._bind_subquery(expr.sub, frames, kind='in')
            return replace(expr, value=bound_value, sub=bound_sub)
        if isinstance(expr, Scalar):
            return Scalar(self._bind_subquery(expr.sub, frames, kind='scalar'))
        raise UnsupportedError(f'不支持的表达式节点: {type(expr).__name__}')

    def _bind_agg(self, agg, frames):
        if agg.func not in AGG_FUNCS:
            raise UnsupportedError(f'仅支持聚合 {AGG_FUNCS}')
        if agg.expr is None:
            if agg.func != 'count':
                raise UnsupportedError('只有 count 允许 * 参数')
            return agg
        return replace(agg, expr=self._bind_expr(agg.expr, frames))

    # -- 子查询 -------------------------------------------------------------

    def _bind_subquery(self, sub, parent_frames, kind):
        bound = self._bind_block(sub, parent_frames, is_top=False)
        parent_bid = parent_frames[-1].bid

        if kind == 'in' and (
            len(bound.select) != 1 or not isinstance(bound.select[0], ProjExpr)
        ):
            raise UnsupportedError('IN 子查询必须且只能投影一个单列')
        if kind == 'scalar' and len(bound.select) != 1:
            raise UnsupportedError('标量子查询必须且只能投影一列')

        # 关联深度检查：本块直接表达式中的外层 ref 只能指向紧邻外层。
        # 嵌套子查询在各自绑定时已做过同样检查（绑定自底向上发生）。
        for node in self._block_own_nodes(bound):
            if isinstance(node, Col) and node.ref is not None and node.ref[0] != bound.bid:
                if node.ref[0] != parent_bid:
                    raise ScopeError(
                        f'子查询捕获了深度超过一层的列 {node.ref[1]}.{node.ref[2]}，'
                        '受限表面只允许紧邻外层关联'
                    )
        return bound

    def _block_own_nodes(self, block):
        """本块直接表达式中的节点（walk_expr 在子查询节点处停住，不下沉）。"""
        if block.where is not None:
            yield from walk_expr(block.where)
        for item in block.select:
            if isinstance(item, ProjExpr):
                yield from walk_expr(item.expr)
            elif isinstance(item, ProjAgg):
                yield from walk_expr(item.agg)
        for key in block.group_by:
            yield from walk_expr(key)

    def _collect_bids(self, block):
        bids = {block.bid}
        if isinstance(block.from_, Query):
            bids |= self._collect_bids(block.from_)
        for node in self._block_own_nodes(block):
            if isinstance(node, (Exists, InSub, Scalar)):
                bids |= self._collect_bids(node.sub)
        return bids

    def _collect_col_refs(self, block):
        for node in self._block_own_nodes(block):
            if isinstance(node, Col) and node.ref is not None:
                yield node.ref
            if isinstance(node, (Exists, InSub, Scalar)):
                yield from self._collect_col_refs(node.sub)
        if isinstance(block.from_, Query):
            yield from self._collect_col_refs(block.from_)
