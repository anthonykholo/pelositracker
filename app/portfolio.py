"""Joint-outcome (portfolio) Kelly sizing for correlated paper bets.

Sizing each position with its own Kelly fraction over-stakes a book of bets that
move together: two bets on the same team side both win or both lose, so betting
each at full Kelly doubles the risk of one underlying outcome. This module sizes
the whole set at once -- it maximizes expected log wealth over the *exact* joint
outcome distribution the correlation model implies, so positively correlated bets
share one risk budget while genuinely independent bets are allowed a little more
total exposure (diversification).

Correlation is modeled conservatively from the caller's correlation group: bets
in the same group are perfectly positively correlated (a single shared latent
uniform, so each still honors its own marginal win probability); bets in
different groups are independent. Under that model the joint outcome space is a
finite, enumerable set of scenarios (no Monte-Carlo sampling), so the optimizer
is deterministic and its optimum is verifiable.

The optimization is the constrained log-optimal-growth (Kelly) program

    maximize   sum_s pi_s * log(1 + R_s . x)
    subject to 0 <= x_i <= cap_i / bankroll,  sum_i x_i <= max_total_fraction,
               1 + R_s . x >= epsilon   for every scenario s,

with the last inequality a real *solvency* constraint (insolvent allocations are
rejected, not floored inside the objective). It is solved by a dependency-free
log-barrier interior-point method with a feasible start, damped Newton steps,
backtracking line search, and a surrogate KKT/duality-gap stopping test. If the
gap tolerance is not met within the iteration budget -- or the scenario space
exceeds the declared cap -- the solver returns WATCH with zero stakes rather than
a non-optimal iterate.

Output is display-grade paper sizing, not investment advice. This models
comonotonic/independent groups only; mutually-exclusive market structures (1X2,
categorical, spread/total push) are a follow-on that needs richer per-candidate
market identity than the ``Candidate`` contract currently carries.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import log


@dataclass(frozen=True, slots=True)
class Candidate:
    prob: float    # model win probability of the backed selection, in (0,1)
    price: float   # executable entry price, in (0,1)
    group: str     # correlation group; same group => perfectly correlated
    cap: float     # maximum stake in dollars (independent exposure cap)


@dataclass(frozen=True, slots=True)
class Scenario:
    """One atom of the exact joint outcome distribution.

    ``returns`` is the per-dollar return of each candidate in candidate order
    (payoff-if-win when the candidate is a winner, ``-1.0`` when it loses).
    """
    probability: float
    returns: tuple[float, ...]


class SolveStatus(str, Enum):
    OPTIMAL = "optimal"
    WATCH = "watch"


@dataclass(frozen=True, slots=True)
class JointKellyResult:
    status: SolveStatus
    stakes: list[float]          # dollars, in candidate order (zeros on WATCH)
    fractions: list[float]       # fraction of bankroll, in candidate order
    kkt_residual: float | None   # surrogate duality gap at the returned point
    iterations: int
    n_scenarios: int
    reason: str


# The exact scenario set is a product over correlation groups. The policy
# grouping keeps this tiny, but a pathological book is refused rather than
# silently approximated by sampling.
_MAX_SCENARIOS = 100_000
_SOLVENCY_EPSILON = 1e-6
# A fraction below this is economically zero; snapping it removes the interior
# point method's residual mass from negative-/zero-edge candidates so they get
# exactly no allocation.
_ZERO_FRACTION = 1e-6


def _payoff_if_win(price: float) -> float:
    """Per-dollar profit if the selection wins: ``1/price - 1`` (>0 for price<1)."""
    return 1.0 / price - 1.0 if 0.0 < price < 1.0 else -1.0


def enumerate_scenarios(candidates: list[Candidate]) -> list[Scenario] | None:
    """Exact joint outcome distribution for the correlation-group model.

    Each group has one latent ``u ~ U(0,1)``; a candidate in that group wins iff
    ``u < prob`` (comonotonic within the group, honoring each marginal). Sweeping
    ``u`` yields at most ``k+1`` distinct win-sets for a ``k``-candidate group;
    independent groups combine as a Cartesian product. Returns ``None`` when the
    product exceeds :data:`_MAX_SCENARIOS`.
    """
    n = len(candidates)
    payoffs = [_payoff_if_win(c.price) for c in candidates]

    # Deterministic group order; members keep their original candidate index.
    group_members: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        group_members.setdefault(candidate.group, []).append(index)

    # Per group: a list of (mass, set-of-winning-candidate-indices).
    group_atoms: list[list[tuple[float, frozenset[int]]]] = []
    total = 1
    for group in sorted(group_members):
        members = group_members[group]
        # Boundaries of u induced by the members' win thresholds (their probs).
        cuts = sorted({0.0, 1.0, *(min(max(candidates[i].prob, 0.0), 1.0) for i in members)})
        atoms: list[tuple[float, frozenset[int]]] = []
        for lo, hi in zip(cuts, cuts[1:]):
            mass = hi - lo
            if mass <= 0.0:
                continue
            # For u in (lo, hi) a member wins iff its prob >= hi.
            winners = frozenset(i for i in members if candidates[i].prob >= hi)
            atoms.append((mass, winners))
        if not atoms:  # degenerate (e.g. all probs 0); treat as a certain loss
            atoms = [(1.0, frozenset())]
        group_atoms.append(atoms)
        total *= len(atoms)
        if total > _MAX_SCENARIOS:
            return None

    # Cartesian product of the per-group atoms in a fixed order.
    combos: list[tuple[float, frozenset[int]]] = [(1.0, frozenset())]
    for atoms in group_atoms:
        combos = [
            (mass * a_mass, winners | a_winners)
            for mass, winners in combos
            for a_mass, a_winners in atoms
        ]
    scenarios = [
        Scenario(mass, tuple(payoffs[i] if i in winners else -1.0 for i in range(n)))
        for mass, winners in combos
    ]
    return scenarios


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve ``matrix @ x = rhs`` by Gaussian elimination with partial pivoting."""
    n = len(rhs)
    augmented = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-15:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        pivot_value = augmented[col][col]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col] / pivot_value
            if factor == 0.0:
                continue
            for k in range(col, n + 1):
                augmented[row][k] -= factor * augmented[col][k]
    return [augmented[i][n] / augmented[i][i] for i in range(n)]


def _barrier_solve(
    returns: list[tuple[float, ...]], probs: list[float], caps: list[float],
    budget: float, *, epsilon: float, mu: float = 15.0, gap_tol: float = 1e-8,
    inner_tol: float = 1e-10, max_outer: int = 80, max_inner: int = 100,
) -> tuple[list[float], float] | None:
    """Maximize ``sum_s probs_s * log(1 + returns_s . x)`` over the box, budget and
    solvency constraints via a log-barrier interior-point method.

    Returns ``(x, surrogate_gap)`` once the surrogate duality gap ``m/t`` drops
    below ``gap_tol``, or ``None`` if that is not reached within the budget (the
    caller then fails closed rather than trusting a non-optimal iterate).
    """
    n = len(caps)
    s = len(probs)
    m_ineq = 2 * n + 1 + s  # lower/upper bounds + budget + one solvency per scenario

    # Strictly interior feasible start: small, inside every box and the budget.
    x = [0.5 * min(caps[i], budget / (n + 1)) for i in range(n)]

    def wealths(point: list[float]) -> list[float]:
        return [1.0 + sum(returns[k][i] * point[i] for i in range(n)) for k in range(s)]

    def feasible(point: list[float]) -> bool:
        if any(xi <= 0.0 or xi >= caps[i] for i, xi in enumerate(point)):
            return False
        if sum(point) >= budget:
            return False
        return all(w > epsilon for w in wealths(point))

    if not feasible(x):
        return None

    def phi_plus_obj(point: list[float], t: float) -> float:
        """t * (negative expected log growth) + barrier -- the value Newton minimizes."""
        w = wealths(point)
        value = -t * sum(probs[k] * log(w[k]) for k in range(s))
        value -= sum(log(xi) + log(caps[i] - xi) for i, xi in enumerate(point))
        value -= log(budget - sum(point))
        value -= sum(log(w[k] - epsilon) for k in range(s))
        return value

    t = 1.0
    for _outer in range(max_outer):
        converged_inner = False
        for _inner in range(max_inner):
            w = wealths(x)
            slack_budget = budget - sum(x)
            # Gradient and Hessian of t*f0 + barrier.
            grad = [0.0] * n
            hess = [[0.0] * n for _ in range(n)]
            for i in range(n):
                grad[i] += -1.0 / x[i] + 1.0 / (caps[i] - x[i]) + 1.0 / slack_budget
                hess[i][i] += 1.0 / x[i] ** 2 + 1.0 / (caps[i] - x[i]) ** 2
                for j in range(n):
                    hess[i][j] += 1.0 / slack_budget ** 2
            for k in range(s):
                r = returns[k]
                inv_obj = probs[k] / w[k]
                inv_obj2 = probs[k] / w[k] ** 2
                inv_barrier = 1.0 / (w[k] - epsilon)
                inv_barrier2 = inv_barrier * inv_barrier
                for i in range(n):
                    grad[i] += -t * inv_obj * r[i] - inv_barrier * r[i]
                    coeff = t * inv_obj2 + inv_barrier2
                    for j in range(n):
                        hess[i][j] += coeff * r[i] * r[j]
            step = _solve_linear(hess, [-g for g in grad])
            if step is None:
                return None
            decrement2 = -sum(grad[i] * step[i] for i in range(n))
            if decrement2 < 0.0:  # numerical noise; treat as converged
                decrement2 = 0.0
            if decrement2 / 2.0 <= inner_tol:
                converged_inner = True
                break
            # Backtracking line search keeping strict feasibility and Armijo decrease.
            base = phi_plus_obj(x, t)
            slope = -decrement2
            scale = 1.0
            for _bt in range(60):
                trial = [x[i] + scale * step[i] for i in range(n)]
                if feasible(trial) and phi_plus_obj(trial, t) <= base + 0.25 * scale * slope:
                    x = trial
                    break
                scale *= 0.5
            else:
                return None  # could not make progress
        if not converged_inner:
            return None
        gap = m_ineq / t
        if gap <= gap_tol:
            return x, gap
        t *= mu
    return None


def solve_joint_kelly(
    candidates: list[Candidate], bankroll: float, *,
    kelly_multiplier: float = 1.0, max_total_fraction: float = 1.0,
    solvency_epsilon: float = _SOLVENCY_EPSILON,
) -> JointKellyResult:
    """Solve the constrained log-optimal-growth program exactly.

    Returns a :class:`JointKellyResult`. On a scenario-space blow-up or a solver
    that fails to reach the KKT/duality-gap tolerance, the status is
    ``SolveStatus.WATCH`` and all stakes are zero -- the final iterate is never
    returned as though optimal.
    """
    n = len(candidates)
    zero = [0.0] * n
    if n == 0 or bankroll <= 0:
        return JointKellyResult(SolveStatus.OPTIMAL, zero, zero, 0.0, 0, 0, "no candidates")

    multiplier = max(0.0, kelly_multiplier)
    budget = min(max(0.0, max_total_fraction), 1.0)
    caps_fraction = [max(0.0, c.cap) / bankroll for c in candidates]
    payoffs = [_payoff_if_win(c.price) for c in candidates]

    # Analytical scalar Kelly for the common single-bet case (exact, no solver).
    if n == 1:
        payoff = payoffs[0]
        raw = (candidates[0].prob * (payoff + 1.0) - 1.0) / payoff if payoff > 0 else 0.0
        fraction = min(max(0.0, raw) * multiplier, caps_fraction[0], budget)
        return JointKellyResult(SolveStatus.OPTIMAL, [fraction * bankroll], [fraction],
                                0.0, 0, 2, "single-bet analytical Kelly")

    # A candidate that cannot be staked (zero cap) or has no upside (price>=1) is
    # fixed at zero and dropped from the optimization; it never helps expected
    # log growth under a comonotonic/independent model.
    active = [i for i in range(n)
              if caps_fraction[i] > 0.0 and payoffs[i] > 0.0]
    if not active:
        return JointKellyResult(SolveStatus.OPTIMAL, zero, zero, 0.0, 0, 0, "no stakeable edge")

    active_candidates = [candidates[i] for i in active]
    scenarios = enumerate_scenarios(active_candidates)
    if scenarios is None:
        return JointKellyResult(SolveStatus.WATCH, zero, zero, None, 0, 0,
                                "portfolio_state_space_too_large")

    probs = [sc.probability for sc in scenarios]
    returns = [sc.returns for sc in scenarios]
    caps = [caps_fraction[i] for i in active]
    solved = _barrier_solve(returns, probs, caps, budget, epsilon=solvency_epsilon)
    if solved is None:
        return JointKellyResult(SolveStatus.WATCH, zero, zero, None, 0, len(scenarios),
                                "kelly_solver_did_not_converge")
    reduced, gap = solved

    # Fractional Kelly shrinks the full-Kelly solution; re-clip to the caps and
    # the total budget so shrinking can never breach a limit.
    reduced = [min(max(0.0, multiplier * value), caps[i]) for i, value in enumerate(reduced)]
    total = sum(reduced)
    if total > budget and total > 0:
        reduced = [value * budget / total for value in reduced]
    reduced = [0.0 if value < _ZERO_FRACTION else value for value in reduced]

    fractions = zero[:]
    for slot, i in enumerate(active):
        fractions[i] = reduced[slot]
    stakes = [f * bankroll for f in fractions]
    return JointKellyResult(SolveStatus.OPTIMAL, stakes, fractions, gap, 0,
                            len(scenarios), "kkt-optimal")


def joint_kelly_stakes(
    candidates: list[Candidate], bankroll: float, *,
    kelly_multiplier: float = 1.0, max_total_fraction: float = 1.0,
) -> list[float]:
    """Per-candidate stake in dollars that maximizes constrained expected log
    wealth over the exact joint outcomes.

    Each stake is non-negative, at most its ``cap``, and the total is at most
    ``max_total_fraction`` of ``bankroll``. ``kelly_multiplier`` shrinks the full
    Kelly solution (e.g. 0.5 for half-Kelly). Returns all zeros when the solver
    fails closed (WATCH); callers that need to distinguish that from a genuine
    zero-edge allocation should use :func:`solve_joint_kelly`.
    """
    return solve_joint_kelly(
        candidates, bankroll,
        kelly_multiplier=kelly_multiplier, max_total_fraction=max_total_fraction,
    ).stakes
