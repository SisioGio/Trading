import pandas as pd
import ccxt
import os
import dotenv
import math
from decimal import Decimal
dotenv.load_dotenv()

API_KEY = os.environ.get("BINANCE_KEY")
SECRET_KEY = os.environ.get("BINANCE_SECRET")
RISK_PER_TRADE = os.environ.get("RISK_PER_TRADE")
pd.set_option('display.max_columns', None)
class Broker():
    def __init__(self,initial_balance,risk_per_trade):
        self.initial_balance =  initial_balance
        self.current_balance = initial_balance
        self.symbol = None
        self.orders = {}
        self.risk_per_trade = risk_per_trade
        self.continue_ordering = True
        self.connect()
    def connect(self):
        self.log("CONNECTING TO EXCHANGE")
        binance = ccxt.binance({
            'apiKey': API_KEY,
            'secretKey': SECRET_KEY
        })
        binance.load_markets()
        self.exchange = binance

    def get_balance(self):
        balance = self.exchange.fetch_balance()
        # Check the balance for USDC
        usdc_balance = balance['total'].get('USDC', 0)
        return usdc_balance
    
    def log(self,txt):
        print(f'[[{self.symbol}]:{txt}')

    def fetch_ohlcv(self,  timeframe='1m', limit=1000):
        self.log("LOADING DATA")
        ohlcv = self.exchange.fetch_ohlcv(self.symbol, timeframe=timeframe, limit=limit)
        
        # Convert OHLCV data into a Pandas DataFrame
        ohlcv_df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        # Convert timestamp to a readable datetime format
        ohlcv_df['timestamp'] = pd.to_datetime(ohlcv_df['timestamp'], unit='ms')

        return ohlcv_df
    
    def calculate_rsi(self,df, period=14):
        # Calculate the price change
        delta = df['close'].diff()
        
        # Separate the gains and losses
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        
        # Calculate the rolling average of gains and losses
        avg_gain = gain.rolling(window=period, min_periods=1).mean()
        avg_loss = loss.rolling(window=period, min_periods=1).mean()
        
        # Calculate the Relative Strength (RS)
        rs = avg_gain / avg_loss
        
        # Calculate the RSI
        rsi = 100 - (100 / (1 + rs))
        
        return rsi

    def calculate_macd(self,df):
        # Calculate MACD (12, 26, 9)
        short_ema = df['close'].ewm(span=12, adjust=False).mean()
        long_ema = df['close'].ewm(span=26, adjust=False).mean()
        macd = short_ema - long_ema
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        lowest_macd = macd.rolling(window=500, min_periods=1).min()
        lowest_macd.where(lowest_macd <= 0, 0)
        
        highest_macd = macd.rolling(window=500, min_periods=1).max()
        highest_macd.where(highest_macd >= 0, 0)

        macd_range = highest_macd - lowest_macd
        macd_weight = round(100 * (macd / abs(macd_range)))
        df['MACD_RANGE'] = macd_range
        df['MACD'] = macd
        df['MACD_SIGNAL'] = macd_signal
        df['LOWEST_MACD'] = lowest_macd
        df['MACD_WEIGHT'] = abs(macd_weight)
        df['MACD_CROSS_BULLISH'] = ((macd >= macd_signal) & (macd <0) & (macd_signal <0) & (macd_weight>40))
        df['MACD_CROSS_BEARISH'] = ((macd <= macd_signal) & (macd >0) & (macd_signal >0) & (macd_weight>40))
        df['BUY'] = ((macd > macd_signal) & (macd <0) & (macd_signal <0) & (macd_weight>40))
        df['SL'] = df['close'].rolling(window=10,min_periods=1).min()
        df['SL'] = df['SL']*0.98

        print(df.tail(20))

    def calculate_indicators(self,df):
        rsi = self.calculate_rsi(df)
        df['RSI'] = rsi
        self.calculate_macd(df)
        return df

    def buy_signal(self,df):
        rsi = df['RSI'].iloc[-1]
        return rsi<30
    def sell_signal(self,df):
        current_price = self.get_current_price()
        order = self.orders[self.symbol]
        tp = order['TP']
        sl = order['SL']
        if current_price <= sl:
            self.log("Stop-loss reached. Stop ordering.")
            self.continue_ordering = False
        rsi = df['RSI'].iloc[-1] or current_price >= tp or current_price <= sl
        return rsi>70
    


    def get_current_price(self):
        ticker = self.exchange.fetch_ticker(self.symbol)
        current_price = ticker['last']
        return current_price
    
    def adjust_quantity(self, quantity):
    
        market = self.exchange.market(self.symbol)

        # Extract LOT_SIZE filter from market['info']['filters']
        filters = market['info']['filters']
        lot_size_filter = next(f for f in filters if f['filterType'] == 'LOT_SIZE')

        # Convert all relevant values to Decimal for precision
        step_size = Decimal(str(lot_size_filter['stepSize']))  # Smallest quantity increment
        min_qty = Decimal(str(lot_size_filter['minQty']))      # Minimum allowable quantity
        quantity = Decimal(str(quantity))                 

        # Truncate the quantity to the nearest stepSize
        adjusted_quantity = (quantity // step_size) * step_size

        # Ensure the quantity is within allowed bounds
        if adjusted_quantity < min_qty:
            raise Exception("Quantity below the min.")
        return adjusted_quantity

    def sync_order_data(self,order):
        order_details = self.exchange.fetch_order(order['id'], self.symbol)
        avg_unit_price = order_details.get('average',None)
        if avg_unit_price is None:
            avg_unit_price = order_details.get('price',None)
        order_details['Price'] = avg_unit_price

    def buy_order(self):
        budget = self.current_balance * self.risk_per_trade
        current_price = self.get_current_price()
        quantity = budget/current_price
        quantity = float(self.adjust_quantity(quantity))
        order = {'id':1,'Qty':quantity,'Price':current_price,'SL':current_price*.99,'TP':current_price*1.02}
        actual_orders = self.orders
        actual_orders[self.symbol] = order
        self.orders = actual_orders
        self.log(f'Buy order placed: {order}')
        self.current_balance = self.current_balance-((current_price)*float(quantity))
        self.log(f'New balance is {self.current_balance}')

    def sell_order(self):
        existing_order = self.orders[self.symbol]
        current_price = self.get_current_price()
        quantity = existing_order['Qty']
        revenue = float(quantity) * current_price
        
        actual_orders = self.orders
        del actual_orders[self.symbol]
        self.orders = actual_orders
        self.log(f'Sell order placed')
        self.current_balance = self.current_balance+revenue
        self.log(f'New balance is {self.current_balance}')

    def run_logic(self,symbol):
        self.symbol = symbol
        self.log('RUNNING LOGIC')
        df = self.fetch_ohlcv()
        df = self.calculate_indicators(df)

        return None
        if self.current_balance < self.initial_balance/10:
            self.log(f'Balance is too low ({self.current_balance})')
            return
        if not self.symbol in self.orders:
            if self.buy_signal(df):
                self.buy_order()
        else:
            if self.sell_signal(df):
                self.sell_order()
                if self.continue_ordering:
                    self.buy_order()
            else:
                self.log("Order exists but no sell signal.")


   



def get_current_price(symbol):
    price = binance.fetch_ticker('BTC/USDT')

    return price['last']


if __name__ == "__main__":
    print("Running")
    symbols = ['BTC/USDT','LINK/USDT','XRP/USDT','UNI/USDT']
    exchange = Broker(100,0.1)
    exchange.run_logic('UNI/USDT')
   

    # while True:
    #     for symbol in symbols:
    #         exchange.run_logic(symbol)