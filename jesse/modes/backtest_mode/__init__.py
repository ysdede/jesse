import time
from typing import Dict, Union

import arrow
import click
import numpy as np
import pandas as pd

import jesse.helpers as jh
import jesse.services.metrics as stats
import jesse.services.required_candles as required_candles
import jesse.services.selectors as selectors
import jesse.services.table as table
from jesse import exceptions
from jesse.config import config
from jesse.enums import timeframes, order_types, order_roles, order_flags
from jesse.models import Candle, Order, Position
from jesse.modes.utils import save_daily_portfolio_balance
from jesse.routes import router
from jesse.services import charts
from jesse.services import logger
from jesse.services import quantstats
from jesse.services import report
from jesse.services.cache import cache
from jesse.services.candle import generate_candle_from_one_minutes, print_candle, candle_includes_price, split_candle
from jesse.services.file import store_logs
from jesse.services.validators import validate_routes
from jesse.store import store


def run(start_date: str, finish_date: str, candles: Dict[str, Dict[str, Union[str, np.ndarray]]] = None,
        chart: bool = False, tradingview: bool = False, full_reports: bool = False,
        csv: bool = False, json: bool = False) -> None:
    # clear the screen
    if not jh.should_execute_silently():
        click.clear()

    # validate routes
    validate_routes(router)

    # initiate candle store
    store.candles.init_storage(5000)

    # load historical candles
    if candles is None:
        print('loading candles...')
        candles = load_candles(start_date, finish_date)
        click.clear()

    if not jh.should_execute_silently():
        # print candles table
        key = f"{config['app']['considering_candles'][0][0]}-{config['app']['considering_candles'][0][1]}"
        table.key_value(stats.candles(candles[key]['candles']), 'candles', alignments=('left', 'right'))
        print('\n')

        # print routes table
        table.multi_value(stats.routes(router.routes))
        print('\n')

        # print guidance for debugging candles
        if jh.is_debuggable('trading_candles') or jh.is_debuggable('shorter_period_candles'):
            print('     Symbol  |     timestamp    | open | close | high | low | volume')

    # run backtest simulation
    simulator(candles)

    if not jh.should_execute_silently():
        # print trades metrics
        if store.completed_trades.count > 0:

            change = []
            # calcualte market change
            for e in router.routes:
                if e.strategy is None:
                    return

                first = Candle.select(
                    Candle.close
                ).where(
                    Candle.timestamp == jh.date_to_timestamp(start_date),
                    Candle.exchange == e.exchange,
                    Candle.symbol == e.symbol
                ).first()
                last = Candle.select(
                    Candle.close
                ).where(
                    Candle.timestamp == jh.date_to_timestamp(finish_date) - 60000,
                    Candle.exchange == e.exchange,
                    Candle.symbol == e.symbol
                ).first()

                change.append(((last.close - first.close) / first.close) * 100.0)

            data = report.portfolio_metrics()
            data.append(['Market Change', f"{str(round(np.average(change), 2))}%"])
            print('\n')
            table.key_value(data, 'Metrics', alignments=('left', 'right'))
            print('\n')

            # save logs
            more = ""
            routes_count = len(router.routes)
            if routes_count > 1:
                more = f"-and-{routes_count - 1}-more"

            study_name = f"{router.routes[0].strategy_name}-{router.routes[0].exchange}-{router.routes[0].symbol}-{router.routes[0].timeframe}{more}-{start_date}-{finish_date}"
            store_logs(study_name, json, tradingview, csv)

            if chart:
                charts.portfolio_vs_asset_returns(study_name)

            # QuantStats' report
            if full_reports:

                price_data = []

                # load close candles for Buy and hold and calculate pct_change
                for index, c in enumerate(config['app']['considering_candles']):
                    exchange, symbol = c[0], c[1]
                    if exchange in config['app']['trading_exchanges'] and symbol in config['app']['trading_symbols']:
                        # fetch from database
                        candles_tuple = Candle.select(
                            Candle.timestamp, Candle.close
                        ).where(
                            Candle.timestamp.between(jh.date_to_timestamp(start_date),
                                                     jh.date_to_timestamp(finish_date) - 60000),
                            Candle.exchange == exchange,
                            Candle.symbol == symbol
                        ).order_by(Candle.timestamp.asc()).tuples()

                        candles = np.array(candles_tuple)

                        timestamps = candles[:, 0]
                        price_data.append(candles[:, 1])

                price_data = np.transpose(price_data)
                price_df = pd.DataFrame(price_data, index=pd.to_datetime(timestamps, unit="ms"), dtype=float).resample(
                    'D').mean()
                price_pct_change = price_df.pct_change(1).fillna(0)
                bh_daily_returns_all_routes = price_pct_change.mean(1)
                quantstats.quantstats_tearsheet(bh_daily_returns_all_routes, study_name)
        else:
            print(jh.color('No trades were made.', 'yellow'))


def load_candles(start_date_str: str, finish_date_str: str) -> Dict[str, Dict[str, Union[str, np.ndarray]]]:
    start_date = jh.date_to_timestamp(start_date_str)
    finish_date = jh.date_to_timestamp(finish_date_str) - 60000

    # validate
    if start_date == finish_date:
        raise ValueError('start_date and finish_date cannot be the same.')
    if start_date > finish_date:
        raise ValueError('start_date cannot be bigger than finish_date.')
    if finish_date > arrow.utcnow().int_timestamp * 1000:
        raise ValueError("Can't load candle data from the future!")

    # load and add required warm-up candles for backtest
    if jh.is_backtesting():
        for c in config['app']['considering_candles']:
            required_candles.inject_required_candles_to_store(
                required_candles.load_required_candles(c[0], c[1], start_date_str, finish_date_str),
                c[0],
                c[1]
            )

    # download candles for the duration of the backtest
    candles = {}
    for c in config['app']['considering_candles']:
        exchange, symbol = c[0], c[1]

        key = jh.key(exchange, symbol)

        cache_key = f"{start_date_str}-{finish_date_str}-{key}"
        cached_value = cache.get_value(cache_key)
        # if cache exists
        if cached_value:
            candles_tuple = cached_value
        # not cached, get and cache for later calls in the next 5 minutes
        else:
            # fetch from database
            candles_tuple = Candle.select(
                Candle.timestamp, Candle.open, Candle.close, Candle.high, Candle.low,
                Candle.volume
            ).where(
                Candle.timestamp.between(start_date, finish_date),
                Candle.exchange == exchange,
                Candle.symbol == symbol
            ).order_by(Candle.timestamp.asc()).tuples()

        # validate that there are enough candles for selected period
        required_candles_count = (finish_date - start_date) / 60_000
        if len(candles_tuple) == 0 or candles_tuple[-1][0] != finish_date or candles_tuple[0][0] != start_date:
            raise exceptions.CandleNotFoundInDatabase(
                f'Not enough candles for {symbol}. Try running "jesse import-candles"')
        elif len(candles_tuple) != required_candles_count + 1:
            raise exceptions.CandleNotFoundInDatabase(
                f'There are missing candles between {start_date_str} => {finish_date_str}')

        # cache it for near future calls
        cache.set_value(cache_key, tuple(candles_tuple), expire_seconds=60 * 60 * 24 * 7)

        candles[key] = {
            'exchange': exchange,
            'symbol': symbol,
            'candles': np.array(candles_tuple)
        }

    return candles


def simulator(*args, **kwargs) -> None:
    yakir_simulator(*args, **kwargs)
    # original_simulator(*args, **kwargs)


def original_simulator(candles: Dict[str, Dict[str, Union[str, np.ndarray]]], hyperparameters: dict = None) -> None:
    begin_time_track = time.time()
    key = f"{config['app']['considering_candles'][0][0]}-{config['app']['considering_candles'][0][1]}"
    first_candles_set = candles[key]['candles']
    length = len(first_candles_set)
    # to preset the array size for performance
    store.app.starting_time = first_candles_set[0][0]
    store.app.time = first_candles_set[0][0]

    # initiate strategies
    for r in router.routes:
        StrategyClass = jh.get_strategy_class(r.strategy_name)

        try:
            r.strategy = StrategyClass()
        except TypeError:
            raise exceptions.InvalidStrategy(
                "Looks like the structure of your strategy directory is incorrect. Make sure to include the strategy INSIDE the __init__.py file."
                "\nIf you need working examples, check out: https://github.com/jesse-ai/example-strategies"
            )
        except:
            raise

        r.strategy.name = r.strategy_name
        r.strategy.exchange = r.exchange
        r.strategy.symbol = r.symbol
        r.strategy.timeframe = r.timeframe

        # inject hyper parameters (used for optimize_mode)
        # convert DNS string into hyperparameters
        if r.dna and hyperparameters is None:
            hyperparameters = jh.dna_to_hp(r.strategy.hyperparameters(), r.dna)

        # inject hyperparameters sent within the optimize mode
        if hyperparameters is not None:
            r.strategy.hp = hyperparameters

        # init few objects that couldn't be initiated in Strategy __init__
        # it also injects hyperparameters into self.hp in case the route does not uses any DNAs
        r.strategy._init_objects()

        selectors.get_position(r.exchange, r.symbol).strategy = r.strategy

    # add initial balance
    save_daily_portfolio_balance()

    with click.progressbar(length=length, label='Executing simulation...') as progressbar:
        for i in range(length):
            # update time
            store.app.time = first_candles_set[i][0] + 60_000
            # add candles
            for j in candles:

                short_candle = candles[j]['candles'][i]
                if i != 0:
                    previous_short_candle = candles[j]['candles'][i - 1]
                    short_candle = _get_fixed_jumped_candle(previous_short_candle, short_candle)
                exchange = candles[j]['exchange']
                symbol = candles[j]['symbol']

                store.candles.add_candle(short_candle, exchange, symbol, '1m', with_execution=False,
                                         with_generation=False)

                # print short candle
                if jh.is_debuggable('shorter_period_candles'):
                    print_candle(short_candle, True, symbol)

                _simulate_price_change_effect(short_candle, exchange, symbol)

                # generate and add candles for bigger timeframes
                for timeframe in config['app']['considering_timeframes']:
                    # for 1m, no work is needed
                    if timeframe == '1m':
                        continue

                    count = jh.timeframe_to_one_minutes(timeframe)

                    if (i + 1) % count == 0:
                        generated_candle = generate_candle_from_one_minutes(
                            timeframe,
                            candles[j]['candles'][(i - (count - 1)):(i + 1)])
                        store.candles.add_candle(generated_candle, exchange, symbol, timeframe, with_execution=False,
                                                 with_generation=False)

            # update progressbar
            if not jh.is_debugging() and not jh.should_execute_silently() and i % 60 == 0:
                progressbar.update(60)

            # now that all new generated candles are ready, execute
            for r in router.routes:
                count = jh.timeframe_to_one_minutes(r.timeframe)
                # 1m timeframe
                if r.timeframe == timeframes.MINUTE_1:
                    r.strategy._execute()
                elif (i + 1) % count == 0:
                    # print candle
                    if jh.is_debuggable('trading_candles'):
                        print_candle(store.candles.get_current_candle(r.exchange, r.symbol, r.timeframe), False,
                                     r.symbol)
                    r.strategy._execute()

            # now check to see if there's any MARKET orders waiting to be executed
            store.orders.execute_pending_market_orders()

            if i != 0 and i % 1440 == 0:
                save_daily_portfolio_balance()

    if not jh.should_execute_silently():
        if jh.is_debuggable('trading_candles') or jh.is_debuggable('shorter_period_candles'):
            print('\n')

        # print executed time for the backtest session
        finish_time_track = time.time()
        print('Executed backtest simulation in: ', f'{round(finish_time_track - begin_time_track, 2)} seconds')

    for r in router.routes:
        r.strategy._terminate()
        store.orders.execute_pending_market_orders()

    # now that backtest is finished, add finishing balance
    save_daily_portfolio_balance()


def yakir_simulator(candles: Dict[str, Dict[str, Union[str, np.ndarray]]], hyperparameters: dict = None) -> None:
    begin_time_track = time.time()
    key = f"{config['app']['considering_candles'][0][0]}-{config['app']['considering_candles'][0][1]}"
    first_candles_set = candles[key]['candles']
    length = len(first_candles_set)
    # to preset the array size for performance
    store.app.starting_time = first_candles_set[0][0]
    store.app.time = first_candles_set[0][0]

    # initiate strategies
    min_timeframe = _initialized_strategies(hyperparameters)

    # add initial balance
    save_daily_portfolio_balance()

    with click.progressbar(length=length, label='Executing simulation...') as progressbar:
        i = min_timeframe_remainder = skip = min_timeframe
        # i is the i'th candle, which means that the first candle is i=1 etc..

        while i <= length:
            # update time
            store.app.time = first_candles_set[i - 1][0] + 60_000

            # add candles
            for j in candles:

                short_candles = candles[j]['candles'][i - skip: i]
                # remove previous_short_candle fix
                exchange = candles[j]['exchange']
                symbol = candles[j]['symbol']

                store.candles.add_candle(short_candles, exchange, symbol, '1m', with_execution=False,
                                         with_generation=False)

                # print short candle
                if jh.is_debuggable('shorter_period_candles'):
                    print_candle(short_candles[-1], True, symbol)

                current_temp_candle = generate_candle_from_one_minutes('',
                                                                       short_candles,
                                                                       accept_forming_candles=True)

                # in this new prices update there might be an order that needs to be executed
                _simulate_price_change_effect(current_temp_candle, exchange, symbol)

                # generate and add candles for bigger timeframes
                for timeframe in config['app']['considering_timeframes']:
                    # for 1m, no work is needed
                    if timeframe == '1m':
                        continue

                    count = jh.timeframe_to_one_minutes(timeframe)

                    if i % count == 0:
                        generated_candle = generate_candle_from_one_minutes(
                            timeframe,
                            candles[j]['candles'][i - count:i])
                        store.candles.add_candle(generated_candle, exchange, symbol, timeframe, with_execution=False,
                                                 with_generation=False)

            # update progressbar
            if not jh.is_debugging() and not jh.should_execute_silently():
                progressbar.update(skip)

            # now that all new generated candles are ready, execute
            _execute_candles(i)

            if i % 1440 == 0:
                save_daily_portfolio_balance()
            # todo: make this method return the next candle
            skip = _skip_n_candles(candles, min_timeframe_remainder, i)
            if skip < min_timeframe_remainder:
                min_timeframe_remainder -= skip
            elif skip == min_timeframe_remainder:
                min_timeframe_remainder = min_timeframe
            i += skip

    _finish_simulation(begin_time_track)


def _initialized_strategies(hyperparameters: dict = None):
    min_timeframe = jh.timeframe_to_one_minutes(timeframes.WEEK_1)
    for r in router.routes:
        StrategyClass = jh.get_strategy_class(r.strategy_name)

        try:
            r.strategy = StrategyClass()
        except TypeError:
            raise exceptions.InvalidStrategy(
                "Looks like the structure of your strategy directory is incorrect. "
                "Make sure to include the strategy INSIDE the __init__.py file.\n"
                "If you need working examples, check out: https://github.com/jesse-ai/example-strategies"
            )

        r.strategy.name = r.strategy_name
        r.strategy.exchange = r.exchange
        r.strategy.symbol = r.symbol
        r.strategy.timeframe = r.timeframe
        min_timeframe = min(min_timeframe, jh.timeframe_to_one_minutes(r.timeframe))
        # inject hyper parameters (used for optimize_mode)
        # convert DNS string into hyperparameters
        if r.dna and hyperparameters is None:
            hyperparameters = jh.dna_to_hp(r.strategy.hyperparameters(), r.dna)

        # inject hyperparameters sent within the optimize mode
        if hyperparameters is not None:
            r.strategy.hp = hyperparameters

        # init few objects that couldn't be initiated in Strategy __init__
        # it also injects hyperparameters into self.hp in case the route does not uses any DNAs
        r.strategy._init_objects()

        selectors.get_position(r.exchange, r.symbol).strategy = r.strategy

    for e in router.extra_candles:
        min_timeframe = min(min_timeframe, jh.timeframe_to_one_minutes(e[2]))

    return min_timeframe


def _execute_candles(i: int):
    for r in router.routes:
        count = jh.timeframe_to_one_minutes(r.timeframe)
        if i % count == 0:
            # print candle
            if jh.is_debuggable('trading_candles'):
                print_candle(store.candles.get_current_candle(r.exchange, r.symbol, r.timeframe), False,
                             r.symbol)
            r.strategy._execute()

    # now check to see if there's any MARKET orders waiting to be executed
    store.orders.execute_pending_market_orders()


def _finish_simulation(begin_time_track: float):
    if not jh.should_execute_silently():
        if jh.is_debuggable('trading_candles') or jh.is_debuggable('shorter_period_candles'):
            print('\n')

        # print executed time for the backtest session
        finish_time_track = time.time()
        print('Executed backtest simulation in: ', f'{round(finish_time_track - begin_time_track, 2)} seconds')

    for r in router.routes:
        r.strategy._terminate()
        store.orders.execute_pending_market_orders()

    # now that backtest is finished, add finishing balance
    save_daily_portfolio_balance()


def _get_fixed_jumped_candle(previous_candle: np.ndarray, candle: np.ndarray) -> np.ndarray:
    """
    A little workaround for the times that the price has jumped and the opening
    price of the current candle is not equal to the previous candle's close!

    :param previous_candle: np.ndarray
    :param candle: np.ndarray
    """
    if previous_candle[2] < candle[1]:
        candle[1] = previous_candle[2]
        candle[4] = min(previous_candle[2], candle[4])
    elif previous_candle[2] > candle[1]:
        candle[1] = previous_candle[2]
        candle[3] = max(previous_candle[2], candle[3])

    return candle


def _skip_n_candles(candles, max_skip: int, i: int) -> int:
    """
    calculate how many 1 minute candles can be skipped by checking if the next candles
    will execute limit and stop orders

    Use binary search to find an interval that only 1 or 0 orders execution is needed
    :param candles: np.ndarray - array of the whole 1 minute candles
    :max_skip: int - the interval that not matter if there is an order to be updated or not.
    :i: int - the current candle that should be executed

    :return: int - the size of the candles in minutes needs to skip
    """
    while True:
        orders_counter = 0
        for r in router.routes:
            if store.orders.count_active_orders(r.exchange, r.symbol) < 2:
                continue

            orders = store.orders.get_orders(r.exchange, r.symbol)
            future_candles = candles[f'{r.exchange}-{r.symbol}']['candles']
            if i >= len(future_candles):
                # if there is a problem with i or with the candles it will raise somewhere else
                # for now it still satisfy the condition that no more than 2 orders will be execute in the next candle
                break

            current_temp_candle = generate_candle_from_one_minutes('',
                                                                   future_candles[i:i+max_skip],
                                                                   accept_forming_candles=True)

            for order in orders:
                if order.is_active and candle_includes_price(current_temp_candle, order.price):
                    orders_counter += 1

        if orders_counter < 2 or max_skip == 1:
            # no more than 2 orders that can interfere each other in this candle.
            # or the candle is 1 minute candle, so I cant reduce it to smaller interval :/
            break

        max_skip //= 2

    return max_skip


def _simulate_price_change_effect(real_candle: np.ndarray, exchange: str, symbol: str) -> None:
    orders = store.orders.get_orders(exchange, symbol)

    current_temp_candle = real_candle.copy()
    executed_order = False

    while True:
        if len(orders) == 0:
            executed_order = False
        else:
            for index, order in enumerate(orders):
                if index == len(orders) - 1 and not order.is_active:
                    executed_order = False

                if not order.is_active:
                    continue

                if candle_includes_price(current_temp_candle, order.price):
                    storable_temp_candle, current_temp_candle = split_candle(current_temp_candle, order.price)
                    store.candles.add_candle(
                        storable_temp_candle, exchange, symbol, '1m',
                        with_execution=False,
                        with_generation=False
                    )
                    p = selectors.get_position(exchange, symbol)
                    p.current_price = storable_temp_candle[2]

                    executed_order = True

                    order.execute()

                    # break from the for loop, we'll try again inside the while
                    # loop with the new current_temp_candle
                    break
                else:
                    executed_order = False

        if not executed_order:
            # add/update the real_candle to the store so we can move on
            store.candles.add_candle(
                real_candle, exchange, symbol, '1m',
                with_execution=False,
                with_generation=False
            )
            p = selectors.get_position(exchange, symbol)
            if p:
                p.current_price = real_candle[2]
            break

    _check_for_liquidations(real_candle, exchange, symbol)


def _check_for_liquidations(candle: np.ndarray, exchange: str, symbol: str) -> None:
    p: Position = selectors.get_position(exchange, symbol)

    if not p:
        return

    # for now, we only support the isolated mode:
    if p.mode != 'isolated':
        return

    if candle_includes_price(candle, p.liquidation_price):
        closing_order_side = jh.closing_side(p.type)

        # create the market order that is used as the liquidation order
        order = Order({
            'id': jh.generate_unique_id(),
            'symbol': symbol,
            'exchange': exchange,
            'side': closing_order_side,
            'type': order_types.MARKET,
            'flag': order_flags.REDUCE_ONLY,
            'qty': jh.prepare_qty(p.qty, closing_order_side),
            'price': p.bankruptcy_price,
            'role': order_roles.CLOSE_POSITION
        })

        store.orders.add_order(order)

        store.app.total_liquidations += 1

        logger.info(f'{p.symbol} liquidated at {p.liquidation_price}')

        order.execute()
