"""1.3.3: the shipped modules load on Python 3.9 (EL 9).

`search_index.py` had one annotation written `dict | None`. That is Python
3.10 syntax when it is evaluated, which it is unless the module starts with
`from __future__ import annotations`; on EL 9 the module did not import, so
search and mail import were broken in the rpm from 1.3.0 to 1.3.2. Nothing
tested the rpm's Python.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULES = sorted(p for p in ROOT.glob('*.py'))


def _union_annotations(tree):
    """Annotations that use `X | Y` (evaluated at import without the future import)."""
    found = []
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            annotations += [a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs]
            annotations += [a.annotation for a in (args.vararg, args.kwarg) if a]
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for ann in filter(None, annotations):
            if any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(ann)):
                found.append(getattr(node, 'name', None) or ast.dump(node.target)[:40])
    return found


@pytest.mark.parametrize('path', MODULES, ids=lambda p: p.name)
def test_module_is_python39_syntax_and_safe_to_import_there(path):
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=path.name, feature_version=(3, 9))   # no match/case etc.
    lazy = any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
               and any(a.name == 'annotations' for a in n.names) for n in tree.body)
    unions = _union_annotations(tree)
    assert lazy or not unions, (
        f'{path.name}: annotations {unions} use `X | Y`; add '
        f'`from __future__ import annotations` or the module will not import on Python 3.9')
