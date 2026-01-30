from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import hashlib
import yfinance as yf
import requests
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'monpecule_secret_key_2026_change_this_in_production'

# --- CONFIGURATION ---
DB_PATH = 'monpecule.db'

# --- UTILS ---
def safe_float(value, default=0.0):
    try:
        return float(value) if value else default
    except (ValueError, TypeError):
        return default

def safe_int(value, default=0):
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default

def hash_password(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT, prenom TEXT, 
                 email TEXT UNIQUE, tel TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS comptes 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, nom_compte TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS actifs 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, compte_id INTEGER, nom_actif TEXT, 
                 ticker_isin TEXT, prix_achat REAL, quantite INTEGER, frais REAL, 
                 prix_actuel REAL, prix_veille REAL)''')
    conn.commit()
    conn.close()

init_db()

# --- API YAHOO FINANCE (yfinance) ---
def fetch_price_from_api(identifier):
    """
    Recherche le prix et le nom d'une action via yfinance.
    Supporte: symboles boursiers (AAPL, BNP.PA), noms de sociétés, codes ISIN.
    """
    if not identifier: 
        return None, None
    
    identifier = identifier.strip()
    
    # 1. On tente une recherche directe d'abord
    try:
        ticker = yf.Ticker(identifier)
        # Utiliser fast_info pour le prix (plus rapide et fiable)
        price = ticker.fast_info.last_price
        # On essaie de récupérer le nom via info (optionnel car lent)
        name = identifier
        try:
            name = ticker.info.get('longName') or ticker.info.get('shortName') or identifier
        except:
            pass
            
        if price and not isinstance(price, (type(None), str)):
            return (round(float(price), 2), name)
    except:
        pass

    # 2. Fallback: Recherche Yahoo Finance pour trouver le Ticker (ISIN ou Nom)
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={identifier}&quotesCount=1"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get('quotes') and len(data['quotes']) > 0:
                symbol = data['quotes'][0]['symbol']
                name = data['quotes'][0].get('longname') or data['quotes'][0].get('shortname') or symbol
                
                # On récupère le prix pour ce symbole trouvé
                ticker = yf.Ticker(symbol)
                price = ticker.fast_info.last_price
                if not price:
                    # Dernier recours: historique
                    hist = ticker.history(period='1d')
                    if not hist.empty:
                        price = hist['Close'].iloc[-1]
                
                if price and not isinstance(price, (type(None), str)):
                    return (round(float(price), 2), name)
    except Exception as e:
        print(f"Erreur recherche yfinance: {e}")
        
    return None, None

# --- ROUTES ---
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    email = request.form.get('email')
    password = request.form.get('password')
    conn = get_connection()
    user = conn.execute('SELECT * FROM users WHERE email = ? AND password = ?', 
                       (email, hash_password(password))).fetchone()
    conn.close()
    if user:
        session['user_id'] = user['id']
        session['user_nom'] = user['nom']
        return redirect(url_for('dashboard'))
    flash('Email ou mot de passe incorrect')
    return redirect(url_for('index'))

@app.route('/register', methods=['POST'])
def register():
    nom = request.form.get('nom')
    email = request.form.get('email')
    password = request.form.get('password')
    if nom and email and password:
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute('INSERT INTO users (nom, email, password) VALUES (?,?,?)', 
                     (nom, email, hash_password(password)))
            uid = c.lastrowid
            c.execute('INSERT INTO comptes (user_id, nom_compte) VALUES (?, "Principal")', (uid,))
            conn.commit()
            session['user_id'] = uid
            session['user_nom'] = nom
            return redirect(url_for('dashboard'))
        except:
            flash('Email déjà utilisé')
        finally:
            conn.close()
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    conn = get_connection()
    comptes = conn.execute('SELECT * FROM comptes WHERE user_id = ?', (session['user_id'],)).fetchall()
    actifs = conn.execute('''SELECT a.*, c.nom_compte FROM actifs a 
                            JOIN comptes c ON a.compte_id = c.id 
                            WHERE c.user_id = ?''', (session['user_id'],)).fetchall()
    conn.close()
    
    # Calculs
    total_pv = 0
    for a in actifs:
        val_actuelle = (a['prix_actuel'] * a['quantite']) + a['frais']
        val_achat = (a['prix_achat'] * a['quantite']) + a['frais']
        total_pv += (val_actuelle - val_achat)
    
    return render_template('dashboard.html', comptes=comptes, actifs=actifs, 
                          user_nom=session.get('user_nom'), total_pv=total_pv)

@app.route('/add_compte', methods=['POST'])
def add_compte():
    if 'user_id' not in session: return redirect(url_for('index'))
    nom_compte = request.form.get('nom_compte')
    conn = get_connection()
    conn.execute('INSERT INTO comptes (user_id, nom_compte) VALUES (?,?)', (session['user_id'], nom_compte))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/add_actif', methods=['POST'])
def add_actif():
    if 'user_id' not in session: return redirect(url_for('index'))
    compte_id = request.form.get('compte_id')
    nom = request.form.get('nom')
    ticker = request.form.get('ticker')
    pa = safe_float(request.form.get('prix_achat'))
    q = safe_int(request.form.get('quantite'), 1)
    fr = safe_float(request.form.get('frais'))
    pnow = safe_float(request.form.get('prix_actuel'))
    
    conn = get_connection()
    conn.execute('INSERT INTO actifs (compte_id, nom_actif, ticker_isin, prix_achat, quantite, frais, prix_actuel, prix_veille) VALUES (?,?,?,?,?,?,?,?)',
                (compte_id, nom, ticker, pa, q, fr, pnow, pnow))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/update_actif/<int:actif_id>', methods=['POST'])
def update_actif(actif_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    nom = request.form.get('nom')
    pa = safe_float(request.form.get('prix_achat'))
    q = safe_int(request.form.get('quantite'))
    fr = safe_float(request.form.get('frais'))
    pnow = safe_float(request.form.get('prix_actuel'))
    
    conn = get_connection()
    conn.execute('UPDATE actifs SET nom_actif=?, prix_achat=?, quantite=?, frais=?, prix_actuel=? WHERE id=?',
                (nom, pa, q, fr, pnow, actif_id))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_actif/<int:actif_id>')
def delete_actif(actif_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    conn = get_connection()
    conn.execute('DELETE FROM actifs WHERE id = ?', (actif_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_compte/<int:compte_id>')
def delete_compte(compte_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    conn = get_connection()
    conn.execute('DELETE FROM actifs WHERE compte_id = ?', (compte_id,))
    conn.execute('DELETE FROM comptes WHERE id = ?', (compte_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/api/search_ticker/<ticker>')
def search_ticker(ticker):
    price, name = fetch_price_from_api(ticker)
    return jsonify({'price': price, 'name': name})

@app.route('/api/update_prices')
def update_prices():
    if 'user_id' not in session: 
        return jsonify({'error': 'Non connecté'})
    
    conn = get_connection()
    tickers = conn.execute('''SELECT DISTINCT UPPER(ticker_isin) as ticker FROM actifs a 
                             JOIN comptes c ON a.compte_id=c.id 
                             WHERE c.user_id=? AND ticker_isin != ""''', (session['user_id'],)).fetchall()
    
    updated = 0
    if tickers:
        try:
            for ticker_row in tickers:
                symbol = ticker_row['ticker']
                ticker = yf.Ticker(symbol)
                # Utilisation de fast_info pour la mise à jour massive
                price = ticker.fast_info.last_price
                if not price:
                    hist = ticker.history(period='1d')
                    if not hist.empty:
                        price = hist['Close'].iloc[-1]
                
                if price and not isinstance(price, (type(None), str)):
                    conn.execute('UPDATE actifs SET prix_actuel = ? WHERE UPPER(ticker_isin) = ?', 
                               (float(price), symbol.upper()))
                    updated += 1
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': f'{updated} cours mis à jour'})
        except Exception as e:
            print(f"Erreur update: {e}")
    conn.close()
    return jsonify({'success': False})

if __name__ == '__main__':
    app.run(debug=True, port=5010)

# WSGI entry point for cPanel
application = app
