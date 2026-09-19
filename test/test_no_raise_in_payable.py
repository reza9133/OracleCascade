"""
Structural safety-net test, independent of the offline SDK stub: parses
each contract file's own source with `ast` and asserts that every
`@gl.public.write.payable` method contains zero `raise` statements anywhere
in its body -- including inside any nested function or lambda defined
inside it. This is the actual mechanism behind PredictionPool.stake()'s
"never raise" guarantee described in its docstring: it is not just a
convention followed by hand, it is enforced by walking the AST.

Across this whole three-contract chain, `stake()` in PredictionPool.py is
the *only* payable method -- EventOracle.py and ForecasterRank.py carry no
value at all, so this test also asserts that fact, to keep it true on
purpose rather than by accident as the contracts evolve.

Run with:  python3 -m unittest discover -s test -v
"""
import ast
import unittest
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

EXPECTED_PAYABLE_METHODS = {
	"EventOracle.py": [],
	"PredictionPool.py": ["stake"],
	"ForecasterRank.py": [],
}


def _is_payable(func: ast.FunctionDef) -> bool:
	for dec in func.decorator_list:
		dumped = ast.dump(dec)
		if "payable" in dumped:
			return True
	return False


def _find_payable_methods(tree: ast.Module) -> list:
	found = []
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef) and _is_payable(node):
			found.append(node)
	return found


def _raise_statements_in(func: ast.FunctionDef) -> list:
	return [n for n in ast.walk(func) if isinstance(n, ast.Raise)]


class NoRaiseInPayableTestCase(unittest.TestCase):
	def test_payable_methods_match_the_documented_set(self):
		for filename, expected_names in EXPECTED_PAYABLE_METHODS.items():
			tree = ast.parse((CONTRACTS_DIR / filename).read_text())
			actual_names = sorted(f.name for f in _find_payable_methods(tree))
			self.assertEqual(actual_names, sorted(expected_names),
				filename + ": payable-method set drifted from what this test documents")

	def test_no_payable_method_ever_raises(self):
		for filename in EXPECTED_PAYABLE_METHODS:
			tree = ast.parse((CONTRACTS_DIR / filename).read_text())
			for func in _find_payable_methods(tree):
				raises = _raise_statements_in(func)
				self.assertEqual(raises, [],
					filename + "::" + func.name + " is payable and must never raise, "
					"but contains a raise statement -- value that rode in with the call "
					"would be lost on revert")

	def test_stake_is_the_only_payable_method_in_the_whole_chain(self):
		total = 0
		for filename in EXPECTED_PAYABLE_METHODS:
			tree = ast.parse((CONTRACTS_DIR / filename).read_text())
			total += len(_find_payable_methods(tree))
		self.assertEqual(total, 1)


if __name__ == "__main__":
	unittest.main()
