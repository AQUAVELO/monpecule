import streamlit as st
import pandas as pd
import sqlite3
import requests
from datetime import datetime, date
import hashlib
import time

# Configuration
st.set_page_config(page_title="MonPecule", layout="wide")

# --- UTILS ---
def hash_password(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def get_connection():
    return sqlite3.connect('monpecule.db')

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

# --- API ---
MARKETSTACK_API_KEY = "d3853c0620d9faf295452b6b541a2980"

def fetch_price_from_api(identifier):
    if not identifier: return None, None
    try:
        # 1. Recherche globale (Nom, Symbole ou ISIN) via l'endpoint tickers
        res_search = requests.get(f"http://api.marketstack.com/v1/tickers", 
                                 params={'access_key': MARKETSTACK_API_KEY, 'search': identifier})
        data_search = res_search.json()
        
        if res_search.status_code == 200 and 'data' in data_search and len(data_search['data']) > 0:
            # On prend le premier résultat le plus pertinent
            best_match = data_search['data'][0]
            symbol = best_match['symbol']
            name = best_match['name']
            
            # 2. Récupérer le dernier prix pour ce symbole précis
            res_price = requests.get(f"http://api.marketstack.com/v1/eod/latest", 
                                    params={'access_key': MARKETSTACK_API_KEY, 'symbols': symbol})
            data_price = res_price.json()
            
            price = None
            if res_price.status_code == 200 and 'data' in data_price and len(data_price['data']) > 0:
                price = data_price['data'][0].get('close')
            
            return (round(price, 2) if price else None, name, symbol)
        return None, None, None
    except: return None, None, None

def update_all_user_prices(user_id):
    conn = get_connection()
    try:
        # Récupérer les symboles uniques en majuscules pour l'utilisateur
        tickers_df = pd.read_sql_query(f"""
            SELECT DISTINCT UPPER(a.ticker_isin) as ticker 
            FROM actifs a 
            JOIN comptes c ON a.compte_id=c.id 
            WHERE c.user_id={user_id} AND a.ticker_isin != ''
        """, conn)
        
        if not tickers_df.empty:
            symbols = ",".join(tickers_df['ticker'].tolist())
            res = requests.get(f"http://api.marketstack.com/v1/eod/latest", 
                              params={'access_key': MARKETSTACK_API_KEY, 'symbols': symbols})
            
            if res.status_code == 200:
                data = res.json()
                if 'data' in data:
                    c = conn.cursor()
                    for item in data['data']:
                        new_price = item.get('close')
                        symbol = item.get('symbol')
                        if new_price is not None and symbol:
                            # Mise à jour rigoureuse par ticker (insensible à la casse)
                            c.execute("UPDATE actifs SET prix_actuel = ? WHERE UPPER(ticker_isin) = ?", 
                                     (new_price, symbol.upper()))
                    conn.commit()
                    st.session_state.last_update = datetime.now().strftime("%H:%M:%S")
                    return True, f"Cours mis à jour à {st.session_state.last_update}"
            return False, f"Erreur API ({res.status_code})"
        return False, "Aucun titre à mettre à jour"
    except Exception as e:
        return False, f"Erreur : {str(e)}"
    finally:
        conn.close()

# --- SESSION STATE ---
if 'user_id' not in st.session_state: st.session_state.user_id = None
if 'edit_mode' not in st.session_state: st.session_state.edit_mode = {}

st.title("💰 MonPecule")

st.markdown("""
<style>
    .compte-card { background-color: #fff; border-radius: 15px; padding: 20px; border: 1px solid #e0e0e0; margin-bottom: 20px; }
    .actif-item { background-color: #f8f9fa; border-radius: 10px; padding: 10px; margin-bottom: 8px; border-left: 5px solid #007bff; }
    .pv-positive { color: #28a745; font-weight: bold; }
    .pv-negative { color: #dc3545; font-weight: bold; }
    .stats-container { background-color: #f0f2f6; padding: 20px; border-radius: 15px; margin-bottom: 25px; }
</style>
""", unsafe_allow_html=True)

if st.session_state.user_id is None:
    t1, t2 = st.tabs(["Connexion", "Inscription"])
    with t1:
        with st.form("login"):
            le, lp = st.text_input("Email"), st.text_input("Password", type="password")
            if st.form_submit_button("Connexion"):
                conn = get_connection(); c = conn.cursor()
                c.execute("SELECT id FROM users WHERE email=? AND password=?", (le, hash_password(lp)))
                u = c.fetchone(); conn.close()
                if u: 
                    st.session_state.user_id = u[0]
                    update_all_user_prices(u[0])
                    st.rerun()
                else: st.error("Erreur")
    with t2:
        with st.form("reg"):
            rn, re, rp = st.text_input("Nom"), st.text_input("Email"), st.text_input("Pass", type="password")
            if st.form_submit_button("S'inscrire"):
                conn = get_connection(); c = conn.cursor()
                try:
                    c.execute("INSERT INTO users (nom, email, password) VALUES (?,?,?)", (rn, re, hash_password(rp)))
                    uid = c.lastrowid
                    c.execute("INSERT INTO comptes (user_id, nom_compte) VALUES (?, 'Principal')", (uid,))
                    conn.commit(); st.session_state.user_id = uid; st.rerun()
                except: st.error("Email pris")
                finally: conn.close()
else:
    # --- DASHBOARD ---
    conn = get_connection()
    user = pd.read_sql_query(f"SELECT * FROM users WHERE id = {st.session_state.user_id}", conn).iloc[0]
    
    ch1, ch2, ch3 = st.columns([4,1,1])
    ch1.header(f"{user['nom']}")
    
    # Affichage de l'heure de dernière mise à jour sous le titre si elle existe
    if 'last_update' in st.session_state and st.session_state.last_update:
        ch1.caption(f"Dernière actualisation des cours : {st.session_state.last_update}")

    if ch2.button("🔄 Mise à jour"):
        with st.spinner("Actualisation des cours..."):
            success, msg = update_all_user_prices(st.session_state.user_id)
            if success:
                st.toast(msg, icon="✅")
            else:
                st.error(msg)
            time.sleep(0.5)
            st.rerun()
    if ch3.button("🚪"): st.session_state.user_id = None; st.rerun()

    all_actifs = pd.read_sql_query(f"SELECT a.* FROM actifs a JOIN comptes c ON a.compte_id=c.id WHERE c.user_id={st.session_state.user_id}", conn)
    
    if not all_actifs.empty:
        all_actifs['Val'] = (all_actifs['prix_actuel'] * all_actifs['quantite']) + all_actifs['frais']
        all_actifs['Achat'] = (all_actifs['prix_achat'] * all_actifs['quantite']) + all_actifs['frais']
        all_actifs['PV'] = all_actifs['Val'] - all_actifs['Achat']
        st.metric("Plus-value", f"{all_actifs['PV'].sum():,.2f} €", delta=f"{all_actifs['PV'].sum():,.2f} €")

    comptes = pd.read_sql_query(f"SELECT * FROM comptes WHERE user_id={st.session_state.user_id}", conn)
    cols = st.columns(len(comptes) + 1)
    
    for i, rc in comptes.iterrows():
        with cols[i]:
            st.markdown(f"### 🏦 {rc['nom_compte']}")
            actifs = all_actifs[all_actifs['compte_id'] == rc['id']] if not all_actifs.empty else pd.DataFrame()
            
            for _, a in actifs.iterrows():
                col = "pv-positive" if a['PV'] >= 0 else "pv-negative"
                st.markdown(f"""<div class="actif-item">
                    <b style="font-size: 1.1rem;">{a['nom_actif']}</b><br/>
                    <small>
                        <b>Cours achat:</b> {a['prix_achat']:,.2f}€ | <b>Total Achat:</b> {a['Achat']:,.2f}€<br/>
                        <b>Cours Actuel:</b> {a['prix_actuel']:,.2f}€ | <b>Total Actuel:</b> {a['Val']:,.2f}€<br/>
                        <span style="font-size: 1rem;">Plus-value: <span class="{col}">{a['PV']:,.2f}€</span></span>
                    </small>
                </div>""", unsafe_allow_html=True)
                
                # --- MODIFICATION VIA EXPANDER ---
                edit_key = f"edit_{a['id']}"
                if st.session_state.edit_mode.get(edit_key, False):
                    with st.expander("📝 Modifier", expanded=True):
                        en = st.text_input("Nom", value=a['nom_actif'], key=f"en_{a['id']}")
                        ep = st.number_input("Cours achat", value=float(a['prix_achat']), key=f"ep_{a['id']}")
                        eq = st.number_input("Qté", value=int(a['quantite']), key=f"eq_{a['id']}")
                        ef = st.number_input("Frais", value=float(a['frais']), key=f"ef_{a['id']}")
                        enow = st.number_input("Cours actuel", value=float(a['prix_actuel']), key=f"enow_{a['id']}")
                        
                        if st.button("✅ Sauvegarder", key=f"save_{a['id']}"):
                            c_db = conn.cursor()
                            c_db.execute("UPDATE actifs SET nom_actif=?, prix_achat=?, quantite=?, frais=?, prix_actuel=? WHERE id=?", 
                                       (en, ep, eq, ef, enow, a['id']))
                            conn.commit()
                            st.session_state.edit_mode[edit_key] = False  # Ferme l'expander
                            st.rerun()
                        if st.button("❌ Annuler", key=f"cancel_{a['id']}"):
                            st.session_state.edit_mode[edit_key] = False
                            st.rerun()
                else:
                    bc1, bc2 = st.columns(2)
                    if bc1.button("📝", key=f"btn_edit_{a['id']}"):
                        st.session_state.edit_mode[edit_key] = True
                        st.rerun()
                    if bc2.button("🗑️", key=f"del_{a['id']}"):
                        c_db = conn.cursor(); c_db.execute("DELETE FROM actifs WHERE id=?", (a['id'],)); conn.commit(); st.rerun()
            
            # --- AJOUTER ---
            st.divider()
            with st.expander("➕ Ajouter"):
                sid = st.text_input("Nom, Symbole ou ISIN", key=f"sid_{rc['id']}")
                if st.button("🔍 Rechercher l'action", key=f"search_{rc['id']}"):
                    p, n, s = fetch_price_from_api(sid)
                    if p:
                        st.session_state[f"pa_{rc['id']}"] = p
                        st.session_state[f"pnow_{rc['id']}"] = p
                        st.session_state[f"na_{rc['id']}"] = n
                        st.session_state[f"ticker_found_{rc['id']}"] = s
                        st.rerun()
                
                na = st.text_input("Nom", value=st.session_state.get(f"na_{rc['id']}", ""), key=f"na_{rc['id']}")
                pa = st.number_input("Cours achat", value=st.session_state.get(f"pa_{rc['id']}", 0.0), key=f"pa_{rc['id']}")
                q = st.number_input("Qté", min_value=1, value=1, key=f"q_{rc['id']}")
                fr = st.number_input("Frais", min_value=0.0, key=f"fr_{rc['id']}")
                pnow = st.number_input("Cours actuel", value=st.session_state.get(f"pnow_{rc['id']}", 0.0), key=f"pnow_{rc['id']}")
                
                if st.button("Enregistrer", key=f"add_{rc['id']}"):
                    # Utiliser le ticker trouvé par la recherche ou ce qui a été saisi
                    final_ticker = st.session_state.get(f"ticker_found_{rc['id']}", sid)
                    c = conn.cursor()
                    c.execute("INSERT INTO actifs (compte_id, nom_actif, ticker_isin, prix_achat, quantite, frais, prix_actuel, prix_veille) VALUES (?,?,?,?,?,?,?,?)",
                             (rc['id'], na, final_ticker, pa, q, fr, pnow, pnow))
                    conn.commit()
                    # Nettoyage
                    for k in [f"pa_{rc['id']}", f"pnow_{rc['id']}", f"na_{rc['id']}", f"ticker_found_{rc['id']}"]:
                        if k in st.session_state: del st.session_state[k]
                    st.rerun()
    
    with cols[-1]:
        st.markdown("### ➕ Nouveau")
        with st.form("nc"):
            nc = st.text_input("Nom")
            if st.form_submit_button("Créer"):
                c = conn.cursor(); c.execute("INSERT INTO comptes (user_id, nom_compte) VALUES (?,?)", (st.session_state.user_id, nc)); conn.commit(); st.rerun()
    
    conn.close()
