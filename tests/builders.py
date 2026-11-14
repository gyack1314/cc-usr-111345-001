"""测试用受限 AST 构造辅助与数据生成。"""
import random

from subquery_data.rel.ast import (
    Table, Query, Col, Lit, Param, Bin, And, Or, Not, IsNull,
    Exists, InSub, Scalar, Agg, ProjStar, ProjExpr, ProjAgg,
)

CATALOG = {
    'requests': ['id', 'tenant', 'tokens'],
    'settlements': ['id', 'tenant', 'amount'],
    'events': ['id', 'tenant', 'kind'],
}


def table(name, alias=None):
    return Table(name, alias or name[0])


def col(alias, column, level=0):
    return Col(alias, column, level=level)


def make_tables(seed, n_requests=12, n_settlements=9, n_events=0):
    """生成带重复键与 NULL 键的确定性快照。"""
    rng = random.Random(seed)
    tenants = ['A', 'A', 'B', None, 'C', 'B', None, 'A']
    requests = []
    for i in range(n_requests):
        tenant = tenants[i % len(tenants)]
        if rng.random() < 0.12:
            tenant = None
        requests.append({
            'id': f'r{i}',
            'tenant': tenant,
            'tokens': rng.randint(1, 9),
        })
    settlements = []
    for i in range(n_settlements):
        tenant = tenants[(i * 3) % len(tenants)]
        if rng.random() < 0.18:
            tenant = None
        settlements.append({
            'id': f's{i}',
            'tenant': tenant,
            'amount': rng.randint(1, 20),
        })
    events = []
    kinds = ['fail', 'ok', 'fail']
    for i in range(n_events):
        events.append({
            'id': f'e{i}',
            'tenant': tenants[i % len(tenants)],
            'kind': kinds[i % len(kinds)],
        })
    tables = {'requests': requests, 'settlements': settlements}
    if n_events:
        tables['events'] = events
    return tables


# -- 业务查询构造（对应“是否有失败请求/有没有匹配结算记录”） -----------------

def q_exists_fail():
    r = table('requests', 'r')
    e = table('events', 'e')
    return Query(
        from_=r,
        where=Exists(Query(
            from_=e,
            where=And((
                Bin('=', col('e', 'tenant'), col('r', 'tenant')),
                Bin('=', col('e', 'kind'), Lit('fail')),
            )),
            select=(ProjExpr(Lit(1), 'one'),),
        )),
        select=(ProjStar(),),
    )


def q_not_exists_settlement():
    r = table('requests', 'r')
    s = table('settlements', 's')
    return Query(
        from_=r,
        where=Not(Exists(Query(
            from_=s,
            where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
            select=(ProjExpr(Lit(1), 'one'),),
        ))),
        select=(ProjStar(),),
    )


def q_in_projection():
    r = table('requests', 'r')
    s = table('settlements', 's')
    return Query(
        from_=r,
        select=(
            ProjStar(),
            ProjExpr(
                InSub(col('r', 'tenant'), Query(
                    from_=s,
                    select=(ProjExpr(col('s', 'tenant'), 'cand'),),
                )),
                'has_match'),
        ),
    )


def q_not_in_projection():
    r = table('requests', 'r')
    s = table('settlements', 's')
    return Query(
        from_=r,
        select=(
            ProjStar(),
            ProjExpr(
                InSub(col('r', 'tenant'), Query(
                    from_=s,
                    select=(ProjExpr(col('s', 'tenant'), 'cand'),),
                ), negated=True),
                'no_match'),
        ),
    )


def q_scalar_count():
    r = table('requests', 'r')
    s = table('settlements', 's')
    return Query(
        from_=r,
        select=(
            ProjStar(),
            ProjExpr(Scalar(Query(
                from_=s,
                where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                select=(ProjAgg(Agg('count'), 'cnt'),),
            )), 'settlement_count'),
        ),
    )


def q_scalar_sum_min(func):
    r = table('requests', 'r')
    s = table('settlements', 's')
    return Query(
        from_=r,
        select=(
            ProjStar(),
            ProjExpr(Scalar(Query(
                from_=s,
                where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                select=(ProjAgg(Agg(func, col('s', 'amount')), 'v'),),
            )), f'{func}_amount'),
        ),
    )


def q_bare_scalar():
    r = table('requests', 'r')
    s = table('settlements', 's')
    return Query(
        from_=r,
        select=(
            ProjStar(),
            ProjExpr(Scalar(Query(
                from_=s,
                where=Bin('=', col('s', 'tenant'), col('r', 'tenant')),
                select=(ProjExpr(col('s', 'amount'), 'v'),),
                limit=2,
            )), 'one_amount'),
        ),
    )


def q_uncorrelated_in():
    r = table('requests', 'r')
    s = table('settlements', 's')
    return Query(
        from_=r,
        select=(
            ProjStar(),
            ProjExpr(
                InSub(col('r', 'tenant'), Query(
                    from_=s,
                    where=Bin('=', col('s', 'amount'), Param('floor')),
                    select=(ProjExpr(col('s', 'tenant'), 'cand'),),
                )),
                'param_match'),
        ),
    )


def q_unsupported_non_equi():
    r = table('requests', 'r')
    s = table('settlements', 's')
    return Query(
        from_=r,
        where=Exists(Query(
            from_=s,
            where=Bin('>', col('s', 'amount'), col('r', 'tokens')),
            select=(ProjExpr(Lit(1), 'one'),),
        )),
        select=(ProjStar(),),
    )


def q_two_level_shadowing():
    """两层别名遮蔽：三层都叫 t，内层默认引用命中最近作用域，
    跨层引用必须用显式 level，绑定后同名列的 ref 落在各自块上。"""
    t1 = Table('requests', 't')
    t2 = Table('settlements', 't')
    t3 = Table('events', 't')
    # 最内层 events 关联中层 settlements：同别名 t，外层侧显式 level=1
    inner_exists = Exists(Query(
        from_=t3,
        where=And((
            Bin('=', col('t', 'tenant'), col('t', 'tenant', level=1)),
            Bin('=', col('t', 'kind'), Lit('fail')),
        )),
        select=(ProjExpr(Lit(1), 'one'),),
    ))
    # 中层 settlements 关联外层 requests：同别名 t，外层侧显式 level=1
    middle_where = And((
        Bin('=', col('t', 'tenant'), col('t', 'tenant', level=1)),
        inner_exists,
    ))
    return Query(
        from_=t1,
        select=(
            ProjStar(),
            ProjExpr(Scalar(Query(
                from_=t2,
                where=middle_where,
                select=(ProjAgg(Agg('sum', col('t', 'amount')), 'v'),),
            )), 'nested_sum'),
        ),
    )
