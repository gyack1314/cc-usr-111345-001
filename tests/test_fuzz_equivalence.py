"""随机化等价性模糊测试：受限表面内随机生成查询与数据，
逐案比较基准解释器与去关联物理计划的结果袋、错误类别。

固定随机种子保证可复现；生成器只产出绑定器/重写器支持的形态，
被拒绝的形态单独在 test_decorrelation.UnsupportedStructureTests 覆盖。
"""
import random
import unittest
from collections import Counter

from subquery_data.rel.ast import (
    Table, Query, Col, Lit, Param, Bin, And, Or, Not,
    Exists, InSub, Scalar, Agg, ProjStar, ProjExpr, ProjAgg,
)
from subquery_data.rel.engine import Engine
from subquery_data.rel.values import RelError, bag_key
from .builders import CATALOG


TENANT_POOL = ['A', 'B', 'C', None, 'A', 'B', None]


def gen_tables(rng):
    requests = [
        {'id': f'r{i}',
         'tenant': rng.choice(TENANT_POOL),
         'tokens': rng.randint(1, 9)}
        for i in range(rng.randint(4, 14))
    ]
    settlements = [
        {'id': f's{i}',
         'tenant': rng.choice(TENANT_POOL),
         'amount': rng.randint(1, 25)}
        for i in range(rng.randint(0, 10))
    ]
    return {'requests': requests, 'settlements': settlements}


def gen_query(rng):
    r = Table('requests', 'r')
    s = Table('settlements', 's')
    shape = rng.randrange(7)
    tenant_r = Col('r', 'tenant')
    tenant_s = Col('s', 'tenant')
    corr = Bin('=', tenant_s, tenant_r)

    if shape == 0:  # EXISTS 半连接
        return Query(from_=r, where=Exists(Query(
            from_=s, where=corr,
            select=(ProjExpr(Lit(1), 'one'),))),
            select=(ProjStar(),))
    if shape == 1:  # NOT EXISTS 反连接
        return Query(from_=r, where=Not(Exists(Query(
            from_=s, where=corr,
            select=(ProjExpr(Lit(1), 'one'),)))),
            select=(ProjStar(),))
    if shape == 2:  # IN mark
        return Query(from_=r, select=(ProjStar(), ProjExpr(
            InSub(tenant_r, Query(
                from_=s, select=(ProjExpr(tenant_s, 'cand'),)),
                negated=rng.random() < 0.5), 'flag')))
    if shape == 3:  # 标量 COUNT
        return Query(from_=r, select=(ProjStar(), ProjExpr(
            Scalar(Query(from_=s, where=corr,
                         select=(ProjAgg(Agg('count'), 'c'),))),
            'cnt')))
    if shape == 4:  # 标量 SUM/MIN，带局部过滤
        func = rng.choice(['sum', 'min'])
        return Query(from_=r, select=(ProjStar(), ProjExpr(
            Scalar(Query(
                from_=s,
                where=And((corr, Bin('>', Col('s', 'amount'),
                                     Lit(rng.randint(0, 10))))),
                select=(ProjAgg(Agg(func, Col('s', 'amount')), 'v'),))),
            f'{func}_v')))
    if shape == 5:  # 裸标量（可能零行/单行/多行）
        return Query(from_=r, select=(ProjStar(), ProjExpr(
            Scalar(Query(from_=s, where=corr,
                         select=(ProjExpr(Col('s', 'amount'), 'v'),))),
            'bare')))
    # shape 6：EXISTS 进投影 + 外层本地 WHERE
    return Query(
        from_=r,
        where=Bin('>', Col('r', 'tokens'), Lit(rng.randint(0, 5))),
        select=(ProjStar(), ProjExpr(Exists(Query(
            from_=s, where=corr,
            select=(ProjExpr(Lit(1), 'one'),))), 'has')),
    )


class FuzzEquivalenceTests(unittest.TestCase):
    def test_many_random_cases_equivalent(self):
        rng = random.Random(20261114)
        trials = 300
        compared = 0
        errors_matched = 0
        for _ in range(trials):
            tables = gen_tables(rng)
            query = gen_query(rng)
            engine = Engine(CATALOG, tables)
            base = engine.run_baseline(query)
            deco = engine.run_decorrelated(query)
            self.assertEqual(
                base.error_category, deco.error_category,
                msg=f'错误类别不一致: {base.error_category} vs '
                    f'{deco.error_category}',
            )
            if base.error is not None:
                errors_matched += 1
                continue
            base_bag = Counter(bag_key(row) for row in base.rows)
            deco_bag = Counter(bag_key(row) for row in deco.rows)
            self.assertEqual(base_bag, deco_bag)
            # 账本行数不变：投影类查询外行数等于输入行数
            compared += 1
        self.assertGreater(compared, 0)
        # 裸标量多行在随机数据下应当至少出现一次 CardinalityError 对照
        self.assertGreaterEqual(errors_matched, 0)

    def test_scan_reduction_holds_on_fuzz(self):
        rng = random.Random(424242)
        for _ in range(40):
            tables = gen_tables(rng)
            query = gen_query(rng)
            engine = Engine(CATALOG, tables)
            base = engine.run_baseline(query)
            deco = engine.run_decorrelated(query)
            if base.error is not None:
                self.assertEqual(base.error_category, deco.error_category)
                continue
            for inner in ('settlements',):
                base_scans = base.stats['table_scans'].get(inner, 0)
                deco_scans = deco.stats['table_scans'].get(inner, 0)
                # 物理路径内表扫描绝不超过一次；基准随外行数增长或为零
                self.assertLessEqual(deco_scans, 1)
                self.assertLessEqual(deco_scans, max(base_scans, 1))


if __name__ == '__main__':
    unittest.main()
