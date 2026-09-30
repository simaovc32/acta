"""
ml/ols.py — shared pure-Python OLS linear regression (no numpy).

Used by bedtime_recommender.py and mental_predictor.py.
Solves the normal equations w = (XᵀX)⁻¹ Xᵀy via Gauss-Jordan inversion.
"""


def _T(A):
    return [[A[i][j] for i in range(len(A))] for j in range(len(A[0]))]


def _mm(A, B):
    n, m, p = len(A), len(A[0]), len(B[0])
    C = [[0.0] * p for _ in range(n)]
    for i in range(n):
        for k in range(m):
            if A[i][k] == 0.0:
                continue
            for j in range(p):
                C[i][j] += A[i][k] * B[k][j]
    return C


def _inv(A):
    n = len(A)
    M = [r[:] + [1.0 if i == j else 0.0 for j in range(n)]
         for i, r in enumerate(A)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[pivot_row] = M[pivot_row], M[col]
        p = M[col][col]
        if abs(p) < 1e-12:
            raise ValueError("Singular matrix — features may be collinear")
        for j in range(2 * n):
            M[col][j] /= p
        for row in range(n):
            if row != col:
                f = M[row][col]
                for j in range(2 * n):
                    M[row][j] -= f * M[col][j]
    return [r[n:] for r in M]


def _ols(X, y):
    Xt = _T(X)
    w  = _mm(_inv(_mm(Xt, X)), _mm(Xt, [[yi] for yi in y]))
    return [w[i][0] for i in range(len(w))]


def _dot(w, x):
    return sum(a * b for a, b in zip(w, x))


def _r_squared(X, y, w):
    """Coefficient of determination of the fit (in-sample)."""
    n = len(y)
    if n == 0:
        return 0.0
    mean = sum(y) / n
    ss_tot = sum((yi - mean) ** 2 for yi in y)
    ss_res = sum((yi - _dot(w, xi)) ** 2 for xi, yi in zip(X, y))
    if ss_tot < 1e-12:
        return 0.0
    return 1.0 - ss_res / ss_tot
