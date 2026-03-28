from flask import Flask, render_template, request, jsonify, session
from datetime import datetime
import psycopg
from psycopg.rows import dict_row
import os
import json
from dotenv import load_dotenv
import random

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-traditional-production')
if os.environ.get('RENDER') or os.environ.get('FLASK_ENV') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

db_initialized = False

@app.before_request
def initialize_once():
    global db_initialized
    if not db_initialized:
        init_db()
        init_stock_data()
        db_initialized = True

@app.route("/health")
def health():
    return "ok", 200

# Database connection
def get_db_connection():
    conn = psycopg.connect(
        os.environ.get('DATABASE_URL_TRADITIONAL', os.environ.get('DATABASE_URL')),
        row_factory=dict_row
    )
    return conn

# Initialize database tables (same as gamified)
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Users table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id SERIAL PRIMARY KEY,
            session_id VARCHAR(255) UNIQUE NOT NULL,
            platform_type VARCHAR(50) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            initial_cash DECIMAL(12, 2) DEFAULT 100000.00,
            current_cash DECIMAL(12, 2) DEFAULT 100000.00
        )
    ''')
    
    # Trades table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            trade_id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(user_id),
            session_id VARCHAR(255) NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            action VARCHAR(10) NOT NULL,
            shares INTEGER NOT NULL,
            price DECIMAL(10, 2) NOT NULL,
            total_cost DECIMAL(12, 2) NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Portfolio table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            portfolio_id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(user_id),
            session_id VARCHAR(255) NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            shares INTEGER NOT NULL,
            avg_price DECIMAL(10, 2) NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, symbol)
        )
    ''')
    
    # Clickstream table for behavioral tracking
    cur.execute('''
        CREATE TABLE IF NOT EXISTS clickstream (
            click_id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(user_id),
            session_id VARCHAR(255) NOT NULL,
            event_type VARCHAR(50) NOT NULL,
            event_data JSONB,
            page_url VARCHAR(255),
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Stock prices table (shared with gamified)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS stock_prices (
            symbol VARCHAR(10) PRIMARY KEY,
            company_name VARCHAR(100) NOT NULL,
            base_price DECIMAL(10, 2) NOT NULL,
            current_price DECIMAL(10, 2) NOT NULL,
            volatility VARCHAR(10) NOT NULL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cur.execute('CREATE INDEX IF NOT EXISTS idx_clickstream_session ON clickstream(session_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_clickstream_user ON clickstream(user_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_portfolio_session ON portfolio(session_id)')
    
    conn.commit()
    cur.close()
    conn.close()

def _mask_participant(session_id):
    if not session_id or len(session_id) < 4:
        return 'Account'
    return f'Account ····{session_id[-4:].upper()}'

def get_traditional_leaderboard_rows(cur, current_user_id):
    cur.execute('''
        WITH user_equity AS (
            SELECT
                u.user_id,
                u.session_id,
                (u.current_cash + COALESCE((
                    SELECT SUM(p.shares * sp.current_price)
                    FROM portfolio p
                    JOIN stock_prices sp ON p.symbol = sp.symbol
                    WHERE p.session_id = u.session_id
                ), 0))::numeric AS total_equity
            FROM users u
            WHERE u.platform_type = %s
        ),
        ranked AS (
            SELECT
                user_id,
                session_id,
                total_equity,
                ROW_NUMBER() OVER (ORDER BY total_equity DESC) AS rank
            FROM user_equity
        )
        SELECT user_id, session_id, total_equity, rank
        FROM ranked
        ORDER BY rank
        LIMIT 10
    ''', ('traditional',))
    top_rows = cur.fetchall()
    leaderboard = []
    for row in top_rows:
        te = float(row['total_equity'])
        ret_pct = ((te - 100000.0) / 100000.0) * 100.0
        rk = int(row['rank'])
        leaderboard.append({
            'rank': rk,
            'name': _mask_participant(row['session_id']),
            'returns': round(ret_pct, 2),
            'is_you': row['user_id'] == current_user_id
        })
    cur.execute('''
        WITH user_equity AS (
            SELECT
                u.user_id,
                u.session_id,
                (u.current_cash + COALESCE((
                    SELECT SUM(p.shares * sp.current_price)
                    FROM portfolio p
                    JOIN stock_prices sp ON p.symbol = sp.symbol
                    WHERE p.session_id = u.session_id
                ), 0))::numeric AS total_equity
            FROM users u
            WHERE u.platform_type = %s
        ),
        ranked AS (
            SELECT user_id, total_equity,
                   ROW_NUMBER() OVER (ORDER BY total_equity DESC) AS rank
            FROM user_equity
        )
        SELECT rank FROM ranked WHERE user_id = %s
    ''', ('traditional', current_user_id))
    rank_row = cur.fetchone()
    user_rank = int(rank_row['rank']) if rank_row else 1
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE platform_type = %s", ('traditional',))
    total_users = int(cur.fetchone()['c'])
    return leaderboard, user_rank, total_users

def init_user():
    if 'session_id' not in session:
        session['session_id'] = os.urandom(16).hex()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM users WHERE session_id = %s', (session['session_id'],))
    existing = cur.fetchone()
    if existing:
        session['user_id'] = existing['user_id']
    else:
        cur.execute('''
            INSERT INTO users (session_id, platform_type, initial_cash, current_cash)
            VALUES (%s, %s, %s, %s)
            RETURNING user_id
        ''', (session['session_id'], 'traditional', 100000.00, 100000.00))
        session['user_id'] = cur.fetchone()['user_id']
        conn.commit()
    cur.close()
    conn.close()

def log_event(event_type, event_data=None):
    if 'session_id' not in session:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO clickstream (user_id, session_id, event_type, event_data, page_url)
            VALUES (%s, %s, %s, %s, %s)
        ''', (
            session.get('user_id'),
            session['session_id'],
            event_type,
            json.dumps(event_data) if event_data else None,
            request.url
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Clickstream log error: {e}")

# Update stock prices with algorithmic volatility
def update_stock_prices():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Get all stocks
    cur.execute('SELECT symbol, base_price, volatility FROM stock_prices')
    stocks = cur.fetchall()
    
    for stock in stocks:
        symbol = stock['symbol']
        base_price = float(stock['base_price'])
        volatility = stock['volatility']
        
        # Determine volatility range
        if volatility == 'high':
            change_percent = random.uniform(-0.05, 0.05)  # ±5%
        elif volatility == 'medium':
            change_percent = random.uniform(-0.02, 0.02)  # ±2%
        else:  # low
            change_percent = random.uniform(-0.01, 0.01)  # ±1%
        
        # Calculate new price
        new_price = base_price * (1 + change_percent)
        new_price = round(new_price, 2)
        
        # Update in database
        cur.execute('''
            UPDATE stock_prices 
            SET current_price = %s, last_updated = CURRENT_TIMESTAMP
            WHERE symbol = %s
        ''', (new_price, symbol))
    
    conn.commit()
    cur.close()
    conn.close()

# Initialize stock data if not exists (same 20 stocks as gamified)
def init_stock_data():
    conn = get_db_connection()
    cur = conn.cursor()
    
    stocks = [
        ('AAPL', 'Apple Inc.', 178.50, 'medium'),
        ('MSFT', 'Microsoft Corporation', 378.50, 'medium'),
        ('GOOGL', 'Alphabet Inc.', 142.00, 'medium'),
        ('AMZN', 'Amazon.com Inc.', 151.25, 'medium'),
        ('META', 'Meta Platforms Inc.', 352.75, 'medium'),
        ('TSLA', 'Tesla Inc.', 242.50, 'high'),
        ('NVDA', 'NVIDIA Corporation', 478.00, 'high'),
        ('AMD', 'Advanced Micro Devices', 138.25, 'high'),
        ('JPM', 'JPMorgan Chase & Co.', 158.75, 'low'),
        ('BAC', 'Bank of America Corp.', 33.50, 'low'),
        ('WMT', 'Walmart Inc.', 168.25, 'low'),
        ('PG', 'Procter & Gamble Co.', 155.50, 'low'),
        ('JNJ', 'Johnson & Johnson', 157.75, 'low'),
        ('DIS', 'The Walt Disney Company', 96.50, 'medium'),
        ('NKE', 'Nike Inc.', 108.75, 'medium'),
        ('NFLX', 'Netflix Inc.', 442.50, 'high'),
        ('COST', 'Costco Wholesale Corp.', 588.25, 'low'),
        ('V', 'Visa Inc.', 258.50, 'low'),
        ('MA', 'Mastercard Inc.', 412.75, 'low'),
        ('PEP', 'PepsiCo Inc.', 172.50, 'low')
    ]
    
    for symbol, name, price, volatility in stocks:
        cur.execute('''
            INSERT INTO stock_prices (symbol, company_name, base_price, current_price, volatility)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (symbol) DO NOTHING
        ''', (symbol, name, price, price, volatility))
    
    conn.commit()
    cur.close()
    conn.close()

# Get current stock prices
def get_market_data():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''
        SELECT symbol, company_name, current_price, base_price, volatility
        FROM stock_prices
        ORDER BY symbol
    ''')
    stocks = cur.fetchall()
    
    cur.close()
    conn.close()
    
    market_data = []
    for stock in stocks:
        current = float(stock['current_price'])
        base = float(stock['base_price'])
        change = current - base
        change_percent = (change / base * 100) if base > 0 else 0
        
        # Calculate bid/ask spread (0.01-0.02% spread)
        spread = current * 0.0001
        bid = round(current - spread, 2)
        ask = round(current + spread, 2)
        
        market_data.append({
            'symbol': stock['symbol'],
            'name': stock['company_name'],
            'bid': bid,
            'ask': ask,
            'last': current,
            'change': change,
            'change_percent': change_percent,
            'volume': f"{random.randint(10, 250)}M"
        })
    
    return market_data

@app.route('/')
def index():
    init_user()
    init_stock_data()
    update_stock_prices()
    log_event('page_view', {'page': 'home'})
    
    session_id = session['session_id']
    user_id = session['user_id']
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Get user's current cash
    cur.execute('SELECT current_cash FROM users WHERE session_id = %s', (session_id,))
    user = cur.fetchone()
    current_cash = float(user['current_cash']) if user else 100000.00
    
    # Get portfolio
    cur.execute('''
        SELECT symbol, shares, avg_price 
        FROM portfolio 
        WHERE session_id = %s
    ''', (session_id,))
    portfolio_data = cur.fetchall()
    
    # Calculate portfolio value
    portfolio_value = current_cash
    positions = []
    market_data = get_market_data()
    
    for item in portfolio_data:
        stock = next((s for s in market_data if s['symbol'] == item['symbol']), None)
        if stock:
            market_value = item['shares'] * stock['last']
            cost_basis = item['shares'] * float(item['avg_price'])
            gain_loss = market_value - cost_basis
            gain_loss_percent = (gain_loss / cost_basis * 100) if cost_basis > 0 else 0
            
            portfolio_value += market_value
            
            positions.append({
                'symbol': item['symbol'],
                'shares': item['shares'],
                'avg_cost': float(item['avg_price']),
                'current_price': stock['last'],
                'market_value': market_value,
                'gain_loss': gain_loss,
                'gain_loss_percent': gain_loss_percent
            })
    
    cur.execute('SELECT COUNT(*) AS c FROM trades WHERE session_id = %s', (session_id,))
    trade_count = int(cur.fetchone()['c'])
    
    leaderboard, user_rank, total_participants = get_traditional_leaderboard_rows(cur, user_id)
    
    tchg_pct = ((portfolio_value - 100000.00) / 100000.00) * 100
    account_summary = {
        'total_value': portfolio_value,
        'cash_balance': current_cash,
        'buying_power': current_cash * 2,
        'today_change': portfolio_value - 100000.00,
        'today_change_percent': tchg_pct,
        'participant_rank': user_rank,
        'total_participants': total_participants,
        'trade_count': trade_count,
        'perf_bar_width': min(100, max(6, int(50 + max(-50, min(50, tchg_pct)))))
    }
    
    market_indices = [
        {'name': 'S&P 500', 'value': '5,234.18', 'chg_pct': '+0.42', 'up': True},
        {'name': 'NASDAQ', 'value': '16,479.32', 'chg_pct': '+0.58', 'up': True},
        {'name': 'DOW', 'value': '38,589.16', 'chg_pct': '-0.12', 'up': False},
        {'name': 'VIX', 'value': '14.82', 'chg_pct': '-2.31', 'up': False},
    ]
    
    # Get trade history
    cur.execute('''
        SELECT symbol, action as side, shares, price, total_cost as total, timestamp
        FROM trades
        WHERE session_id = %s
        ORDER BY timestamp DESC
        LIMIT 20
    ''', (session_id,))
    history = cur.fetchall()
    
    cur.close()
    conn.close()
    
    # Format history
    formatted_history = []
    for trade in history:
        formatted_history.append({
            'symbol': trade['symbol'],
            'side': trade['side'],
            'shares': trade['shares'],
            'price': float(trade['price']),
            'total': float(trade['total']),
            'timestamp': trade['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        })
    
    session_suffix = session_id[-4:].upper() if len(session_id) >= 4 else session_id.upper()
    return render_template('traditional.html',
                         account_summary=account_summary,
                         positions=positions,
                         market_data=market_data,
                         orders=[],
                         history=formatted_history,
                         leaderboard=leaderboard,
                         market_indices=market_indices,
                         session_suffix=session_suffix)

@app.route('/trade', methods=['POST'])
def trade():
    init_user()
    
    data = request.json
    symbol = data.get('symbol', '').upper()
    shares = int(data.get('shares', 0))
    action = data.get('action')
    
    log_event('trade_attempt', {
        'symbol': symbol,
        'shares': shares,
        'action': action
    })
    
    if not symbol or shares <= 0:
        return jsonify({'success': False, 'message': 'Invalid order parameters'})
    
    market_data = get_market_data()
    stock = next((s for s in market_data if s['symbol'] == symbol), None)
    if not stock:
        return jsonify({'success': False, 'message': 'Please select a symbol from the Market Data list'})
    
    session_id = session['session_id']
    price = stock['last']
    total_cost = shares * price
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('SELECT current_cash FROM users WHERE session_id = %s', (session_id,))
    user = cur.fetchone()
    current_cash = float(user['current_cash'])
    
    if action == 'buy':
        if total_cost > current_cash:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Insufficient funds'})
        
        # Update cash
        new_cash = current_cash - total_cost
        cur.execute('UPDATE users SET current_cash = %s WHERE session_id = %s', (new_cash, session_id))
        
        # Update portfolio
        cur.execute('SELECT shares, avg_price FROM portfolio WHERE session_id = %s AND symbol = %s', (session_id, symbol))
        existing = cur.fetchone()
        
        if existing:
            old_shares = existing['shares']
            old_avg = float(existing['avg_price'])
            new_shares = old_shares + shares
            new_avg = ((old_shares * old_avg) + (shares * price)) / new_shares
            
            cur.execute('''
                UPDATE portfolio 
                SET shares = %s, avg_price = %s, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = %s AND symbol = %s
            ''', (new_shares, new_avg, session_id, symbol))
        else:
            cur.execute('''
                INSERT INTO portfolio (user_id, session_id, symbol, shares, avg_price)
                VALUES (%s, %s, %s, %s, %s)
            ''', (session['user_id'], session_id, symbol, shares, price))
        
        # Record trade
        cur.execute('''
            INSERT INTO trades (user_id, session_id, symbol, action, shares, price, total_cost)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (session['user_id'], session_id, symbol, 'BUY', shares, price, total_cost))
        
        conn.commit()
        cur.close()
        conn.close()
        
        log_event('trade_completed', {
            'symbol': symbol,
            'shares': shares,
            'action': 'buy',
            'price': price,
            'total': total_cost
        })
        
        return jsonify({
            'success': True,
            'message': f'Order filled: Bought {shares} shares of {symbol} at ${price:.2f}',
            'cash': new_cash
        })
    
    elif action == 'sell':
        cur.execute('SELECT shares FROM portfolio WHERE session_id = %s AND symbol = %s', (session_id, symbol))
        portfolio_item = cur.fetchone()
        
        if not portfolio_item or portfolio_item['shares'] < shares:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Insufficient shares'})
        
        # Update cash
        new_cash = current_cash + total_cost
        cur.execute('UPDATE users SET current_cash = %s WHERE session_id = %s', (new_cash, session_id))
        
        # Update portfolio
        new_shares = portfolio_item['shares'] - shares
        if new_shares == 0:
            cur.execute('DELETE FROM portfolio WHERE session_id = %s AND symbol = %s', (session_id, symbol))
        else:
            cur.execute('''
                UPDATE portfolio 
                SET shares = %s, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = %s AND symbol = %s
            ''', (new_shares, session_id, symbol))
        
        # Record trade
        cur.execute('''
            INSERT INTO trades (user_id, session_id, symbol, action, shares, price, total_cost)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (session['user_id'], session_id, symbol, 'SELL', shares, price, total_cost))
        
        conn.commit()
        cur.close()
        conn.close()
        
        log_event('trade_completed', {
            'symbol': symbol,
            'shares': shares,
            'action': 'sell',
            'price': price,
            'total': total_cost
        })
        
        return jsonify({
            'success': True,
            'message': f'Order filled: Sold {shares} shares of {symbol} at ${price:.2f}',
            'cash': new_cash
        })
    
    cur.close()
    conn.close()
    return jsonify({'success': False, 'message': 'Invalid action'})

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
