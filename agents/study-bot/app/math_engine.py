"""
math_engine.py — Math computation layer for Study Bot
Primary:  Wolfram Engine (via wolframclient)
Fallback: SymPy → NumPy/SciPy

All public functions return a MathResult dataclass with:
    - result_str   : human-readable string
    - latex        : LaTeX string for rendering
    - engine_used  : "wolfram" | "sympy" | "numpy"
    - success      : bool
    - error        : error message if failed

Usage:
    from math_engine import MathEngine
    engine = MathEngine()
    engine.start()
    r = engine.compute("Factor[x^2 - 5x + 6]")
    print(r.latex)
    engine.stop()
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("math_engine")

# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class MathResult:
    result_str: str  = ""
    latex:      str  = ""
    engine_used:str  = ""
    success:    bool = False
    error:      str  = ""

    def __str__(self):
        if self.success:
            return f"[{self.engine_used}] {self.result_str}"
        return f"[ERROR/{self.engine_used}] {self.error}"


# ── LaTeX helpers ─────────────────────────────────────────────────────────────

def _clean_wolfram_number(s: str) -> str:
    """Strip Wolfram's internal precision notation e.g. 3.14`10. → 3.14"""
    return re.sub(r'`[\d.]*', '', s)


def _sympy_to_latex(expr) -> str:
    """Convert a SymPy expression to LaTeX string."""
    try:
        from sympy import latex
        return latex(expr)
    except Exception:
        return str(expr)


# ── Wolfram interface ─────────────────────────────────────────────────────────

class _WolframSession:
    """Wrapper around WolframLanguageSession with lazy start and auto-restart."""

    def __init__(self):
        self._session = None
        self._started = False

    def start(self):
        if self._started:
            return
        log.info("[Wolfram] Starting kernel...")
        try:
            from wolframclient.evaluation import WolframLanguageSession
            self._session = WolframLanguageSession()
            self._session.start()
            self._started = True
            log.info("[Wolfram] Kernel ready.")
        except Exception as e:
            log.error(f"[Wolfram] Failed to start kernel: {e}")
            self._started = False

    def stop(self):
        if self._session and self._started:
            try:
                self._session.terminate()
                log.info("[Wolfram] Kernel terminated.")
            except Exception:
                pass
        self._started = False
        self._session = None

    def evaluate(self, expr: str) -> Optional[str]:
        """
        Evaluate a Wolfram Language expression string.
        Returns result as a string, or None on failure.
        """
        if not self._started:
            self.start()
        if not self._started:
            return None
        try:
            from wolframclient.language import wlexpr
            result = self._session.evaluate(
                wlexpr(f'ToString[{expr}, InputForm]')
            )
            return _clean_wolfram_number(str(result))
        except Exception as e:
            log.warning(f"[Wolfram] Evaluation failed for '{expr}': {e}")
            return None

    def evaluate_latex(self, expr: str) -> Optional[str]:
        """
        Evaluate a Wolfram Language expression and return LaTeX string.
        Returns None on failure.
        """
        if not self._started:
            self.start()
        if not self._started:
            return None
        try:
            from wolframclient.language import wlexpr
            result = self._session.evaluate(
                wlexpr(f'ToString[TeXForm[{expr}]]')
            )
            return str(result)
        except Exception as e:
            log.warning(f"[Wolfram] LaTeX eval failed for '{expr}': {e}")
            return None

    @property
    def available(self) -> bool:
        return self._started


# ── SymPy fallback ────────────────────────────────────────────────────────────

def _sympy_compute(expression: str) -> MathResult:
    """
    Attempt to evaluate an expression using SymPy.
    Supports: factor, expand, diff, integrate, simplify, solve, matrix ops.
    """
    try:
        from sympy import (
            symbols, sympify, factor, expand, simplify,
            diff, integrate, solve, latex, Matrix,
            sin, cos, tan, exp, log as symlog, sqrt, pi, E
        )
        from sympy.parsing.sympy_parser import (
            parse_expr, standard_transformations,
            implicit_multiplication_application
        )

        transformations = standard_transformations + (implicit_multiplication_application,)

        # Try to parse and simplify the expression
        expr = parse_expr(expression, transformations=transformations)
        result = simplify(expr)
        result_str = str(result)
        latex_str  = _sympy_to_latex(result)

        return MathResult(
            result_str  = result_str,
            latex       = latex_str,
            engine_used = "sympy",
            success     = True,
        )

    except Exception as e:
        log.warning(f"[SymPy] Failed to evaluate '{expression}': {e}")
        return MathResult(
            engine_used = "sympy",
            success     = False,
            error       = str(e),
        )


def _numpy_compute(expression: str) -> MathResult:
    """
    Attempt to evaluate a numerical expression using NumPy/SciPy.
    Best for: numerical arrays, linear algebra, statistics.
    """
    try:
        import numpy as np
        import scipy

        # Safe eval namespace with numpy and scipy
        namespace = {
            "np": np,
            "scipy": scipy,
            "array": np.array,
            "matrix": np.array,
            "linspace": np.linspace,
            "zeros": np.zeros,
            "ones": np.ones,
            "eye": np.eye,
            "inv": np.linalg.inv,
            "det": np.linalg.det,
            "eig": np.linalg.eig,
            "norm": np.linalg.norm,
            "dot": np.dot,
            "pi": np.pi,
            "e": np.e,
            "sin": np.sin,
            "cos": np.cos,
            "sqrt": np.sqrt,
            "exp": np.exp,
            "log": np.log,
        }

        result = eval(expression, {"__builtins__": {}}, namespace)
        result_str = str(result)

        return MathResult(
            result_str  = result_str,
            latex       = result_str,   # no LaTeX for raw numpy output
            engine_used = "numpy",
            success     = True,
        )

    except Exception as e:
        log.warning(f"[NumPy] Failed to evaluate '{expression}': {e}")
        return MathResult(
            engine_used = "numpy",
            success     = False,
            error       = str(e),
        )


# ── Main engine ───────────────────────────────────────────────────────────────

class MathEngine:
    """
    Primary math computation interface for Study Bot.

    Usage:
        engine = MathEngine()
        engine.start()
        result = engine.compute("Factor[x^2 - 5x + 6]")
        result = engine.compute_latex("Integrate[x^2, x]")
        engine.stop()

    Wolfram expressions use Wolfram Language syntax.
    Fallback expressions use Python/SymPy syntax.
    """

    def __init__(self):
        self._wolfram = _WolframSession()
        self._wolfram_available = False

    def start(self):
        """Start the Wolfram kernel. Call once at app startup."""
        self._wolfram.start()
        self._wolfram_available = self._wolfram.available
        if self._wolfram_available:
            log.info("[MathEngine] Wolfram primary engine ready.")
        else:
            log.warning("[MathEngine] Wolfram unavailable — will use SymPy/NumPy fallback.")

    def stop(self):
        """Terminate the Wolfram kernel. Call at app shutdown."""
        self._wolfram.stop()

    @property
    def wolfram_available(self) -> bool:
        return self._wolfram_available

    def compute(self, wolfram_expr: str, fallback_expr: Optional[str] = None) -> MathResult:
        """
        Compute a math expression.

        Args:
            wolfram_expr:  Expression in Wolfram Language syntax
            fallback_expr: Expression in Python/SymPy syntax (optional).
                           If not provided, wolfram_expr is tried with SymPy too.

        Returns:
            MathResult with result_str, latex, engine_used, success, error
        """
        # ── Try Wolfram first ─────────────────────────────────────────────────
        if self._wolfram_available:
            result_str = self._wolfram.evaluate(wolfram_expr)
            latex_str  = self._wolfram.evaluate_latex(wolfram_expr)

            if result_str is not None:
                return MathResult(
                    result_str  = result_str,
                    latex       = latex_str or result_str,
                    engine_used = "wolfram",
                    success     = True,
                )
            else:
                log.warning(f"[MathEngine] Wolfram failed for '{wolfram_expr}' — trying fallback")

        # ── Fallback to SymPy ─────────────────────────────────────────────────
        py_expr = fallback_expr or wolfram_expr
        result  = _sympy_compute(py_expr)
        if result.success:
            return result

        # ── Fallback to NumPy ─────────────────────────────────────────────────
        result = _numpy_compute(py_expr)
        if result.success:
            return result

        # ── All engines failed ────────────────────────────────────────────────
        return MathResult(
            engine_used = "none",
            success     = False,
            error       = f"All engines failed for expression: {wolfram_expr}",
        )

    def compute_latex(self, wolfram_expr: str, fallback_expr: Optional[str] = None) -> str:
        """
        Convenience method — returns just the LaTeX string.
        Returns empty string on failure.
        """
        result = self.compute(wolfram_expr, fallback_expr)
        if result.success:
            return result.latex
        return ""

    def verify_answer(self, wolfram_check_expr: str) -> Optional[bool]:
        """
        Evaluate a boolean Wolfram expression for answer verification.
        Example: verify_answer("Factor[x^2-5x+6] === (x-2)(x-3)")
        Returns True/False/None (None = could not determine)
        """
        if not self._wolfram_available:
            return None
        try:
            from wolframclient.language import wlexpr
            result = self._wolfram._session.evaluate(
                wlexpr(f'ToString[TrueQ[{wolfram_check_expr}]]')
            )
            s = str(result).strip().lower()
            if s == "true":  return True
            if s == "false": return False
            return None
        except Exception as e:
            log.warning(f"[MathEngine] verify_answer failed: {e}")
            return None


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    engine = MathEngine()
    engine.start()

    tests = [
        ("Factor[x^2 - 5x + 6]",            "factor(x**2 - 5*x + 6)"),
        ("D[Sin[x]^2, x]",                   "diff(sin(x)**2, x)"),
        ("Integrate[x^2, {x, 0, 1}]",        "integrate(x**2, (x, 0, 1))"),
        ("Inverse[{{1,2},{3,4}}]",            None),
        ("Eigenvalues[{{2,1},{1,2}}]",        None),
        ("Simplify[Sin[x]^2 + Cos[x]^2]",    "simplify(sin(x)**2 + cos(x)**2)"),
    ]

    print(f"\n{'='*60}")
    print(f"MathEngine test — Wolfram available: {engine.wolfram_available}")
    print(f"{'='*60}\n")

    for wolfram_expr, fallback in tests:
        result = engine.compute(wolfram_expr, fallback)
        status = "✅" if result.success else "❌"
        print(f"{status} [{result.engine_used}] {wolfram_expr}")
        print(f"   Result : {result.result_str}")
        print(f"   LaTeX  : {result.latex}")
        print()

    engine.stop()
    print("Done.")
