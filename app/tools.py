"""
app/tools.py — Math computation tools for the ReAct agent
Primary:  Wolfram Engine (via MathEngine)
Fallback: SymPy → NumPy/SciPy
"""

import traceback
import sympy as sp
import numpy as np
from scipy import linalg as scipy_linalg
from langchain.tools import tool
from app.math_engine import MathEngine

# ── Shared engine instance ────────────────────────────────────────────────────
# Started once when tools.py is imported, reused across all agent calls
_engine = MathEngine()
_engine.start()


@tool
def wolfram_compute(expression: str) -> str:
    """
    Evaluate a Wolfram Language expression using the local Wolfram Engine.
    Use this as the PRIMARY tool for all math computation.
    Supports: algebra, calculus, linear algebra, number theory, statistics,
    equation solving, factoring, integration, differentiation, matrix ops,
    eigenvalues, determinants, simplification, and more.

    Input: a valid Wolfram Language expression string.
    Example inputs:
      'Factor[x^2 - 5x + 6]'
      'D[Sin[x]^2, x]'
      'Integrate[x^2, {x, 0, 1}]'
      'Inverse[{{1,2},{3,4}}]'
      'Eigenvalues[{{2,1},{1,2}}]'
      'Solve[x^2 - 4 == 0, x]'
      'Det[{{1,2,3},{4,5,6},{7,8,9}}]'
    """
    try:
        result = _engine.compute(wolfram_expr=expression)
        if result.success:
            return (
                f"Wolfram result: {result.result_str}\n"
                f"LaTeX: {result.latex}\n"
                f"Engine: {result.engine_used}"
            )
        else:
            return f"Wolfram failed: {result.error}"
    except Exception as e:
        return f"Wolfram error: {e}\n{traceback.format_exc()}"


@tool
def sympy_compute(expression: str) -> str:
    """
    Evaluate a symbolic math expression using SymPy.
    Use this as FALLBACK if wolfram_compute fails or is unavailable.
    Supports: algebra, calculus, matrix operations, equation solving,
    simplification, factoring, eigenvalues, determinants.

    Input: a valid Python/SymPy expression as a string.
    Example inputs:
      'sp.det(sp.Matrix([[1,2],[3,4]]))'
      'sp.solve(sp.Eq(x**2 - 4, 0), x)'
      'sp.simplify(sp.sin(x)**2 + sp.cos(x)**2)'
    """
    try:
        x, y, z, t, n = sp.symbols('x y z t n')
        A, B, C = sp.symbols('A B C')
        namespace = {
            'sp': sp, 'sympy': sp,
            'x': x, 'y': y, 'z': z, 't': t, 'n': n,
            'A': A, 'B': B, 'C': C,
            'Matrix': sp.Matrix,
            'Rational': sp.Rational,
            'sqrt': sp.sqrt,
            'pi': sp.pi,
            'oo': sp.oo,
            'I': sp.I,
        }
        result = eval(expression, {"__builtins__": {}}, namespace)
        return f"SymPy result: {result}"
    except Exception as e:
        return f"SymPy error: {e}\n{traceback.format_exc()}"


@tool
def numpy_compute(expression: str) -> str:
    """
    Evaluate a numerical math expression using NumPy.
    Use this as FALLBACK for numerical linear algebra when exact
    symbolic answer isn't needed.

    Input: a valid Python/NumPy expression as a string.
    Example inputs:
      'np.linalg.det(np.array([[1,2],[3,4]]))'
      'np.dot(np.array([1,2,3]), np.array([4,5,6]))'
      'np.linalg.norm(np.array([3,4]))'
    """
    try:
        namespace = {
            'np': np,
            'numpy': np,
            'array': np.array,
            'matrix': np.matrix,
        }
        result = eval(expression, {"__builtins__": {}}, namespace)
        return f"NumPy result: {result}"
    except Exception as e:
        return f"NumPy error: {e}\n{traceback.format_exc()}"


@tool
def scipy_compute(expression: str) -> str:
    """
    Evaluate a numerical expression using SciPy.
    Use this as FALLBACK for LU decomposition, SVD, eigenvalue decomposition,
    solving linear systems numerically, matrix factorizations.

    Input: a valid Python/SciPy expression as a string.
    Example inputs:
      'scipy_linalg.lu(np.array([[1,2],[3,4]], dtype=float))'
      'scipy_linalg.svd(np.array([[1,2],[3,4]], dtype=float))'
    """
    try:
        namespace = {
            'scipy_linalg': scipy_linalg,
            'np': np,
            'numpy': np,
            'array': np.array,
        }
        result = eval(expression, {"__builtins__": {}}, namespace)
        return f"SciPy result: {result}"
    except Exception as e:
        return f"SciPy error: {e}\n{traceback.format_exc()}"


# ── Tool registry ─────────────────────────────────────────────────────────────
MATH_TOOLS = [wolfram_compute, sympy_compute, numpy_compute, scipy_compute]
TOOL_NAMES  = [t.name for t in MATH_TOOLS]