from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import hashlib
import requests
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'monpecule_secret_key_2026_change_this_in_production'

# --- CONFIGURATION ---
DB_PATH = 'monpecule.db'
MARKETSTACK_API_KEY = "d3853c0620d9faf295452b6b541a2980"

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

# --- API MARKETSTACK ---
def fetch_price_from_api(identifier):
    """
    Recherche une action par nom, symbole (AAPL, OR.PA) ou ISIN (FR0000120321)
    """
    if not identifier: return None, None
    
    identifier = identifier.strip()
    
    try:
        # Détection ISIN (12 caractères alphanumériques commençant par 2 lettres)
        if len(identifier) == 12 and identifier[:2].isalpha() and identifier[2:].isalnum():
            # C'est probablement un ISIN, on essaie de chercher par ISIN
            search_res = requests.get(f"http://api.marketstack.com/v1/tickers", 
                                    params={'access_key': MARKETSTACK_API_KEY, 'search': identifier})
            if search_res.status_code == 200:
                data = search_res.json()
                if 'data' in data and len(data['data']) > 0:
                    symbol = data['data'][0]['symbol']
                    name = data['data'][0]['name']
                    # Récupérer le prix
                    res = requests.get(f"http://api.marketstack.com/v1/eod/latest", 
                                      params={'access_key': MARKETSTACK_API_KEY, 'symbols': symbol})
                    if res.status_code == 200:
                        price_data = res.json()
                        if 'data' in price_data and len(price_data['data']) > 0:
                            price = price_data['data'][0].get('close')
                            return (round(price, 2) if price else None, name)
        
        # Essai 1: Recherche directe par symbole (AAPL, TSLA, OR.PA, etc.)
        symbol = identifier.upper()
        res = requests.get(f"http://api.marketstack.com/v1/eod/latest", 
                          params={'access_key': MARKETSTACK_API_KEY, 'symbols': symbol})
        
        if res.status_code == 200:
            data = res.json()
            if 'data' in data and len(data['data']) > 0:
                price = data['data'][0].get('close')
                # Récupérer le nom complet
                ticker_res = requests.get(f"http://api.marketstack.com/v1/tickers/{symbol}", 
                                        params={'access_key': MARKETSTACK_API_KEY})
                name = symbol
                if ticker_res.status_code == 200:
                    ticker_data = ticker_res.json()
                    name = ticker_data.get('name', symbol)
                return (round(price, 2) if price else None, name)
        
        # Essai 2: Recherche par nom de société ou texte
        search_res = requests.get(f"http://api.marketstack.com/v1/tickers", 
                                params={'access_key': MARKETSTACK_API_KEY, 'search': identifier, 'limit': 5})
        
        if search_res.status_code == 200:
            search_data = search_res.json()
            if 'data' in search_data and len(search_data['data']) > 0:
                # Prendre le premier résultat
                first_result = search_data['data'][0]
                symbol = first_result['symbol']
                name = first_result['name']
                
                # Récupérer le prix pour ce symbole
                res = requests.get(f"http://api.marketstack.com/v1/eod/latest", 
                                  params={'access_key': MARKETSTACK_API_KEY, 'symbols': symbol})
                if res.status_code == 200:
                    price_data = res.json()
                    if 'data' in price_data and len(price_data['data']) > 0:
                        price = price_data['data'][0].get('close')
                        return (round(price, 2) if price else None, name)
        
        return None, None
        
    except Exception as e:
        print(f"Erreur API: {e}")
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
    if tickers:
        symbols = ",".join([t['ticker'] for t in tickers])
        try:
            res = requests.get(f"http://api.marketstack.com/v1/eod/latest", 
                              params={'access_key': MARKETSTACK_API_KEY, 'symbols': symbols})
            if res.status_code == 200:
                data = res.json()
                for item in data.get('data', []):
                    conn.execute('UPDATE actifs SET prix_actuel = ? WHERE UPPER(ticker_isin) = ?', 
                               (item['close'], item['symbol'].upper()))
                conn.commit()
                conn.close()
                return jsonify({'success': True, 'message': 'Cours mis à jour'})
        except: pass
    conn.close()
    return jsonify({'success': False})

if __name__ == '__main__':
    app.run(debug=True)
