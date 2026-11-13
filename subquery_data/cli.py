"""检查资料的完整性并列出业务记录数量。"""
import argparse
import json
from .io import load, DataError
from .domain import inspect_tables

def main():
    parser = argparse.ArgumentParser(description='检查业务资料的身份与完整性')
    parser.add_argument('manifest')
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_tables(load(args.manifest)), ensure_ascii=False, indent=2))
    except (DataError, ValueError) as exc:
        parser.exit(2, str(exc)+'\n')

if __name__ == '__main__':
    main()
