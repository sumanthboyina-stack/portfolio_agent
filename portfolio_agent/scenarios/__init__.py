"""
Rebalance scenarios: frozen inputs → proposed trades → deterministic evaluation.

    prepare.prepare_inputs()   gathers holdings, prices, cash, limits, research   (I/O)
    propose.propose_trades()   suggests trades from the prepared context           (pure)
    engine.evaluate()          evaluates the trades the user selected              (pure, Decimal)

Scenarios never edit live holdings or place orders.
"""
