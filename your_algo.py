from base import Exchange, Trade, Order, Product, Msg, Rest
from typing import List, Dict, Optional
import math

class PlayerAlgorithm:
    """
    Currently tracks position and order flow
    Set to quote both sides near the top of the order book
    Cancels and requotes every turn 
    Quotes based on how much inventory we hold currently (INV_SKEW_DIVISOR)
    - If we hold too many units and price drops we lose a lot
    - so skew prices to encourage trading in opposite direction
    - if long, lower buy price and raise sell price and vice versa
    Also quotes based on flow - people selling or buying more (FLOW TICKS + THRESHOLD)
    """

    # Tunables - keep adjusting to get better pnl
    # (I havent tested any iteration besides these parameters which gpt told me were good start)
    BASE_SIZE = 5                 # how much we quote per side per turn
    IMPROVE_TICKS = 1             # step inside book by min amount to get better priority 
    MAX_SKEW_TICKS = 5            # Risk control- if algo says quote 8 ticks down we cap it at 5
    INV_SKEW_DIVISOR = 50         # how much we skew is = position / INV_SKEW_DIVISOR
    # currently @ 1 tick skew per 50 units inventory. if we holding a lot then push sell and buy price down to offload faster

    FLOW_SKEW_TICKS = 1           # if flow +ve (aggressive buying) lean quotes in that dir and vice versa by min tick
    FLOW_STRONG_THRESHOLD = 20    # if abs(flow) < 20 ignore it (just picked 20 randomly)

    def __init__(self, products: List[Product]):
        self.products = products
        self.name = "tt3"            
        self.team_members = ["Theo, Luke"]   
        self.timestamp_num = 0

        # State
        self.pos: Dict[str, int] = {p.ticker: 0 for p in products} # start with 0 position
        self.last_mid: Dict[str, Optional[float]] = {p.ticker: None for p in products} # remember last mid
        self.last_flow: Dict[str, int] = {p.ticker: 0 for p in products}  
        self.live_orders: Dict[str, Dict[str, set]] = {
            p.ticker: {"Buy": set(), "Sell": set()} for p in products
        } # keep track of our orders

        # Will be set by set_idx() when round starts
        self.idx: int = 0

    # ticks + book reading
    @staticmethod
    def _floor_tick(price: float, mpv: float) -> float:
        return math.floor(price / mpv) * mpv

    @staticmethod
    def _ceil_tick(price: float, mpv: float) -> float:
        return math.ceil(price / mpv) * mpv

    @staticmethod
    def _book_sides(book_for_ticker: Dict[str, List[Rest]]):
        # can take bids / asks or bid /ask key
        bids = book_for_ticker.get("Bids") or book_for_ticker.get("Bid") or []
        asks = book_for_ticker.get("Asks") or book_for_ticker.get("Ask") or []
        return bids, asks

    def _best_bid_ask_mid(self, ticker: str, book) -> (Optional[float], Optional[float], Optional[float]):
        bids, asks = self._book_sides(book[ticker])
        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        if best_bid is not None and best_ask is not None:
            mid = 0.5 * (best_bid + best_ask)
        else:
            mid = self.last_mid[ticker]  # fall back to last mid if one side missing
        return best_bid, best_ask, mid

    # idx starter
    def set_idx(self, idx: int) -> None:
        self.idx = idx

    def create_order(self, ticker: str, size: int, price: float, direction: str) -> Msg:
        order_idx = self.idx
        new_order = Order(ticker=ticker, price=price, size=size, order_id=order_idx,
            agg_dir=direction, bot_name=self.name)
        msg = Msg("ORDER", new_order)
        # track our live orders so we can cancel/requote next turn
        self.live_orders[ticker][direction].add(order_idx)
        self.idx += 1
        return msg

    def remove_order(self, order_idx: int) -> Msg:
        return Msg("REMOVE", order_idx)

    def process_trades(self, trades: List[Trade]) -> None:
        """
        Update position & recent flow after each market turn
        Based on whether we were aggressor or resting side.
        -> flow counts net agg volume direction across ALL trades in the turn
        """
        flow = 0  # +ve if net aggressive BUY, -ve if net aggressive SELL
        for tr in trades:
            # Track position for our bot
            if tr.agg_bot == self.name:
                if tr.agg_dir == "Buy":
                    self.pos[tr.ticker] += tr.size
                else:  # we sell, subtract size
                    self.pos[tr.ticker] -= tr.size
            if tr.rest_bot == self.name:
                # Resting
                if tr.agg_dir == "Buy":
                    self.pos[tr.ticker] -= tr.size  # we sold to them
                else:
                    self.pos[tr.ticker] += tr.size  # we bought

            # net flow for all trades
            flow += tr.size if tr.agg_dir == "Buy" else -tr.size

        # flow is stored per ticker, right now only UEC ticker though
        for p in self.products:
            self.last_flow[p.ticker] = flow

    def send_messages(self, book: Dict[str, Dict[str, List[Rest]]]) -> List[Msg]:
        """
        Cancel any stale quotes, place fresh passive quotes on both sides,
        slightly improving/joining the top of book - skewed by inventory & flow
        """
        msgs: List[Msg] = []
        for p in self.products:
            tkr, mpv = p.ticker, p.mpv
            pos_limit = 200

            # cancel all our resting orders
            for side in ("Buy", "Sell"):
                for order_idx in list(self.live_orders[tkr][side]):
                    msgs.append(self.remove_order(order_idx))
                self.live_orders[tkr][side].clear()

            # read book, find best bid ask mid
            best_bid, best_ask, mid = self._best_bid_ask_mid(tkr, book)
            if mid is not None:
                self.last_mid[tkr] = mid

            # if dont have both sides for some reason, skip quoting this ticker (risk managemnet)
            if best_bid is None or best_ask is None or mid is None:
                continue

            # Calc inventory & flow skew (in ticks)
            inv_skew_ticks = int(self.pos[tkr] / self.INV_SKEW_DIVISOR)
            inv_skew_ticks = max(-self.MAX_SKEW_TICKS, min(self.MAX_SKEW_TICKS, inv_skew_ticks))
            flow_skew = 0
            f = self.last_flow[tkr]
            if abs(f) >= self.FLOW_STRONG_THRESHOLD:
                flow_skew = self.FLOW_SKEW_TICKS if f > 0 else -self.FLOW_SKEW_TICKS
            # Positive inv_skew_ticks means we long -> push quotes down to sell off
            total_skew = max(-self.MAX_SKEW_TICKS, min(self.MAX_SKEW_TICKS, inv_skew_ticks + flow_skew))

            # dont cross best bid/ask spread
            # improve/join best bid by IMPROVE_TICKS, then apply skew
            desired_bid = best_bid + mpv * self.IMPROVE_TICKS
            desired_ask = best_ask - mpv * self.IMPROVE_TICKS
            # Apply skew (Long will shift down, short up)
            desired_bid -= mpv * total_skew
            desired_ask -= mpv * total_skew

            max_bid = best_ask - mpv  # below best ask
            min_ask = best_bid + mpv  # above best bid
            bid_px = min(desired_bid, max_bid)
            ask_px = max(desired_ask, min_ask)

            # use helper fcns 
            bid_px = self._floor_tick(bid_px, mpv)
            ask_px = self._ceil_tick(ask_px, mpv)

            # clamp sizes (ensure fills cannot breach limits)
            curr_pos = self.pos[tkr]
            max_buy_cap = max(0, pos_limit - curr_pos)           
            max_sell_cap = max(0, curr_pos + pos_limit)          

            buy_size = min(self.BASE_SIZE, max_buy_cap)
            sell_size = min(self.BASE_SIZE, max_sell_cap)

            # place our orders
            if buy_size > 0:
                msgs.append(self.create_order(tkr, buy_size, bid_px, "Buy"))
            if sell_size > 0:
                msgs.append(self.create_order(tkr, sell_size, ask_px, "Sell"))

        self.timestamp_num += 1
        return msgs