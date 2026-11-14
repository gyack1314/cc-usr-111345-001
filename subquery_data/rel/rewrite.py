"""规范化与去关联重写。

分层边界（本模块只做逻辑重写，不触碰行数据与扫描）：
  绑定AST ──rewrite.decorrelate──> 物理计划(physical.*) ──physical.run──> 结果袋

每条改写都先做等价条件检查，产出 Eligibility（规则名 + 适用依据），
条件不满足时抛 UnsupportedError，由 engine 决定 reject 还是回退基准执行，
绝不静默近似。

规则一览（依据均为可证明的代数等价，账本行数不因少扫表而改变）：
  R1 顶层合取 EXISTS    -> 半连接 PxHashSemiJoin
  R2 顶层合取 NOT EXISTS -> 反连接 PxHashAntiJoin（NOT EXISTS 无 UNKNOWN，反连接安全）
  R3 EXISTS（投影/OR内） -> mark 连接 mode=exists，布尔确定性
  R4 IN                 -> mark 连接 mode=in，三态 TRUE/FALSE/UNKNOWN 完整保留
  R5 NOT IN             -> R4 标记取反，禁止化简为 anti join（NULL 候选会改变结果）
  R6 标量聚合子查询      -> PxAggJoin：右侧按关联键分组聚合，外行 1:1 左探测
  R7 裸标量子查询        -> PxScalarJoin：零行 NULL / 一行取值 / 多行 CardinalityError
通用前提：关联只能是 AND 下的等值比较，一侧外层列一侧内层列；
其余谓词必须只引用内层列（局部过滤），否则不具备去关联等价条件。
"""
from dataclasses import dataclass

from .ast import (
    And, Or, Not, Bin, Col, Lit, IsNull,
    Exists, InSub, Scalar, Query, Table,
    ProjStar, ProjExpr, ProjAgg,
    walk_expr, contains_subquery,
)
from . import physical as px
from .values import UnsupportedError

INTERNAL_PREFIX = '$'


@dataclass(frozen=True)
class Eligibility:
    rule: str
    basis: str
    node_id: int


@dataclass(frozen=True)
class _EligibilityInfo:
    equi_pairs: tuple
    local_filters: tuple
    inner_bid: int
    outer_bid: int

    @property
    def inner_cols(self):
        return tuple(pair[1] for pair in self.equi_pairs)


class Rewriter:
    def __init__(self, catalog):
        self.catalog = catalog
        self._next_node = 0
        self._next_synth = 0
        self.decisions = []  # 全部改写决定，供 explain 审计

    def _node_id(self):
        nid = self._next_node
        self._next_node += 1
        return nid

    def _synth_ref(self, label):
        ref = (f'{INTERNAL_PREFIX}syn', label, self._next_synth)
        self._next_synth += 1
        return ref

    def _record(self, rule, basis, node_id):
        decision = Eligibility(rule, basis, node_id)
        self.decisions.append(decision)
        return decision

    # -- 入口 ---------------------------------------------------------------

    def decorrelate(self, query):
        return self._block_plan(query, is_top=True, outer_bid=None,
                                parent_bid=None)

    # -- 块 -> 物理计划 ------------------------------------------------------

    def _block_plan(self, block, is_top, outer_bid, parent_bid=None,
                    strip_equi_refs=()):
        """outer_bid：本块若作为子查询，其关联能指向的紧邻外层块 bid。
        parent_bid：本块在嵌套树中的父块 bid（派生表/连接右端场景），
        用于块内嵌套子查询的关联判定，避免同 bid 数字造成误判。
        """
        from_ = block.from_
        if isinstance(from_, Table):
            plan = px.PxTable(
                table=from_.name, alias=from_.alias, bid=from_.bid,
                schema=tuple(self.catalog[from_.name]), node_id=self._node_id(),
            )
        elif isinstance(from_, Query):
            # 绑定器已保证派生表不关联外层；其嵌套子查询的父块是派生表自身
            plan = self._block_plan(from_, is_top=False,
                                    outer_bid=outer_bid, parent_bid=from_.bid)
            parent_bid = block.bid
            parent_alias = from_.sub_alias
            names = tuple(
                (child_ref(item), (parent_bid, parent_alias, item.name))
                for item in from_.select if hasattr(item, 'name')
            )
            plan = px.PxRename(child=plan, names=names, node_id=self._node_id())
        else:
            raise UnsupportedError('未知 FROM 节点')

        own_bid = block.bid

        # 无 GROUP BY 的整表聚合：
        # - 顶层 ProjAgg：单次扫描全局归约（R0）；
        # - 无外层（无关联）的标量聚合子查询：同样全局归约；
        # - 有关联的聚合子查询必须保留明细行，分组聚合由上层 PxAggJoin(R6) 完成。
        is_global_agg = (
            len(block.select) == 1
            and isinstance(block.select[0], ProjAgg)
            and not block.group_by
            and (is_top or outer_bid is None)
        )
        if is_global_agg:
            if block.where is not None:
                plan = px.PxFilter(plan, block.where, self._node_id())
            agg_item = block.select[0]
            out_ref = self._synth_ref('global_agg')
            plan = px.PxAggGlobal(plan, agg_item.agg, out_ref, self._node_id())
            if is_top:
                plan = px.PxRename(plan, ((out_ref, agg_item.name),),
                                   to_user=True, node_id=self._node_id())
            if block.limit is not None:
                plan = px.PxLimit(plan, block.limit, self._node_id())
            self._record('R0', '无关联整表聚合，单次扫描全局归约', plan.node_id)
            return plan

        # 关联聚合子查询块：保留明细行，分组聚合交给上层 PxAggJoin
        raw_conjuncts = self._split_conjuncts(block.where)
        if strip_equi_refs:
            conjuncts = [c for c in raw_conjuncts if id(c) not in strip_equi_refs]
        else:
            conjuncts = raw_conjuncts
        sub_occurrences = []
        for index, conj in enumerate(conjuncts):
            for node in walk_expr(conj):
                if isinstance(node, (Exists, InSub, Scalar)):
                    # 只登记“整个合取就是该子查询（或其否定）”的顶层出现，
                    # 索引必须是本块 conjuncts 的全局索引，consumed 才能正确删除。
                    if conj in (node, Not(node)):
                        sub_occurrences.append((node, 'where', index))
                    else:
                        sub_occurrences.append((node, 'where-embedded', index))
        for item in block.select:
            if isinstance(item, ProjExpr):
                for node in walk_expr(item.expr):
                    if isinstance(node, (Exists, InSub, Scalar)):
                        sub_occurrences.append((node, 'project', None))

        replacement = {}
        consumed = set()
        # 外层局部谓词（不含子查询、只引用本块列）下推到连接左端：
        # 过滤先于连接构建，外层零行时连接保持惰性，不扫描内表。
        for index, conj in enumerate(conjuncts):
            if not contains_subquery(conj) and self._is_local(conj, own_bid):
                plan = px.PxFilter(plan, conj, self._node_id())
                consumed.add(index)
        # 本块内部子查询能关联的紧邻外层：优先用显式父块 bid，
        # 顶层块（无父）则用本块 bid。
        nested_outer_bid = parent_bid if parent_bid is not None else own_bid
        for sub_node, context, conj_index in sub_occurrences:
            top_conjunct = (
                context == 'where'
                and conj_index is not None
                and conjuncts[conj_index] in (sub_node, Not(sub_node))
            )
            plan, col_ref = self._apply_subquery(
                sub_node, plan, top_conjunct, replacement,
                consumed, conj_index, conjuncts, nested_outer_bid,
            )
            if col_ref is not None:
                replacement[id(sub_node)] = col_ref
            if context == 'where-embedded':
                # 嵌在 AND/OR 内部的子查询被改写为标记列后，原合取整体保留，
                # 由 _replace_refs 把其中的子查询节点替换成标记引用。
                pass
        surviving = [
            self._replace_refs(conj, replacement)
            for index, conj in enumerate(conjuncts) if index not in consumed
        ]
        if surviving:
            new_where = surviving[0] if len(surviving) == 1 else And(tuple(surviving))
            plan = px.PxFilter(plan, new_where, self._node_id())

        outputs = []
        rename_names = []
        frame = self._frame_schema(block)
        for item in block.select:
            if isinstance(item, ProjStar):
                for column in frame['schema']:
                    ref = (frame['bid'], frame['alias'], column)
                    outputs.append((ref, Col(frame['alias'], column, ref)))
                    rename_names.append((ref, column))
            elif isinstance(item, ProjExpr):
                rewritten = self._replace_refs(item.expr, replacement)
                out_ref = self._synth_ref('proj')
                outputs.append((out_ref, rewritten))
                rename_names.append((out_ref, item.name))
        if outputs:
            plan = px.PxProject(plan, tuple(outputs), self._node_id())
        if is_top:
            plan = px.PxRename(plan, tuple(rename_names),
                               to_user=True, node_id=self._node_id())
        if block.limit is not None:
            plan = px.PxLimit(plan, block.limit, self._node_id())
        return plan

    def _frame_schema(self, block):
        from_ = block.from_
        if isinstance(from_, Table):
            return {'bid': from_.bid, 'alias': from_.alias,
                    'schema': self.catalog[from_.name]}
        return {'bid': block.bid, 'alias': from_.sub_alias,
                'schema': [item.name for item in from_.select if hasattr(item, 'name')]}

    @staticmethod
    def _split_conjuncts(where):
        if where is None:
            return []
        if isinstance(where, And):
            return list(where.exprs)
        return [where]

    # -- 单个子查询 -> 在当前计划上叠加连接 ----------------------------------

    def _apply_subquery(self, node, plan, top_conjunct, replacement,
                        consumed, conj_index, conjuncts, outer_bid):
        if isinstance(node, Exists):
            return self._apply_exists(node, plan, top_conjunct,
                                      consumed, conj_index, conjuncts, outer_bid)
        if isinstance(node, InSub):
            return self._apply_in(node, plan, outer_bid)
        if isinstance(node, Scalar):
            return self._apply_scalar(node, plan, outer_bid)
        raise UnsupportedError(f'不支持的子查询节点: {type(node).__name__}')

    def _apply_exists(self, node, plan, top_conjunct,
                      consumed, conj_index, conjuncts, outer_bid):
        eligibility = self._check_correlation(node.sub, outer_bid)
        right, left_keys, right_keys = self._build_join_right(node.sub, eligibility)
        negated = top_conjunct and isinstance(conjuncts[conj_index], Not)
        if top_conjunct:
            nid = self._node_id()
            if negated:
                plan = px.PxHashAntiJoin(plan, right, tuple(left_keys),
                                         tuple(right_keys), nid)
                self._record('R2', 'NOT EXISTS 不产生 UNKNOWN，反连接与逐行判定袋等价', nid)
            else:
                plan = px.PxHashSemiJoin(plan, right, tuple(left_keys),
                                         tuple(right_keys), nid)
                self._record('R1', 'EXISTS 是存在性判定，半连接逐键一次构建袋等价', nid)
            consumed.add(conj_index)
            return plan, None
        nid = self._node_id()
        mark_ref = self._synth_ref('mark_exists')
        plan = px.PxHashMarkJoin(
            plan, right, tuple(left_keys), tuple(right_keys),
            mark_ref, mode='exists', negated=negated, node_id=nid,
        )
        self._record('R3', '投影/析取中的 EXISTS 需要布尔标记列，mark 连接保留三态语境', nid)
        return plan, Col(INTERNAL_PREFIX, 'mark', mark_ref)

    def _apply_in(self, node, plan, outer_bid):
        eligibility = self._check_correlation(node.sub, outer_bid)
        sub = node.sub
        candidate_item = sub.select[0]
        if not isinstance(candidate_item, ProjExpr) or isinstance(
                candidate_item.expr, (Exists, InSub, Scalar)):
            raise UnsupportedError('IN 候选列必须是不含子查询的单值表达式')
        candidate_ref = self._expr_own_ref(candidate_item.expr, sub.bid)
        right, left_keys, right_keys = self._build_join_right(sub, eligibility)
        nid = self._node_id()
        mark_ref = self._synth_ref('mark_in')
        plan = px.PxHashMarkJoin(
            plan, right, tuple(left_keys), tuple(right_keys),
            mark_ref, mode='in', value_col=node.value,
            candidate_ref=candidate_ref, negated=node.negated, node_id=nid,
        )
        if node.negated:
            self._record(
                'R5',
                'NOT IN 必须保留 UNKNOWN（候选含 NULL 且无真匹配），只做标记取反，禁止 anti join',
                nid,
            )
        else:
            self._record('R4', 'IN 的三态结果 TRUE/FALSE/UNKNOWN 由 mark 连接逐键判定，袋等价', nid)
        return plan, Col(INTERNAL_PREFIX, 'mark', mark_ref)

    def _apply_scalar(self, node, plan, outer_bid):
        eligibility = self._check_correlation(node.sub, outer_bid)
        sub = node.sub
        item = sub.select[0]
        if isinstance(item, ProjAgg):
            group_refs = [self._expr_own_ref(key, sub.bid) for key in sub.group_by]
            inner_key_refs = [col.ref for col in eligibility.inner_cols]
            if sub.group_by and sorted(map(repr, group_refs)) != sorted(map(repr, inner_key_refs)):
                raise UnsupportedError(
                    '聚合子查询 GROUP BY 必须恰好等于关联内层列，否则分组键与连接键不构成等价'
                )
            right, left_keys, right_keys = self._build_join_right(sub, eligibility)
            out_ref = self._synth_ref('agg')
            nid = self._node_id()
            plan = px.PxAggJoin(plan, right, tuple(left_keys), tuple(right_keys),
                                item.agg, out_ref, nid)
            self._record(
                'R6',
                '右侧按关联键分组聚合后左探测，外行 1:1；空匹配按 COUNT=0、SUM/MIN=NULL 处理',
                nid,
            )
            return plan, Col(INTERNAL_PREFIX, 'agg', out_ref)
        value_expr = item.expr
        if isinstance(value_expr, (Exists, InSub, Scalar)):
            raise UnsupportedError('裸标量子查询投影不允许再嵌套子查询')
        for en in walk_expr(value_expr):
            if isinstance(en, Col) and en.ref[0] != sub.bid:
                raise UnsupportedError('裸标量子查询投影只能引用内层列（外层值无法在构建端确定）')
        value_ref = self._expr_own_ref(value_expr, sub.bid)
        right, left_keys, right_keys = self._build_join_right(sub, eligibility)
        out_ref = self._synth_ref('scalar')
        nid = self._node_id()
        plan = px.PxScalarJoin(plan, right, tuple(left_keys), tuple(right_keys),
                               value_ref, out_ref, nid)
        self._record(
            'R7',
            '裸标量按关联键分桶：零行 NULL、一行取值、多行 CardinalityError，与逐行语义一致',
            nid,
        )
        return plan, Col(INTERNAL_PREFIX, 'scalar', out_ref)

    # -- 关联等价条件检查 ----------------------------------------------------

    def _check_correlation(self, sub, outer_bid):
        equi_pairs = []
        local_filters = []
        for conj in self._split_conjuncts(sub.where):
            pair = self._as_equi_pair(conj, sub.bid, outer_bid)
            if pair is not None:
                equi_pairs.append(pair)
                continue
            # 含嵌套子查询的合取不是本地行过滤：它由本块自己的重写流程
            # （半连接/mark 连接）消费，绝不能放进构建侧 PxFilter。
            if contains_subquery(conj):
                continue
            if outer_bid is None or self._is_local(conj, sub.bid):
                local_filters.append(conj)
                continue
            raise UnsupportedError(
                '子查询存在非等值关联或析取内关联，不满足去关联的等值前提: '
                + self._describe(conj)
            )
        for item in sub.select:
            if isinstance(item, ProjExpr):
                for en in walk_expr(item.expr):
                    if isinstance(en, Col) and outer_bid is not None and en.ref[0] == outer_bid:
                        raise UnsupportedError('子查询投影引用外层列，不支持去关联')
        return _EligibilityInfo(
            equi_pairs=tuple(equi_pairs),
            local_filters=tuple(local_filters),
            inner_bid=sub.bid,
            outer_bid=outer_bid,
        )

    def _as_equi_pair(self, expr, inner_bid, outer_bid):
        if not isinstance(expr, Bin) or expr.op != '=':
            return None
        left, right = expr.left, expr.right
        if not (isinstance(left, Col) and isinstance(right, Col)):
            return None
        left_side = self._side_of(left, inner_bid, outer_bid)
        right_side = self._side_of(right, inner_bid, outer_bid)
        if left_side is None or right_side is None or left_side == right_side:
            return None
        return (left, right) if left_side == 'outer' else (right, left)

    @staticmethod
    def _side_of(col, inner_bid, outer_bid):
        if col.ref[0] == inner_bid:
            return 'inner'
        if outer_bid is not None and col.ref[0] == outer_bid:
            return 'outer'
        return None

    def _is_local(self, expr, inner_bid):
        for node in walk_expr(expr):
            if isinstance(node, Col) and node.ref[0] != inner_bid:
                return False
        return True

    @staticmethod
    def _describe(expr):
        if isinstance(expr, Col):
            return f'{expr.ref[1]}.{expr.ref[2]}'
        return type(expr).__name__

    # -- 构建侧计划 ----------------------------------------------------------

    def _build_join_right(self, sub, eligibility):
        # 子块作为连接右端构建。块内若还有嵌套子查询，它要关联的紧邻外层
        # 就是本块 sub，因此 parent_bid 显式传 sub.bid——不能依赖继承的
        # outer_bid，因为当 sub 本身也是某个连接的右端时两者数字可能相同。
        right = self._block_plan(
            sub, is_top=False, outer_bid=sub.bid,
            parent_bid=sub.bid,
            strip_equi_refs=frozenset(
                id(conj) for conj in self._split_conjuncts(sub.where)
                if self._as_equi_pair(
                    conj, sub.bid, eligibility.outer_bid) is not None
            ),
        )
        pushed = list(eligibility.local_filters)
        # 关联等值谓词已剥离（键相等由哈希连接承担）；构建侧只额外要求内层
        # 关联键非 NULL：含 NULL 键的内行进入空桶，任何探测都不命中，
        # 与基准解释器 NULL=NULL 为 UNKNOWN 的逐行判定袋等价。
        for _outer_col, inner_col in eligibility.equi_pairs:
            pushed.append(IsNull(inner_col, negated=True))
        if pushed:
            right = px.PxFilter(
                right,
                pushed[0] if len(pushed) == 1 else And(tuple(pushed)),
                self._node_id(),
            )
        left_keys = [pair[0] for pair in eligibility.equi_pairs]
        right_keys = [pair[1] for pair in eligibility.equi_pairs]
        return right, left_keys, right_keys

    def _expr_own_ref(self, expr, own_bid):
        if isinstance(expr, Col) and expr.ref[0] == own_bid:
            return expr.ref
        raise UnsupportedError('连接取值列必须是内层直接列引用')

    # -- 引用替换 ------------------------------------------------------------

    def _replace_refs(self, expr, replacement):
        if expr is None:
            return None
        marker = replacement.get(id(expr))
        if marker is not None:
            return marker
        if isinstance(expr, (Col, Lit)):
            return expr
        if isinstance(expr, Bin):
            return Bin(expr.op,
                       self._replace_refs(expr.left, replacement),
                       self._replace_refs(expr.right, replacement))
        if isinstance(expr, IsNull):
            return IsNull(self._replace_refs(expr.expr, replacement), expr.negated)
        if isinstance(expr, And):
            return And(tuple(self._replace_refs(e, replacement) for e in expr.exprs))
        if isinstance(expr, Or):
            return Or(tuple(self._replace_refs(e, replacement) for e in expr.exprs))
        if isinstance(expr, Not):
            return Not(self._replace_refs(expr.expr, replacement))
        raise UnsupportedError(f'重写期出现未预期节点: {type(expr).__name__}')


def child_ref(item):
    """派生表投影项在子块输出中的列名（ProjExpr/ProjAgg 均有 name）。"""
    return item.name
