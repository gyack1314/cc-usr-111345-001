"""业务表的登记关系与资料概况。"""
from .io import DataError

EXPECTED = ['requests', 'settlements', 'query_cases']
REFERENCES = {}

def inspect_tables(tables):
    if set(tables) != set(EXPECTED):
        raise DataError('业务表集合与登记不符')
    for table, relations in REFERENCES.items():
        for column, target in relations.items():
            keys = {row['id'] for row in tables[target]}
            for row in tables[table]:
                if row[column] not in keys:
                    raise DataError(f'{table}.{column}引用不存在的{target}记录')
    return {'tables': {name: len(rows) for name, rows in tables.items()}, 'identity_checked': True}
