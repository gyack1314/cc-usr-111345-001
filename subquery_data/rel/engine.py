"""统一引擎入口：规范化 -> 等价条件检查 -> 物理执行，并与基准路径对照。

Engine 绑定一次输入快照（tables 身份 + 行数 + 末行身份构成快照令牌）。
构建缓存的键是 (节点id, 快照令牌, 参数指纹)：
- 同 Engine 同参数重复执行可命中构建缓存，不再重扫内表；
- 换参数必然换指纹，换快照必然换令牌（或直接换新 Engine），旧结果不可能误用。
"""
from dataclasses import dataclass
from collections import Counter

from .ast import validate_surface
from .binder import Binder
from .baseline import BaselineRunner
from .rewrite import Rewriter
from .physical import ExecContext, ExecStats, BuildCache, run, explain
from .values import RelError, bag_key


def _structure_fingerprint(node):
    """绑定后 AST 的结构指纹：仅取决于查询形状与列绑定，与运行数据无关。"""
    if node is None:
        return ('None',)
    if isinstance(node, (int, str, bool, float)) or node is None:
        return ('lit', repr(node))
    if isinstance(node, (list, tuple)):
        return ('seq', tuple(_structure_fingerprint(item) for item in node))
    attrs = []
    for key in sorted(getattr(node, '__dataclass_fields__', {})):
        attrs.append((key, _structure_fingerprint(getattr(node, key))))
    return (type(node).__name__, tuple(attrs))


@dataclass
class RunResult:
    rows: list
    error: object
    error_category: str
    stats: dict
    plan_text: str
    decisions: tuple
    route: str = ''
    fallback_reason: str = ''


class Engine:
    def __init__(self, catalog, tables, params=None):
        self.catalog = dict(catalog)
        self.tables = tables
        self.params = dict(params or {})
        self.snapshot_token = self._snapshot_token(tables)
        self._cache = BuildCache()
        # 计划身份按查询结构稳定登记：同一查询重复 prepare 得到同一 plan_id，
        # 构建缓存可跨执行复用；不同查询必然不同身份。
        self._plan_registry = {}

    def _plan_id(self, query_fp):
        if query_fp not in self._plan_registry:
            self._plan_registry[query_fp] = len(self._plan_registry)
        return self._plan_registry[query_fp]

    @staticmethod
    def _snapshot_token(tables):
        token = []
        for name in sorted(tables):
            rows = tables[name]
            tail_id = rows[-1].get('id') if rows else None
            token.append((name, len(rows), id(rows), tail_id))
        return tuple(token)

    def prepare(self, query):
        """表面校验 + 作用域绑定 + 去关联重写；不支持的结构在此拒绝。

        计划实例号按查询结构指纹稳定分配：同一查询重复执行拿到同一 plan_id，
        构建缓存可命中；不同查询即使内部节点编号相同（node#3 之类），
        计划身份不同，缓存键不会相撞。
        """
        validate_surface(query)
        bound = Binder(self.catalog).bind(query)
        rewriter = Rewriter(self.catalog)
        plan = rewriter.decorrelate(bound)
        query_fp = _structure_fingerprint(bound)
        plan_id = self._plan_id(query_fp)
        return bound, (plan_id, plan), rewriter.decisions

    # -- 基准路径（逐外行） --------------------------------------------------

    def run_baseline(self, query):
        try:
            validate_surface(query)
            bound = Binder(self.catalog).bind(query)
            runner = BaselineRunner(self.catalog, self.tables, self.params)
            rows = runner.run(bound)
            return RunResult(rows, None, '', runner.stats.as_dict(), '', ())
        except RelError as exc:
            return RunResult([], exc, type(exc).__name__, {}, '', ())

    # -- 正式路径（去关联物理计划） ------------------------------------------

    def run(self, query, mode='reject'):
        """统一执行策略。

        mode='reject'（默认）：不支持的结构直接抛出 UnsupportedError，
            不产生任何近似结果；
        mode='fallback'：不支持时显式回退到逐外行基准解释器，并在结果中标注
            route='baseline-fallback'，调用方可以看到这次没有享受去关联。
        两种模式都不会悄悄改写语义。
        """
        result = self.run_decorrelated(query)
        if result.error is None:
            result.route = 'decorrelated'
            return result
        if mode == 'fallback' and result.error_category == 'UnsupportedError':
            baseline = self.run_baseline(query)
            baseline.route = 'baseline-fallback'
            baseline.fallback_reason = str(result.error)
            return baseline
        return result

    def run_decorrelated(self, query):
        try:
            _, (plan_id, plan), decisions = self.prepare(query)
            stats = ExecStats()
            ctx = ExecContext(self.catalog, self.tables, self.params,
                              stats, self._cache, self.snapshot_token, plan_id)
            rows = run(plan, ctx)
            return RunResult(rows, None, '', stats.as_dict(),
                             explain(plan), tuple(decisions))
        except RelError as exc:
            # prepare 阶段（绑定/重写拒绝）与执行阶段错误统一返回，保留类别
            return RunResult([], exc, type(exc).__name__,
                             getattr(self, '_last_stats', {}), '', ())

    def explain(self, query):
        _, (_, plan), decisions = self.prepare(query)
        lines = [explain(plan), '', '改写依据:']
        for decision in decisions:
            lines.append(f'  {decision.rule} node#{decision.node_id}: {decision.basis}')
        return '\n'.join(lines)

    # -- 等价对照 ------------------------------------------------------------

    def assert_equivalent(self, query):
        """按结果袋与错误类别比较两条路径，返回对照报告。"""
        base = self.run_baseline(query)
        deco = self.run_decorrelated(query)
        if base.error_category != deco.error_category:
            raise AssertionError(
                f'错误类别不一致: baseline={base.error_category} '
                f'decorrelated={deco.error_category}'
            )
        if base.error is not None:
            return {
                'equivalent': True,
                'error_category': base.error_category,
                'baseline_stats': base.stats,
                'decorrelated_stats': deco.stats,
            }
        base_bag = Counter(bag_key(row) for row in base.rows)
        deco_bag = Counter(bag_key(row) for row in deco.rows)
        if base_bag != deco_bag:
            raise AssertionError(
                f'结果袋不一致:\n baseline={dict(base_bag)}\n decorrelated={dict(deco_bag)}'
            )
        return {
            'equivalent': True,
            'row_count': len(base.rows),
            'baseline_stats': base.stats,
            'decorrelated_stats': deco.stats,
            'plan_text': deco.plan_text,
            'decisions': [
                {'rule': d.rule, 'node_id': d.node_id, 'basis': d.basis}
                for d in deco.decisions
            ],
        }
