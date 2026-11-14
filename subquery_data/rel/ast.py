"""受限关系 AST。

设计原则：
- 只接受程序化构造的 AST，本项目不提供任何 SQL 文本解析入口；
- 关联列必须是明确作用域内的 Col（alias 在外层 FROM 中登记），禁止隐式捕获；
- 不出现任何非确定函数（没有 now/random 等节点），同一快照与参数下结果确定；
- 子查询节点只允许出现在 WHERE 谓词与 SELECT 投影项里，WHERE 只保留 TRUE，
  投影必须原样保留 UNKNOWN（绑定后用 None 表示）。

表达式节点：
  Lit/Param/Col/Bin/IsNull/And/Or/Not
  Exists / InSub / Scalar（仅这三类子查询节点）
聚合：
  Agg(func='count'|'sum'|'min', expr=None|Col|Bin...)
  count(*) 用 Agg('count', None)；投影聚合与 Scalar 内部聚合走同一节点。
查询块：
  Query(from_=Table(alias,...), where, select, group_by, limit)
  group_by 仅允许内层聚合子查询使用，且必须恰好是其关联列。
"""
from dataclasses import dataclass
from typing import Any, Optional

from .values import CMP_OPS, ARITH_OPS, RelError, UnsupportedError

AGG_FUNCS = ('count', 'sum', 'min')


@dataclass(frozen=True)
class Lit:
    value: Any  # None 即 SQL NULL；int/str/bool/float 均可，禁止函数调用节点


@dataclass(frozen=True)
class Param:
    name: str  # 参数显式命名；执行时 params 必须提供，否则 EvalError


@dataclass(frozen=True)
class Col:
    alias: str          # FROM 别名，作用域必须明确
    column: str         # 列名
    ref: Any = None     # 绑定器填入 (block_id, alias, column) 不透明键，构造时留空
    # 显式作用域层级：0=最近作用域（默认，内层同名别名遮蔽外层），
    # 1=紧邻外层，2=再外一层。关联列来自明确作用域，禁止靠字符串猜捕获。
    level: int = 0


@dataclass(frozen=True)
class Bin:
    op: str             # 比较或算术运算符，取值见 values.CMP_OPS/ARITH_OPS
    left: Any
    right: Any


@dataclass(frozen=True)
class IsNull:
    expr: Any
    negated: bool = False  # True 表示 IS NOT NULL


@dataclass(frozen=True)
class And:
    exprs: tuple  # 扁平化合取，绑定器校验非空


@dataclass(frozen=True)
class Or:
    exprs: tuple  # 扁平化析取，绑定器校验非空


@dataclass(frozen=True)
class Not:
    expr: Any


@dataclass(frozen=True)
class Exists:
    sub: 'Query'


@dataclass(frozen=True)
class InSub:
    """单列 IN / NOT IN 子查询：value IN (SELECT key_col ...)。"""
    value: Any
    sub: 'Query'
    negated: bool = False


@dataclass(frozen=True)
class Scalar:
    """标量子查询：裸表达式（零行/一行/多行报错）或单聚合（允许关联分组）。"""
    sub: 'Query'


@dataclass(frozen=True)
class Agg:
    func: str
    expr: Any = None  # count(*) 时为 None；其余要求非空


@dataclass(frozen=True)
class ProjExpr:
    expr: Any
    name: str  # 输出列别名，用户命名空间，禁止以 '$' 开头（内部列前缀）


@dataclass(frozen=True)
class ProjStar:
    """展开当前块 FROM 表的全部用户列（顺序按表模式）。"""


@dataclass(frozen=True)
class ProjAgg:
    """顶层无关联聚合（GROUP BY 受限表面不开放，仅整表聚合）。"""
    agg: Agg
    name: str


@dataclass(frozen=True)
class Table:
    name: str
    alias: str
    bid: Optional[int] = None  # 绑定器分配的查询块编号


@dataclass(frozen=True)
class Query:
    from_: Any                     # 受限表面仅支持单表 Table；子查询 FROM 同表或另一查询块
    where: Any = None              # 布尔表达式或 None
    select: tuple = ()             # ProjStar / ProjExpr / ProjAgg
    group_by: tuple = ()           # 仅内层聚合子查询可用，元素必须是内层 Col
    limit: Optional[int] = None    # 可选行数上限，基准与物理语义一致
    bid: Optional[int] = None      # 绑定器分配
    sub_alias: Optional[str] = None  # 作为子查询 FROM 时的别名


_SUBQUERY_NODES = (Exists, InSub, Scalar)
_SCALAR_NODES = (Lit, Param, Col, Bin, IsNull, And, Or, Not) + _SUBQUERY_NODES


def walk_expr(expr):
    """自底向上产出表达式全部节点（不含 Query 内部，子查询节点在节点处停住）。"""
    if expr is None:
        return
    if isinstance(expr, _SUBQUERY_NODES):
        yield expr
        return
    yield expr
    if isinstance(expr, (Lit,)):
        return
    if isinstance(expr, Param):
        return
    if isinstance(expr, Col):
        return
    if isinstance(expr, Bin):
        yield from walk_expr(expr.left)
        yield from walk_expr(expr.right)
        return
    if isinstance(expr, IsNull):
        yield from walk_expr(expr.expr)
        return
    if isinstance(expr, (And, Or)):
        for sub in expr.exprs:
            yield from walk_expr(sub)
        return
    if isinstance(expr, Not):
        yield from walk_expr(expr.expr)
        return
    if isinstance(expr, Agg):
        if expr.expr is not None:
            yield from walk_expr(expr.expr)
        return
    raise RelError(f'未知表达式节点: {type(expr).__name__}')


def contains_subquery(expr):
    if expr is None:
        return False
    return any(isinstance(node, _SUBQUERY_NODES) for node in walk_expr(expr))


def validate_surface(query):
    """构造期静态表面校验（不含作用域，作用域归 binder）。

    任何超出受限表面的结构直接 UnsupportedError，杜绝后续悄悄近似。
    """
    if not isinstance(query, Query):
        raise UnsupportedError('查询根必须是 Query')

    def check_expr(expr, allow_subquery):
        if expr is None:
            return
        for node in walk_expr(expr):
            if isinstance(node, Bin) and node.op not in CMP_OPS + ARITH_OPS:
                raise UnsupportedError(f'不支持的运算符: {node.op}')
            if isinstance(node, _SUBQUERY_NODES) and not allow_subquery:
                raise UnsupportedError('子查询只允许出现在 WHERE 或投影项中')
            if isinstance(node, Exists):
                _validate_block(node.sub)
            elif isinstance(node, InSub):
                _validate_block(node.sub)
                check_expr(node.value, False)
            elif isinstance(node, Scalar):
                _validate_block(node.sub)

    def _validate_block(block):
        if not isinstance(block, Query):
            raise UnsupportedError('子查询 FROM 必须是受限 Query')
        if isinstance(block.from_, Query):
            if not block.from_.sub_alias:
                raise UnsupportedError('子查询 FROM 必须显式登记别名')
            _validate_block(block.from_)
        elif not isinstance(block.from_, Table):
            raise UnsupportedError('FROM 仅支持登记过的表或受限子查询')
        check_expr(block.where, True)
        if block.group_by:
            for key in block.group_by:
                if not isinstance(key, Col):
                    raise UnsupportedError('GROUP BY 仅允许内层列')
        if not block.select:
            raise UnsupportedError('查询块必须显式给出投影')
        for item in block.select:
            if isinstance(item, ProjExpr):
                check_expr(item.expr, True)
                if item.name.startswith('$'):
                    raise UnsupportedError('用户别名禁止以 $ 开头（内部列保留前缀）')
            elif isinstance(item, ProjAgg):
                if item.agg.func not in AGG_FUNCS:
                    raise UnsupportedError(f'仅支持聚合 {AGG_FUNCS}')
                check_expr(item.agg.expr, False)
                if item.name.startswith('$'):
                    raise UnsupportedError('用户别名禁止以 $ 开头（内部列保留前缀）')
            elif not isinstance(item, ProjStar):
                raise UnsupportedError(f'不支持的投影项: {type(item).__name__}')
        if block.limit is not None and (not isinstance(block.limit, int) or block.limit < 0):
            raise UnsupportedError('limit 必须是非负整数')

    _validate_block(query)
    # 顶层若用 ProjAgg，不允许与其它投影混用（整表聚合只产一行）
    if any(isinstance(item, ProjAgg) for item in query.select) and len(query.select) != 1:
        raise UnsupportedError('整表聚合查询只能有一个聚合投影')
    names = [item.name for item in query.select if hasattr(item, 'name')]
    if len(names) != len(set(names)):
        raise UnsupportedError('投影别名重复')
    return query
