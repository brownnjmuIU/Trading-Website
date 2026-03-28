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
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production-please')
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
        os.environ.get('DATABASE_URL_GAMIFIED', os.environ.get('DATABASE_URL')),
        row_factory=dict_row
    )
    return conn

# Initialize database tables
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
    
    # Clickstream table for detailed behavioral tracking
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
    
    # Stock prices table
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
    
    # Achievements table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS achievements (
            achievement_id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(user_id),
            session_id VARCHAR(255) NOT NULL,
            achievement_name VARCHAR(100) NOT NULL,
            unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, achievement_name)
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
        return 'Participant'
    return f'Participant ····{session_id[-4:].upper()}'

def get_gamified_leaderboard_rows(cur, current_user_id):
    """Real rankings for gamified users by total equity (cash + marked-to-market positions)."""
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
    ''', ('gamified',))
    top_rows = cur.fetchall()
    leaderboard = []
    for row in top_rows:
        te = float(row['total_equity'])
        ret_pct = ((te - 100000.0) / 100000.0) * 100.0
        cur.execute(
            'SELECT COUNT(*) AS c FROM trades WHERE session_id = %s',
            (row['session_id'],)
        )
        tc = cur.fetchone()['c']
        streak_display = min(int(tc), 99)
        rk = int(row['rank'])
        if rk == 1:
            badge_icon = 'fa-solid fa-medal'
            badge_class = 'medal-gold'
        elif rk == 2:
            badge_icon = 'fa-solid fa-medal'
            badge_class = 'medal-silver'
        elif rk == 3:
            badge_icon = 'fa-solid fa-medal'
            badge_class = 'medal-bronze'
        else:
            badge_icon = 'fa-solid fa-chart-line'
            badge_class = 'medal-default'
        leaderboard.append({
            'rank': rk,
            'name': _mask_participant(row['session_id']),
            'returns': round(ret_pct, 2),
            'streak': streak_display,
            'badge_icon': badge_icon,
            'badge_class': badge_class,
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
        SELECT rank, total_equity FROM ranked WHERE user_id = %s
    ''', ('gamified', current_user_id))
    rank_row = cur.fetchone()
    user_rank = int(rank_row['rank']) if rank_row else 1
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE platform_type = %s", ('gamified',))
    total_users = int(cur.fetchone()['c'])
    return leaderboard, user_rank, total_users

# Initialize session and user
def init_user():
    # Always ensure we have a session_id
    if 'session_id' not in session:
        session['session_id'] = os.urandom(16).hex()
        print(f"Created new session_id: {session['session_id']}")
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Check if user already exists for this session_id
    cur.execute('SELECT user_id FROM users WHERE session_id = %s', (session['session_id'],))
    existing_user = cur.fetchone()
    
    if existing_user:
        # User exists, just set the session variable
        session['user_id'] = existing_user['user_id']
        print(f"Found existing user_id: {session['user_id']} for session: {session['session_id']}")
    else:
        # Create new user in database
        print(f"Creating new user for session: {session['session_id']}")
        cur.execute('''
            INSERT INTO users (session_id, platform_type, initial_cash, current_cash)
            VALUES (%s, %s, %s, %s)
            RETURNING user_id
        ''', (session['session_id'], 'gamified', 100000.00, 100000.00))
        
        user = cur.fetchone()
        session['user_id'] = user['user_id']
        print(f"Created new user_id: {session['user_id']}")
        
        # Unlock only the $100K Portfolio achievement initially
        cur.execute('''
            INSERT INTO achievements (user_id, session_id, achievement_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (session_id, achievement_name) DO NOTHING
        ''', (session['user_id'], session['session_id'], '$100K Portfolio'))
        
        conn.commit()
        print(f"Committed user {session['user_id']} to database")
    
    cur.close()
    conn.close()
    
    # Double-check the user exists
    conn2 = get_db_connection()
    cur2 = conn2.cursor()
    cur2.execute('SELECT user_id FROM users WHERE user_id = %s', (session['user_id'],))
    verify = cur2.fetchone()
    cur2.close()
    conn2.close()
    
    if not verify:
        print(f"ERROR: User {session['user_id']} not found after init!")
        raise Exception(f"User {session['user_id']} does not exist in database")
    
    print(f"Verified user {session['user_id']} exists in database")

# Log clickstream event
def log_event(event_type, event_data=None):
    if 'session_id' not in session or 'user_id' not in session:
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
        # Silently fail if logging fails - don't break the app
        print(f"Logging error: {e}")
        pass

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

# Initialize stock data if not exists
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
        percent = (change / base * 100) if base > 0 else 0
        
        market_data.append({
            'symbol': stock['symbol'],
            'name': stock['company_name'],
            'price': current,
            'change': change,
            'percent': percent,
            'volume': f"{random.randint(10, 250)}M"
        })
    
    return market_data

# Get user's unlocked achievements
def get_user_achievements():
    if 'session_id' not in session:
        return []
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''
        SELECT achievement_name
        FROM achievements
        WHERE session_id = %s
    ''', (session['session_id'],))
    
    unlocked = [row['achievement_name'] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    all_achievements = [
        {'name': 'First Trade', 'icon': 'fa-solid fa-bullseye', 'unlocked': 'First Trade' in unlocked},
        {'name': '10 Day Streak', 'icon': 'fa-solid fa-fire', 'unlocked': '10 Day Streak' in unlocked},
        {'name': 'Green Week', 'icon': 'fa-solid fa-chart-line', 'unlocked': 'Green Week' in unlocked},
        {'name': '$100K Portfolio', 'icon': 'fa-solid fa-gem', 'unlocked': '$100K Portfolio' in unlocked},
        {'name': 'Top 100', 'icon': 'fa-solid fa-trophy', 'unlocked': 'Top 100' in unlocked},
        {'name': 'Day Trader', 'icon': 'fa-solid fa-bolt', 'unlocked': 'Day Trader' in unlocked}
    ]
    
    return all_achievements

@app.route('/')
def index():
    try:
        init_db()  # Ensure tables exist first
        init_stock_data()
        update_stock_prices()
        init_user()  # Initialize user - this now guarantees user_id is set
        
        print(f"After init_user - session_id: {session.get('session_id')}, user_id: {session.get('user_id')}")
        
        # IMPORTANT: Only log events AFTER user is fully initialized
        log_event('page_view', {'page': 'home'})
        
        session_id = session['session_id']
        user_id = session['user_id']  # This is now guaranteed to exist
        
        # Get user's current cash
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT current_cash, user_id FROM users WHERE user_id = %s', (user_id,))
        user = cur.fetchone()
        
        if not user:
            # This should never happen, but just in case
            print(f"ERROR: User {user_id} not found when querying")
            cur.close()
            conn.close()
            return f"Error: User {user_id} not found in database. Session: {session_id}", 500
        
        print(f"Found user: {user}")
        current_cash = float(user['current_cash'])
        
        # Get portfolio from database
        cur.execute('''
            SELECT symbol, shares, avg_price 
            FROM portfolio 
            WHERE session_id = %s
        ''', (session_id,))
        portfolio_data = cur.fetchall()
        
        # Calculate portfolio value
        portfolio_value = current_cash
        portfolio_items = []
        market_data = get_market_data()
        
        for item in portfolio_data:
            stock = next((s for s in market_data if s['symbol'] == item['symbol']), None)
            if stock:
                current_value = item['shares'] * stock['price']
                cost_basis = item['shares'] * float(item['avg_price'])
                gain_loss = current_value - cost_basis
                gain_loss_percent = (gain_loss / cost_basis * 100) if cost_basis > 0 else 0
                
                portfolio_value += current_value
                
                portfolio_items.append({
                    'symbol': item['symbol'],
                    'shares': item['shares'],
                    'avg_price': float(item['avg_price']),
                    'current_price': stock['price'],
                    'current_value': current_value,
                    'gain_loss': gain_loss,
                    'gain_loss_percent': gain_loss_percent
                })
        
        # Get trade history
        cur.execute('''
            SELECT symbol, action, shares, price, total_cost, timestamp
            FROM trades
            WHERE session_id = %s
            ORDER BY timestamp DESC
            LIMIT 10
        ''', (session_id,))
        trade_history = cur.fetchall()
        
        cur.execute('SELECT COUNT(*) AS c FROM trades WHERE session_id = %s', (session_id,))
        trade_count = int(cur.fetchone()['c'])
        
        leaderboard, user_rank, total_users = get_gamified_leaderboard_rows(cur, user_id)
        
        if user_rank <= 100:
            cur.execute('''
                INSERT INTO achievements (user_id, session_id, achievement_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id, achievement_name) DO NOTHING
            ''', (user_id, session_id, 'Top 100'))
            conn.commit()
        
        pnl_pct = ((portfolio_value - 100000.00) / 100000.00) * 100.0
        activity_pts = min(50, trade_count * 5)
        return_pts = min(50, max(0, 25 + (portfolio_value - 100000.0) / 4000.0))
        pulse_score = int(min(100, max(0, activity_pts + return_pts)))
        
        xp = min(950, trade_count * 80 + int(max(0, (portfolio_value - 100000.0) / 500.0)))
        next_level_xp = 1000
        if xp < 250:
            level = 'Novice'
        elif xp < 600:
            level = 'Active'
        else:
            level = 'Elite'
        
        streak_display = min(trade_count, 99)
        
        cur.close()
        conn.close()
        
        xp_pct = min(100, max(0, int(100 * xp / next_level_xp))) if next_level_xp else 0
        
        user_stats = {
            'rank': user_rank,
            'total_users': total_users,
            'streak': streak_display,
            'badges': 1,
            'portfolio_value': portfolio_value,
            'cash': current_cash,
            'daily_change': portfolio_value - 100000.00,
            'daily_change_percent': pnl_pct,
            'level': level,
            'xp': xp,
            'next_level_xp': next_level_xp,
            'xp_pct': xp_pct,
            'pulse_score': pulse_score,
            'trade_count': trade_count
        }
        
        achievements = get_user_achievements()
        
        # Format trade history
        formatted_history = []
        for trade in trade_history:
            formatted_history.append({
                'symbol': trade['symbol'],
                'action': trade['action'],
                'shares': trade['shares'],
                'price': float(trade['price']),
                'total': float(trade['total_cost']),
                'timestamp': trade['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            })
        
        return render_template('gamified.html',
                             user_stats=user_stats,
                             leaderboard=leaderboard,
                             market_data=market_data,
                             achievements=achievements,
                             portfolio=portfolio_items,
                             trade_history=formatted_history)
        
    except Exception as e:
        print(f"Index route error: {e}")
        import traceback
        traceback.print_exc()
        return f"Error loading page: {str(e)}", 500

@app.route('/trade', methods=['POST'])
def trade():
    try:
        # Ensure user is initialized
        if 'session_id' not in session or 'user_id' not in session:
            init_user()
        
        data = request.json
        if not data:
            return jsonify({'success': False, 'message': 'No data received'})
        
        symbol = data.get('symbol')
        shares = int(data.get('shares', 0))
        action = data.get('action')
        
        log_event('trade_attempt', {
            'symbol': symbol,
            'shares': shares,
            'action': action
        })
        
        if shares <= 0:
            return jsonify({'success': False, 'message': 'Invalid number of shares'})
        
        if not symbol:
            return jsonify({'success': False, 'message': 'Please select a symbol from the Market Data list'})
        
        market_data = get_market_data()
        stock = next((s for s in market_data if s['symbol'] == symbol), None)
        if not stock:
            return jsonify({'success': False, 'message': 'Please select a symbol from the Market Data list'})
        
        price = stock['price']
        total_cost = shares * price
        session_id = session['session_id']
        user_id = session['user_id']
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get current cash
        cur.execute('SELECT current_cash FROM users WHERE session_id = %s', (session_id,))
        user = cur.fetchone()
        if not user:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'message': 'User not found'})
        
        current_cash = float(user['current_cash'])
        
        # Check if this is the user's first trade
        cur.execute('SELECT COUNT(*) as count FROM trades WHERE session_id = %s', (session_id,))
        trade_count = cur.fetchone()['count']
        is_first_trade = trade_count == 0
        
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
                ''', (user_id, session_id, symbol, shares, price))
            
            # Record trade
            cur.execute('''
                INSERT INTO trades (user_id, session_id, symbol, action, shares, price, total_cost)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (user_id, session_id, symbol, 'BUY', shares, price, total_cost))
            
            # Unlock First Trade achievement if this is first trade
            if is_first_trade:
                cur.execute('''
                    INSERT INTO achievements (user_id, session_id, achievement_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (session_id, achievement_name) DO NOTHING
                ''', (user_id, session_id, 'First Trade'))
            
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
            
            response = {
                'success': True,
                'message': f'Successfully bought {shares} shares of {symbol}!',
                'cash': new_cash
            }
            
            if is_first_trade:
                response['achievement_unlocked'] = 'First Trade'
            
            return jsonify(response)
        
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
            ''', (user_id, session_id, symbol, 'SELL', shares, price, total_cost))
            
            # Unlock First Trade achievement if this is first trade
            if is_first_trade:
                cur.execute('''
                    INSERT INTO achievements (user_id, session_id, achievement_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (session_id, achievement_name) DO NOTHING
                ''', (user_id, session_id, 'First Trade'))
            
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
            
            response = {
                'success': True,
                'message': f'Successfully sold {shares} shares of {symbol}!',
                'cash': new_cash
            }
            
            if is_first_trade:
                response['achievement_unlocked'] = 'First Trade'
            
            return jsonify(response)
        
        else:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Invalid action'})
        
    except Exception as e:
        print(f"Trade route error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'})

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
