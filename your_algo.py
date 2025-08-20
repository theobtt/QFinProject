from base import Exchange, Trade, Order, Product, Msg, Rest
from typing import List, Dict
import math


class PlayerAlgorithm:
    # tunables to adjust to get better pnl
    BASE_SIZE = 5 # how much we quote per side per turn
    IMPROVE_TICKS = 1 # step inside book by min amount to get better priority 
    FLOW_SKEW_TICKS = 1 # if flow +ve (aggressive buying) lean quotes in that dir and vice versa by min tick
    FLOW_STRONG_THRESHOLD = 20 # if abs(flow) < 20 ignore it (just picked 20 randomly)

    def __init__(self, products: List[Product]):
        self.products = products
        self.name = "Philips greatest soldiers"
        self.pos: Dict[str, int] = {p.ticker: 0 for p in products}
        self.last_flow: Dict[str, int] = {p.ticker: 0 for p in products} # last turn net flow
        # remember live orders to cancel next turn
        self.live_orders: Dict[str, Dict[str, set]] = {p.ticker: {"Buy": set(), "Sell": set()} for p in products}
        self.idx = 0

    def set_idx(self, idx: int) -> None:
        self.idx = idx

        # edited to try to cancel orders each turn
    def create_order(self, ticker: str, size: int, price: float, direction: str) -> Msg:
        order_idx = self.idx
        new_order = Order(ticker=ticker, price=price, size=size, order_id=order_idx,
                          agg_dir=direction, bot_name=self.name)
        new_message = Msg("ORDER", new_order)
        # to record live orders for cancellation
        self.live_orders[ticker][direction].add(order_idx)
        self.idx += 1
        return new_message

    def remove_order(self, order_idx):
        new_message = Msg("REMOVE", order_idx)
        return new_message

        # sums aggressive buys and sells across whole market last turn, and stores it for decision
    def process_trades(self, trades: List[Trade]) -> None:
        flow = 0
        for tr in trades:
            # update our position based on whether we were agressor or resting
            if tr.agg_bot == self.name:
                self.pos[tr.ticker] += tr.size if tr.agg_dir == "Buy" else -tr.size
            elif tr.rest_bot == self.name:
                self.pos[tr.ticker] -= tr.size if tr.agg_dir == "Buy" else tr.size
            flow += tr.size if tr.agg_dir == "Buy" else -tr.size
        for p in self.products:
            self.last_flow[p.ticker] = flow


    def send_messages(self, book: Dict[str, Dict[str, List[Rest]]]) -> List[Msg]:
        msgs: List[Msg] = []
        for p in self.products:
            tkr, mpv = p.ticker, p.mpv

            # cancel all our resting orders
            for side in ("Buy", "Sell"):
                for order_idx in list(self.live_orders[tkr][side]):
                    msgs.append(self.remove_order(order_idx))
                self.live_orders[tkr][side].clear()

            # read top of book
            bids = book[tkr].get("Bids") or []
            asks = book[tkr].get("Asks") or []
            if not bids or not asks:
                continue  
            # Only take the best (most aggressive) price
            best_bid = bids[0].price
            best_ask = asks[0].price

            # skew by flow on last turn: if the last cycle had strong buying, shift both quotes up a tick
            # strong selling shift down a tick, else no tick
            skew = 0
            f = self.last_flow[tkr]
            if abs(f) >= self.FLOW_STRONG_THRESHOLD:
                skew = self.FLOW_SKEW_TICKS if f > 0 else -self.FLOW_SKEW_TICKS

            # Step inside top of book by IMPROVETICKS (currently 1), then apply skew
            bid_px = min(best_bid + mpv * (self.IMPROVE_TICKS + skew), best_ask - mpv)
            ask_px = max(best_ask - mpv * (self.IMPROVE_TICKS - skew), best_bid + mpv)

            # stay passive (dont cross the spread) bid stay below best ask and ask above the best bid
            bid_px = math.floor(bid_px / mpv) * mpv
            ask_px = math.ceil(ask_px / mpv) * mpv

            # currently placing one order (BASE SIZE) on both sides
            msgs.append(self.create_order(tkr, self.BASE_SIZE, bid_px, "Buy"))
            msgs.append(self.create_order(tkr, self.BASE_SIZE, ask_px, "Sell"))
        return msgs

