# 用量仓相关子查询的袋语义去关联

本项目保存仓库与湖表开发所需的本地资料，提供内容摘要、记录身份与登记关系检查。当前读取入口只返回表数量及身份检查结果，不执行关系计划、版本合并、聚合或查询规划。

资料表包含requests、settlements、query_cases。字段单位和关系见 docs/data-contract.md。data/normal.json登记常规资料，data/boundary.json登记特殊业务情形，data/broken-digest.json用于检查资料损坏的拒绝行为。JSONL文件按清单内SHA256和字节长度绑定。

运行环境为 Python 3.11及以上，当前仅使用标准库，无需额外服务。包内io.py负责资料读取，domain.py负责表集合与引用检查，cli.py提供本地检查入口。

```sh
python -m unittest discover -s tests -v
python -m compileall -q subquery_data tests
python -m subquery_data.cli data/normal.json
python -m subquery_data.cli data/boundary.json
```

容器内使用同样的检查入口：

```sh
docker build -t subquery-data .
docker run --rm subquery-data
```
