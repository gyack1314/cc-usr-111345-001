"""受限关系去关联的命令行演示。

在登记的业务快照上运行两个分析师习惯查询：
  fails      每个租户“是否有失败请求”（EXISTS 关联）
  settle     每个租户“有没有匹配的结算记录”（NOT IN / 标量 COUNT）
并打印基准路径与去关联路径的结果袋对照、扫描次数、构建键与改写依据。

用法：
  python -m subquery_data.rel.cli data/normal.json
  python -m subquery_data.rel.cli data/boundary.json --case settle
"""
import argparse
import json

from ..io import load, DataError
from .ast import (
    Table, Query, Col, Bin,
    InSub, Scalar, Agg, ProjStar, ProjExpr, ProjAgg,
)
from .engine import Engine
from .values import RelError


# 与 docs/data-contract.md 登记的字段集合一致；空表也能据此确定模式，
# 不依赖“从首行数据推断列”，避免空内表场景下目录缺列。
SCHEMA = {
    'requests': ['id', 'tenant', 'tokens'],
    'settlements': ['id', 'tenant', 'amount'],
    'events': ['id', 'tenant', 'kind'],
    'query_cases': ['id', 'operator', 'outer', 'inner', 'key', 'location'],
}


def build_catalog(tables):
    return {name: list(SCHEMA.get(name,
                (list(rows[0].keys()) if rows else [])))
            for name, rows in tables.items()}


def q_failure_exists():
    r = Table('requests', 'r')
    return Query(
        from_=r,
        select=(
            ProjStar(),
            ProjExpr(
                InSub(Col('r', 'tenant'), _settlement_tenants(),
                      negated=True),
                'tenant_without_settlement'),
            ProjExpr(Scalar(Query(
                from_=Table('settlements', 's'),
                where=Bin('=', Col('s', 'tenant'), Col('r', 'tenant')),
                select=(ProjAgg(Agg('count'), 'c'),),
            )), 'matched_settlement_rows'),
        ),
    )


def _settlement_tenants():
    return Query(
        from_=Table('settlements', 's'),
        select=(ProjExpr(Col('s', 'tenant'), 'cand'),),
    )


def q_settlement_audit():
    return q_failure_exists()


CASES = {
    'settle': q_settlement_audit,
}


def run_case(engine, case):
    query = CASES[case]()
    print(f'== 案例 {case} ==')
    print('-- 物理计划与改写依据 --')
    print(engine.explain(query))
    print()
    report = engine.assert_equivalent(query)
    print('-- 等价对照 --')
    print('结果袋等价: 是   输出账本行数:', report['row_count'])
    print('基准路径表扫描:', report['baseline_stats']['table_scans'])
    print('去关联表扫描  :', report['decorrelated_stats']['table_scans'])
    print('构建端明细    :',
          json.dumps(report['decorrelated_stats']['builds'],
                     ensure_ascii=False))
    print('缓存命中/未命中:',
          report['decorrelated_stats']['cache_hits'],
          report['decorrelated_stats']['cache_misses'])
    print()
    print('-- 去关联结果（UNKNOWN 显示为 null）--')
    for row in engine.run_decorrelated(query).rows:
        print(json.dumps(row, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description='受限关系去关联演示')
    parser.add_argument('manifest')
    parser.add_argument('--case', choices=sorted(CASES), default='settle')
    args = parser.parse_args(argv)
    try:
        tables = load(args.manifest)
        engine = Engine(build_catalog(tables), tables)
        run_case(engine, args.case)
    except (DataError, RelError) as exc:
        parser.exit(2, f'{type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    main()
