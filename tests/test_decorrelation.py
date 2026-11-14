"""去关联等价性与扫描行为测试。

覆盖需求点名的全部场景：
- 重复外行（独立 row_id，结果袋保留重复）；
- 空内表（COUNT=0、SUM/MIN=NULL、IN=FALSE、NOT IN=TRUE、EXISTS=FALSE）；
- 内表带 NULL（IN/NOT IN 的 UNKNOWN、空键不与空键相等）；
- 裸标量多行报 CardinalityError，零行给 NULL；
- 两层别名遮蔽，同名列不被错误捕获；
- 基准 vs 去关联的结果袋与错误类别一致；
- 去关联后内表只扫一次（不再逐外行全扫），并给出构建键与缓存范围；
- 参数/快照变化后缓存不误用旧结果；
- 不支持结构显式拒绝。
"""
import copy
import unittest

from subquery_data.rel.ast import (
    Table, Query, Col, Lit, Param, Bin, And, Or, Not, IsNull,
    Exists, InSub, Scalar, Agg, ProjStar, ProjExpr, ProjAgg,
)
from subquery_data.rel.engine import Engine
from subquery_data.rel.values import (
    UnsupportedError, ScopeError, CardinalityError, RelError,
)
from subquery_data.rel.physical import BuildCache, ExecContext, ExecStats, run

from .builders import (
    CATALOG, make_tables,
    q_exists_fail, q_not_exists_settlement,
    q_in_projection, q_not_in_projection,
    q_scalar_count, q_scalar_sum_min, q_bare_scalar,
    q_uncorrelated_in, q_unsupported_non_equi, q_two_level_shadowing,
    col, table,
)


class EquivalenceMixin:
    def assertEquivalent(self, engine, query, expect_rows=None):
        report = engine.assert_equivalent(query)
        self.assertTrue(report['equivalent'])
        if expect_rows is not None:
            self.assertEqual(report['row_count'], expect_rows)
        return report


class DuplicateOuterTests(unittest.TestCase, EquivalenceMixin):
    def setUp(self):
        self.tables = make_tables(seed=1, n_events=10)
        # 人为制造业务键完全相同但 row_id 独立的外行
        dup = dict(self.tables['requests'][0])
        dup['id'] = 'r-dup'
        self.tables['requests'].append(dup)
        self.engine = Engine(CATALOG, self.tables)

    def test_duplicate_rows_preserved_as_bag(self):
        for query in (q_scalar_count(), q_not_in_projection(), q_exists_fail()):
            report = self.assertEquivalent(self.engine, query)
            base = self.engine.run_baseline(query)
            # 账本行数：去关联不得增删外行（WHERE 过滤后基准/物理行数一致即可）
            deco = self.engine.run_decorrelated(query)
            self.assertEqual(len(base.rows), len(deco.rows))
        # 重复业务键产出重复结果（投影查询不过滤外行，保留全部输入行）
        rows = self.engine.run_decorrelated(q_scalar_count()).rows
        self.assertEqual(len(rows), len(self.tables['requests']))
        ids = [row['id'] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))  # row_id 独立
        first = rows[0]
        dup = [r for r in rows if r['tenant'] == first['tenant']
               and r['tokens'] == first['tokens']]
        self.assertGreaterEqual(len(dup), 2)
        self.assertEqual([r['settlement_count'] for r in dup],
                         [r['settlement_count'] for r in dup])

    def test_row_count_never_changes(self):
        query = q_not_exists_settlement()
        report = self.assertEquivalent(self.engine, query)
        base = self.engine.run_baseline(query)
        deco = self.engine.run_decorrelated(query)
        self.assertEqual(len(base.rows), len(deco.rows))


class EmptyInnerTests(unittest.TestCase, EquivalenceMixin):
    def setUp(self):
        self.tables = make_tables(seed=2)
        self.tables['settlements'] = []
        self.engine = Engine(CATALOG, self.tables)

    def test_empty_aggregates(self):
        count_rows = self.engine.run_decorrelated(q_scalar_count()).rows
        self.assertTrue(all(r['settlement_count'] == 0 for r in count_rows))
        for func in ('sum', 'min'):
            rows = self.engine.run_decorrelated(q_scalar_sum_min(func)).rows
            self.assertTrue(all(r[f'{func}_amount'] is None for r in rows))
        # 与基准袋等价
        self.assertEquivalent(self.engine, q_scalar_count())
        self.assertEquivalent(self.engine, q_scalar_sum_min('sum'))
        self.assertEquivalent(self.engine, q_scalar_sum_min('min'))

    def test_empty_in_exists_family(self):
        in_rows = self.engine.run_decorrelated(q_in_projection()).rows
        non_null = [r for r in in_rows if r['tenant'] is not None]
        null_tenant = [r for r in in_rows if r['tenant'] is None]
        self.assertTrue(all(r['has_match'] is False for r in non_null))
        self.assertTrue(all(r['has_match'] is None for r in null_tenant))
        not_in_rows = self.engine.run_decorrelated(q_not_in_projection()).rows
        # 空候选集：NOT IN 对非 NULL 值为 TRUE（没有 NULL 候选，不产生 UNKNOWN）
        non_null = [r for r in not_in_rows if r['tenant'] is not None]
        null_tenant = [r for r in not_in_rows if r['tenant'] is None]
        self.assertTrue(all(r['no_match'] is True for r in non_null))
        self.assertTrue(all(r['no_match'] is None for r in null_tenant))
        exists_rows = self.engine.run_decorrelated(q_not_exists_settlement()).rows
        self.assertEqual(len(exists_rows), len(self.tables['requests']))
        self.assertEquivalent(self.engine, q_in_projection())
        self.assertEquivalent(self.engine, q_not_in_projection())
        self.assertEquivalent(self.engine, q_not_exists_settlement())

    def test_empty_bare_scalar_is_null(self):
        rows = self.engine.run_decorrelated(q_bare_scalar()).rows
        self.assertTrue(all(r['one_amount'] is None for r in rows))
        self.assertEquivalent(self.engine, q_bare_scalar())


class NullInnerTests(unittest.TestCase, EquivalenceMixin):
    def setUp(self):
        self.tables = make_tables(seed=3)
        # 构造：租户 D 只在结算表出现 NULL 候选场景；C 在结算表完全缺席
        self.tables['settlements'] = [
            {'id': 's1', 'tenant': 'A', 'amount': 10},
            {'id': 's2', 'tenant': None, 'amount': 5},
            {'id': 's3', 'tenant': None, 'amount': 7},
        ]
        self.tables['requests'] = [
            {'id': 'r1', 'tenant': 'A', 'tokens': 1},
            {'id': 'r2', 'tenant': 'C', 'tokens': 2},  # 无真匹配，候选含 NULL
            {'id': 'r3', 'tenant': None, 'tokens': 3},
        ]
        self.engine = Engine(CATALOG, self.tables)

    def test_in_unknown_when_null_candidate(self):
        rows = {r['id']: r for r in
                self.engine.run_decorrelated(q_in_projection()).rows}
        self.assertIs(rows['r1']['has_match'], True)
        self.assertIsNone(rows['r2']['has_match'])     # UNKNOWN 必须保留
        self.assertIsNone(rows['r3']['has_match'])     # NULL 输入恒 UNKNOWN
        self.assertEquivalent(self.engine, q_in_projection())

    def test_not_in_never_anti_join(self):
        rows = {r['id']: r for r in
                self.engine.run_decorrelated(q_not_in_projection()).rows}
        self.assertIs(rows['r1']['no_match'], False)
        self.assertIsNone(rows['r2']['no_match'])      # NOT(UNKNOWN)=UNKNOWN
        self.assertIsNone(rows['r3']['no_match'])
        # 若错误化简成 anti join，r2 会被当成 TRUE；这里必须不是
        self.assertIsNot(rows['r2']['no_match'], True)
        self.assertEquivalent(self.engine, q_not_in_projection())

    def test_null_key_does_not_match_null_key(self):
        # 两个 NULL 租户行不能互相匹配：r3 的 COUNT 必须为 0
        rows = {r['id']: r for r in
                self.engine.run_decorrelated(q_scalar_count()).rows}
        self.assertEqual(rows['r3']['settlement_count'], 0)
        self.assertEqual(rows['r1']['settlement_count'], 1)
        self.assertEqual(rows['r2']['settlement_count'], 0)
        self.assertEquivalent(self.engine, q_scalar_count())

    def test_where_drops_unknown_but_projection_keeps_it(self):
        # WHERE 中的 IN 产生 UNKNOWN 时该行被剔除
        q_where = Query(
            from_=table('requests', 'r'),
            where=InSub(col('r', 'tenant'), Query(
                from_=table('settlements', 's'),
                select=(ProjExpr(col('s', 'tenant'), 'cand'),),
            )),
            select=(ProjStar(),),
        )
        where_rows = self.engine.run_decorrelated(q_where).rows
        self.assertEqual([r['id'] for r in where_rows], ['r1'])  # UNKNOWN 被剔除
        proj_rows = self.engine.run_decorrelated(q_in_projection()).rows
        self.assertEqual(len(proj_rows), 3)                       # 投影全部保留
        self.assertEquivalent(self.engine, q_where)

    def test_outer_null_field_vs_missing_subquery_row(self):
        # 外层自身 NULL 字段：r3.tenant 本身为 None；
        # 子查询无返回行：裸标量对 C 给 None。两者输出都是 None 但来源不同，
        # 基准与物理必须给出同样的袋。
        rows = {r['id']: r for r in
                self.engine.run_decorrelated(q_bare_scalar()).rows}
        self.assertIsNone(rows['r2']['one_amount'])  # 无匹配组 -> NULL
        self.assertIsNone(rows['r3']['one_amount'])  # 空键不匹配 -> NULL
        self.assertEquivalent(self.engine, q_bare_scalar())


class ScalarCardinalityTests(unittest.TestCase, EquivalenceMixin):
    def setUp(self):
        self.tables = make_tables(seed=4)
        self.tables['settlements'] = [
            {'id': 's1', 'tenant': 'A', 'amount': 10},
            {'id': 's2', 'tenant': 'A', 'amount': 20},
            {'id': 's3', 'tenant': 'B', 'amount': 30},
        ]
        self.tables['requests'] = [
            {'id': 'r1', 'tenant': 'A', 'tokens': 1},
            {'id': 'r2', 'tenant': 'B', 'tokens': 2},
            {'id': 'r3', 'tenant': 'X', 'tokens': 3},
        ]
        self.engine = Engine(CATALOG, self.tables)

    def test_multi_row_scalar_raises_in_both_paths(self):
        base = self.engine.run_baseline(q_bare_scalar())
        deco = self.engine.run_decorrelated(q_bare_scalar())
        self.assertEqual(base.error_category, 'CardinalityError')
        self.assertEqual(deco.error_category, 'CardinalityError')
        # 等价对照同样识别为“错误类别一致”而非结果袋比较
        report = self.engine.assert_equivalent(q_bare_scalar())
        self.assertEqual(report['error_category'], 'CardinalityError')

    def test_single_row_value_and_zero_row_null(self):
        # 去掉 A 的重复行后单值可取
        self.tables['settlements'] = [
            {'id': 's3', 'tenant': 'B', 'amount': 30},
        ]
        rows = {r['id']: r for r in
                self.engine.run_decorrelated(q_bare_scalar()).rows}
        self.assertEqual(rows['r2']['one_amount'], 30)
        self.assertIsNone(rows['r3']['one_amount'])
        self.assertEquivalent(self.engine, q_bare_scalar())


class ShadowingTests(unittest.TestCase, EquivalenceMixin):
    def setUp(self):
        self.tables = make_tables(seed=5, n_events=10)
        self.engine = Engine(CATALOG, self.tables)

    def test_two_level_same_alias_resolution(self):
        from subquery_data.rel.ast import walk_expr
        query = q_two_level_shadowing()
        report = self.assertEquivalent(self.engine, query)
        bound, _, _ = self.engine.prepare(query)

        all_refs = []
        def walk_block(block):
            if block.where:
                for node in walk_expr(block.where):
                    if isinstance(node, Col):
                        all_refs.append(node.ref)
            for item in block.select:
                if isinstance(item, ProjExpr):
                    for node in walk_expr(item.expr):
                        if isinstance(node, Col):
                            all_refs.append(node.ref)
                        if isinstance(node, (Exists, InSub, Scalar)):
                            walk_block(node.sub)
                elif isinstance(item, ProjAgg) and item.agg.expr is not None:
                    for node in walk_expr(item.agg.expr):
                        if isinstance(node, Col):
                            all_refs.append(node.ref)
            if isinstance(block.from_, Query):
                walk_block(block.from_)
        walk_block(bound)
        bids = {ref[0] for ref in all_refs}
        # 三个查询块（requests/settlements/events）的列都被引用
        self.assertGreaterEqual(len(bids), 2)
        # 同一别名 t 在不同块上的 ref 块编号不同，证明遮蔽解析正确
        t_refs = [ref for ref in all_refs if ref[1] == 't']
        self.assertGreaterEqual(len({ref[0] for ref in t_refs}), 2)

    def test_shadowed_outer_name_not_captured(self):
        # 内外层同别名 t：内层默认 t.tenant 绑定内层帧；
        # 要关联外层同名别名必须显式 level=1，绑定后两侧 ref 分属不同块。
        q = Query(
            from_=Table('requests', 't'),
            select=(ProjStar(), ProjExpr(Exists(Query(
                from_=Table('settlements', 't'),
                where=Bin('=', col('t', 'tenant'), col('t', 'tenant', level=1)),
                select=(ProjExpr(Lit(1), 'one'),),
            )), 'has')),
        )
        bound, _, _ = self.engine.prepare(q)
        exists_node = None
        from subquery_data.rel.ast import walk_expr
        for node in walk_expr(bound.select[1].expr):
            if isinstance(node, Exists):
                exists_node = node
        inner_bid = exists_node.sub.bid
        outer_bid = bound.bid
        eq = exists_node.sub.where
        side_bids = {eq.left.ref[0], eq.right.ref[0]}
        self.assertEqual(side_bids, {inner_bid, outer_bid})
        self.assertEquivalent(self.engine, q)

    def test_shadowed_default_stays_inner_and_level_out_of_range_rejected(self):
        # 无 level 时内层同名列绝不外逃：只有一层同名作用域却请求 level=1 -> 拒绝
        q_bad = Query(
            from_=Table('requests', 't'),
            select=(ProjExpr(col('t', 'tenant', level=1), 'outer_only'),),
        )
        with self.assertRaises(ScopeError):
            self.engine.prepare(q_bad)


class ScanReductionTests(unittest.TestCase, EquivalenceMixin):
    def setUp(self):
        self.tables = make_tables(seed=6, n_requests=50, n_settlements=30,
                                  n_events=40)
        self.engine = Engine(CATALOG, self.tables)

    def test_inner_scanned_once_not_per_outer(self):
        # (查询, 内表名)：每个查询的子查询 FROM 表才是被逐外行重扫的对象
        cases = (
            (q_scalar_count(), 'settlements'),
            (q_not_in_projection(), 'settlements'),
            (q_scalar_sum_min('sum'), 'settlements'),
            (q_exists_fail(), 'events'),
        )
        for query, inner in cases:
            base = self.engine.run_baseline(query)
            deco = self.engine.run_decorrelated(query)
            surviving = len(base.rows)
            # 基准：每个存活外行都触发一次内表全扫
            self.assertGreaterEqual(
                base.stats['table_scans'].get(inner, 0), surviving,
            )
            # 物理：内表在整个计划中只扫描一次
            self.assertEqual(deco.stats['table_scans'].get(inner, 0), 1)
            for build in deco.stats['builds']:
                self.assertGreaterEqual(build['rows_built'], 0)
            # 关键对照：基准扫描的内表行数随外行数增长，物理恒定为一次
            self.assertGreater(
                base.stats['rows_scanned'].get(inner, 0),
                deco.stats['rows_scanned'].get(inner, 0),
            )

    def test_build_keys_and_stats_reported(self):
        deco = self.engine.run_decorrelated(q_scalar_count())
        build = deco.stats['builds'][0]
        self.assertEqual(build['build_keys'], [(build['build_keys'][0][0],
                                                's', 'tenant')])
        self.assertIn('buckets', build)
        self.assertIn('node_id', build)

    def test_empty_outer_skips_build(self):
        # 外层过滤后零行：惰性构建不扫描内表
        q = Query(
            from_=table('requests', 'r'),
            where=Bin('=', col('r', 'tenant'), Lit('__nonexistent__')),
            select=(ProjStar(), ProjExpr(Scalar(Query(
                from_=table('settlements', 's'),
                where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                select=(ProjAgg(Agg('count'), 'c'),),
            )), 'n')),
        )
        deco = self.engine.run_decorrelated(q)
        self.assertEqual(deco.rows, [])
        self.assertEqual(deco.stats['table_scans'].get('settlements', 0), 0)
        base = self.engine.run_baseline(q)
        self.assertEqual(base.stats['table_scans'].get('settlements', 0), 0)


class CacheTests(unittest.TestCase, EquivalenceMixin):
    def setUp(self):
        self.tables = make_tables(seed=7)
        self.engine = Engine(CATALOG, self.tables)

    def test_repeat_execution_uses_cache(self):
        q = q_scalar_count()
        first = self.engine.run_decorrelated(q)
        second = self.engine.run_decorrelated(q)
        self.assertEqual(first.stats['cache_misses'], [3])
        self.assertEqual(second.stats['cache_hits'], [3])
        # 命中缓存后内表不再扫描
        self.assertNotIn('settlements', second.stats['table_scans'])
        self.assertEqual(first.rows, second.rows)

    def test_param_change_invalidates_cache(self):
        # 确定性数据：amount=10 的结算记录租户 A，与 requests 中 A 租户形成真匹配
        self.tables['settlements'] = [
            {'id': 's1', 'tenant': 'A', 'amount': 10},
            {'id': 's2', 'tenant': 'B', 'amount': 50},
        ]
        self.tables['requests'] = [
            {'id': 'r1', 'tenant': 'A', 'tokens': 1},
            {'id': 'r2', 'tenant': 'C', 'tokens': 2},
        ]
        q = q_uncorrelated_in()
        engine_a = Engine(CATALOG, self.tables, params={'floor': 10})
        first = engine_a.run_decorrelated(q)
        engine_b = Engine(CATALOG, self.tables, params={'floor': 999})
        second = engine_b.run_decorrelated(q)
        # 参数指纹不同 -> 不是缓存命中
        self.assertEqual(second.stats['cache_hits'], [])
        # 低门槛有真匹配（r1=TRUE），高门槛无任何匹配
        self.assertTrue(any(r['param_match'] is True for r in first.rows))
        self.assertFalse(any(r['param_match'] is True for r in second.rows))

    def test_snapshot_change_invalidates_cache(self):
        q = q_scalar_count()
        first = self.engine.run_decorrelated(q)
        # 快照变化：换入新行列表（新 Engine 携带新快照令牌）
        new_tables = copy.deepcopy(self.tables)
        new_tables['settlements'].append(
            {'id': 's-new', 'tenant': 'A', 'amount': 99})
        engine2 = Engine(CATALOG, new_tables)
        second = engine2.run_decorrelated(q)
        self.assertEqual(second.stats['cache_hits'], [])
        a_before = [r['settlement_count'] for r in first.rows
                    if r['tenant'] == 'A']
        a_after = [r['settlement_count'] for r in second.rows
                   if r['tenant'] == 'A']
        self.assertEqual(max(a_after), max(a_before) + 1)

    def test_mutated_same_identity_detected_or_isolated(self):
        # 原地修改表后新建 Engine：快照令牌含行数/末行身份，必然不同
        q = q_scalar_count()
        token_before = self.engine.snapshot_token
        self.tables['settlements'].pop()
        engine2 = Engine(CATALOG, self.tables)
        self.assertNotEqual(token_before, engine2.snapshot_token)
        report = engine2.assert_equivalent(q)
        self.assertTrue(report['equivalent'])


class UnsupportedStructureTests(unittest.TestCase):
    def setUp(self):
        self.tables = make_tables(seed=8)
        self.engine = Engine(CATALOG, self.tables)

    def test_non_equi_correlation_rejected(self):
        with self.assertRaises(UnsupportedError):
            self.engine.prepare(q_unsupported_non_equi())

    def test_or_correlation_rejected(self):
        q = Query(
            from_=table('requests', 'r'),
            where=Exists(Query(
                from_=table('settlements', 's'),
                where=Or((
                    Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                    Bin('=', col('s', 'amount'), col('r', 'tokens')),
                )),
                select=(ProjExpr(Lit(1), 'one'),),
            )),
            select=(ProjStar(),),
        )
        with self.assertRaises(UnsupportedError):
            self.engine.prepare(q)

    def test_skip_level_correlation_rejected(self):
        # 派生表内引用最外层列：绑定器 ScopeError
        q = Query(
            from_=table('requests', 'r'),
            select=(ProjStar(), ProjExpr(Scalar(Query(
                from_=Query(
                    from_=table('settlements', 's'),
                    where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                    select=(ProjExpr(col('s', 'amount'), 'amt'),),
                    sub_alias='d',
                ),
                select=(ProjAgg(Agg('sum', col('d', 'amt')), 'v'),),
            )), 'v')),
        )
        with self.assertRaises(ScopeError):
            self.engine.prepare(q)

    def test_user_alias_internal_prefix_rejected(self):
        q = Query(
            from_=table('requests', 'r'),
            select=(ProjExpr(col('r', 'tenant'), '$secret'),),
        )
        with self.assertRaises(UnsupportedError):
            self.engine.prepare(q)

    def test_reject_and_explicit_fallback_modes(self):
        q = q_unsupported_non_equi()
        # 默认拒绝：错误结果带明确类别，不产生近似行
        rejected = self.engine.run(q, mode='reject')
        self.assertEqual(rejected.error_category, 'UnsupportedError')
        self.assertEqual(rejected.rows, [])
        # 显式回退：走基准解释器，结果中标注路由与原因
        fallback = self.engine.run(q, mode='fallback')
        self.assertEqual(fallback.route, 'baseline-fallback')
        self.assertIn('等值', fallback.fallback_reason)
        self.assertIsNone(fallback.error)
        self.assertEqual(
            [r['id'] for r in fallback.rows],
            [r['id'] for r in self.engine.run_baseline(q).rows],
        )


class AggregateNullSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tables = {
            'requests': [{'id': 'r1', 'tenant': 'A', 'tokens': 1}],
            'settlements': [
                {'id': 's1', 'tenant': 'A', 'amount': None},
                {'id': 's2', 'tenant': 'A', 'amount': 10},
                {'id': 's3', 'tenant': 'A', 'amount': None},
            ],
        }
        self.engine = Engine(CATALOG, self.tables)

    def _scalar_agg(self, agg, alias):
        return Query(
            from_=table('requests', 'r'),
            select=(ProjExpr(Scalar(Query(
                from_=table('settlements', 's'),
                where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                select=(ProjAgg(agg, 'v'),),
            )), alias),),
        )

    def test_count_star_vs_count_column_and_sum_min_null(self):
        cases = (
            (Agg('count'), 'n', 3),
            (Agg('count', col('s', 'amount')), 'n', 1),
            (Agg('sum', col('s', 'amount')), 'v', 10),
            (Agg('min', col('s', 'amount')), 'v', 10),
        )
        for agg, alias, expected in cases:
            query = self._scalar_agg(agg, alias)
            self.assertTrue(self.engine.assert_equivalent(query)['equivalent'])
            self.assertEqual(
                self.engine.run_decorrelated(query).rows[0][alias], expected)

    def test_projection_exists_is_never_unknown_and_internal_cols_hidden(self):
        q = Query(
            from_=table('requests', 'r'),
            select=(ProjStar(), ProjExpr(Exists(Query(
                from_=table('settlements', 's'),
                where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                select=(ProjExpr(Lit(1), 'one'),),
            )), 'has_settle')),
        )
        rows = self.engine.run_decorrelated(q).rows
        self.assertTrue(all(isinstance(r['has_settle'], bool) for r in rows))
        self.assertTrue(all('$' not in key for r in rows for key in r))
        self.assertTrue(self.engine.assert_equivalent(q)['equivalent'])


class LocalFilterAndTopLevelTests(unittest.TestCase, EquivalenceMixin):
    def setUp(self):
        self.tables = make_tables(seed=9, n_events=12)
        self.engine = Engine(CATALOG, self.tables)

    def test_local_filter_pushed_to_build_side(self):
        # 用确定性数据保证局部过滤后构建侧仍有行，且行数严格少于内表
        self.tables['settlements'] = [
            {'id': 's1', 'tenant': 'A', 'amount': 10},
            {'id': 's2', 'tenant': 'A', 'amount': 2},
            {'id': 's3', 'tenant': 'B', 'amount': 20},
        ]
        engine = Engine(CATALOG, self.tables)
        q = Query(
            from_=table('requests', 'r'),
            select=(ProjStar(), ProjExpr(Scalar(Query(
                from_=table('settlements', 's'),
                where=And((
                    Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                    Bin('>', col('s', 'amount'), Lit(5)),
                )),
                select=(ProjAgg(Agg('count'), 'c'),),
            )), 'big_count')),
        )
        # 先各自独立执行一次（避免等价对照内部的缓存命中掩盖构建统计）
        base = engine.run_baseline(q)
        deco = engine.run_decorrelated(q)
        # 两条路径结果袋一致
        self.assertEqual(
            sorted(tuple(sorted(r.items())) for r in base.rows),
            sorted(tuple(sorted(r.items())) for r in deco.rows),
        )
        self.assertTrue(deco.stats['builds'])
        build = deco.stats['builds'][0]
        self.assertLess(build['rows_built'], len(self.tables['settlements']))
        # 基准逐行扫描看到的是完整内表（每个外行全扫一次再过滤）
        self.assertEqual(
            base.stats['rows_scanned']['settlements'],
            len(self.tables['settlements']) * len(self.tables['requests']),
        )
        # 物理只扫一次内表，扫描行数等于内表总行数（过滤在扫描后）
        self.assertEqual(deco.stats['rows_scanned']['settlements'],
                         len(self.tables['settlements']))

    def test_top_level_global_agg(self):
        q = Query(
            from_=table('requests', 'r'),
            where=Bin('>', col('r', 'tokens'), Lit(0)),
            select=(ProjAgg(Agg('sum', col('r', 'tokens')), 'total'),),
        )
        report = self.assertEquivalent(self.engine, q)
        self.assertEqual(report['row_count'], 1)

    def test_exists_inside_or_uses_mark(self):
        q = Query(
            from_=table('requests', 'r'),
            where=Or((
                Bin('=', col('r', 'tenant'), Lit('C')),
                Exists(Query(
                    from_=table('settlements', 's'),
                    where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                    select=(ProjExpr(Lit(1), 'one'),),
                )),
            )),
            select=(ProjStar(),),
        )
        self.assertEquivalent(self.engine, q)
        plan_text = self.engine.explain(q)
        self.assertIn('PxHashMarkJoin', plan_text)


if __name__ == '__main__':
    unittest.main()
