from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import hashlib
import yfinance as yf
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'monpecule_secret_key_2026_change_this_in_production'

# --- CONFIGURATION ---
DB_PATH = 'monpecule.db'

# --- UTILS ---
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
    Recherche une action par symbole (AAPL, TSLA, BNP.PA, OR.PA)
    Utilise Yahoo Finance via yfinance (gratuit, illimité, délai 15 min)
    """
    if not identifier: return None, None
    
    identifier = identifier.strip().upper()
    
    try:
        # Créer un objet Ticker
        ticker = yf.Ticker(identifier)
        
        # Récupérer les infos
        info = ticker.info
        
        # Vérifier si le ticker existe
        if not info or 'symbol' not in info:
            return None, None
        
        # Récupérer le prix actuel
        price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
        
        # Récupérer le nom
        name = info.get('longName') or info.get('shortName') or identifier
        
        if price:
            return (round(float(price), 2), name)
        
        return None, None
        
    except Exception as e:
        print(f"Erreur yfinance: {e}")
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
    pa = float(request.form.get('prix_achat', 0))
    q = int(request.form.get('quantite', 1))
    fr = float(request.form.get('frais', 0))
    pnow = float(request.form.get('prix_actuel', 0))
    
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
    pa = float(request.form.get('prix_achat'))
    q = int(request.form.get('quantite'))
    fr = float(request.form.get('frais'))
    pnow = float(request.form.get('prix_actuel'))
    
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
    if 'user_id' not in session: return jsonify({'error': 'Non connecté'})
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
                info = ticker.info
                
                if info and 'symbol' in info:
                    price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
                    if price:
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
    app.run(debug=True)
