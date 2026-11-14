# 用量仓相关子查询的袋语义去关联

本项目保存仓库与湖表开发所需的本地资料，提供内容摘要、记录身份与登记关系检查。当前读取入口只返回表数量及身份检查结果，不执行关系计划、版本合并、聚合或查询规划。

资料表包含requests、settlements、query_cases。字段单位和关系见 docs/data-contract.md。data/normal.json登记常规资料，data/boundary.json登记特殊业务情形，data/broken-digest.json用于检查资料损坏的拒绝行为。JSONL文件按清单内SHA256和字节长度绑定。

运行环境为 Python 3.11及以上，当前仅使用标准库，无需额外服务。包内io.py负责资料读取，domain.py负责表集合与引用检查，cli.py提供本地检查入口。

## 受限关系去关联子系统（subquery_data.rel）

在资料读取层之上提供受限关系AST（不解析SQL文本）、逐外行基准解释器、
去关联重写与一次构建的物理执行。支持投影与WHERE中的EXISTS/NOT EXISTS、
标量聚合子查询（COUNT/SUM/MIN）、单列IN/NOT IN；关联限AND下等值比较，
列作用域显式登记，同名列按层级解析避免别名遮蔽误捕获，不含非确定函数。

设计边界、三值逻辑与R0-R7改写依据见 docs/decorrelation-design.md。
要点：WHERE只留TRUE、投影保留UNKNOWN；NOT IN不化简为anti join；
COUNT空集为0、SUM/MIN空集为NULL；裸标量多行报CardinalityError；
空键不与空键相等。不支持的结构默认拒绝，也可显式回退基准执行，不做近似。

```sh
# 资料检查与关系测试（按包模块名运行，保证 tests 包上下文）
python -m unittest tests.test_material tests.test_decorrelation tests.test_fuzz_equivalence -v
python -m compileall -q subquery_data tests
# 去关联演示：结果袋对照、扫描次数、构建键与缓存范围
python -m subquery_data.rel.cli data/normal.json
python -m subquery_data.rel.cli data/boundary.json
```

## 资料检查入口

```sh
python -m unittest tests.test_material -v
python -m subquery_data.cli data/normal.json
python -m subquery_data.cli data/boundary.json
```

容器内使用同样的检查入口：

```sh
docker build -t subquery-data .
docker run --rm subquery-data
```
