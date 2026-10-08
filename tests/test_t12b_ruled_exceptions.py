"""The T12b ruled exceptions (tests/t12b_ruled_exceptions.py) name tests that exist and cite their ruling.

GREEN-IF every test the record names exists at the named path, and each R3 replacement's docstring cites "T12b R3"
(so a replacement cannot drift from the record unnoticed). A guard over the record, not a property of the product.
"""
from __future__ import annotations

import ast
from pathlib import Path

import t12b_ruled_exceptions as ruled

ROOT = Path(__file__).resolve().parents[1]


def _functions(path):
    tree = ast.parse(path.read_text())
    return {node.name: ast.get_docstring(node) or '' for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


def test_t12b_ruled_exceptions_name_existing_tests_that_cite_their_ruling():
    problems = []
    for table, cite in ((ruled.R2, None), (ruled.R3, 'T12b R3'), (ruled.ALSO_RULED, None), (ruled.R4, None),
                        ({entry['rows']: None for entry in ruled.R4.values()}, None)):
        for name in table:
            path, _, function = name.partition('::')
            functions = _functions(ROOT / path) if (ROOT / path).is_file() else {}
            if function not in functions:
                problems.append(f'{name}: no such test')
            elif cite and cite not in functions[function]:
                problems.append(f'{name}: its docstring does not cite {cite!r}')
    assert len(ruled.R3) == 6, f'the meet ruled six R3 replacements, the record names {len(ruled.R3)}'
    assert len(ruled.R4) == 1, f'the meet ruled one R4 exception (T10 P6), the record names {len(ruled.R4)}'
    assert not problems, '\n'.join(problems)
