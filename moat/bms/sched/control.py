"""
Charge/discharge optimizer.
"""
from dataclasses import dataclass

from moat.util import attrdict
from ortools.linear_solver import pywraplp


@dataclass
class FutureData:
    """
    Collects projected data at some point in time.

    Prices are per Wh.
    """

    price_buy: float = 0.0
    price_sell: float = 0.0
    load: float = 0.0
    pv: float = 0.0


class Model:
    """
    Calculate optimum charge/discharge behavior based on
    minimizing cost / maximizing income.

    Initial input:
    * hardware model
    * future data points: list of FutureData items
    * periods per hour (default 1)
    plus
    * assumed cost of low battery (ongoing, default 0.0)
    * assumed cost of low battery (final, default 0.0)

    Solver input:
    * current charge level (SoC)

    Output (first period):
    * grid input (negative: send energy)
    * battery SoC at end of first period

    No other output is provided. Re-run the model with the actual SoC at
    the start of the next period.
    """

    def __init__(self, hardware, data, per_hour=1, chg_inter=0, chg_last=0):
        self.hardware = hardware
        self.data = data
        self.per_hour = per_hour
        self.chg_inter = chg_inter
        self.chg_last = chg_last

        self._setup()

    def _setup(self):
        hardware = self.hardware
        data = iter(self.data)
        per_hour = self.per_hour

        # ORtools
        self.solver = solver = pywraplp.Solver("B", pywraplp.Solver.GLOP_LINEAR_PROGRAMMING)
        self.objective = solver.Objective()

        # Starting battery charge
        self.cap_init = cap_prev = solver.NumVar(
            hardware.capacity * 0.05, hardware.capacity * 0.95, "b_init"
        )
        self.constr_init = solver.Constraint(0, 0)
        self.constr_init.SetCoefficient(self.cap_init, 1)

        self.constraints = c = attrdict()
        c.battery = []
        c.dc = []
        c.ac = []
        self.g_ins = []
        self.g_outs = []
        self.caps = []
        c.price = []
        self.moneys = []

        i = -1
        _pr = None
        while True:
            i += 1
            if i:
                # Attribute a fake monetary value of keeping the battery charged
                _pr.SetCoefficient(cap, self.chg_inter / hardware.capacity)

            # input constraints
            try:
                dt = next(data)
            except StopIteration:
                break
            if dt.price_buy == dt.price_sell:
                dt.price_buy *= 1.001
            elif dt.price_buy < dt.price_sell:
                raise ValueError(f"At {i}: buy {dt.price_buy} < sell {dt.price_sell} ??")

            # ### Variables to consider

            # future battery charge
            cap = solver.NumVar(hardware.capacity * hardware.batt_min_soc, hardware.capacity * hardware.batt_max_soc, f"b{i}")
            self.caps.append(cap)

            # battery charge/discharge
            b_chg = solver.NumVar(0, hardware.batt_max_chg / per_hour, f"bc{i}")
            b_dis = solver.NumVar(0, hardware.batt_max_dis / per_hour, f"bd{i}")

            # solar power input. We may not be able to take all
            s_in = solver.NumVar(0, dt.pv / per_hour, f"pv{i}")

            # inverter charge/discharge
            i_chg = solver.NumVar(0, hardware.inv_max_chg / per_hour, f"ic{i}")
            i_dis = solver.NumVar(0, hardware.inv_max_dis / per_hour, f"id{i}")

            # local load
            l_out = solver.NumVar(dt.load, dt.load / per_hour, f"ld{i}")

            # grid
            g_in = solver.NumVar(0, hardware.grid_max_in / per_hour, f"gi{i}")
            g_out = solver.NumVar(0, hardware.grid_max_out / per_hour, f"go{i}")
            self.g_ins.append(g_in)
            self.g_outs.append(g_out)

            # income to maximize
            money = solver.NumVar(-solver.infinity(), solver.infinity(), f"pr{i}")
            self.moneys.append(money)

            # ### Constraints (actually Relationships, as they're all equalities)

            # Battery charge. old + charge - discharge == new, so ??? - new == 0.
            _bt = solver.Constraint(0, 0)
            c.battery.append(_bt)
            _bt.SetCoefficient(cap_prev, 1)
            _bt.SetCoefficient(b_chg, hardware.batt_eff_chg)
            _bt.SetCoefficient(b_dis, -1)
            _bt.SetCoefficient(cap, -1)

            # DC power bar. power_in - power_out == zero.
            _dc = solver.Constraint(0, 0)
            c.dc.append(_dc)
            # Power in
            _dc.SetCoefficient(s_in, 1)
            _dc.SetCoefficient(b_dis, hardware.batt_eff_dis)
            _dc.SetCoefficient(i_chg, hardware.inv_eff_chg)
            # Power out
            _dc.SetCoefficient(b_chg, -1)
            _dc.SetCoefficient(i_dis, -1)

            # AC power bar. power_in - power_out == zero.
            _ac = solver.Constraint(0, 0)
            c.ac.append(_ac)
            # Power in
            _ac.SetCoefficient(g_in, 1)
            _ac.SetCoefficient(i_dis, hardware.inv_eff_dis)
            # Power out
            _ac.SetCoefficient(g_out, -1)
            _ac.SetCoefficient(l_out, -1)
            _ac.SetCoefficient(i_chg, -1)

            # Money earned: grid_out*price_sell - grid_in*price_buy == money, so ??? - money = zero.
            _pr = solver.Constraint(0, 0)
            c.price.append(_pr)
            _pr.SetCoefficient(g_out, dt.price_sell)
            _pr.SetCoefficient(g_in, -dt.price_buy)
            _pr.SetCoefficient(money, -1)

            self.objective.SetCoefficient(money, 1)
            cap_prev = cap
            if not i:
                self.g_in, self.g_out = g_in, g_out
                self.cap = cap
                self.money = money

        if _pr is not None:
            # Attribute a fake monetary value of ending with a charged battery
            _pr.SetCoefficient(cap, self.chg_last / hardware.capacity)

        self.objective.SetMaximization()

    def propose(self, charge):
        """
        Assuming that the current SoC is @charge, return
        - how much power to take from / -feed to the grid [W]
        - the SoC at the end of the current period [0???1]
        - this period's earnings / -cost [$$]

        """
        charge *= self.hardware.capacity
        self.constr_init.SetLb(charge)
        self.constr_init.SetUb(charge)

        self.solver.Solve()
        return (
            (self.g_in.solution_value() - self.g_out.solution_value()) * self.per_hour,
            self.cap.solution_value() / self.hardware.capacity,
            self.money.solution_value(),
        )

    def proposed(self, charge):
        """
        As "propose" but iterates over results
        """
        charge *= self.hardware.capacity
        self.constr_init.SetLb(charge)
        self.constr_init.SetUb(charge)

        self.solver.Solve()

        for g_in,g_out,cap,money in zip(self.g_ins,self.g_outs,self.caps,self.moneys):
            yield (
                (g_in.solution_value() - g_out.solution_value()) * self.per_hour,
                cap.solution_value() / self.hardware.capacity,
                money.solution_value(),
        )
