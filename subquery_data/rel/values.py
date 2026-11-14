"""SQL风格三值逻辑(3VL)、标量运算与错误类别。

真值用 True / False / None 表示，其中 None 同时承担 UNKNOWN 与 NULL：
布尔语境下的 NULL 就是 UNKNOWN。所有比较与算术对 NULL 遵守 SQL 空值传播，
绝不出现 Python 原生 None 比较那种静默布尔值。
"""

UNKNOWN = None
TRUE = True
FALSE = False

CMP_OPS = ('=', '<>', '<', '<=', '>', '>=')
ARITH_OPS = ('+', '-', '*', '/')


class RelError(ValueError):
    """关系子系统错误基类，类别本身就是对照基准/物理两条路径的依据。"""


class ScopeError(RelError):
    """作用域无法解析（含同名列被内层别名遮蔽后无法外提）。"""


class UnsupportedError(RelError):
    """结构超出受限表面，不能保证等价，重写路径拒绝或显式回退基准。"""


class CardinalityError(RelError):
    """裸标量子查询（无聚合）返回多于一行，语义要求报错而非取第一条。"""


class EvalError(RelError):
    """行级求值错误，如除零、参数未提供。"""


def not3(value):
    """UNKNOWN 取反仍为 UNKNOWN。"""
    if value is None:
        return UNKNOWN
    return not value


def and3(left, right):
    if left is FALSE or right is FALSE:
        return FALSE
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return TRUE


def or3(left, right):
    if left is TRUE or right is TRUE:
        return TRUE
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return FALSE


def compare(op, left, right):
    """任一操作数为 NULL -> UNKNOWN；否则返回确定布尔。"""
    if left is None or right is None:
        return UNKNOWN
    if op == '=':
        return left == right
    if op == '<>':
        return left != right
    if op == '<':
        return left < right
    if op == '<=':
        return left <= right
    if op == '>':
        return left > right
    if op == '>=':
        return left >= right
    raise RelError(f'未知比较运算符: {op}')


def arith(op, left, right):
    """NULL 传播；除零归入 EvalError，保证两条路径错误类别一致。"""
    if left is None or right is None:
        return None
    try:
        if op == '+':
            return left + right
        if op == '-':
            return left - right
        if op == '*':
            return left * right
        if op == '/':
            return left // right if isinstance(left, int) and isinstance(right, int) else left / right
    except ZeroDivisionError as exc:
        raise EvalError('除数为零') from exc
    raise RelError(f'未知算术运算符: {op}')


def eval_scalar(expr, row, params):
    """对不含子查询节点的标量表达式求值。

    基准路径先把 EXISTS/IN/Scalar 节点自行处理后再委托本函数；
    物理计划中所有子查询已被连接算子替代，因此同样只遇到标量节点。
    row 的键是绑定器/重写器决定的不透明列键，本函数不解释其结构。
    """
    kind = type(expr).__name__
    if kind == 'Lit':
        return expr.value
    if kind == 'Param':
        if params is None or expr.name not in params:
            raise EvalError(f'参数未提供: {expr.name}')
        return params[expr.name]
    if kind == 'Col':
        return row[expr.ref]
    if kind == 'Bin':
        left = eval_scalar(expr.left, row, params)
        right = eval_scalar(expr.right, row, params)
        if expr.op in CMP_OPS:
            return compare(expr.op, left, right)
        return arith(expr.op, left, right)
    if kind == 'IsNull':
        value = eval_scalar(expr.expr, row, params)
        result = value is None
        return (not result) if expr.negated else result
    if kind == 'And':
        outcome = TRUE
        for sub in expr.exprs:
            outcome = and3(outcome, eval_scalar(sub, row, params))
        return outcome
    if kind == 'Or':
        outcome = FALSE
        for sub in expr.exprs:
            outcome = or3(outcome, eval_scalar(sub, row, params))
        return outcome
    if kind == 'Not':
        return not3(eval_scalar(expr.expr, row, params))
    raise RelError(f'物理/标量语境出现不支持的表达式节点: {kind}')


def is_true(value):
    """WHERE 只保留 TRUE，FALSE 与 UNKNOWN 都被剔除。"""
    return value is TRUE


def bag_key(row):
    """结果袋比较用的可哈希行表示。"""
    return tuple(sorted(row.items(), key=lambda item: item[0]))
