"""未定义名检查（标准库实现，不依赖 ruff / pyflakes）。

背景：项目大量使用 `from __future__ import annotations`，类型注解在运行时不会被求值，
因此"注解里引用了未导入的名字"（如 `-> list[Tile]`）在本机完全不会报错，
只有 CI 上的 ruff F821 才能发现——这类问题已经真实发生过一次。

本工具用 AST 精确重建作用域，报告所有"被引用但未定义"的名字：
  * 类型注解（函数签名、返回值、变量注解）
  * 函数体内的名字引用
  * 覆盖 import 绑定、参数、局部赋值、推导式变量、with/except 别名、
    global/nonlocal 声明、lambda 参数、类作用域等

用法：
    python tools/check_names.py [路径...]
退出码：0 = 全部通过，1 = 存在未定义名
"""
from __future__ import annotations

import ast
import builtins
import os
import sys

BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__class__", "__debug__",
    "__annotations__", "__dict__", "__module__", "__qualname__",
}


class Scope:
    """一个作用域：收集绑定的名字与其中引用的名字。"""

    def __init__(self, kind: str, parent: "Scope | None" = None):
        self.kind = kind                 # module / class / function
        self.parent = parent
        self.bindings: set[str] = set()
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()

    def resolve(self, name: str) -> bool:
        """名字能否被解析（自身绑定 → 外层函数作用域 → 模块 → 内置）。"""
        if name in BUILTINS:
            return True
        if name in self.globals:
            return True
        if name in self.bindings:
            return True
        if self.kind == "function":
            # 函数可以闭包捕获外层函数的名字
            p = self.parent
            while p is not None and p.kind == "function":
                if name in p.bindings:
                    return True
                p = p.parent
        if self.parent is not None and self.parent.kind == "class":
            # 类体里的名字在方法中不可见，故只查模块层
            p = self.parent.parent
        else:
            p = self.parent
        while p is not None:
            if name in p.bindings:
                return True
            p = p.parent
        return False


class Checker(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.module = Scope("module")
        self.scope = self.module
        self.problems: list[tuple[str, int, str]] = []

    def precollect_module(self, tree: ast.Module) -> None:
        """先收集模块级全部绑定。

        模块级名字在调用时才解析，因此函数体里引用"定义在文件后面"的模块常量是合法的，
        必须先把整个模块的绑定收齐，否则会产生大量误报。
        """
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                self.module.bindings.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        self.module.bindings.add((a.asname or a.name.split(".")[0]))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.module.bindings.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self.module.bindings.add(node.name)
            elif isinstance(node, ast.Global):
                for n in node.names:
                    self.module.bindings.add(n)

    # -- 绑定收集 -------------------------------------------------------
    def _bind(self, name: str) -> None:
        self.scope.bindings.add(name)

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._bind((a.asname or a.name).split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for a in node.names:
            if a.name == "*":
                continue
            self._bind(a.asname or a.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._bind(node.name)
        for dec in node.decorator_list:
            self._expr(dec)
        for arg in list(node.args.args) + list(node.args.posonlyargs) + \
                list(node.args.kwonlyargs):
            self._bind(arg.arg)
            if arg.annotation is not None:
                self._expr(arg.annotation)
        if node.args.vararg:
            self._bind(node.args.vararg.arg)
        if node.args.kwarg:
            self._bind(node.args.kwarg.arg)
        if node.returns is not None:
            self._expr(node.returns)

        outer = self.scope
        self.scope = Scope("function", outer)
        # 先收集函数体内的所有绑定（Python 允许在任意位置赋值）
        self._precollect(node)
        for stmt in node.body:
            self.visit(stmt)
        self.scope = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for arg in list(node.args.args) + list(node.args.posonlyargs) + \
                list(node.args.kwonlyargs):
            self._bind(arg.arg)
        outer = self.scope
        self.scope = Scope("function", outer)
        self._precollect(node)
        self._expr(node.body)
        self.scope = outer

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind(node.name)
        for dec in node.decorator_list:
            self._expr(dec)
        for base in node.bases:
            self._expr(base)
        outer = self.scope
        self.scope = Scope("class", outer)
        for stmt in node.body:
            self.visit(stmt)
        self.scope = outer

    def _precollect(self, node) -> None:
        """预先收集函数体内的绑定名（赋值、for、with、except、推导式等）。"""
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                self._bind(child.id)
            elif isinstance(child, ast.arg):
                self._bind(child.arg)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for a in child.names:
                    if a.name != "*":
                        self._bind(a.asname or a.name.split(".")[0])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._bind(child.name)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                self._bind(child.name)
            elif isinstance(child, ast.Global):
                for n in child.names:
                    self.scope.globals.add(n)
                    self.module.bindings.add(n)
            elif isinstance(child, ast.Nonlocal):
                for n in child.names:
                    self.scope.nonlocals.add(n)

    # -- 引用检查 -------------------------------------------------------
    def _expr(self, node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                if not self.scope.resolve(node.id):
                    self.problems.append((self.path, node.lineno, node.id))
            return
        for child in ast.iter_child_nodes(node):
            self._expr(child)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.target is not None:
            self._expr(node.target)
        self._expr(node.annotation)
        if node.value is not None:
            self._expr(node.value)

    def generic_visit(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self.visit(child)
            else:
                self._expr(child)


def check_file(path: str) -> list[tuple[str, int, str]]:
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, path)
    ck = Checker(path)
    ck.precollect_module(tree)
    ck.visit(tree)
    # 模块层未定义的名字也报出来
    return ck.problems


def main(argv: list[str]) -> int:
    roots = argv[1:] or ["."]
    files: list[str] = []
    for root in roots:
        if os.path.isfile(root):
            files.append(root)
            continue
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d not in ("__pycache__", ".git", ".workbuddy", ".shots",
                                    "saves", "build", "dist", ".venv", "venv")]
            for n in names:
                if n.endswith(".py"):
                    files.append(os.path.join(base, n))

    total = 0
    for f in sorted(files):
        try:
            problems = check_file(f)
        except SyntaxError as exc:
            print(f"[语法错误] {f}: {exc}")
            total += 1
            continue
        for path, line, name in problems:
            print(f"[未定义名] {path}:{line}: {name}")
            total += 1

    print(f"检查 {len(files)} 个文件，发现 {total} 个问题")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
