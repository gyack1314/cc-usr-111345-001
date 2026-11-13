# 业务资料约定

id是来源内行身份，不同id允许业务字段完全相同。tenant=null遵守SQL空值比较。query_cases仅登记资料中的查询意图，不是已实现的关系计划；后续接口以明确作用域的AST为准。

每行id在该表内唯一，文件顺序不代表业务事件顺序。清单schema=1，各表登记文件名、字节长度、SHA256、行数及完整字段集合，未知版本或内容变化应重新登记。业务身份、金额、版本及测量数值均为本地资料，不代表任何现实机构的经营数据。

- requests：id、tenant、tokens。
- settlements：id、tenant、amount。
- query_cases：id、operator、outer、inner、key、location。

normal与boundary是相互独立的资料包，不能直接拼接重复id。引用字段按本项目domain.py的REFERENCES登记。读取检查只检查完整性、身份及这些直接引用，不替代仓库查询语义、快照可见性或递归完备性判断。
