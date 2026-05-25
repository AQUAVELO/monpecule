import os
import sys
import re
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import hashlib
import yfinance as yf
import requests
from datetime import datetime
import threading
import smtplib
from email.message import EmailMessage

app = Flask(__name__)
app.secret_key = 'monpecule_secret_key_2026_change_this_in_production'

# Token pour les appels CRON (peut être défini via variable d'environnement)
CRON_TOKEN = os.environ.get('CRON_TOKEN', 'monpecule_cron_2026_change_this')
EODHD_API_KEY = os.environ.get('EODHD_API_KEY', '6980ce5e766dd6.91379679')
INTRADAY_EMAIL_TO = os.environ.get('INTRADAY_EMAIL_TO', 'aqua.cannes@gmail.com')
SMTP_HOST = os.environ.get('SMTP_HOST')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
SMTP_FROM = os.environ.get('SMTP_FROM') or SMTP_USER

# Redirection www vers domaine principal
@app.before_request
def redirect_www():
    """Redirige www.monpecule.fr vers monpecule.fr"""
    if request.host.startswith('www.'):
        return redirect(request.url.replace('www.', '', 1), code=301)

# Taux de change (EUR comme base)
EXCHANGE_RATES = {
    'EUR': 1.0,
    'USD': 1.08,  # 1 EUR = 1.08 USD
    'GBP': 0.86   # 1 EUR = 0.86 GBP
}

CURRENCY_SYMBOLS = {
    'EUR': '€',
    'USD': '$',
    'GBP': '£'
}

def detect_currency_from_symbol(symbol):
    """Détecte la devise de cotation d'après le symbole"""
    if not symbol:
        return 'EUR'
    symbol = symbol.upper()
    # ISIN britanniques (GB...), symboles .L ou contient HAYS
    if symbol.startswith('GB') or '.L' in symbol or 'HAYS' in symbol:
        return 'GBP'
    else:
        return 'EUR'

def convert_currency(amount, from_currency='EUR', to_currency='EUR'):
    """Convertit un montant d'une devise à une autre"""
    if from_currency == to_currency:
        return amount
    # Convertir d'abord en EUR (base), puis vers la devise cible
    amount_in_eur = amount / EXCHANGE_RATES.get(from_currency, 1.0)
    return amount_in_eur * EXCHANGE_RATES.get(to_currency, 1.0)

# Filtre personnalisé pour formater les dates
@app.template_filter('format_date')
def format_date(date_str):
    """Convertit une date YYYY-MM-DD en JJ/MM/AA"""
    if not date_str:
        return ''
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        return date_obj.strftime('%d/%m/%y')
    except:
        return date_str

# Filtre pour convertir les montants
@app.template_filter('convert')
def convert_filter(amount, to_currency='EUR'):
    """Filtre Jinja2 pour convertir les montants"""
    return convert_currency(amount, 'EUR', to_currency)

# --- CONFIGURATION (Chemin absolu indispensable pour cPanel) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Utiliser le volume Fly.io si disponible, sinon local
if os.path.exists('/data'):
    DB_PATH = '/data/monpecule.db'
    print(f"💾 Utilisation du volume Fly.io: {DB_PATH}")
else:
    DB_PATH = os.path.join(BASE_DIR, 'monpecule.db')
    print(f"💾 Utilisation base locale: {DB_PATH}")

# --- UTILS ---
def safe_float(value, default=0.0):
    try:
        if value is None or value == "": return default
        return float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        return default

def safe_int(value, default=0):
    try:
        if value is None or value == "": return default
        return int(value)
    except (ValueError, TypeError):
        return default

def hash_password(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT, prenom TEXT, 
                     email TEXT UNIQUE, tel TEXT, password TEXT, derniere_maj TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS comptes 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, nom_compte TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS actifs 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, compte_id INTEGER, nom_actif TEXT, 
                     ticker_isin TEXT, prix_achat REAL, quantite INTEGER, frais REAL, 
                     prix_actuel REAL, prix_veille REAL, date_achat TEXT, devise_cotation TEXT DEFAULT 'EUR')''')
        
        # Table historique des prix
        c.execute('''CREATE TABLE IF NOT EXISTS historique_prix 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      actif_id INTEGER, 
                      date TEXT, 
                      prix REAL, 
                      devise TEXT,
                      FOREIGN KEY (actif_id) REFERENCES actifs(id) ON DELETE CASCADE)''')
        
        # Index pour accélérer les requêtes historiques
        c.execute('''CREATE INDEX IF NOT EXISTS idx_historique_actif_date 
                     ON historique_prix(actif_id, date)''')
        
        # Table cumul PV mensuelle (cumul des variations journalières)
        c.execute('''CREATE TABLE IF NOT EXISTS cumul_pv_mois 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      actif_id INTEGER, 
                      mois TEXT, 
                      cumul_pv REAL DEFAULT 0,
                      derniere_mise_a_jour TEXT,
                      UNIQUE(actif_id, mois),
                      FOREIGN KEY (actif_id) REFERENCES actifs(id) ON DELETE CASCADE)''')
        
        # Migration : ajouter derniere_maj si elle n'existe pas
        try:
            c.execute("ALTER TABLE users ADD COLUMN derniere_maj TEXT")
            print("Migration: Colonne derniere_maj ajoutee a users")
        except:
            pass  # Colonne deja presente
        
        # Migration : ajouter prix_veille si elle n'existe pas
        try:
            c.execute("ALTER TABLE actifs ADD COLUMN prix_veille REAL DEFAULT 0")
            print("Migration: Colonne prix_veille ajoutee a actifs")
        except:
            pass  # Colonne deja presente
        
        # Migration : ajouter date_achat si elle n'existe pas
        try:
            c.execute("ALTER TABLE actifs ADD COLUMN date_achat TEXT")
            print("Migration: Colonne date_achat ajoutee a actifs")
        except:
            pass  # Colonne deja presente
        
        # Migration : ajouter devise si elle n'existe pas
        try:
            c.execute("ALTER TABLE users ADD COLUMN devise TEXT DEFAULT 'EUR'")
            print("Migration: Colonne devise ajoutee a users")
        except:
            pass  # Colonne deja presente
        
        # Migration : ajouter devise_cotation aux actifs
        try:
            c.execute("ALTER TABLE actifs ADD COLUMN devise_cotation TEXT DEFAULT 'EUR'")
            print("Migration: Colonne devise_cotation ajoutee a actifs")
        except:
            pass  # Colonne deja presente
        
        # Migration : ajouter prix_debut_mois aux actifs
        try:
            c.execute("ALTER TABLE actifs ADD COLUMN prix_debut_mois REAL")
            # Initialiser avec le prix actuel pour les actifs existants
            c.execute("UPDATE actifs SET prix_debut_mois = prix_actuel WHERE prix_debut_mois IS NULL")
            print("Migration: Colonne prix_debut_mois ajoutee a actifs")
        except:
            pass  # Colonne deja presente
        
        # Table market_analysis (cache pour les conseils)
        c.execute('''CREATE TABLE IF NOT EXISTS market_analysis 
                     (ticker TEXT PRIMARY KEY, 
                      name TEXT, 
                      score REAL, 
                      nb_news INTEGER, 
                      signal TEXT, 
                      signal_class TEXT, 
                      price REAL, 
                      last_updated TEXT)''')
        
        # Table etf_analysis (cache pour les conseils ETF)
        c.execute('''CREATE TABLE IF NOT EXISTS etf_analysis 
                     (ticker TEXT PRIMARY KEY, 
                      name TEXT, 
                      score REAL, 
                      nb_news INTEGER, 
                      signal TEXT, 
                      signal_class TEXT, 
                      price REAL, 
                      last_updated TEXT,
                      expense_ratio TEXT,
                      category TEXT,
                      day_change_pct REAL,
                      trend_15d_pct REAL)''')
        
        # Migration: ajouter colonnes si manquantes
        try:
            c.execute("ALTER TABLE etf_analysis ADD COLUMN expense_ratio TEXT")
            c.execute("ALTER TABLE etf_analysis ADD COLUMN category TEXT")
            c.execute("ALTER TABLE etf_analysis ADD COLUMN day_change_pct REAL")
            c.execute("ALTER TABLE etf_analysis ADD COLUMN trend_15d_pct REAL")
        except: pass

        # Table trading_signals (cache pour l'analyse technique)
        c.execute('''CREATE TABLE IF NOT EXISTS trading_signals
                     (ticker TEXT PRIMARY KEY,
                      name TEXT,
                      rsi_14 REAL,
                      sma_20 REAL,
                      sma_50 REAL,
                      ema_12 REAL,
                      ema_26 REAL,
                      bb_upper REAL,
                      bb_middle REAL,
                      bb_lower REAL,
                      macd REAL,
                      macd_signal REAL,
                      current_price REAL,
                      previous_close REAL,
                      technical_score REAL,
                      signal TEXT,
                      signal_class TEXT,
                      signal_strength TEXT,
                      rsi_score REAL,
                      ma_score REAL,
                      bb_score REAL,
                      macd_score REAL,
                      last_updated TEXT,
                      devise TEXT,
                      data_points INTEGER)''')

        # Table ticker_fundamentals (cache des fondamentaux : dividende, PER)
        c.execute('''CREATE TABLE IF NOT EXISTS ticker_fundamentals
                     (ticker TEXT PRIMARY KEY,
                      dividend_yield REAL,
                      dividend_rate REAL,
                      trailing_pe REAL,
                      forward_pe REAL,
                      currency TEXT,
                      last_updated TEXT)''')

        # Recommandations intraday envoyées par email (sert au récap de 17h)
        c.execute('''CREATE TABLE IF NOT EXISTS intraday_email_recommendations
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      sent_date TEXT,
                      sent_time TEXT,
                      ticker TEXT,
                      name TEXT,
                      direction TEXT,
                      entry REAL,
                      stop_loss REAL,
                      target REAL,
                      technical_score REAL,
                      sentiment_score REAL,
                      news_count INTEGER,
                      devise TEXT,
                      created_at TEXT,
                      UNIQUE(sent_date, ticker, direction))''')

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur init_db: {e}")

# Initialisation au démarrage
init_db()

# --- CONSTANTES ---
# Liste des tickers du SBF 120 + CAC Mid 60 + CAC Small (Approx 250 valeurs)
SBF120_TICKERS = [
    # CAC 40 & SBF 120 (Principales)
    "AC.PA", "ACA.PA", "AI.PA", "AIR.PA", "AKE.PA", "ALO.PA", "AM.PA", "AMUN.PA", 
    "ATE.PA", "ATO.PA", "BEN.PA", "BIM.PA", "BN.PA", "BNP.PA", "BOL.PA", "BOU.PA", 
    "CA.PA", "CAP.PA", "CDI.PA", "CG.PA", "CNP.PA", "CO.PA", "COV.PA", "CS.PA", 
    "DEC.PA", "DG.PA", "DSY.PA", "EDEN.PA", "EL.PA", "ELIS.PA", "EN.PA", "ENG.PA", 
    "EO.PA", "ERF.PA", "ETL.PA", "FDJ.PA", "FGR.PA", "FNAC.PA", "FR.PA", "GFC.PA", 
    "GLE.PA", "HO.PA", "ICAD.PA", "IPN.PA", "IPS.PA", "ITP.PA", "JMT.PA", "KER.PA", 
    "KOF.PA", "LI.PA", "LR.PA", "MC.PA", "MDM.PA", "MF.PA", "ML.PA", "MMB.PA", 
    "MND.PA", "MT.PA", "NEO.PA", "NEX.PA", "NK.PA", "NOKIA.PA", "ORA.PA", "OR.PA", 
    "ORP.PA", "OVH.PA", "POM.PA", "PUB.PA", "RCO.PA", "RHA.PA", "RNO.PA", "RMS.PA", 
    "RUI.PA", "SAF.PA", "SAN.PA", "SART.PA", "SCR.PA", "SEV.PA", "SGO.PA", "SK.PA", 
    "SOI.PA", "SOLB.BR", "SOP.PA", "SPIE.PA", "STLA.PA", "STM.PA", "SU.PA", "SW.PA", 
    "TTE.PA", "UBI.PA", "URW.PA", "VAC.PA", "VIV.PA", "VK.PA", "VLA.PA", "WLN.PA",
    "XFAB.PA", "YOO.PA", "AF.PA", "ADP.PA", "GET.PA", "GTT.PA", "IDL.PA", "LTA.PA",
    "SESG.PA", "TEP.PA", "TFI.PA", "TRIG.PA", "VAL.PA", "VER.PA", "VIRP.PA",
    
    # Mid & Small Caps (Complément pour atteindre ~250)
    "ABCA.PA", "AB.PA", "ALNEV.PA", "ALTHE.PA", "ALTUR.PA", "ARG.PA", "ARR.PA", 
    "ATO.PA", "ATAME.PA", "BCOMP.PA", "BIG.PA", "BOI.PA", "BON.PA", "CEN.PA", 
    "ALCG.PA", "CLA.PA", "CLAR.PA", "CRLA.PA", "DBV.PA", "DER.PA", "DBG.PA", 
    "DPT.PA", "ECO.PA", "ELEC.PA", "ESI.PA", "ES.PA", "EXC.PA", "FII.PA", 
    "GAU.PA", "GJA.PA", "GLO.PA", "GM.PA", "GRA.PA", "GUE.PA", "GUI.PA", 
    "HAV.PA", "HER.PA", "HEXA.PA", "IDI.PA", "INF.PA", "ITL.PA", "JXR.PA", 
    "LAC.PA", "LEC.PA", "LNA.PA", "LOUP.PA", "LSS.PA", "LUM.PA", "MAU.PA", 
    "MEMS.PA", "MERY.PA", "METEX.PA", "MRB.PA", "MRN.PA", "NRO.PA", "OLG.PA", 
    "ORA.PA", "OSI.PA", "OTE.PA", "PARRO.PA", "PIG.PA", "POXEL.PA", "PRO.PA", 
    "PVL.PA", "RAL.PA", "RBT.PA", "ROTH.PA", "RXL.PA", "SEC.PA", "SII.PA", 
    "SMCP.PA", "SQS.PA", "STEF.PA", "SYN.PA", "TCH.PA", "TER.PA", "TFF.PA", 
    "TNG.PA", "TOUP.PA", "TRICS.PA", "TXT.PA", "U10.PA", "VANTI.PA", "VET.PA", 
    "VIL.PA", "VRLA.PA", "WAVE.PA", "WED.PA", "XIL.PA"
]

CAC40_TICKERS = [
    "AI.PA", "AIR.PA", "ALO.PA", "MT.PA", "CS.PA", "BNP.PA", "EN.PA", "CAP.PA",
    "CA.PA", "ACA.PA", "BN.PA", "DSY.PA", "EDEN.PA", "ENG.PA", "EL.PA", "ERF.PA",
    "RMS.PA", "KER.PA", "OR.PA", "LR.PA", "MC.PA", "ML.PA", "ORA.PA", "RI.PA",
    "PUB.PA", "RNO.PA", "SAF.PA", "SGO.PA", "SAN.PA", "SU.PA", "GLE.PA", "STLA.PA",
    "STM.PA", "TEP.PA", "HO.PA", "TTE.PA", "URW.PA", "VIE.PA", "DG.PA", "VIV.PA"
]

TICKER_NAMES_MAP = {
    # CAC 40 & SBF 120 (Principales)
    "AC.PA": "Accor", "ACA.PA": "Crédit Agricole", "AI.PA": "Air Liquide", "AIR.PA": "Airbus",
    "AKE.PA": "Arkema", "ALO.PA": "Alstom", "AM.PA": "Dassault Aviation", "AMUN.PA": "Amundi",
    "ATE.PA": "Alten", "ATO.PA": "Atos", "BEN.PA": "Beneteau", "BIM.PA": "BioMérieux",
    "BN.PA": "Danone", "BNP.PA": "BNP Paribas", "BOL.PA": "Bolloré", "BOU.PA": "Bouygues",
    "CA.PA": "Carrefour", "CAP.PA": "Capgemini", "CDI.PA": "Christian Dior", "CG.PA": "Capgemini",
    "CNP.PA": "CNP Assurances", "CO.PA": "Casino", "COV.PA": "Covivio", "CS.PA": "AXA",
    "DEC.PA": "JCDecaux", "DG.PA": "Vinci", "DSY.PA": "Dassault Systèmes", "EDEN.PA": "Edenred",
    "EL.PA": "EssilorLuxottica", "ELIS.PA": "Elis", "EN.PA": "Bouygues", "ENG.PA": "Engie",
    "EO.PA": "Faurecia", "ERF.PA": "Eurofins", "ETL.PA": "Eutelsat", "FDJ.PA": "La Française des Jeux",
    "FGR.PA": "Eiffage", "FNAC.PA": "Fnac Darty", "FR.PA": "Valeo", "GFC.PA": "Gecina",
    "GLE.PA": "Société Générale", "HO.PA": "Thales", "ICAD.PA": "Icade", "IPN.PA": "Ipsen",
    "IPS.PA": "Ipsos", "ITP.PA": "Interparfums", "JMT.PA": "Jmartins", "KER.PA": "Kering",
    "KOF.PA": "Kaufman & Broad", "LI.PA": "Klépierre", "LR.PA": "Legrand", "MC.PA": "LVMH",
    "MDM.PA": "Maisons du Monde", "MF.PA": "Wendel", "ML.PA": "Michelin", "MMB.PA": "Lagardère",
    "MND.PA": "Manitou", "MT.PA": "ArcelorMittal", "NEO.PA": "Neoen", "NEX.PA": "Nexans",
    "NK.PA": "Imerys", "NOKIA.PA": "Nokia", "ORA.PA": "Orange", "OR.PA": "L'Oréal",
    "ORP.PA": "Orpea", "OVH.PA": "OVHcloud", "POM.PA": "Plastic Omnium", "PUB.PA": "Publicis",
    "RCO.PA": "Remy Cointreau", "RHA.PA": "Korian", "RNO.PA": "Renault", "RMS.PA": "Hermès",
    "RUI.PA": "Rubis", "SAF.PA": "Safran", "SAN.PA": "Sanofi", "SART.PA": "Sartorius Stedim",
    "SCR.PA": "Scor", "SEV.PA": "Suez", "SGO.PA": "Saint-Gobain", "SK.PA": "SEB",
    "SOI.PA": "Soitec", "SOLB.BR": "Solvay", "SOP.PA": "Sopra Steria", "SPIE.PA": "Spie",
    "STLA.PA": "Stellantis", "STM.PA": "STMicroelectronics", "SU.PA": "Schneider Electric",
    "SW.PA": "Sodexo", "TTE.PA": "TotalEnergies", "UBI.PA": "Ubisoft", "URW.PA": "Unibail-Rodamco",
    "VAC.PA": "Vallourec", "VIV.PA": "Vivendi", "VK.PA": "Vallourec", "VLA.PA": "Valneva",
    "WLN.PA": "Worldline", "XFAB.PA": "X-Fab", "YOO.PA": "Yoox Net-A-Porter",
    "AF.PA": "Air France-KLM", "ADP.PA": "Aéroports de Paris", "GET.PA": "Getlink",
    "GTT.PA": "GTT", "IDL.PA": "ID Logistics", "LTA.PA": "Altamir",
    "SESG.PA": "SES", "TEP.PA": "Teleperformance", "TFI.PA": "TF1",
    "TRIG.PA": "Trigano", "VAL.PA": "Vallourec", "VER.PA": "Verallia", "VIRP.PA": "Virbac",

    # Mid & Small Caps
    "ABCA.PA": "ABC Arbitrage", "AB.PA": "AB Science", "ALNEV.PA": "Nova", 
    "ALTHE.PA": "Thema", "ALTUR.PA": "Altur Investissement", "ARG.PA": "Argan", 
    "ARR.PA": "Altarea", "ATAME.PA": "Atari", "BCOMP.PA": "Bigben", "BIG.PA": "Bigben", 
    "BOI.PA": "Boiron", "BON.PA": "Bonduelle", "CEN.PA": "Cegedim", "ALCG.PA": "Cogelec", 
    "CLA.PA": "Claranova", "CLAR.PA": "Claranova", "CRLA.PA": "Carmila", 
    "DBV.PA": "DBV Technologies", "DER.PA": "Derichebourg", "DBG.PA": "Derichebourg", 
    "DPT.PA": "Dépôt", "ECO.PA": "Econocom", "ELEC.PA": "Electricité de Strasbourg", 
    "ESI.PA": "ESI Group", "ES.PA": "Esso", "EXC.PA": "Exel Industries", "FII.PA": "LDC", 
    "GAU.PA": "Gaumont", "GLO.PA": "GL Events", "GRA.PA": "Graines Voltz", 
    "GUE.PA": "Guerbet", "GUI.PA": "Guillemot", "HAV.PA": "Havas", "HER.PA": "Hermès", 
    "HEXA.PA": "Hexaom", "IDI.PA": "IDI", "INF.PA": "Infotel", "ITL.PA": "Itesoft", 
    "JXR.PA": "Jacquet Metals", "LAC.PA": "Lacroix", "LEC.PA": "Lectra", 
    "LNA.PA": "LNA Santé", "LUM.PA": "Lumibird", "MAU.PA": "Mauna Kea", 
    "MEMS.PA": "Memscap", "MERY.PA": "Mercier", "METEX.PA": "Metabolic Explorer", 
    "MRB.PA": "Mersen", "MRN.PA": "Mersen", "NRO.PA": "Neurones", "OLG.PA": "OL Groupe", 
    "PARRO.PA": "Parrot", "POXEL.PA": "Poxel", "PVL.PA": "Plastivaloire", 
    "RAL.PA": "Rallye", "RBT.PA": "Robertet", "ROTH.PA": "Rothschild & Co", 
    "RXL.PA": "Rexel", "SII.PA": "SII", "SMCP.PA": "SMCP", "STEF.PA": "Stef", 
    "SYN.PA": "Synergie", "TCH.PA": "Technicolor", "TFF.PA": "TFF Group", 
    "TNG.PA": "Transgene", "TOUP.PA": "Toupargel", "U10.PA": "U10", 
    "VANTI.PA": "Vantiva", "VIL.PA": "Vilmorin", "VRLA.PA": "Valneva", 
    "WAVE.PA": "Wavestone", "WED.PA": "Wedia", "XIL.PA": "Xilam"
}

# --- LISTE ETF (Trackers) avec Métadonnées Manuelles ---
# Format: Ticker: (Nom, Frais %, "Type/Cap")
ETF_METADATA = {
    # Monde
    "CW8.PA": ("Amundi MSCI World", "0.38%", "Monde"),
    "EWLD.PA": ("Lyxor MSCI World", "0.45%", "Monde"),
    "WLEA.PA": ("Amundi MSCI World", "0.38%", "Monde"),
    
    # USA
    "ESE.PA": ("BNP S&P 500", "0.15%", "USA"),
    "PE500.PA": ("Amundi S&P 500", "0.15%", "USA"),
    "PUST.PA": ("Amundi US Tech", "0.30%", "Tech USA"),
    "ANX.PA": ("Amundi Nasdaq-100", "0.23%", "Tech USA"),
    "PANX.PA": ("Amundi Nasdaq-100", "0.23%", "Tech USA"),
    
    # Europe
    "ETZ.PA": ("BNP Stoxx Europe 600", "0.20%", "Europe"),
    "PMEH.PA": ("Amundi PEA Eau", "0.60%", "Thématique"),
    "MEA.PA": ("Amundi Euro Stoxx 50", "0.15%", "Europe"),
    "ETE.PA": ("BNP Stoxx Europe 600", "0.20%", "Europe"),
    
    # France
    "C40.PA": ("Amundi CAC 40", "0.25%", "France"),
    "LVC.PA": ("Lyxor CAC 40 (Levier x2)", "0.40%", "France x2"),
    "BX4.PA": ("Lyxor CAC 40 (Inverse x2)", "0.40%", "Short x2"),
    
    # Émergents / Asie
    "PAASI.PA": ("Amundi PEA Asie", "0.20%", "Asie"),
    "PLEM.PA": ("Amundi PEA Émergents", "0.20%", "Émergents"),
    "AEEM.PA": ("Amundi MSCI Emerging", "0.20%", "Émergents"),
    "INR.PA": ("Amundi MSCI India", "0.85%", "Inde"),
    
    # Autres
    "PME.PA": ("Amundi PEA PME", "0.50%", "Small Cap"),
    "HND.PA": ("Lyxor Nasdaq-100", "0.30%", "Tech USA"),
    "CL2.PA": ("Amundi MSCI USA x2", "0.35%", "USA x2"),
}

ETF_TICKERS = list(ETF_METADATA.keys())
# On garde ETF_NAMES_MAP pour compatibilité si besoin, mais on utilisera METADATA
ETF_NAMES_MAP = {k: v[0] for k, v in ETF_METADATA.items()}

# Correspondances forcées pour éviter les mauvaises places de cotation
# (ex: WRDU -> Amsterdam/USD alors qu'on veut Paris/EUR)
FORCED_SYMBOL_MAP = {
    "WRDU": "MWRD.PA",
    "IE000BI8OT95": "MWRD.PA",
    "IE000BI8OT95-WRDU": "MWRD.PA",
    "IE000BI8OT95 - WRDU": "MWRD.PA",
    "AM.CORE MSCI WORLD UC.ETF USD": "MWRD.PA",
    "AM CORE MSCI WORLD UC ETF USD": "MWRD.PA",
    "BNP": "BNP.PA",  # BNP Paribas sur Euronext Paris
    "AXA": "CS.PA",   # AXA SA sur Euronext Paris
    "SOPRA": "SOP.PA",  # Sopra Steria Group sur Euronext Paris
    "SOPRA STERIA": "SOP.PA",
    "SOPRA STERIA GROUP": "SOP.PA",
    "SOP": "SOP.PA",
    "IE00BHZRQZ17": "FLXI.PA", # Franklin FTSE India (EUR)
    "FRANKLIN FSTE INDIA UCITS ETF": "FLXI.PA", # Cas spécifique utilisateur (typo FSTE)
    "FRANKLIN FTSE INDIA UCITS ETF": "FLXI.PA",
    # Or : Yahoo renvoie souvent AMGOLDN.MX en premier (3156 MXN affiché à tort en EUR)
    "FR0013416716": "GOLD.MI",
    "AMGOLDN.MX": "GOLD.MI",
    "AMUNDI PHYSICAL GOLD ETC C": "GOLD.MI",
}

def normalize_forced_symbol(identifier):
    """Normalise certains identifiants ambigus vers un ticker canonique."""
    if not identifier:
        return identifier

    ident_upper = identifier.upper().strip()

    # ISIN Amundi Physical Gold ETC C (souvent collé à un libellé dans ticker_isin)
    if "FR0013416716" in ident_upper:
        return "GOLD.MI"

    # Match exact direct
    if ident_upper in FORCED_SYMBOL_MAP:
        return FORCED_SYMBOL_MAP[ident_upper]

    # Match par présence de motif (libellé long ETF)
    if "AM.CORE MSCI WORLD" in ident_upper or "AM CORE MSCI WORLD" in ident_upper:
        return "MWRD.PA"

    # Match par tokens robustes (gère tirets, slash, parenthèses...)
    tokens = [t for t in re.split(r"[^A-Z0-9\\.]+", ident_upper) if t]
    for token in tokens:
        if token in FORCED_SYMBOL_MAP:
            return FORCED_SYMBOL_MAP[token]

    return identifier

# --- API YAHOO FINANCE (yfinance) ---
def fetch_price_from_api(identifier):
    if not identifier: return None, None, None, None
    identifier = normalize_forced_symbol(identifier.strip())
    print(f"DEBUG fetch: Recherche pour '{identifier}'")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://finance.yahoo.com/'
    }

    symbol = None
    name = identifier

    # Priorité 0: mapping forcé pour certains tickers/ISIN ambigus
    ident_upper = identifier.upper()
    mapped_symbol = FORCED_SYMBOL_MAP.get(ident_upper)
    if not mapped_symbol:
        # Essayer en extrayant des tokens (cas "IE000... - WRDU")
        for token in [t.strip() for t in ident_upper.replace('/', ' ').replace('_', ' ').split() if t.strip()]:
            if token in FORCED_SYMBOL_MAP:
                mapped_symbol = FORCED_SYMBOL_MAP[token]
                break
    if mapped_symbol:
        symbol = mapped_symbol
        print(f"DEBUG fetch: Mapping forcé appliqué -> {symbol}")
    
    # Strategie 1: Si ca ressemble a un symbole (court, majuscules, avec .PA etc), essayer directement
    if (not symbol) and (len(identifier) <= 6 or '.' in identifier or identifier.isupper()):
        symbol = identifier.upper()
        print(f"DEBUG fetch: Test direct du symbole = {symbol}")
        # Verifier si le symbole existe en essayant de recuperer le prix
        try:
            test_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
            res = requests.get(test_url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('chart', {}).get('result'):
                    meta = data['chart']['result'][0].get('meta', {})
                    name = meta.get('longName') or meta.get('shortName') or symbol
                    print(f"DEBUG fetch: Symbole direct valide = {symbol}, nom = {name}")
                    # On garde ce symbole
                else:
                    symbol = None  # Pas trouve, on va chercher
            else:
                symbol = None
        except:
            symbol = None
    
    # Strategie 2: Si pas trouve ou pas un symbole, rechercher par nom/ISIN
    if not symbol:
        try:
            # Nettoyer certaines ponctuations qui dégradent la recherche Yahoo (ex: AM.CORE ...)
            search_query = identifier
            for ch in ['.', '-', '_', '/']:
                search_query = search_query.replace(ch, ' ')
            search_query = ' '.join(search_query.split())
            if not search_query:
                search_query = identifier
            
            search_url = f"https://query1.finance.yahoo.com/v1/finance/search?q={search_query}"
            res = requests.get(search_url, headers=headers, timeout=10)
            print(f"DEBUG fetch: Status recherche = {res.status_code} (query={search_query})")
            if res.status_code == 200:
                data = res.json()
                quotes = data.get('quotes') or []
                if quotes:
                    ident_upper = identifier.upper()
                    is_etf_query = any(k in ident_upper for k in ['ETF', 'TRACKER', 'UCITS', 'MSCI'])
                    # ISIN France (12 car.) : éviter Mexique / hors-Europe quand plusieurs cotes (scores égaux → max = premier résultat Yahoo)
                    is_fr_isin = (
                        len(ident_upper) == 12
                        and ident_upper.startswith('FR')
                        and ident_upper[2:11].isdigit()
                    )

                    def quote_score(q):
                        q_symbol = (q.get('symbol') or '').upper()
                        q_exchange = (q.get('exchange') or q.get('fullExchangeName') or '').upper()
                        q_type = (q.get('quoteType') or '').upper()
                        score = 0

                        # Priorité Euronext Paris (cas utilisateur principal)
                        if q_symbol.endswith('.PA') or q_exchange == 'PAR' or 'EURONEXT PARIS' in q_exchange:
                            score += 120
                        # Seconde priorité Bruxelles
                        if q_symbol.endswith('.BR') or q_exchange == 'BRU' or 'BRUSSELS' in q_exchange:
                            score += 70
                        # Bonus ETF quand la requête ressemble à un ETF
                        if is_etf_query and q_type == 'ETF':
                            score += 40
                        # Bonus correspondance exacte du symbole saisi
                        if q_symbol == ident_upper:
                            score += 200
                        # ISIN FR : cote où l'ISIN apparaît dans le symbole (ex. FR0013416716.SG)
                        if is_fr_isin and ident_upper in q_symbol:
                            score += 260
                        # ISIN FR : favoriser Europe, pénaliser Mexique / hors-zone (évite AMGOLDN.MX ≈ même nom, devise MXN)
                        if is_fr_isin:
                            if q_symbol.endswith(('.PA', '.AS', '.BR', '.MI', '.DE', '.SG', '.SW', '.VI', '.LS', '.IR', '.HE')):
                                score += 85
                            if q_symbol.endswith('.MX') or 'MEX' in q_exchange or 'MEXICO' in q_exchange:
                                score -= 400
                        return score

                    best_quote = max(quotes, key=quote_score)
                    symbol = best_quote.get('symbol')
                    name = best_quote.get('longname') or best_quote.get('shortname') or symbol
                    print(f"DEBUG fetch: Symbole retenu via recherche = {symbol}")
        except Exception as e:
            print(f"DEBUG fetch: Erreur recherche = {e}")
    
    # Strategie 3: En dernier recours, utiliser l'identifiant tel quel
    if not symbol:
        symbol = identifier.upper()
        print(f"DEBUG fetch: Utilisation du ticker brut = {symbol}")

    # 2. Prix via EODHD (prioritaire pour cohérence des places de cotation)
    try:
        eodhd_url = f"https://eodhd.com/api/real-time/{symbol}"
        eodhd_resp = requests.get(
            eodhd_url,
            params={"api_token": EODHD_API_KEY, "fmt": "json"},
            timeout=6
        )
        print(f"DEBUG fetch EODHD: Status prix = {eodhd_resp.status_code} ({symbol})")
        if eodhd_resp.status_code == 200:
            eodhd_data = eodhd_resp.json()
            if isinstance(eodhd_data, dict):
                # Selon endpoint EODHD, on peut recevoir close/last/adjusted_close
                price = eodhd_data.get('close')
                if price in [None, 'N/A', 'NA', '']:
                    price = eodhd_data.get('last')
                if price in [None, 'N/A', 'NA', '']:
                    price = eodhd_data.get('adjusted_close')

                prev_close = eodhd_data.get('previousClose')
                if prev_close in [None, 'N/A', 'NA', '']:
                    prev_close = eodhd_data.get('previous_close')

                if price not in [None, 'N/A', 'NA', '']:
                    price = float(price)
                    prev_close = float(prev_close) if prev_close not in [None, 'N/A', 'NA', ''] else price
                    currency = detect_currency_from_symbol(symbol)

                    # Conversion pence -> livres pour titres UK
                    if currency == 'GBP' and price > 10:
                        price = price / 100.0
                        prev_close = prev_close / 100.0
                        print(f"DEBUG fetch EODHD: Conversion pence -> livres: {price*100} -> {price}")

                    print(f"DEBUG fetch EODHD: Prix actuel = {price}, Prix veille = {prev_close}, Devise = {currency}")
                    return (round(price, 4), name, round(prev_close, 4), currency)
    except Exception as e:
        print(f"DEBUG fetch EODHD: Erreur prix = {e}")

    # 3. Fallback Yahoo
    try:
        chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
        res = requests.get(chart_url, headers=headers, timeout=10)
        print(f"DEBUG fetch: Status prix = {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            result = data.get('chart', {}).get('result')
            if result:
                meta = result[0].get('meta', {})
                price = meta.get('regularMarketPrice')
                prev_close = meta.get('previousClose')
                yahoo_currency = (meta.get('currency') or '').strip().upper()
                # Devise réelle Yahoo (MXN, USD…) ; sinon heuristique symbole (évite d'afficher des MXN comme des EUR)
                currency = yahoo_currency if yahoo_currency else detect_currency_from_symbol(symbol)
                
                # Conversion pence -> livres pour TOUTES les actions britanniques (GBP)
                if currency == 'GBP' and price and price > 10:
                    price = price / 100.0
                    prev_close = (prev_close / 100.0) if prev_close else price
                    print(f"DEBUG fetch: Action GB - Conversion pence -> livres: {price*100} -> {price}")
                
                print(f"DEBUG fetch: Prix actuel = {price}, Prix veille = {prev_close}, Devise = {currency}")
                if price is not None:
                    return (round(float(price), 4), name, round(float(prev_close or price), 4), currency)
    except Exception as e:
        print(f"DEBUG fetch: Erreur prix = {e}")

    print(f"DEBUG fetch: Aucune donnee pour {identifier}")
    return None, None, None, None

# --- ROUTES ---
@app.route('/')
def index():
    if 'user_id' in session: return redirect(url_for('dashboard'))
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

@app.route('/fix_franklin')
def fix_franklin():
    """Correction spécifique pour Franklin India (ticker et devise)"""
    if 'user_id' not in session: return redirect(url_for('index'))
    
    conn = get_connection()
    # Chercher par nom approximatif ou ticker
    franklin = conn.execute('''SELECT id, nom_actif, ticker_isin, prix_achat, quantite 
                               FROM actifs a 
                               JOIN comptes c ON a.compte_id = c.id 
                               WHERE c.user_id = ? 
                               AND (UPPER(nom_actif) LIKE '%FRANKLIN%' OR UPPER(ticker_isin) LIKE '%IE00BHZRQZ17%')
                               LIMIT 1''', (session['user_id'],)).fetchone()
    
    if franklin:
        # Force ticker FLXI.PA (Euronext Paris) et devise EUR
        # On met aussi à jour le prix actuel et veille pour éviter les sauts
        # On suppose un prix ~37€ (FLXI.PA)
        # Mais on laisse update_prices faire le vrai boulot ensuite
        conn.execute('''UPDATE actifs 
                        SET ticker_isin = 'FLXI.PA', 
                            devise_cotation = 'EUR',
                            prix_actuel = 37.5, 
                            prix_veille = 37.0 
                        WHERE id = ?''', (franklin['id'],))
        conn.commit()
        conn.close()
        
        # Lancer une mise à jour immédiate
        # On triche un peu en appelant la logique update interne ou via l'API
        # Mais rediriger vers dashboard suffit, l'utilisateur pourra cliquer sur update
        flash(f"✅ Franklin India corrigé : Ticker FLXI.PA, Devise EUR. Veuillez lancer une mise à jour des prix.", "success")
    else:
        conn.close()
        flash("❌ Franklin India non trouvé.", "error")
        
    return redirect(url_for('dashboard'))


@app.route('/fix_amundi_gold')
def fix_amundi_gold():
    """Corrige ticker + cours pour Amundi Physical Gold ETC C (bug Yahoo MXN → affiché en EUR)."""
    if "user_id" not in session:
        return redirect(url_for("index"))
    conn = get_connection()
    uid = session["user_id"]
    rows = conn.execute(
        """
        SELECT a.id, a.ticker_isin, a.nom_actif
        FROM actifs a
        JOIN comptes c ON a.compte_id = c.id
        WHERE c.user_id = ?
          AND (
            INSTR(UPPER(IFNULL(a.ticker_isin, '')), 'FR0013416716') > 0
            OR INSTR(UPPER(IFNULL(a.ticker_isin, '')), 'AMGOLDN') > 0
            OR UPPER(IFNULL(a.nom_actif, '')) LIKE '%PHYSICAL GOLD ETC C%'
          )
        """,
        (uid,),
    ).fetchall()
    if not rows:
        conn.close()
        flash("Aucun actif Amundi Physical Gold ETC C trouvé.", "error")
        return redirect(url_for("dashboard"))

    p, _name, pv, currency = fetch_price_from_api("GOLD.MI")
    if not p:
        conn.close()
        flash("Impossible de récupérer le cours pour GOLD.MI. Réessayez plus tard.", "error")
        return redirect(url_for("dashboard"))

    cur = (currency or "EUR").upper()
    for r in rows:
        conn.execute(
            """
            UPDATE actifs
            SET ticker_isin = 'GOLD.MI',
                devise_cotation = ?,
                prix_actuel = ?,
                prix_veille = ?
            WHERE id = ?
            """,
            (cur, float(p), float(pv) if pv is not None else float(p), r["id"]),
        )
    conn.commit()
    conn.close()
    flash(
        f"✅ {len(rows)} ligne(s) corrigée(s) : ticker GOLD.MI, cours ~{p:.2f} {cur}.",
        "success",
    )
    return redirect(url_for("dashboard"))


@app.route('/api/fix_ticker')
def api_fix_ticker():
    """Diagnostique et corrige les tickers mal résolus"""
    token = request.args.get('token')
    if token != CRON_TOKEN:
        return jsonify({'error': 'Non autorisé'}), 401
    
    conn = get_connection()
    # Lister tous les actifs avec leur ticker_isin actuel
    all_actifs = conn.execute('''SELECT a.id, a.nom_actif, a.ticker_isin, a.prix_actuel
                                 FROM actifs a''').fetchall()
    
    fixed = []
    for a in all_actifs:
        old_ticker = (a['ticker_isin'] or '').strip()
        nom = (a['nom_actif'] or '').upper()
        new_ticker = normalize_forced_symbol(old_ticker)
        
        # Aussi vérifier par nom si le ticker n'a pas été normalisé
        if new_ticker == old_ticker:
            new_ticker = normalize_forced_symbol(nom)
            if new_ticker == nom:
                new_ticker = old_ticker  # Pas de changement
        
        if new_ticker != old_ticker and new_ticker != nom:
            conn.execute('UPDATE actifs SET ticker_isin = ? WHERE id = ?', (new_ticker, a['id']))
            fixed.append({'id': a['id'], 'nom': a['nom_actif'], 'old': old_ticker, 'new': new_ticker})
    
    conn.commit()
    
    # Afficher aussi le diagnostic complet avec prix_veille
    all_actifs_full = conn.execute('''SELECT a.id, a.nom_actif, a.ticker_isin, a.prix_actuel, a.prix_veille, a.quantite
                                       FROM actifs a ORDER BY a.id''').fetchall()
    diag = [{'id': a['id'], 'nom': a['nom_actif'], 'ticker': a['ticker_isin'], 
             'prix': a['prix_actuel'], 'prix_veille': a['prix_veille'],
             'qty': a['quantite'], 
             'pv_jour_unitaire': round(a['prix_actuel'] - a['prix_veille'], 2) if a['prix_veille'] else 0,
             'pv_jour_total': round((a['prix_actuel'] - a['prix_veille']) * a['quantite'], 2) if a['prix_veille'] else 0
            } for a in all_actifs_full]
    conn.close()
    
    return jsonify({'fixed': fixed, 'all_actifs': diag})

@app.route('/fix_all_currencies')
def fix_all_currencies():
    """Force toutes les actions sauf Hays en EUR"""
    if 'user_id' not in session: return redirect(url_for('index'))
    conn = get_connection()
    
    # 1. Hays en GBP (et convertir pence -> livres si nécessaire)
    hays_actifs = conn.execute('''SELECT a.id, a.prix_actuel, a.prix_veille 
                                  FROM actifs a 
                                  JOIN comptes c ON a.compte_id = c.id 
                                  WHERE c.user_id = ? AND UPPER(a.ticker_isin) LIKE '%HAYS%' ''', 
                                (session['user_id'],)).fetchall()
    
    for actif in hays_actifs:
        if actif['prix_actuel'] > 10:  # En pence, convertir
            nouveau_prix_actuel = actif['prix_actuel'] / 100.0
            nouveau_prix_veille = actif['prix_veille'] / 100.0 if actif['prix_veille'] else nouveau_prix_actuel
            conn.execute('UPDATE actifs SET prix_actuel = ?, prix_veille = ?, devise_cotation = ? WHERE id = ?', 
                        (nouveau_prix_actuel, nouveau_prix_veille, 'GBP', actif['id']))
            print(f"Hays corrigé: {actif['prix_actuel']} pence -> {nouveau_prix_actuel} £")
        else:  # Déjà en livres
            conn.execute('UPDATE actifs SET devise_cotation = ? WHERE id = ?', ('GBP', actif['id']))
            print(f"Hays: devise mise à GBP")
    
    # 2. Tout le reste en EUR
    conn.execute('''UPDATE actifs 
                   SET devise_cotation = 'EUR' 
                   WHERE id IN (
                       SELECT a.id FROM actifs a 
                       JOIN comptes c ON a.compte_id = c.id 
                       WHERE c.user_id = ? AND UPPER(a.ticker_isin) NOT LIKE '%HAYS%'
                   )''', (session['user_id'],))
    
    conn.commit()
    conn.close()
    print("Toutes les devises corrigées: Hays=GBP, reste=EUR")
    return redirect(url_for('dashboard'))

@app.route('/change_currency/<currency>')
def change_currency(currency):
    if 'user_id' not in session: return redirect(url_for('index'))
    if currency in ['EUR', 'USD', 'GBP']:
        conn = get_connection()
        conn.execute('UPDATE users SET devise = ? WHERE id = ?', (currency, session['user_id']))
        conn.commit()
        conn.close()
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session: return redirect(url_for('index'))

    # Mise à jour automatique des prix à l'ouverture du dashboard
    # (anti-doublon : 5 min minimum entre deux rafraîchissements automatiques)
    auto_refresh_launched, _ = trigger_prices_refresh_async(
        user_id=session['user_id'], cumul=False, is_cron=False,
        min_interval_seconds=300,
    )

    try:
        conn = get_connection()
        comptes = conn.execute('SELECT * FROM comptes WHERE user_id = ?', (session['user_id'],)).fetchall()
        actifs = conn.execute('''SELECT a.*, c.nom_compte FROM actifs a
                                JOIN comptes c ON a.compte_id = c.id
                                WHERE c.user_id = ?''', (session['user_id'],)).fetchall()

        # Fetch trading signals for user's assets
        trading_signals = {}
        try:
            signals_data = conn.execute('SELECT * FROM trading_signals').fetchall()
            for sig in signals_data:
                trading_signals[sig['ticker']] = dict(sig)
        except:
            pass  # Table might not exist yet

        user_info = conn.execute('SELECT derniere_maj, devise FROM users WHERE id = ?', 
                                   (session['user_id'],)).fetchone()
        derniere_maj = user_info['derniere_maj'] if user_info and user_info['derniere_maj'] else 'Jamais'
        user_devise = user_info['devise'] if user_info and user_info['devise'] else 'EUR'
        currency_symbol = CURRENCY_SYMBOLS.get(user_devise, '€')
        
        # Calculer le mois actuel
        from datetime import datetime
        mois_actuel = datetime.now().strftime("%Y-%m")
        
        total_achat = 0
        total_actuel = 0
        total_pv = 0
        total_day_pv = 0
        total_month_pv = 0
        comptes_stats = {}
        
        for c in comptes:
            comptes_stats[c['id']] = {'achat': 0, 'actuel': 0, 'pv': 0, 'day_pv': 0}

        # Pour trouver les top/bottom performers du jour
        day_performances = []
        
        for a in actifs:
            p_actuel = safe_float(a['prix_actuel'])
            p_achat = safe_float(a['prix_achat'])
            p_veille = safe_float(a['prix_veille'])
            
            qty = safe_int(a['quantite'])
            frais = safe_float(a['frais'])
            try:
                devise_cotation = a['devise_cotation'] or 'EUR'
            except (KeyError, IndexError):
                devise_cotation = 'EUR'
            
            # Calculer les valeurs dans la devise de cotation
            val_actuelle = (p_actuel * qty) + frais
            val_achat = (p_achat * qty) + frais
            val_veille = (p_veille * qty) + frais
            
            pv = val_actuelle - val_achat
            
            # PV du jour : détecter si prix_veille est aberrant
            # Si prix_veille est trop éloigné du prix actuel (> 20% d'écart), utiliser prix_achat
            if p_veille == 0 or abs(p_veille - p_actuel) > (p_actuel * 0.20):
                # Prix de veille aberrant : calculer par rapport au prix d'achat
                day_pv = val_actuelle - val_achat
            else:
                # Prix de veille normal : calculer la variation du jour
                day_pv = val_actuelle - val_veille
            
            # Convertir vers EUR pour les totaux (devise de référence)
            val_actuelle_eur = convert_currency(val_actuelle, devise_cotation, 'EUR')
            val_achat_eur = convert_currency(val_achat, devise_cotation, 'EUR')
            pv_eur = convert_currency(pv, devise_cotation, 'EUR')
            day_pv_eur = convert_currency(day_pv, devise_cotation, 'EUR')
            
            # Récupérer le cumul du mois depuis la table dédiée
            # IMPORTANT : Afficher SEULEMENT le cumul (PAS la PV du jour en cours)
            # Le cumul sera mis à jour à 17h45 par le CRON
            cumul_mois_row = conn.execute(
                'SELECT cumul_pv FROM cumul_pv_mois WHERE actif_id = ? AND mois = ?',
                (a['id'], mois_actuel)
            ).fetchone()
            
            if cumul_mois_row:
                month_pv_eur = safe_float(cumul_mois_row['cumul_pv'])
            else:
                # Pas encore de cumul pour ce mois : afficher 0
                month_pv_eur = 0
            
            # Calcul de la variation journalière en %
            if p_veille > 0:
                day_perf_pct = ((p_actuel - p_veille) / p_veille) * 100
                day_performances.append({
                    'nom': a['nom_actif'],
                    'perf': day_perf_pct,
                    'pv_jour_eur': day_pv_eur
                })
            
            # Additionner en EUR
            total_achat += val_achat_eur
            total_actuel += val_actuelle_eur
            total_pv += pv_eur
            total_day_pv += day_pv_eur
            total_month_pv += month_pv_eur
            
            if a['compte_id'] in comptes_stats:
                comptes_stats[a['compte_id']]['achat'] += val_achat_eur
                comptes_stats[a['compte_id']]['actuel'] += val_actuelle_eur
                comptes_stats[a['compte_id']]['pv'] += pv_eur
                comptes_stats[a['compte_id']]['day_pv'] += day_pv_eur
        
        # Top 4 / bottom 4 performers du jour
        sorted_day_perfs = sorted(day_performances, key=lambda x: x['perf'], reverse=True)
        top_gainers = sorted_day_perfs[:4]
        top_losers = sorted(day_performances, key=lambda x: x['perf'])[:4]
        
        conn.close()
        
        return render_template('dashboard.html', comptes=comptes, actifs=actifs,
                              trading_signals=trading_signals,
                              user_nom=session.get('user_nom'), total_pv=total_pv,
                              total_achat=total_achat, total_actuel=total_actuel,
                              total_day_pv=total_day_pv, total_month_pv=total_month_pv,
                              derniere_maj=derniere_maj,
                              comptes_stats=comptes_stats,
                              top_gainers=top_gainers, top_losers=top_losers,
                              user_devise=user_devise, currency_symbol=currency_symbol,
                              auto_refresh_launched=auto_refresh_launched,
                              cron_token=CRON_TOKEN)
    except Exception as e:
        return f"Erreur Dashboard: {e}"

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
    ticker_input = (request.form.get('ticker') or '').strip()
    # Si l'utilisateur saisit le symbole dans le champ Nom, on le récupère quand même.
    ticker = normalize_forced_symbol(ticker_input or nom or '')
    pa = safe_float(request.form.get('prix_achat'))
    q = safe_int(request.form.get('quantite'), 1)
    fr = safe_float(request.form.get('frais'))
    pnow = safe_float(request.form.get('prix_actuel'))
    date_achat = request.form.get('date_achat', '')
    devise_cotation = detect_currency_from_symbol(ticker)

    if ticker and pnow <= 0:
        price, fetched_name, prev_close, currency = fetch_price_from_api(ticker)
        if price is not None:
            pnow = price
            devise_cotation = currency or devise_cotation
            if not nom:
                nom = fetched_name or ticker
    
    conn = get_connection()
    # Initialiser prix_veille avec prix_achat pour que la PV du premier jour soit calculée par rapport à l'achat
    cursor = conn.execute('INSERT INTO actifs (compte_id, nom_actif, ticker_isin, prix_achat, quantite, frais, prix_actuel, prix_veille, date_achat, devise_cotation) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (compte_id, nom, ticker, pa, q, fr, pnow, pa, date_achat, devise_cotation))
    
    # Archiver le prix initial dans l'historique
    actif_id = cursor.lastrowid
    date_actuelle = datetime.now().strftime("%Y-%m-%d")
    mois_actuel = datetime.now().strftime("%Y-%m")
    conn.execute('INSERT INTO historique_prix (actif_id, date, prix, devise) VALUES (?, ?, ?, ?)',
                (actif_id, date_actuelle, pnow, devise_cotation))
    
    # Initialiser le cumul du mois à 0 pour ce nouvel actif
    conn.execute('INSERT INTO cumul_pv_mois (actif_id, mois, cumul_pv, derniere_mise_a_jour) VALUES (?, ?, 0, ?)',
                (actif_id, mois_actuel, date_actuelle))
    
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/update_actif/<int:actif_id>', methods=['POST'])
def update_actif(actif_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    
    conn = get_connection()
    # Récupérer la devise de cotation de l'actif
    actif_info = conn.execute('SELECT devise_cotation FROM actifs WHERE id = ?', (actif_id,)).fetchone()
    devise_cotation = actif_info['devise_cotation'] if actif_info else 'EUR'
    
    nom = request.form.get('nom')
    pa = safe_float(request.form.get('prix_achat'))
    q = safe_int(request.form.get('quantite'))
    fr = safe_float(request.form.get('frais'))
    pnow = safe_float(request.form.get('prix_actuel'))
    date_achat = request.form.get('date_achat', '')
    
    # Les prix sont déjà dans la devise de cotation de l'actif, pas besoin de conversion
    conn.execute('UPDATE actifs SET nom_actif=?, prix_achat=?, quantite=?, frais=?, prix_actuel=?, date_achat=? WHERE id=?',
                (nom, pa, q, fr, pnow, date_achat, actif_id))
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
    price, name, prev_close, currency = fetch_price_from_api(ticker)
    return jsonify({
        'price': price,
        'name': name,
        'prev_close': prev_close,
        'currency': currency,
        'ticker': normalize_forced_symbol(ticker)
    })

# Cache global pour l'analyse
conseil_cache = {
    'data': None,
    'timestamp': None
}

import concurrent.futures


INTRADAY_BULLETIN_PROMPT = """Tu es un analyste financier expert en trading intraday sur les marchés actions (CAC 40, S&P 500, Nasdaq). Tu travailles pour le site monpecule.fr, dont le public est composé de débutants qui apprennent à trader. Ton rôle est de produire des analyses claires, pédagogiques et actionnables, sans jargon inutile — ou en expliquant chaque terme technique utilisé.

---

Pour la séance du {DATE}, génère un bulletin intraday complet structuré en 3 blocs :

BLOC 1 — Calendrier économique
• Liste les événements macro importants de la journée (heure, indicateur, pays, impact attendu : faible / moyen / fort)
• Explique en 1 phrase simple pourquoi chaque événement fort peut faire bouger les marchés

BLOC 2 — Analyse technique du marché
• Donne l'état général des indices principaux (CAC 40, S&P 500) : tendance dominante, zone de support et résistance du jour
• Identifie 1 à 2 actions ou secteurs à surveiller en priorité
• Pour chaque opportunité, précise : la direction (achat / vente), le niveau d'entrée suggéré, le stop-loss et l'objectif de gain

BLOC 3 — Signaux de trading
• Présente 2 à 3 signaux concrets pour la journée au format :
  → Actif : [nom de l'action]
  → Direction : Achat ou Vente
  → Entrée : [niveau de prix]
  → Stop-loss : [niveau de prix]
  → Objectif : [niveau de prix]
  → Justification : [explication simple en 2-3 phrases pour un débutant]

---

• Écris pour un débutant complet : définis chaque terme technique la première fois qu'il apparaît
• Sois factuel et précis — évite les formulations vagues comme "le marché pourrait monter"
• Utilise des emojis discrets pour aérer (📅 🔍 📊) mais ne surcharge pas
• Longueur idéale : 400 à 600 mots par bulletin
• Termine toujours par une note de risque rappelant que le trading comporte des pertes et que le contenu est éducatif, pas un conseil financier

---

Si tu es sollicité plusieurs fois dans la journée, adapte le bulletin en précisant l'heure de mise à jour et en signalant tout changement notable par rapport au bulletin du matin (ex : "⚠️ Mise à jour 14h30 — Publication des chiffres de l'emploi US : résultat supérieur aux attentes, révision du signal sur le S&P 500")."""


# =============================================================================
# Fondamentaux : dividende et PER (cache yfinance)
# =============================================================================
_fundamentals_refresh_lock = threading.Lock()
_fundamentals_refresh_running = False


def fetch_fundamentals_yfinance(ticker):
    """Récupère dividende et PER pour un ticker via yfinance.

    Retourne un dict ou None en cas d'échec. Les champs peuvent être None
    si yfinance ne les fournit pas (ETF, action sans dividende, etc.).
    """
    try:
        t = yf.Ticker(ticker)
        info = {}
        try:
            info = t.info or {}
        except Exception:
            # Sur certaines versions de yfinance, .info peut lever ; on tente fast_info
            try:
                fi = getattr(t, 'fast_info', None)
                if fi:
                    info = {
                        'currency': getattr(fi, 'currency', None),
                        'trailingPE': None,
                        'dividendYield': None,
                        'dividendRate': None,
                    }
            except Exception:
                info = {}

        dy = info.get('dividendYield')
        if dy is not None:
            try:
                dy = float(dy)
                # yfinance peut retourner 0.025 (décimal) ou 2.5 (%) selon versions
                if dy < 1:
                    dy *= 100
            except Exception:
                dy = None

        def _safe(value):
            try:
                f = float(value)
                if f != f or f in (float('inf'), float('-inf')):
                    return None
                return f
            except (TypeError, ValueError):
                return None

        return {
            'ticker': ticker,
            'dividend_yield': dy,
            'dividend_rate': _safe(info.get('dividendRate')),
            'trailing_pe': _safe(info.get('trailingPE')),
            'forward_pe': _safe(info.get('forwardPE')),
            'currency': info.get('currency') or detect_currency_from_symbol(ticker),
        }
    except Exception as e:
        print(f"fetch_fundamentals_yfinance {ticker}: {e}")
        return None


def _run_fundamentals_refresh(tickers):
    """Rafraîchit les fondamentaux des tickers en parallèle et écrit en base."""
    global _fundamentals_refresh_running
    try:
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            future_to_t = {ex.submit(fetch_fundamentals_yfinance, t): t for t in tickers}
            for fut in concurrent.futures.as_completed(future_to_t):
                try:
                    res = fut.result()
                    if res:
                        results.append(res)
                except Exception as e:
                    print(f"[fundamentals] erreur future: {e}")

        if not results:
            return

        now = datetime.now().strftime('%d/%m/%Y à %H:%M')
        conn = get_connection()
        try:
            for r in results:
                conn.execute('''INSERT INTO ticker_fundamentals
                               (ticker, dividend_yield, dividend_rate, trailing_pe, forward_pe, currency, last_updated)
                               VALUES (?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(ticker) DO UPDATE SET
                                 dividend_yield=excluded.dividend_yield,
                                 dividend_rate=excluded.dividend_rate,
                                 trailing_pe=excluded.trailing_pe,
                                 forward_pe=excluded.forward_pe,
                                 currency=excluded.currency,
                                 last_updated=excluded.last_updated''',
                             (r['ticker'], r['dividend_yield'], r['dividend_rate'],
                              r['trailing_pe'], r['forward_pe'], r['currency'], now))
            conn.commit()
        finally:
            conn.close()
        print(f"[fundamentals] {len(results)} tickers rafraîchis")
    except Exception as e:
        print(f"[fundamentals] erreur globale: {e}")
    finally:
        with _fundamentals_refresh_lock:
            _fundamentals_refresh_running = False


def trigger_fundamentals_refresh_async(tickers, max_age_hours=24, force=False):
    """Lance un rafraîchissement des fondamentaux en arrière-plan pour les tickers
    dont la donnée en cache est absente ou plus vieille que max_age_hours.

    Retourne True si lancé, False sinon (déjà en cours ou rien à rafraîchir).
    """
    global _fundamentals_refresh_running
    tickers = [t.upper().strip() for t in tickers if t]
    if not tickers:
        return False

    # Sélection des tickers à rafraîchir
    cutoff = datetime.now()
    to_refresh = []
    try:
        conn = get_connection()
        rows = conn.execute(
            'SELECT ticker, last_updated FROM ticker_fundamentals WHERE ticker IN ({})'.format(
                ','.join('?' for _ in tickers)
            ),
            tickers,
        ).fetchall()
        last_by_ticker = {row['ticker'].upper(): row['last_updated'] for row in rows}
        conn.close()
    except Exception:
        last_by_ticker = {}

    for t in tickers:
        last_str = last_by_ticker.get(t)
        if force or not last_str:
            to_refresh.append(t)
            continue
        last_dt = _parse_signal_datetime(last_str)
        if last_dt is None or (cutoff - last_dt).total_seconds() > max_age_hours * 3600:
            to_refresh.append(t)

    if not to_refresh:
        return False

    with _fundamentals_refresh_lock:
        if _fundamentals_refresh_running:
            return False
        _fundamentals_refresh_running = True

    thread = threading.Thread(target=_run_fundamentals_refresh, args=(to_refresh,), daemon=True)
    thread.start()
    return True


def get_fundamentals_for_tickers(tickers):
    """Retourne {ticker_upper: {dividend_yield, dividend_rate, trailing_pe, ...}}."""
    tickers = [t.upper().strip() for t in tickers if t]
    if not tickers:
        return {}
    try:
        conn = get_connection()
        rows = conn.execute(
            'SELECT * FROM ticker_fundamentals WHERE ticker IN ({})'.format(
                ','.join('?' for _ in tickers)
            ),
            tickers,
        ).fetchall()
        conn.close()
        return {row['ticker'].upper(): dict(row) for row in rows}
    except Exception as e:
        print(f"get_fundamentals_for_tickers: {e}")
        return {}


def _parse_signal_datetime(value):
    """Parse le champ last_updated 'DD/MM/YYYY à HH:MM' en datetime, None si invalide."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%d/%m/%Y à %H:%M')
    except Exception:
        try:
            return datetime.strptime(value.split(' à ')[0].strip(), '%d/%m/%Y')
        except Exception:
            return None


def get_trading_signals_freshness():
    """Retourne (count, last_updated_str, last_updated_dt) pour la table trading_signals."""
    conn = get_connection()
    try:
        row = conn.execute(
            'SELECT COUNT(*) as cnt, MAX(last_updated) as last_updated FROM trading_signals'
        ).fetchone()
        count = row['cnt'] if row else 0
        last_str = row['last_updated'] if row else None
    except Exception:
        count, last_str = 0, None
    finally:
        conn.close()
    return count, last_str, _parse_signal_datetime(last_str)


def build_intraday_universe(include_cac40=True, include_user_assets=True, extra_tickers=None):
    """Construit l'univers de tickers à analyser et un mapping nom complet.

    Par défaut : CAC 40 + tous les actifs présents dans le portefeuille des utilisateurs.
    """
    universe = set()
    ticker_names = dict(TICKER_NAMES_MAP)

    if include_cac40:
        universe.update(t.upper() for t in CAC40_TICKERS)

    if include_user_assets:
        try:
            conn = get_connection()
            rows = conn.execute(
                'SELECT DISTINCT ticker_isin, nom_actif FROM actifs WHERE ticker_isin IS NOT NULL AND ticker_isin != ""'
            ).fetchall()
            conn.close()
            for r in rows:
                t = (r['ticker_isin'] or '').upper().strip()
                if t:
                    universe.add(t)
                    ticker_names.setdefault(t, r['nom_actif'] or t)
        except Exception as e:
            print(f"build_intraday_universe: erreur lecture actifs: {e}")

    if extra_tickers:
        for t in extra_tickers:
            if t:
                universe.add(t.upper())

    return sorted(universe), ticker_names


# Verrou pour éviter plusieurs rafraîchissements simultanés
_signals_refresh_lock = threading.Lock()
_signals_refresh_running = False


def _run_trading_signals_refresh(tickers, ticker_names):
    """Exécute l'analyse technique et remplace la table trading_signals."""
    global _signals_refresh_running
    try:
        print(f"[signaux] Démarrage de l'analyse technique pour {len(tickers)} tickers")
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_ticker = {
                executor.submit(analyze_ticker_technical, t, EODHD_API_KEY, ticker_names): t
                for t in tickers
            }
            for future in concurrent.futures.as_completed(future_to_ticker):
                try:
                    res = future.result()
                    if res:
                        results.append(res)
                except Exception as e:
                    print(f"[signaux] erreur future: {e}")

        if not results:
            print("[signaux] Aucun résultat exploitable, table conservée.")
            return

        now = datetime.now().strftime('%d/%m/%Y à %H:%M')
        conn = get_connection()
        try:
            conn.execute('DELETE FROM trading_signals')
            for r in results:
                conn.execute('''INSERT INTO trading_signals
                               (ticker, name, rsi_14, sma_20, sma_50, ema_12, ema_26,
                                bb_upper, bb_middle, bb_lower, macd, macd_signal,
                                current_price, previous_close, technical_score,
                                signal, signal_class, signal_strength,
                                rsi_score, ma_score, bb_score, macd_score,
                                last_updated, devise, data_points)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                             (r['ticker'], r['name'], r['rsi_14'], r['sma_20'], r['sma_50'],
                              r['ema_12'], r['ema_26'], r['bb_upper'], r['bb_middle'], r['bb_lower'],
                              r['macd'], r['macd_signal'], r['current_price'], r['previous_close'],
                              r['technical_score'], r['signal'], r['signal_class'], r['signal_strength'],
                              r['rsi_score'], r['ma_score'], r['bb_score'], r['macd_score'],
                              now, r['devise'], r['data_points']))
            conn.commit()
        finally:
            conn.close()
        print(f"[signaux] Analyse technique terminée : {len(results)} tickers sauvegardés")
    except Exception as e:
        print(f"[signaux] erreur globale: {e}")
    finally:
        with _signals_refresh_lock:
            _signals_refresh_running = False


def trigger_trading_signals_refresh_async(tickers=None, ticker_names=None):
    """Lance en arrière-plan un rafraîchissement de la table trading_signals.

    Retourne True si lancé, False si un rafraîchissement est déjà en cours."""
    global _signals_refresh_running
    with _signals_refresh_lock:
        if _signals_refresh_running:
            return False
        _signals_refresh_running = True

    if tickers is None:
        tickers, ticker_names = build_intraday_universe()
    elif ticker_names is None:
        _, ticker_names = build_intraday_universe()

    thread = threading.Thread(
        target=_run_trading_signals_refresh,
        args=(tickers, ticker_names),
        daemon=True,
    )
    thread.start()
    return True


def build_intraday_opportunities(limit=6, tickers=None, prefer_today=True):
    """Sélectionne les meilleurs signaux techniques et ajoute des niveaux simples de trading.

    - Privilégie les signaux mis à jour aujourd'hui (filtre par date).
    - Ajoute un tie-breaker quotidien pour faire tourner les opportunités
      lorsque plusieurs actions ont des scores très proches.
    """
    tickers_filter = {t.upper() for t in tickers} if tickers else None
    today_str = datetime.now().strftime('%d/%m/%Y')

    conn = get_connection()
    try:
        technical_rows = conn.execute('''
            SELECT *
            FROM trading_signals
            WHERE current_price IS NOT NULL
              AND current_price > 0
              AND signal_strength IN ('ACHAT FORT', 'ACHAT', 'VENTE FORTE', 'VENTE')
            LIMIT 200
        ''').fetchall()

        sentiment_rows = conn.execute('SELECT ticker, score, nb_news, signal FROM market_analysis').fetchall()
        sentiment_by_ticker = {row['ticker']: dict(row) for row in sentiment_rows}
    except Exception:
        technical_rows = []
        sentiment_by_ticker = {}
    finally:
        conn.close()

    today_rows = [r for r in technical_rows if (r['last_updated'] or '').startswith(today_str)]
    if prefer_today and today_rows:
        rows_to_use = today_rows
    else:
        rows_to_use = list(technical_rows)

    daily_seed = int(datetime.now().strftime('%Y%m%d'))

    opportunities = []
    for row in rows_to_use:
        item = dict(row)
        if tickers_filter and item['ticker'].upper() not in tickers_filter:
            continue
        sentiment = sentiment_by_ticker.get(item['ticker'], {})
        sentiment_score = safe_float(sentiment.get('score'), 0.5)
        news_count = safe_int(sentiment.get('nb_news'), 0)
        technical_score = safe_float(item.get('technical_score'), 0)
        price = safe_float(item.get('current_price'))
        direction = 'Achat' if 'ACHAT' in (item.get('signal_strength') or '') else 'Vente'
        if direction == 'Achat':
            technical_strength = technical_score
            sentiment_boost = sentiment_score * 20
            stop_loss = price * 0.985
            target = price * 1.025
        else:
            technical_strength = 100 - technical_score
            sentiment_boost = (1 - sentiment_score) * 20
            stop_loss = price * 1.015
            target = price * 0.975
        combined_score = technical_strength + sentiment_boost + min(news_count, 5)

        # Tie-breaker quotidien : variation pseudo-aléatoire stable sur la journée
        # (la même journée donne le même ordre, mais l'ordre change chaque jour).
        rotation_bonus = ((hash(item['ticker']) ^ daily_seed) % 1000) / 1000.0

        opportunities.append({
            'ticker': item['ticker'],
            'name': item.get('name') or item['ticker'],
            'direction': direction,
            'entry': round(price, 2),
            'stop_loss': round(stop_loss, 2),
            'target': round(target, 2),
            'technical_score': round(technical_score, 1),
            'sentiment_score': round(sentiment_score, 2),
            'news_count': news_count,
            'signal': item.get('signal') or item.get('signal_strength'),
            'rsi': item.get('rsi_14'),
            'combined_score': round(combined_score, 1),
            'last_updated': item.get('last_updated'),
            '_rotation_bonus': rotation_bonus,
            'devise': item.get('devise') or detect_currency_from_symbol(item['ticker'])
        })

    # Tri : score combiné d'abord, puis rotation quotidienne pour départager les scores proches.
    opportunities.sort(key=lambda x: (x['combined_score'], x['_rotation_bonus']), reverse=True)
    for opp in opportunities:
        opp.pop('_rotation_bonus', None)

    top = opportunities[:limit]

    # Enrichissement : dividende (rendement %, valeur par action) et PER
    if top:
        tickers_top = [opp['ticker'] for opp in top]
        fundamentals = get_fundamentals_for_tickers(tickers_top)
        for opp in top:
            f = fundamentals.get(opp['ticker'].upper(), {})
            opp['dividend_yield'] = f.get('dividend_yield')
            opp['dividend_rate'] = f.get('dividend_rate')
            opp['trailing_pe'] = f.get('trailing_pe')
            opp['forward_pe'] = f.get('forward_pe')
        # Déclenche un rafraîchissement async des fondamentaux (cache 24h).
        try:
            trigger_fundamentals_refresh_async(tickers_top, max_age_hours=24)
        except Exception as e:
            print(f"trigger_fundamentals_refresh_async: {e}")

    return top


def build_intraday_prompt_with_context(opportunities):
    today = datetime.now().strftime('%d/%m/%Y')
    update_time = datetime.now().strftime('%H:%M')
    prompt = INTRADAY_BULLETIN_PROMPT.replace('{DATE}', f'{today} — mise à jour {update_time}')
    if not opportunities:
        return prompt

    lines = [
        "",
        "---",
        "",
        "Données internes monpecule.fr à prendre en compte pour sélectionner les signaux :"
    ]
    for opp in opportunities[:6]:
        # Fondamentaux (peuvent être None si yfinance n'a pas la donnée)
        dy = opp.get('dividend_yield')
        dr = opp.get('dividend_rate')
        tpe = opp.get('trailing_pe') or opp.get('forward_pe')
        if dy is not None and dy > 0:
            if dr is not None and dr > 0:
                div_str = f"dividende {dy:.2f}% ({dr:.2f} {opp['devise']}/action)"
            else:
                div_str = f"dividende {dy:.2f}%"
        else:
            div_str = "pas de dividende"
        per_str = f"PER {tpe:.1f}" if tpe is not None and tpe > 0 else "PER N/A"

        lines.append(
            f"- {opp['name']} ({opp['ticker']}) : {opp['direction']}, entrée {opp['entry']} {opp['devise']}, "
            f"stop-loss {opp['stop_loss']} {opp['devise']}, objectif {opp['target']} {opp['devise']}, "
            f"score technique {opp['technical_score']}/100, sentiment news {opp['sentiment_score']}, "
            f"{opp['news_count']} news récentes, {div_str}, {per_str}."
        )
    lines.append("")
    lines.append("Sélectionne seulement les 2 à 3 meilleures opportunités et explique clairement pourquoi.")
    return prompt + "\n".join(lines)


def send_email(subject, text_body, html_body=None, to_email=None):
    """Envoie un email via SMTP configuré par variables d'environnement."""
    if not SMTP_HOST or not SMTP_FROM:
        raise RuntimeError("SMTP non configuré : définir SMTP_HOST, SMTP_USER/SMTP_FROM et SMTP_PASSWORD")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email or INTRADAY_EMAIL_TO
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        if SMTP_USER and SMTP_PASSWORD:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


def refresh_cac40_trading_signals():
    """Recalcule les signaux techniques CAC 40 en synchrone pour les emails cron."""
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_ticker = {
            executor.submit(analyze_ticker_technical, ticker, EODHD_API_KEY, TICKER_NAMES_MAP): ticker
            for ticker in CAC40_TICKERS
        }
        for future in concurrent.futures.as_completed(future_to_ticker):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                print(f"Erreur signal CAC40: {e}")

    if not results:
        return 0

    conn = get_connection()
    now = datetime.now().strftime('%d/%m/%Y à %H:%M')
    for r in results:
        conn.execute('''REPLACE INTO trading_signals
                       (ticker, name, rsi_14, sma_20, sma_50, ema_12, ema_26,
                        bb_upper, bb_middle, bb_lower, macd, macd_signal,
                        current_price, previous_close, technical_score,
                        signal, signal_class, signal_strength,
                        rsi_score, ma_score, bb_score, macd_score,
                        last_updated, devise, data_points)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                     (r['ticker'], r['name'], r['rsi_14'], r['sma_20'], r['sma_50'],
                      r['ema_12'], r['ema_26'], r['bb_upper'], r['bb_middle'], r['bb_lower'],
                      r['macd'], r['macd_signal'], r['current_price'], r['previous_close'],
                      r['technical_score'], r['signal'], r['signal_class'], r['signal_strength'],
                      r['rsi_score'], r['ma_score'], r['bb_score'], r['macd_score'],
                      now, r['devise'], r['data_points']))
    conn.commit()
    conn.close()
    return len(results)


def save_morning_intraday_recommendations(opportunities):
    today = datetime.now().strftime('%Y-%m-%d')
    now = datetime.now().strftime('%H:%M')
    created_at = datetime.now().isoformat(timespec='seconds')
    conn = get_connection()
    conn.execute('DELETE FROM intraday_email_recommendations WHERE sent_date = ?', (today,))
    for opp in opportunities:
        conn.execute('''INSERT INTO intraday_email_recommendations
                        (sent_date, sent_time, ticker, name, direction, entry, stop_loss, target,
                         technical_score, sentiment_score, news_count, devise, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                     (today, now, opp['ticker'], opp['name'], opp['direction'], opp['entry'],
                      opp['stop_loss'], opp['target'], opp['technical_score'], opp['sentiment_score'],
                      opp['news_count'], opp['devise'], created_at))
    conn.commit()
    conn.close()


def build_morning_intraday_email(opportunities):
    today = datetime.now().strftime('%d/%m/%Y')
    rows = []
    for opp in opportunities:
        rows.append(
            f"{opp['name']} ({opp['ticker']})\n"
            f"Direction : {opp['direction']}\n"
            f"Entrée : {opp['entry']} {opp['devise']} | Stop-loss : {opp['stop_loss']} {opp['devise']} | Objectif : {opp['target']} {opp['devise']}\n"
            f"Score technique : {opp['technical_score']}/100 | Sentiment news : {opp['sentiment_score']} ({opp['news_count']} news)\n"
        )
    text = (
        f"Bonjour,\n\n"
        f"Voici les opportunités CAC 40 proposées pour la séance du {today} à 9h.\n\n"
        + "\n".join(rows)
        + "\nNote de risque : contenu éducatif, pas un conseil financier. Le trading peut entraîner des pertes.\n"
    )
    html_rows = "".join(
        f"<tr><td><b>{opp['name']}</b><br><small>{opp['ticker']}</small></td>"
        f"<td>{opp['direction']}</td><td>{opp['entry']} {opp['devise']}</td>"
        f"<td>{opp['stop_loss']} {opp['devise']}</td><td>{opp['target']} {opp['devise']}</td>"
        f"<td>{opp['technical_score']}/100<br><small>News {opp['sentiment_score']} · {opp['news_count']}</small></td></tr>"
        for opp in opportunities
    )
    html = f"""
    <h2>Opportunités CAC 40 du {today} - 9h</h2>
    <table border="1" cellspacing="0" cellpadding="8" style="border-collapse:collapse;font-family:Arial,sans-serif;">
      <tr><th>Actif</th><th>Direction</th><th>Entrée</th><th>Stop-loss</th><th>Objectif</th><th>Score</th></tr>
      {html_rows}
    </table>
    <p><b>Note de risque :</b> contenu éducatif, pas un conseil financier. Le trading peut entraîner des pertes.</p>
    """
    return text, html


def build_evening_intraday_email():
    today_iso = datetime.now().strftime('%Y-%m-%d')
    today_fr = datetime.now().strftime('%d/%m/%Y')
    conn = get_connection()
    rows = conn.execute('''
        SELECT *
        FROM intraday_email_recommendations
        WHERE sent_date = ?
        ORDER BY id
    ''', (today_iso,)).fetchall()
    conn.close()

    if not rows:
        text = f"Aucune recommandation CAC 40 du matin trouvée pour le {today_fr}."
        return text, f"<p>{text}</p>", []

    recap = []
    for row in rows:
        current_price, _name, _prev_close, currency = fetch_price_from_api(row['ticker'])
        current = safe_float(current_price)
        entry = safe_float(row['entry'])
        direction = row['direction']
        if current > 0 and entry > 0:
            perf_pct = ((current - entry) / entry) * 100
            good = perf_pct > 0 if direction == 'Achat' else perf_pct < 0
            move_text = f"{perf_pct:+.2f}%"
        else:
            good = False
            move_text = "N/A"
        recap.append({
            'name': row['name'],
            'ticker': row['ticker'],
            'direction': direction,
            'entry': entry,
            'current': round(current, 2) if current else None,
            'devise': currency or row['devise'],
            'move_text': move_text,
            'good': good,
            'result': 'Bonne' if good else 'Pas validée'
        })

    good_count = sum(1 for item in recap if item['good'])
    text_lines = [
        f"Bonsoir,\n\nRécap des opportunités CAC 40 envoyées ce matin ({today_fr}).",
        f"Résultat : {good_count}/{len(recap)} signal(aux) dans le bon sens.\n"
    ]
    for item in recap:
        text_lines.append(
            f"{item['result']} — {item['name']} ({item['ticker']}) : {item['direction']}, "
            f"entrée {item['entry']} {item['devise']}, cours 17h {item['current']} {item['devise']}, "
            f"variation {item['move_text']}."
        )
    text_lines.append("\nNote : récap éducatif basé sur le prix disponible à 17h, pas un conseil financier.")

    html_rows = "".join(
        f"<tr><td>{'✅' if item['good'] else '⚠️'} {item['result']}</td>"
        f"<td><b>{item['name']}</b><br><small>{item['ticker']}</small></td>"
        f"<td>{item['direction']}</td><td>{item['entry']} {item['devise']}</td>"
        f"<td>{item['current']} {item['devise']}</td><td>{item['move_text']}</td></tr>"
        for item in recap
    )
    html = f"""
    <h2>Récap CAC 40 du {today_fr} - 17h</h2>
    <p><b>Résultat :</b> {good_count}/{len(recap)} signal(aux) dans le bon sens.</p>
    <table border="1" cellspacing="0" cellpadding="8" style="border-collapse:collapse;font-family:Arial,sans-serif;">
      <tr><th>Verdict</th><th>Actif</th><th>Direction</th><th>Entrée</th><th>Cours 17h</th><th>Variation</th></tr>
      {html_rows}
    </table>
    <p><b>Note :</b> récap éducatif basé sur le prix disponible à 17h, pas un conseil financier.</p>
    """
    return "\n".join(text_lines), html, recap

# =============================================================================
# TECHNICAL ANALYSIS ENGINE - Trading Signals
# =============================================================================

def calculate_rsi(prices, period=14):
    """
    Calculate Relative Strength Index
    RSI = 100 - (100 / (1 + RS))
    RS = Average Gain / Average Loss
    """
    if len(prices) < period + 1:
        return None

    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilders smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)

def calculate_sma(prices, period):
    """Calculate Simple Moving Average"""
    if len(prices) < period:
        return None
    return round(sum(prices[-period:]) / period, 4)

def calculate_ema(prices, period):
    """
    Calculate Exponential Moving Average
    EMA = Price(t) × k + EMA(y) × (1 − k)
    k = 2 / (N + 1)
    """
    if len(prices) < period:
        return None

    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # Start with SMA

    for price in prices[period:]:
        ema = (price * k) + (ema * (1 - k))

    return round(ema, 4)

def calculate_bollinger_bands(prices, period=20, std_dev=2):
    """
    Calculate Bollinger Bands
    Middle Band = SMA(20)
    Upper Band = SMA(20) + (StdDev(20) × 2)
    Lower Band = SMA(20) - (StdDev(20) × 2)
    """
    if len(prices) < period:
        return None, None, None

    recent_prices = prices[-period:]
    middle = sum(recent_prices) / period

    variance = sum((p - middle) ** 2 for p in recent_prices) / period
    std = variance ** 0.5

    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)

    return round(upper, 4), round(middle, 4), round(lower, 4)

def calculate_macd(prices, fast=12, slow=26, signal=9):
    """
    Calculate MACD (Moving Average Convergence Divergence)
    MACD Line = EMA(12) - EMA(26)
    Signal Line = EMA(9) of MACD Line
    """
    if len(prices) < slow:
        return None, None

    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)

    if ema_fast is None or ema_slow is None:
        return None, None

    macd_line = ema_fast - ema_slow

    # Approximation simplifiée pour la signal line
    # En production, il faudrait calculer l'EMA des valeurs MACD historiques
    signal_line = macd_line * 0.9

    return round(macd_line, 4), round(signal_line, 4)

def fetch_historical_data_technical(ticker, days=70):
    """
    Fetch historical OHLC data from EODHD API
    Returns list of closing prices (oldest to newest)
    """
    try:
        from datetime import timedelta
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        hist_url = f"https://eodhd.com/api/eod/{ticker}"
        params = {
            "from": start_date.strftime("%Y-%m-%d"),
            "to": end_date.strftime("%Y-%m-%d"),
            "api_token": EODHD_API_KEY,
            "fmt": "json"
        }

        resp = requests.get(hist_url, params=params, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                prices = [float(candle['close']) for candle in data]
                return prices

        return None
    except Exception as e:
        print(f"Error fetching historical data for {ticker}: {e}")
        return None

def calculate_technical_score(rsi, current_price, sma_20, sma_50, ema_12, ema_26,
                               bb_upper, bb_middle, bb_lower, macd, macd_signal):
    """
    Calculate weighted technical score (0-100)

    Weights:
    - RSI: 30%
    - Moving Averages: 35%
    - Bollinger Bands: 20%
    - MACD: 15%
    """
    scores = {}

    # 1. RSI Score (30 points max)
    # Oversold (<30) = Strong Buy, Overbought (>70) = Strong Sell
    if rsi is not None:
        if rsi <= 30:
            scores['rsi'] = 30  # Strong buy signal
        elif rsi <= 40:
            scores['rsi'] = 25  # Buy signal
        elif rsi <= 60:
            scores['rsi'] = 15  # Neutral
        elif rsi <= 70:
            scores['rsi'] = 5   # Sell signal
        else:
            scores['rsi'] = 0   # Strong sell signal
    else:
        scores['rsi'] = 15  # Neutral if no data

    # 2. Moving Average Score (35 points max)
    # Golden Cross (short > long) = Bullish
    # Death Cross (short < long) = Bearish
    ma_score = 0
    if sma_20 and sma_50 and ema_12 and ema_26 and current_price:
        # SMA crossover (15 points)
        if sma_20 > sma_50:
            ma_score += 15  # Bullish
        elif sma_20 < sma_50:
            ma_score += 0   # Bearish
        else:
            ma_score += 7.5  # Neutral

        # Price vs SMA-20 (10 points)
        if current_price > sma_20:
            ma_score += 10  # Above SMA = Bullish
        elif current_price < sma_20:
            ma_score += 0   # Below SMA = Bearish
        else:
            ma_score += 5

        # EMA crossover (10 points)
        if ema_12 > ema_26:
            ma_score += 10  # Bullish
        elif ema_12 < ema_26:
            ma_score += 0   # Bearish
        else:
            ma_score += 5
    else:
        ma_score = 17.5  # Neutral if no data

    scores['ma'] = ma_score

    # 3. Bollinger Bands Score (20 points max)
    # Near lower band = Oversold (Buy)
    # Near upper band = Overbought (Sell)
    if bb_upper and bb_middle and bb_lower and current_price:
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            position = (current_price - bb_lower) / bb_range

            if position <= 0.2:
                scores['bb'] = 20  # Near lower band = Strong buy
            elif position <= 0.4:
                scores['bb'] = 15  # Below middle = Buy
            elif position <= 0.6:
                scores['bb'] = 10  # Around middle = Neutral
            elif position <= 0.8:
                scores['bb'] = 5   # Above middle = Sell
            else:
                scores['bb'] = 0   # Near upper band = Strong sell
        else:
            scores['bb'] = 10  # Neutral
    else:
        scores['bb'] = 10  # Neutral if no data

    # 4. MACD Score (15 points max)
    # MACD > Signal = Bullish
    # MACD < Signal = Bearish
    if macd is not None and macd_signal is not None:
        diff = macd - macd_signal

        if diff > 0.5:
            scores['macd'] = 15  # Strong bullish divergence
        elif diff > 0:
            scores['macd'] = 12  # Bullish
        elif diff > -0.5:
            scores['macd'] = 3   # Bearish
        else:
            scores['macd'] = 0   # Strong bearish divergence
    else:
        scores['macd'] = 7.5  # Neutral if no data

    # Total score
    total_score = sum(scores.values())

    return round(total_score, 2), scores

def generate_signal_from_score(score):
    """
    Convert technical score to signal strength and display
    """
    if score >= 80:
        return "🟢 ACHAT FORT", "signal-achat", "ACHAT FORT"
    elif score >= 60:
        return "🟢 ACHAT", "signal-achat", "ACHAT"
    elif score >= 40:
        return "🟡 NEUTRE", "signal-neutre", "NEUTRE"
    elif score >= 20:
        return "🔴 VENTE", "signal-vente", "VENTE"
    else:
        return "🔴 VENTE FORTE", "signal-vente", "VENTE FORTE"

def analyze_ticker_technical(ticker, api_key, ticker_names):
    """
    Analyze a single ticker with technical indicators
    Executed in parallel via ThreadPoolExecutor
    """
    try:
        # Normalize ticker
        search_ticker = ticker
        if '.' not in search_ticker and not search_ticker.isdigit():
            search_ticker = f"{search_ticker}.PA"

        name = ticker_names.get(ticker, ticker)

        # 1. Fetch historical data (70 days for 50-period indicators + buffer)
        prices = fetch_historical_data_technical(search_ticker, days=70)

        if not prices or len(prices) < 20:
            print(f"Insufficient data for {ticker}: {len(prices) if prices else 0} days")
            return None

        print(f"✓ Analyzing {ticker} with {len(prices)} days of data")
        current_price = prices[-1]
        previous_close = prices[-2] if len(prices) > 1 else current_price

        # 2. Calculate technical indicators
        rsi_14 = calculate_rsi(prices, 14)
        sma_20 = calculate_sma(prices, 20)
        sma_50 = calculate_sma(prices, 50)
        ema_12 = calculate_ema(prices, 12)
        ema_26 = calculate_ema(prices, 26)
        bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(prices, 20, 2)
        macd, macd_signal = calculate_macd(prices, 12, 26, 9)

        # 3. Calculate weighted score
        technical_score, component_scores = calculate_technical_score(
            rsi_14, current_price, sma_20, sma_50, ema_12, ema_26,
            bb_upper, bb_middle, bb_lower, macd, macd_signal
        )

        # 4. Generate signal
        signal, signal_class, signal_strength = generate_signal_from_score(technical_score)

        # 5. Detect currency
        devise = detect_currency_from_symbol(search_ticker)

        return {
            'ticker': ticker,
            'name': name,
            'rsi_14': rsi_14,
            'sma_20': sma_20,
            'sma_50': sma_50,
            'ema_12': ema_12,
            'ema_26': ema_26,
            'bb_upper': bb_upper,
            'bb_middle': bb_middle,
            'bb_lower': bb_lower,
            'macd': macd,
            'macd_signal': macd_signal,
            'current_price': current_price,
            'previous_close': previous_close,
            'technical_score': technical_score,
            'signal': signal,
            'signal_class': signal_class,
            'signal_strength': signal_strength,
            'rsi_score': component_scores['rsi'],
            'ma_score': component_scores['ma'],
            'bb_score': component_scores['bb'],
            'macd_score': component_scores['macd'],
            'devise': devise,
            'data_points': len(prices)
        }

    except Exception as e:
        print(f"Error analyzing {ticker}: {e}")
        return None

def analyze_ticker(ticker, api_key, base_url, realtime_url, ticker_names):
    """Analyse un seul ticker (exécuté en parallèle)"""
    try:
        # Normalisation du ticker : Ajouter .PA si pas de suffixe (pour Euronext Paris par défaut)
        search_ticker = ticker
        if '.' not in search_ticker and not search_ticker.isdigit() and not search_ticker.endswith('.BR'):
             search_ticker = f"{search_ticker}.PA"
             
        # 1. Sentiment (News)
        score = 0.5  # Neutre par défaut
        nb_news = 0
        sentiments = []
        
        try:
            params = {"s": search_ticker, "limit": 10, "api_token": api_key, "fmt": "json"}
            resp = requests.get(base_url, params=params, timeout=3).json()
            
            if isinstance(resp, list):
                # Filtrer les news trop vieilles (> 30 jours)
                # Format date API: "2025-05-06T10:32:49+00:00"
                current_date = datetime.now()
                valid_sentiments = []
                
                for item in resp:
                    if isinstance(item, dict) and item.get("sentiment") and item["sentiment"].get("polarity") is not None:
                        date_str = item.get("date", "")
                        try:
                            # Tenter de parser la date (format ISO simple)
                            if date_str:
                                news_date = datetime.strptime(date_str.split('T')[0], "%Y-%m-%d")
                                delta = current_date - news_date
                                # Si la news a plus de 10 jours, on l'ignore (news fraiches uniquement)
                                if delta.days > 10 or delta.days < -2:
                                    continue
                        except:
                            pass # Si erreur de date, on garde par prudence ou on jette ? Gardons pour l'instant.
                            
                        valid_sentiments.append(item["sentiment"]["polarity"])
                
                if valid_sentiments:
                    score = sum(valid_sentiments) / len(valid_sentiments)
                    nb_news = len(valid_sentiments)
                else:
                    # Aucune news récente valide
                    nb_news = 0
                    
        except Exception as e:
            print(f"Erreur sentiment {ticker}: {e}")

        # 2. Prix actuel
        price = None
        try:
            price_resp = requests.get(f"{realtime_url}/{search_ticker}", 
                                     params={"api_token": api_key, "fmt": "json"}, 
                                     timeout=3)
            price_data = price_resp.json()
            if 'close' in price_data and price_data['close'] not in ['NA', 'N/A', None, '']:
                price = float(price_data['close'])
        except Exception as e:
            print(f"Erreur prix {ticker}: {e}")
        
        # 4. Signal
        if nb_news == 0:
            signal = "⚪ PAS DE NEWS"
            signal_class = "signal-neutre"
            score = 0.5 # Force neutre
        elif score >= 0.5:
            signal = "🟢 ACHAT"
            signal_class = "signal-achat"
        elif score < 0.0:
            signal = "🔴 VENTE"
            signal_class = "signal-vente"
        else:
            signal = "🟡 NEUTRE"
            signal_class = "signal-neutre"
        
        return {
            "ticker": ticker, 
            "name": ticker_names.get(ticker, ticker),
            "score": score,
            "nb_news": nb_news,
            "signal": signal,
            "signal_class": signal_class,
            "price": price
        }
    except Exception as e:
        print(f"Erreur thread global {ticker}: {e}")
    return None

def analyze_etf_trend(ticker, api_key, realtime_url, ticker_names):
    """Analyse technique ETF : Tendance sur 15 jours"""
    try:
        from datetime import timedelta
        # Récupérer les métadonnées manuelles
        meta = ETF_METADATA.get(ticker, (ticker_names.get(ticker, ticker), "N/A", "ETF"))
        name, expense, category = meta[0], meta[1], meta[2]
        
        # 1. Récupérer l'historique récent (20 jours pour être large avec les weekends)
        today = datetime.now()
        start_date = (today - timedelta(days=25)).strftime("%Y-%m-%d")
        
        hist_url = f"https://eodhd.com/api/eod/{ticker}"
        params = {
            "from": start_date,
            "api_token": api_key,
            "fmt": "json"
        }
        
        try:
            resp = requests.get(hist_url, params=params, timeout=5)
            data = resp.json()
            
            if isinstance(data, list) and len(data) > 10:
                # Prix actuel (le dernier de la liste)
                last_candle = data[-1]
                price = float(last_candle['close'])
                
                # Prix de la veille (avant-dernier élément)
                prev_candle = data[-2] if len(data) >= 2 else data[-1]
                price_prev = float(prev_candle['close'])
                
                # Variation journalière
                day_change_pct = ((price - price_prev) / price_prev) * 100
                
                # Prix il y a environ 15 jours (on vise l'index -11 car ~10 jours de bourse = 2 semaines)
                # Si on a assez de données, on prend l'élément à l'index -11 (10 jours ouvrés en arrière)
                idx_past = -11 if len(data) >= 11 else 0
                past_candle = data[idx_past]
                price_past = float(past_candle['close'])
                date_past = past_candle['date'] # Pour info
                
                # Calcul de la tendance 15j
                trend_pct = ((price - price_past) / price_past) * 100
                
                # Score et Signal
                # > +1.5% en 15j = Forte Hausse
                # entre 0 et 1.5% = Légère Hausse
                # < 0 = Baisse
                
                score = 0.5
                if trend_pct > 2.0:
                    score = 0.9
                    signal = f"🟢 TENDANCE FORTE (+{trend_pct:.1f}%)"
                    signal_class = "signal-achat"
                elif trend_pct > 0.5:
                    score = 0.7
                    signal = f"🟢 HAUSSE (+{trend_pct:.1f}%)"
                    signal_class = "signal-achat"
                elif trend_pct < -2.0:
                    score = 0.1
                    signal = f"🔴 BAISSE FORTE ({trend_pct:.1f}%)"
                    signal_class = "signal-vente"
                elif trend_pct < -0.5:
                    score = 0.3
                    signal = f"🔴 BAISSE ({trend_pct:.1f}%)"
                    signal_class = "signal-vente"
                else:
                    score = 0.5
                    signal = f"⚪ STABLE ({trend_pct:.1f}%)"
                    signal_class = "signal-neutre"

                return {
                    "ticker": ticker, 
                    "name": name,
                    "score": score,
                    "nb_news": 15, 
                    "signal": signal,
                    "signal_class": signal_class,
                    "price": price,
                    "expense_ratio": expense,
                    "category": category,
                    "day_change_pct": day_change_pct,
                    "trend_15d_pct": trend_pct
                }
                
        except Exception as e:
            print(f"Erreur historique ETF {ticker}: {e}")
            return None

    except Exception as e:
        print(f"Erreur analyze_etf_trend {ticker}: {e}")
    return None

@app.route('/conseil-du-jour')
def conseil_du_jour():
    """Affiche l'analyse de sentiment depuis la base de données.

    Enrichit chaque ticker avec son dividende (% et valeur) et son PER
    depuis la table ticker_fundamentals (rafraîchi en arrière-plan).
    """
    if 'user_id' not in session:
        return redirect(url_for('index'))

    conn = get_connection()
    results_db = conn.execute('SELECT * FROM market_analysis ORDER BY score DESC').fetchall()
    conn.close()

    last_update = "Jamais"
    if results_db:
        last_update = results_db[0]['last_updated']

    results = [dict(row) for row in results_db]

    # Enrichissement dividende + PER pour toutes les valeurs affichées
    if results:
        tickers = [r['ticker'] for r in results if r.get('ticker')]
        fundamentals = get_fundamentals_for_tickers(tickers)
        for r in results:
            f = fundamentals.get((r.get('ticker') or '').upper(), {})
            r['dividend_yield'] = f.get('dividend_yield')
            r['dividend_rate'] = f.get('dividend_rate')
            r['trailing_pe'] = f.get('trailing_pe')
            r['forward_pe'] = f.get('forward_pe')
            r['fundamentals_currency'] = f.get('currency')

        # Rafraîchit en arrière-plan les tickers absents ou périmés (> 24h)
        try:
            trigger_fundamentals_refresh_async(tickers, max_age_hours=24)
        except Exception as e:
            print(f"trigger_fundamentals_refresh_async (conseil): {e}")

    achats = sum(1 for r in results if r['signal'] == "🟢 ACHAT")
    ventes = sum(1 for r in results if r['signal'] == "🔴 VENTE")
    neutres = sum(1 for r in results if r['signal'] == "🟡 NEUTRE")

    data = {
        'results': results,
        'achats': achats,
        'ventes': ventes,
        'neutres': neutres,
        'date_maj': last_update,
    }

    return render_template('conseil.html', **data)


def build_portfolio_recommendations(user_id, user_devise='EUR'):
    """Pour chaque actif du portefeuille de l'utilisateur, croise les signaux
    techniques (table trading_signals) et de sentiment news (market_analysis)
    avec la plus-value courante pour proposer une action.

    Actions possibles :
      - VENDRE_FORT      : signal technique très négatif + sentiment baissier
      - VENDRE           : signal négatif ou survente confirmée
      - ALLEGER          : prise de bénéfice (PV > 20%) avec signal au mieux neutre
      - COUPER_PERTE     : perte importante (< -15%) sans signal positif fort
      - RENFORCER        : signal très positif + sentiment positif
      - CONSERVER        : aucun signal d'action

    Retourne une liste d'actifs enrichis triée par sell_score décroissant.
    """
    conn = get_connection()
    try:
        actifs_rows = conn.execute('''
            SELECT a.*, c.nom_compte
            FROM actifs a
            JOIN comptes c ON a.compte_id = c.id
            WHERE c.user_id = ?
        ''', (user_id,)).fetchall()

        signal_rows = conn.execute('SELECT * FROM trading_signals').fetchall()
        signals = {row['ticker'].upper(): dict(row) for row in signal_rows}

        sentiment_rows = conn.execute('SELECT ticker, score, nb_news, signal FROM market_analysis').fetchall()
        sentiments = {row['ticker'].upper(): dict(row) for row in sentiment_rows}
    finally:
        conn.close()

    # action_order : ordre d'affichage demandé
    #   1. Couper perte (le plus urgent à arbitrer)
    #   2. Vendre fort, puis vendre
    #   3. Alléger
    #   4. Renforcer
    #   5. Conserver
    actions_meta = {
        'COUPER_PERTE':  ('🟠 Couper la perte',  'action-couper',      1),
        'VENDRE_FORT':   ('🔴 Vendre rapidement', 'action-vendre-fort', 2),
        'VENDRE':        ('🟠 Vendre',           'action-vendre',      3),
        'ALLEGER':       ('🟡 Alléger (prendre bénéfice)', 'action-alleger', 4),
        'RENFORCER':     ('🟢 Renforcer',        'action-renforcer',   5),
        'CONSERVER':     ('⚪ Conserver',         'action-conserver',   6),
    }

    recommendations = []
    for a in actifs_rows:
        ticker = (a['ticker_isin'] or '').upper().strip()
        if not ticker:
            continue

        # Données portefeuille
        p_actuel = safe_float(a['prix_actuel'])
        p_achat = safe_float(a['prix_achat'])
        quantite = safe_int(a['quantite'])
        try:
            devise_cot = a['devise_cotation'] or 'EUR'
        except (KeyError, IndexError):
            devise_cot = 'EUR'

        valeur_actuelle = p_actuel * quantite
        valeur_achat = p_achat * quantite
        pv_pct = ((p_actuel - p_achat) / p_achat * 100) if p_achat > 0 else 0
        pv_eur = convert_currency(valeur_actuelle - valeur_achat, devise_cot, 'EUR')
        val_actuelle_user = convert_currency(valeur_actuelle, devise_cot, user_devise)

        # Signaux : recherche exacte puis sur le ticker court (avant le ".")
        sig = signals.get(ticker) or signals.get(ticker.split('.')[0])
        sent = sentiments.get(ticker) or sentiments.get(ticker.split('.')[0])

        technical_score = safe_float(sig.get('technical_score'), 50) if sig else 50
        rsi_14 = safe_float(sig.get('rsi_14'), 50) if sig else 50
        signal_strength = (sig.get('signal_strength') or '').upper() if sig else ''
        sig_label = (sig.get('signal') if sig else '') or ''

        sentiment_score = safe_float(sent.get('score'), 0.5) if sent else 0.5
        nb_news = safe_int(sent.get('nb_news'), 0) if sent else 0
        sentiment_label = (sent.get('signal') if sent else '') or ''

        # Score de vente : 100 = vente forte, 0 = conserver/acheter
        sell_score = (
            (100 - technical_score) * 0.50
            + (1 - sentiment_score) * 100 * 0.30
            + max(0, rsi_14 - 50) * 2 * 0.20
        )
        if pv_pct > 25:
            sell_score += 8
        elif pv_pct > 15:
            sell_score += 4
        if pv_pct < -15:
            sell_score += 6

        # Décision (priorité descendante)
        action = 'CONSERVER'
        reasons = []

        is_strong_sell = signal_strength == 'VENTE FORTE'
        is_sell = signal_strength in ('VENTE', 'VENTE FORTE')
        is_strong_buy = signal_strength == 'ACHAT FORT'
        is_buy = signal_strength in ('ACHAT', 'ACHAT FORT')

        if is_strong_sell or (rsi_14 >= 75 and sentiment_score < 0.45):
            action = 'VENDRE_FORT'
        elif is_sell or (rsi_14 >= 70 and sentiment_score < 0.5) or (technical_score < 30 and sentiment_score < 0.45):
            action = 'VENDRE'
        elif pv_pct >= 20 and (is_sell or rsi_14 >= 65 or sentiment_score < 0.45):
            action = 'ALLEGER'
        elif pv_pct <= -15 and not is_strong_buy and sentiment_score < 0.55:
            action = 'COUPER_PERTE'
        elif is_strong_buy and sentiment_score >= 0.55:
            action = 'RENFORCER'
        else:
            action = 'CONSERVER'

        # Justifications
        if sig:
            if signal_strength:
                reasons.append(f"Signal technique : {signal_strength.title()} (score {technical_score:.0f}/100)")
            if rsi_14 >= 70:
                reasons.append(f"RSI surcheté à {rsi_14:.0f}")
            elif rsi_14 <= 30:
                reasons.append(f"RSI survendu à {rsi_14:.0f}")
        else:
            reasons.append("Pas d'analyse technique disponible (lancer Analyse Technique)")

        if sent and nb_news > 0:
            polarity_pct = (sentiment_score - 0.5) * 200  # -100 → +100
            if polarity_pct <= -15:
                reasons.append(f"Sentiment news négatif ({polarity_pct:+.0f} sur {nb_news} news)")
            elif polarity_pct >= 15:
                reasons.append(f"Sentiment news positif ({polarity_pct:+.0f} sur {nb_news} news)")
            else:
                reasons.append(f"Sentiment news neutre ({nb_news} news récentes)")

        if pv_pct >= 20:
            reasons.append(f"Plus-value latente confortable ({pv_pct:+.1f}%) → prise de bénéfice envisageable")
        elif pv_pct <= -15:
            reasons.append(f"Moins-value latente importante ({pv_pct:+.1f}%) → couper la perte ?")

        action_label, action_class, action_order = actions_meta[action]

        # Clé secondaire intra-catégorie (du plus urgent au moins urgent) :
        #   COUPER_PERTE : PV la plus négative en premier
        #   VENDRE_FORT/VENDRE : sell_score le plus élevé en premier
        #   ALLEGER      : PV la plus élevée en premier (verrouille gain max)
        #   RENFORCER    : sell_score le plus bas en premier (= signal d'achat le plus fort)
        #   CONSERVER    : sell_score décroissant pour rester cohérent
        if action == 'COUPER_PERTE':
            secondary = pv_pct  # plus négatif = priorité (tri croissant)
        elif action == 'ALLEGER':
            secondary = -pv_pct  # plus haut = priorité (tri croissant après négation)
        elif action == 'RENFORCER':
            secondary = sell_score  # plus bas = priorité (tri croissant)
        else:
            secondary = -sell_score  # plus haut = priorité (tri croissant après négation)

        recommendations.append({
            'id': a['id'],
            'compte': a['nom_compte'],
            'name': a['nom_actif'],
            'ticker': ticker,
            'quantite': quantite,
            'prix_achat': round(p_achat, 4),
            'prix_actuel': round(p_actuel, 4),
            'devise_cotation': devise_cot,
            'pv_pct': round(pv_pct, 2),
            'pv_eur': round(pv_eur, 2),
            'val_actuelle_user': round(val_actuelle_user, 2),
            'technical_score': round(technical_score, 1) if sig else None,
            'rsi_14': round(rsi_14, 1) if sig else None,
            'signal_strength': sig.get('signal_strength') if sig else None,
            'signal_label': sig_label,
            'sentiment_score': round(sentiment_score, 2) if sent else None,
            'sentiment_label': sentiment_label,
            'nb_news': nb_news,
            'has_signal': bool(sig),
            'has_sentiment': bool(sent),
            'sell_score': round(sell_score, 1),
            'action': action,
            'action_label': action_label,
            'action_class': action_class,
            'action_order': action_order,
            '_secondary': secondary,
            'reasons': reasons,
        })

    recommendations.sort(key=lambda x: (x['action_order'], x['_secondary']))
    for r in recommendations:
        r.pop('_secondary', None)
    return recommendations


@app.route('/conseil-portefeuille')
def conseil_portefeuille():
    """Page qui propose une action (vendre/alléger/conserver/renforcer) sur
    chaque actif du portefeuille à partir des signaux techniques et news.

    Rafraîchit automatiquement les signaux techniques quand ils sont périmés
    (plus de 4 h ou date différente) en réutilisant le même mécanisme que la
    page Opportunités Intraday.
    """
    if 'user_id' not in session:
        return redirect(url_for('index'))

    now = datetime.now()
    force_refresh = request.args.get('refresh') == '1'

    count, last_updated_str, last_updated_dt = get_trading_signals_freshness()
    is_stale = True
    if last_updated_dt is not None and count > 0:
        same_day = last_updated_dt.date() == now.date()
        recent = (now - last_updated_dt).total_seconds() < 4 * 3600
        is_stale = not (same_day and recent)

    refresh_launched = False
    if force_refresh or is_stale:
        refresh_launched = trigger_trading_signals_refresh_async()

    # Récupérer la devise préférée de l'utilisateur
    conn = get_connection()
    user_info = conn.execute('SELECT devise FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    user_devise = user_info['devise'] if user_info and user_info['devise'] else 'EUR'
    currency_symbol = CURRENCY_SYMBOLS.get(user_devise, '€')

    recommendations = build_portfolio_recommendations(session['user_id'], user_devise=user_devise)

    # Statistiques d'ensemble
    counters = {'VENDRE_FORT': 0, 'VENDRE': 0, 'ALLEGER': 0, 'COUPER_PERTE': 0, 'RENFORCER': 0, 'CONSERVER': 0}
    for r in recommendations:
        counters[r['action']] = counters.get(r['action'], 0) + 1

    return render_template(
        'conseil_portefeuille.html',
        recommendations=recommendations,
        counters=counters,
        signals_last_updated=last_updated_str,
        is_stale=is_stale,
        refresh_launched=refresh_launched,
        signals_running=_signals_refresh_running,
        date_seance=now.strftime('%d/%m/%Y'),
        heure_maj=now.strftime('%H:%M'),
        user_devise=user_devise,
        currency_symbol=currency_symbol,
        user_nom=session.get('user_nom'),
    )


@app.route('/opportunites-intraday')
def opportunites_intraday():
    """Page de sélection des meilleures opportunités intraday + prompt IA.

    Vérifie automatiquement la fraîcheur des signaux et lance un recalcul
    asynchrone (CAC 40 + portefeuille utilisateur) si les données sont
    périmées (autre jour, ou plus de 4 heures).
    """
    if 'user_id' not in session:
        return redirect(url_for('index'))

    now = datetime.now()
    force_refresh = request.args.get('refresh') == '1'

    count, last_updated_str, last_updated_dt = get_trading_signals_freshness()
    is_stale = True
    if last_updated_dt is not None and count > 0:
        same_day = last_updated_dt.date() == now.date()
        recent = (now - last_updated_dt).total_seconds() < 4 * 3600
        is_stale = not (same_day and recent)

    refresh_launched = False
    if force_refresh or is_stale:
        refresh_launched = trigger_trading_signals_refresh_async()

    opportunities = build_intraday_opportunities(limit=6)
    prompt = build_intraday_prompt_with_context(opportunities)

    if last_updated_str:
        freshness_label = f"Signaux mis à jour le {last_updated_str}"
    else:
        freshness_label = "Aucun signal en cache pour l'instant"

    if refresh_launched:
        refresh_status = (
            "Recalcul lancé en arrière-plan sur le CAC 40 + ton portefeuille. "
            "Rafraîchis la page dans 1 à 2 minutes pour voir la sélection du jour."
        )
    elif _signals_refresh_running:
        refresh_status = "Analyse technique déjà en cours, rafraîchis dans 1 à 2 minutes."
    else:
        refresh_status = None

    return render_template(
        'opportunites_intraday.html',
        opportunities=opportunities,
        prompt=prompt,
        date_seance=now.strftime('%d/%m/%Y'),
        heure_maj=now.strftime('%H:%M'),
        freshness_label=freshness_label,
        refresh_status=refresh_status,
        is_stale=is_stale,
        signals_count=count,
    )


@app.route('/api/cron/intraday_email/<moment>')
def cron_intraday_email(moment):
    """Envoie l'email intraday CAC 40 du matin ou le récap du soir."""
    if 'user_id' not in session and request.args.get('token') != CRON_TOKEN:
        return jsonify({'error': 'Non autorisé'}), 401

    moment = moment.lower().strip()
    if moment not in ['morning', 'evening']:
        return jsonify({'error': 'Moment invalide. Utiliser morning ou evening.'}), 400

    try:
        if moment == 'morning':
            refreshed = refresh_cac40_trading_signals()
            opportunities = build_intraday_opportunities(limit=3, tickers=CAC40_TICKERS)
            if not opportunities:
                return jsonify({
                    'success': False,
                    'message': 'Aucune opportunité CAC 40 exploitable après recalcul.',
                    'refreshed': refreshed
                }), 200

            save_morning_intraday_recommendations(opportunities)
            text_body, html_body = build_morning_intraday_email(opportunities)
            subject = f"MonPecule - Opportunités CAC 40 du {datetime.now().strftime('%d/%m/%Y')} à 9h"
            send_email(subject, text_body, html_body)
            return jsonify({
                'success': True,
                'sent_to': INTRADAY_EMAIL_TO,
                'moment': 'morning',
                'refreshed': refreshed,
                'opportunities': opportunities
            })

        text_body, html_body, recap = build_evening_intraday_email()
        subject = f"MonPecule - Récap CAC 40 du {datetime.now().strftime('%d/%m/%Y')} à 17h"
        send_email(subject, text_body, html_body)
        return jsonify({
            'success': True,
            'sent_to': INTRADAY_EMAIL_TO,
            'moment': 'evening',
            'recap': recap
        })
    except Exception as e:
        print(f"Erreur email intraday {moment}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/update_market_analysis')
def update_market_analysis():
    """Lance la mise à jour de l'analyse en arrière-plan"""
    # Token de sécurité simple pour éviter les abus (optionnel)
    if 'user_id' not in session and request.args.get('token') != CRON_TOKEN:
        return jsonify({'error': 'Non autorisé'}), 401

    def run_update():
        conn = get_connection()
        # Configuration API
        API_KEY = EODHD_API_KEY
        BASE_URL = "https://eodhd.com/api/news"
        REALTIME_API_URL = "https://eodhd.com/api/real-time"
        
        # Combiner SBF 120 + Actifs utilisateurs
        all_tickers = set(SBF120_TICKERS)
        
        # Ajouter les actifs de l'utilisateur qui ne seraient pas dans la liste
        try:
            user_actifs = conn.execute('SELECT DISTINCT ticker_isin, nom_actif FROM actifs WHERE ticker_isin != ""').fetchall()
            for actif in user_actifs:
                t = actif['ticker_isin'].upper()
                all_tickers.add(t)
                if t not in TICKER_NAMES_MAP:
                    TICKER_NAMES_MAP[t] = actif['nom_actif']
        except:
            pass
            
        final_list = list(all_tickers)
        print(f"DEBUG: Lancement analyse pour {len(final_list)} titres")
        
        # Exécution parallèle optimisée pour 250 titres
        results_to_save = []
        # Augmenter à 15 workers pour accélérer (EODHD supporte bien la concurrence)
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            future_to_ticker = {
                executor.submit(analyze_ticker, t, API_KEY, BASE_URL, REALTIME_API_URL, TICKER_NAMES_MAP): t 
                for t in final_list
            }
            for future in concurrent.futures.as_completed(future_to_ticker):
                try:
                    res = future.result()
                    if res:
                        results_to_save.append(res)
                except Exception as e:
                    print(f"Erreur future: {e}")
        
        # Sauvegarde en base
        now = datetime.now().strftime('%d/%m/%Y à %H:%M')
        
        # Vider la table avant d'insérer (ou faire un upsert)
        conn.execute('DELETE FROM market_analysis')
        
        for r in results_to_save:
            conn.execute('''INSERT INTO market_analysis (ticker, name, score, nb_news, signal, signal_class, price, last_updated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                         (r['ticker'], r['name'], r['score'], r['nb_news'], r['signal'], r['signal_class'], r['price'], now))
        
        conn.commit()
        conn.close()
        print(f"DEBUG: Analyse terminée et sauvegardée ({len(results_to_save)} titres)")

    # Lancer le thread
    thread = threading.Thread(target=run_update)
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'message': 'Analyse lancée en fond. Rafraichissez dans quelques minutes.'})

@app.route('/api/check_analysis_status')
def check_analysis_status():
    """Vérifie si des données sont disponibles dans la table market_analysis"""
    if 'user_id' not in session: return jsonify({'error': 'Non autorisé'}), 401
    
    conn = get_connection()
    count = conn.execute('SELECT COUNT(*) as cnt FROM market_analysis').fetchone()['cnt']
    last_updated = "Jamais"
    if count > 0:
        row = conn.execute('SELECT last_updated FROM market_analysis LIMIT 1').fetchone()
        if row: last_updated = row['last_updated']
        
    conn.close()
    
    # On renvoie le nombre de résultats et la date de dernière mise à jour
    # Le frontend pourra décider de rafraîchir la page si count > 0 et que c'était 0 avant
    return jsonify({'count': count, 'last_updated': last_updated})

@app.route('/api/update_trading_signals')
def update_trading_signals():
    """Analyse technique d'un univers élargi (CAC 40 + portefeuille utilisateur).

    Authentification : session utilisateur OU token CRON.
    Le calcul est exécuté en arrière-plan pour ne pas bloquer la requête.
    """
    if 'user_id' not in session and request.args.get('token') != CRON_TOKEN:
        return jsonify({'error': 'Non autorisé'}), 401

    universe, ticker_names = build_intraday_universe()
    if not universe:
        return jsonify({
            'success': False,
            'message': 'Aucun ticker à analyser (portefeuille vide et CAC 40 indisponible).'
        })

    launched = trigger_trading_signals_refresh_async(universe, ticker_names)
    return jsonify({
        'success': True,
        'launched': launched,
        'tickers': len(universe),
        'message': (
            f"Analyse technique lancée sur {len(universe)} tickers (CAC 40 + portefeuille). "
            "Rafraîchissez la page dans 1 à 2 minutes."
            if launched else
            "Une analyse technique est déjà en cours. Rafraîchissez dans 1 à 2 minutes."
        )
    })

@app.route('/api/check_trading_status')
def check_trading_status():
    """Check if trading signals data is available"""
    if 'user_id' not in session:
        return jsonify({'error': 'Non autorisé'}), 401

    conn = get_connection()
    count = conn.execute('SELECT COUNT(*) as cnt FROM trading_signals').fetchone()['cnt']
    last_updated = "Jamais"

    if count > 0:
        row = conn.execute('SELECT last_updated FROM trading_signals LIMIT 1').fetchone()
        if row:
            last_updated = row['last_updated']

    conn.close()

    return jsonify({'count': count, 'last_updated': last_updated})

@app.route('/debug_stellantis')
def debug_stellantis():
    """Voir les valeurs exactes de Stellantis dans la base"""
    if 'user_id' not in session: 
        return redirect(url_for('index'))
    
    try:
        conn = get_connection()
        
        stellantis = conn.execute('''SELECT a.* 
                                     FROM actifs a 
                                     JOIN comptes c ON a.compte_id = c.id 
                                     WHERE c.user_id = ? 
                                     AND (a.nom_actif LIKE '%Stellantis%' OR a.ticker_isin LIKE '%STLA%')
                                     LIMIT 1''', 
                                 (session['user_id'],)).fetchone()
        
        if stellantis:
            try:
                devise = stellantis['devise_cotation'] or 'EUR'
            except (KeyError, IndexError):
                devise = 'EUR'
            
            info = f"""
            <html><body style="font-family: Arial; padding: 20px;">
            <h2>Debug Stellantis</h2>
            <p><strong>ID:</strong> {stellantis['id']}</p>
            <p><strong>Nom:</strong> {stellantis['nom_actif']}</p>
            <p><strong>Ticker:</strong> {stellantis['ticker_isin']}</p>
            <p><strong>Date achat:</strong> {stellantis['date_achat']}</p>
            <p><strong>Prix achat:</strong> {stellantis['prix_achat']}€</p>
            <p><strong>Prix actuel:</strong> {stellantis['prix_actuel']}€</p>
            <p><strong>Prix veille:</strong> {stellantis['prix_veille']}€</p>
            <p><strong>Quantité:</strong> {stellantis['quantite']}</p>
            <p><strong>Frais:</strong> {stellantis['frais']}€</p>
            <p><strong>Devise:</strong> {devise}</p>
            
            <h3>Calculs:</h3>
            <p><strong>Total Achat:</strong> {stellantis['prix_achat'] * stellantis['quantite'] + stellantis['frais']:.2f}€</p>
            <p><strong>Total Actuel:</strong> {stellantis['prix_actuel'] * stellantis['quantite'] + stellantis['frais']:.2f}€</p>
            <p><strong>Total Veille:</strong> {stellantis['prix_veille'] * stellantis['quantite'] + stellantis['frais']:.2f}€</p>
            <p><strong>Variation calculée:</strong> {(stellantis['prix_actuel'] - stellantis['prix_veille']) * stellantis['quantite']:.2f}€</p>
            <p><strong>Variation attendue:</strong> {(stellantis['prix_actuel'] - stellantis['prix_achat']) * stellantis['quantite']:.2f}€</p>
            
            <h3>Actions:</h3>
            <p><a href="/force_fix_stellantis" style="background: #28a745; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block; margin: 10px 0;">Forcer la correction de Stellantis</a></p>
            <p><a href="/dashboard" style="background: #667eea; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Retour au dashboard</a></p>
            </body></html>
            """
            conn.close()
            return info
        else:
            conn.close()
            return "<html><body><h2>Stellantis non trouvé</h2><p><a href='/dashboard'>Retour au dashboard</a></p></body></html>"
    except Exception as e:
        return f"<html><body><h2>Erreur</h2><p>{str(e)}</p><p><a href='/dashboard'>Retour au dashboard</a></p></body></html>"

@app.route('/force_fix_stellantis')
def force_fix_stellantis():
    """Forcer prix_veille = prix_achat pour Stellantis"""
    if 'user_id' not in session: 
        return redirect(url_for('index'))
    
    conn = get_connection()
    
    # Trouver Stellantis
    stellantis = conn.execute('''SELECT a.id, a.prix_achat, a.nom_actif
                                 FROM actifs a 
                                 JOIN comptes c ON a.compte_id = c.id 
                                 WHERE c.user_id = ? 
                                 AND (a.nom_actif LIKE '%Stellantis%' OR a.ticker_isin LIKE '%STLA%')
                                 LIMIT 1''', 
                             (session['user_id'],)).fetchone()
    
    if stellantis:
        # Forcer prix_veille = prix_achat
        conn.execute('UPDATE actifs SET prix_veille = ? WHERE id = ?',
                   (stellantis['prix_achat'], stellantis['id']))
        conn.commit()
        flash(f"✅ {stellantis['nom_actif']}: prix_veille forcé à {stellantis['prix_achat']}€", 'success')
    else:
        flash('❌ Stellantis non trouvé', 'error')
    
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/fix_today_purchases')
def fix_today_purchases():
    """Forcer prix_veille = prix_achat pour tous les actifs achetés AUJOURD'HUI"""
    if 'user_id' not in session: 
        return redirect(url_for('index'))
    
    conn = get_connection()
    
    # Date actuelle dans les deux formats
    date_actuelle_iso = datetime.now().strftime("%Y-%m-%d")  # 2026-02-06
    date_actuelle_fr = datetime.now().strftime("%d/%m/%Y")   # 06/02/2026
    
    # Récupérer tous les actifs achetés aujourd'hui
    actifs_aujourdhui = conn.execute('''SELECT a.id, a.nom_actif, a.prix_achat, a.date_achat
                                        FROM actifs a 
                                        JOIN comptes c ON a.compte_id = c.id 
                                        WHERE c.user_id = ? 
                                        AND (a.date_achat = ? OR a.date_achat = ?)''', 
                                     (session['user_id'], date_actuelle_iso, date_actuelle_fr)).fetchall()
    
    fixed = 0
    for actif in actifs_aujourdhui:
        # Forcer prix_veille = prix_achat pour les achats du jour
        conn.execute('UPDATE actifs SET prix_veille = ? WHERE id = ?',
                   (actif['prix_achat'], actif['id']))
        fixed += 1
        print(f"DEBUG: Actif '{actif['nom_actif']}' acheté aujourd'hui, prix_veille = {actif['prix_achat']}")
    
    conn.commit()
    conn.close()
    
    if fixed > 0:
        flash(f'✅ {fixed} actif(s) acheté(s) aujourd\'hui corrigé(s)', 'success')
    else:
        flash('ℹ️ Aucun actif acheté aujourd\'hui trouvé', 'info')
    
    return redirect(url_for('dashboard'))

@app.route('/fix_prix_veille')
def fix_prix_veille():
    """Réinitialiser TOUS les prix_veille = prix_achat (reset complet)"""
    if 'user_id' not in session: 
        return redirect(url_for('index'))
    
    conn = get_connection()
    
    # Forcer TOUS les prix_veille = prix_achat pour l'utilisateur
    conn.execute('''UPDATE actifs 
                    SET prix_veille = prix_achat 
                    WHERE id IN (
                        SELECT a.id FROM actifs a 
                        JOIN comptes c ON a.compte_id = c.id 
                        WHERE c.user_id = ?
                    )''', (session['user_id'],))
    
    rows_updated = conn.total_changes
    conn.commit()
    conn.close()
    
    flash(f'✅ Tous les prix de veille ont été réinitialisés ({rows_updated} actifs)', 'success')
    return redirect(url_for('dashboard'))

@app.route('/reset_pv_mois')
def reset_pv_mois():
    """Réinitialise le cumul de la PV du mois à zéro (pour l'utilisateur connecté)"""
    if 'user_id' not in session: 
        return redirect(url_for('index'))
    
    conn = get_connection()
    mois_actuel = datetime.now().strftime("%Y-%m")
    
    # Récupérer tous les actifs de l'utilisateur
    actifs = conn.execute('''SELECT a.id 
                             FROM actifs a 
                             JOIN comptes c ON a.compte_id = c.id 
                             WHERE c.user_id = ?''', 
                         (session['user_id'],)).fetchall()
    
    updated = 0
    for actif in actifs:
        # Mettre à jour ou créer l'enregistrement avec cumul = 0
        existing = conn.execute(
            'SELECT id FROM cumul_pv_mois WHERE actif_id = ? AND mois = ?',
            (actif['id'], mois_actuel)
        ).fetchone()
        
        if existing:
            # '1970-01-01' permet au CRON de recalculer dès aujourd'hui
            conn.execute('UPDATE cumul_pv_mois SET cumul_pv = 0, derniere_mise_a_jour = ? WHERE id = ?',
                       ('1970-01-01', existing['id']))
        else:
            conn.execute('INSERT INTO cumul_pv_mois (actif_id, mois, cumul_pv, derniere_mise_a_jour) VALUES (?, ?, 0, ?)',
                       (actif['id'], mois_actuel, '1970-01-01'))
        
        updated += 1
    
    conn.commit()
    conn.close()
    
    flash(f'✅ Plus-value du mois réinitialisée. Elle sera recalculée à la prochaine mise à jour CRON.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/api/reset_month')
def api_reset_month():
    """API pour réinitialiser le cumul mensuel de tous les utilisateurs (appelé par CRON le 1er du mois)"""
    # Vérifier l'authentification par token
    cron_token = request.args.get('token')
    if cron_token != CRON_TOKEN:
        return jsonify({'error': 'Non autorise'}), 401
    
    conn = get_connection()
    mois_actuel = datetime.now().strftime("%Y-%m")
    date_actuelle = datetime.now().strftime("%Y-%m-%d")
    
    # Réinitialiser tous les cumuls du nouveau mois
    # On supprime tous les enregistrements du mois actuel pour repartir à zéro
    conn.execute('DELETE FROM cumul_pv_mois WHERE mois = ?', (mois_actuel,))
    
    # Créer des enregistrements à 0 pour tous les actifs
    actifs = conn.execute('SELECT id FROM actifs').fetchall()
    for actif in actifs:
        conn.execute('INSERT INTO cumul_pv_mois (actif_id, mois, cumul_pv, derniere_mise_a_jour) VALUES (?, ?, 0, ?)',
                   (actif['id'], mois_actuel, date_actuelle))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': f'Cumul mensuel réinitialisé pour {len(actifs)} actifs'})

def fetch_closes_eodhd_periode(ticker_eodhd, date_from, date_to):
    """
    Récupère les prix de clôture EODHD entre deux dates.
    Retourne une liste de dicts {date, close} triés par date.
    """
    try:
        url = f"https://eodhd.com/api/eod/{ticker_eodhd}"
        params = {
            "from": date_from,
            "to": date_to,
            "api_token": EODHD_API_KEY,
            "fmt": "json"
        }
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return data  # [{date, open, high, low, close, ...}, ...]
    except Exception:
        pass
    return []


def resoudre_ticker_eodhd(ticker_isin_brut):
    """
    Convertit un ticker_isin (ISIN, nom, ticker Yahoo) en format EODHD utilisable.
    Stratégie :
    1. normalize_forced_symbol → ticker Yahoo (ex: BNP.PA, TSLA)
    2. Si le ticker Yahoo contient '.PA' → '{symbol}.PA' pour EODHD
    3. Si pas de suffixe → essayer '.PA' puis sans suffixe (US)
    Retourne (ticker_eodhd, devise_detectee)
    """
    normalise = normalize_forced_symbol(ticker_isin_brut.strip())
    upper = normalise.upper()

    # Mapping suffixe Yahoo → suffixe EODHD
    SUFFIXES = {
        '.PA': '.PA',    # Euronext Paris
        '.L':  '.LSE',   # London Stock Exchange
        '.AS': '.AS',    # Amsterdam
        '.MI': '.MI',    # Milan
        '.DE': '.XETRA', # Allemagne
        '.BR': '.BR',    # Bruxelles
    }

    for yahoo_sfx, eodhd_sfx in SUFFIXES.items():
        if upper.endswith(yahoo_sfx):
            base = upper[:-len(yahoo_sfx)]
            return base + eodhd_sfx, detect_currency_from_symbol(upper)

    # Ticker sans suffixe (US par défaut) ou court
    if '.' not in upper and len(upper) <= 6:
        return upper, 'USD'

    # ISIN ou nom long → on essaie de résoudre via fetch_price_from_api pour obtenir le vrai symbol
    # Mais c'est trop lourd ici, on retourne tel quel et on laisse EODHD chercher
    return upper, detect_currency_from_symbol(upper)


@app.route('/api/calcul_pv_mois_historique')
def api_calcul_pv_mois_historique():
    """
    Calcule la PV d'un mois passé pour tous les actifs de l'utilisateur.
    Utilise EODHD (comme fetch_historical_data_technical) + normalize_forced_symbol.
    Paramètre : mois=YYYY-MM (ex: 2026-01)
    """
    if 'user_id' not in session:
        return jsonify({'error': 'Non connecté'}), 401

    mois = request.args.get('mois')
    if not mois:
        return jsonify({'error': 'Paramètre mois requis (YYYY-MM)'}), 400

    try:
        from datetime import timedelta, date
        annee, num_mois = int(mois.split('-')[0]), int(mois.split('-')[1])
        debut_mois = date(annee, num_mois, 1)
        if num_mois == 12:
            debut_mois_suivant = date(annee + 1, 1, 1)
        else:
            debut_mois_suivant = date(annee, num_mois + 1, 1)
        # Élargir la fenêtre pour capturer le dernier jour de bourse du mois précédent
        date_from = (debut_mois - timedelta(days=10)).strftime('%Y-%m-%d')
        date_to   = (debut_mois_suivant + timedelta(days=3)).strftime('%Y-%m-%d')
    except Exception as e:
        return jsonify({'error': f'Format de mois invalide : {e}'}), 400

    conn = get_connection()
    actifs = conn.execute('''
        SELECT a.id, a.ticker_isin, a.nom_actif, a.quantite, a.devise_cotation
        FROM actifs a
        JOIN comptes c ON a.compte_id = c.id
        WHERE c.user_id = ? AND TRIM(a.ticker_isin) != ""
    ''', (session['user_id'],)).fetchall()

    resultats = []
    erreurs = []

    for actif in actifs:
        ticker_brut = actif['ticker_isin'].strip()
        quantite    = safe_int(actif['quantite'])
        devise      = actif['devise_cotation'] or 'EUR'
        nom         = actif['nom_actif']

        ticker_eodhd, devise_auto = resoudre_ticker_eodhd(ticker_brut)

        # Si la devise_cotation n'est pas définie, utiliser celle détectée
        if not actif['devise_cotation']:
            devise = devise_auto

        # Tentatives EODHD : ticker résolu, puis variante .PA si pas de suffixe
        candidats = [ticker_eodhd]
        if '.' not in ticker_eodhd:
            candidats.append(ticker_eodhd + '.PA')

        closes = []
        ticker_ok = None
        for cand in candidats:
            raw = fetch_closes_eodhd_periode(cand, date_from, date_to)
            if raw:
                closes = raw
                ticker_ok = cand
                break

        if not closes:
            erreurs.append(f"{nom} ({ticker_brut}→{ticker_eodhd}) : pas de données EODHD")
            continue

        # Trier par date
        closes.sort(key=lambda x: x['date'])

        # Prix avant le mois (dernier jour de bourse du mois précédent)
        avant = [c for c in closes if c['date'] < debut_mois.strftime('%Y-%m-%d')]
        dans  = [c for c in closes if debut_mois.strftime('%Y-%m-%d') <= c['date'] < debut_mois_suivant.strftime('%Y-%m-%d')]

        if not avant or not dans:
            erreurs.append(f"{nom} ({ticker_ok}) : données incomplètes ({len(avant)} avant / {len(dans)} dans le mois)")
            continue

        prix_debut = float(avant[-1]['close'])
        prix_fin   = float(dans[-1]['close'])

        pv_brute = (prix_fin - prix_debut) * quantite
        pv_eur   = convert_currency(pv_brute, devise, 'EUR')

        # Sauvegarder dans cumul_pv_mois
        existing = conn.execute(
            'SELECT id FROM cumul_pv_mois WHERE actif_id = ? AND mois = ?',
            (actif['id'], mois)
        ).fetchone()

        if existing:
            conn.execute('UPDATE cumul_pv_mois SET cumul_pv = ?, derniere_mise_a_jour = ? WHERE id = ?',
                        (round(pv_eur, 4), mois + '-recalc', existing['id']))
        else:
            conn.execute('INSERT INTO cumul_pv_mois (actif_id, mois, cumul_pv, derniere_mise_a_jour) VALUES (?, ?, ?, ?)',
                        (actif['id'], mois, round(pv_eur, 4), mois + '-recalc'))

        resultats.append({
            'actif': nom,
            'ticker': ticker_ok,
            'prix_debut': round(prix_debut, 4),
            'prix_fin':   round(prix_fin, 4),
            'pv_eur':     round(pv_eur, 2)
        })

    conn.commit()
    conn.close()

    total_pv = sum(r['pv_eur'] for r in resultats)
    return jsonify({
        'success': True,
        'mois': mois,
        'nb_actifs': len(resultats),
        'total_pv_eur': round(total_pv, 2),
        'resultats': resultats,
        'erreurs': erreurs
    })


@app.route('/api/historique_pv_mois')
def api_historique_pv_mois():
    """Retourne le cumul de PV par mois pour l'utilisateur connecté"""
    if 'user_id' not in session:
        return jsonify({'error': 'Non connecté'}), 401

    conn = get_connection()

    # Cumul global par mois (toutes les valeurs sont déjà en EUR)
    mois_rows = conn.execute('''
        SELECT c.mois, SUM(c.cumul_pv) as total_pv
        FROM cumul_pv_mois c
        JOIN actifs a ON c.actif_id = a.id
        JOIN comptes co ON a.compte_id = co.id
        WHERE co.user_id = ?
        GROUP BY c.mois
        ORDER BY c.mois DESC
    ''', (session['user_id'],)).fetchall()

    # Détail par compte et par mois
    detail_rows = conn.execute('''
        SELECT c.mois, co.nom_compte, SUM(c.cumul_pv) as pv_compte
        FROM cumul_pv_mois c
        JOIN actifs a ON c.actif_id = a.id
        JOIN comptes co ON a.compte_id = co.id
        WHERE co.user_id = ?
        GROUP BY c.mois, co.id
        ORDER BY c.mois DESC, co.nom_compte
    ''', (session['user_id'],)).fetchall()

    conn.close()

    # Formater les mois en français
    mois_fr = {
        '01': 'Janvier', '02': 'Février', '03': 'Mars', '04': 'Avril',
        '05': 'Mai', '06': 'Juin', '07': 'Juillet', '08': 'Août',
        '09': 'Septembre', '10': 'Octobre', '11': 'Novembre', '12': 'Décembre'
    }

    historique = []
    for row in mois_rows:
        annee, num_mois = row['mois'].split('-')
        label = f"{mois_fr.get(num_mois, num_mois)} {annee}"
        historique.append({
            'mois': row['mois'],
            'label': label,
            'total_pv': round(row['total_pv'] or 0, 2)
        })

    detail = []
    for row in detail_rows:
        annee, num_mois = row['mois'].split('-')
        label = f"{mois_fr.get(num_mois, num_mois)} {annee}"
        detail.append({
            'mois': row['mois'],
            'label': label,
            'compte': row['nom_compte'],
            'pv_compte': round(row['pv_compte'] or 0, 2)
        })

    return jsonify({'success': True, 'historique': historique, 'detail': detail})


@app.route('/api/stats_historique')
def stats_historique():
    """Retourne des statistiques sur l'historique des prix"""
    if 'user_id' not in session: 
        return jsonify({'error': 'Non connecte'})
    
    conn = get_connection()
    
    # Nombre total d'enregistrements dans l'historique
    total_records = conn.execute('''SELECT COUNT(*) as count FROM historique_prix h
                                    JOIN actifs a ON h.actif_id = a.id
                                    JOIN comptes c ON a.compte_id = c.id
                                    WHERE c.user_id = ?''', 
                                (session['user_id'],)).fetchone()['count']
    
    # Date du plus ancien enregistrement
    oldest_record = conn.execute('''SELECT MIN(date) as oldest FROM historique_prix h
                                    JOIN actifs a ON h.actif_id = a.id
                                    JOIN comptes c ON a.compte_id = c.id
                                    WHERE c.user_id = ?''', 
                                (session['user_id'],)).fetchone()['oldest']
    
    # Nombre d'actifs avec historique
    actifs_count = conn.execute('''SELECT COUNT(DISTINCT h.actif_id) as count 
                                   FROM historique_prix h
                                   JOIN actifs a ON h.actif_id = a.id
                                   JOIN comptes c ON a.compte_id = c.id
                                   WHERE c.user_id = ?''', 
                               (session['user_id'],)).fetchone()['count']
    
    conn.close()
    
    return jsonify({
        'total_enregistrements': total_records,
        'date_plus_ancien': oldest_record,
        'nombre_actifs': actifs_count
    })

@app.route('/api/historique/<int:actif_id>')
def get_historique(actif_id):
    """Retourne l'historique des prix pour un actif donné"""
    if 'user_id' not in session: 
        return jsonify({'error': 'Non connecte'})
    
    conn = get_connection()
    
    # Vérifier que l'actif appartient à l'utilisateur
    actif = conn.execute('''SELECT a.nom_actif FROM actifs a 
                            JOIN comptes c ON a.compte_id = c.id 
                            WHERE a.id = ? AND c.user_id = ?''', 
                         (actif_id, session['user_id'])).fetchone()
    
    if not actif:
        conn.close()
        return jsonify({'error': 'Actif introuvable'})
    
    # Récupérer l'historique
    historique = conn.execute('''SELECT date, prix, devise 
                                 FROM historique_prix 
                                 WHERE actif_id = ? 
                                 ORDER BY date ASC''', 
                             (actif_id,)).fetchall()
    
    conn.close()
    
    return jsonify({
        'nom': actif['nom_actif'],
        'historique': [{'date': h['date'], 'prix': h['prix'], 'devise': h['devise']} 
                      for h in historique]
    })

# --- État global pour la mise à jour des prix (anti-doublon) ---
_prices_refresh_lock = threading.Lock()
# user_id (int) ou 'cron' -> datetime de démarrage
_prices_refresh_state = {}


def _run_prices_update(user_id, is_cron, cumul_actif):
    """Exécute la mise à jour des prix (utilisée par /api/update_prices et l'auto-refresh)."""
    try:
        conn = get_connection()
        if is_cron:
            actifs_db = conn.execute('''SELECT a.id, a.compte_id, UPPER(a.ticker_isin) as ticker, c.user_id
                                     FROM actifs a
                                     JOIN comptes c ON a.compte_id=c.id
                                     WHERE a.ticker_isin != ""''').fetchall()
        else:
            actifs_db = conn.execute('''SELECT a.id, a.compte_id, UPPER(a.ticker_isin) as ticker, c.user_id
                                     FROM actifs a
                                     JOIN comptes c ON a.compte_id=c.id
                                     WHERE c.user_id=? AND a.ticker_isin != ""''', (user_id,)).fetchall()

        updated = 0
        date_actuelle = datetime.now().strftime("%Y-%m-%d")
        mois_actuel = datetime.now().strftime("%Y-%m")
        heure_actuelle = datetime.now().strftime("%d/%m %H:%M")

        print(f"DEBUG: Debut mise a jour pour {len(actifs_db)} titres (user={user_id}, cron={is_cron})")
        for row in actifs_db:
            actif_info = conn.execute(
                'SELECT prix_actuel, prix_veille, quantite, frais, devise_cotation FROM actifs WHERE id = ?',
                (row['id'],)
            ).fetchone()

            ancien_prix = safe_float(actif_info['prix_actuel'])
            quantite = safe_int(actif_info['quantite'])

            p, n, pv, currency = fetch_price_from_api(row['ticker'])
            if p is not None:
                if pv and float(pv) > 0:
                    nouveau_prix_veille = float(pv)
                elif ancien_prix > 0:
                    nouveau_prix_veille = ancien_prix
                else:
                    nouveau_prix_veille = float(p)

                pv_jour = (float(p) - nouveau_prix_veille) * quantite
                pv_jour_eur = convert_currency(pv_jour, currency, 'EUR')

                if cumul_actif:
                    cumul_existant = conn.execute(
                        'SELECT id, cumul_pv, derniere_mise_a_jour FROM cumul_pv_mois WHERE actif_id = ? AND mois = ?',
                        (row['id'], mois_actuel)
                    ).fetchone()
                    if cumul_existant:
                        derniere_maj = cumul_existant['derniere_mise_a_jour']
                        if derniere_maj != date_actuelle:
                            nouveau_cumul = cumul_existant['cumul_pv'] + pv_jour_eur
                            conn.execute('UPDATE cumul_pv_mois SET cumul_pv = ?, derniere_mise_a_jour = ? WHERE id = ?',
                                       (nouveau_cumul, date_actuelle, cumul_existant['id']))
                    else:
                        conn.execute('INSERT INTO cumul_pv_mois (actif_id, mois, cumul_pv, derniere_mise_a_jour) VALUES (?, ?, ?, ?)',
                                   (row['id'], mois_actuel, pv_jour_eur, date_actuelle))

                conn.execute('UPDATE actifs SET prix_actuel = ?, prix_veille = ?, devise_cotation = ? WHERE id = ?',
                           (float(p), nouveau_prix_veille, currency, row['id']))

                existing = conn.execute(
                    'SELECT id FROM historique_prix WHERE actif_id = ? AND date = ?',
                    (row['id'], date_actuelle)
                ).fetchone()
                if existing:
                    conn.execute('UPDATE historique_prix SET prix = ?, devise = ? WHERE id = ?',
                               (float(p), currency, existing['id']))
                else:
                    conn.execute('INSERT INTO historique_prix (actif_id, date, prix, devise) VALUES (?, ?, ?, ?)',
                               (row['id'], date_actuelle, float(p), currency))

                updated += 1
                print(f"DEBUG: Mis a jour {row['ticker']} -> {p} {currency}")

        if is_cron:
            conn.execute('UPDATE users SET derniere_maj = ? WHERE id IN (SELECT DISTINCT c.user_id FROM comptes c)',
                        (heure_actuelle,))
        else:
            conn.execute('UPDATE users SET derniere_maj = ? WHERE id = ?',
                        (heure_actuelle, user_id))

        conn.commit()
        conn.close()
        print(f"DEBUG: Mise a jour terminee, {updated} actifs mis a jour")
    except Exception as e:
        print(f"DEBUG: Erreur _run_prices_update: {e}")
    # NB: on conserve le timestamp dans _prices_refresh_state pour faire respecter
    # le min_interval_seconds (anti-doublon) entre deux rafraîchissements.


def trigger_prices_refresh_async(user_id, cumul=False, is_cron=False, min_interval_seconds=300, force=False):
    """Lance en arrière-plan la mise à jour des prix d'un utilisateur (ou CRON).

    Retourne (launched: bool, last_started_at: datetime|None).
    Empêche un nouveau lancement si le dernier date de moins de min_interval_seconds.
    """
    key = 'cron' if is_cron else user_id
    now = datetime.now()
    with _prices_refresh_lock:
        last = _prices_refresh_state.get(key)
        if last is not None and not force:
            elapsed = (now - last).total_seconds()
            if elapsed < min_interval_seconds:
                return False, last
        _prices_refresh_state[key] = now

    thread = threading.Thread(
        target=_run_prices_update,
        args=(user_id, is_cron, cumul),
        daemon=True,
    )
    thread.start()
    return True, now


@app.route('/api/update_prices')
def update_prices():
    """Endpoint manuel/CRON pour rafraîchir les prix (toujours en arrière-plan)."""
    cron_token = request.args.get('token')
    is_cron = (cron_token == CRON_TOKEN)
    cumul_actif = request.args.get('cumul') == 'true'

    if not is_cron and 'user_id' not in session:
        return jsonify({'error': 'Non connecte'}), 401

    user_id = session.get('user_id') if not is_cron else None
    # Force=True : l'appel manuel ne doit jamais être ignoré
    trigger_prices_refresh_async(
        user_id=user_id, cumul=cumul_actif, is_cron=is_cron, force=True
    )
    return jsonify({'success': True, 'message': 'Mise a jour demarree en arriere-plan'})

# --- ROUTE ETF ---
@app.route('/conseil-etf')
def conseil_etf():
    """Affiche l'analyse des ETF"""
    if 'user_id' not in session: return redirect(url_for('index'))
    
    conn = get_connection()
    results_db = conn.execute('SELECT * FROM etf_analysis ORDER BY score DESC').fetchall()
    conn.close()
    
    last_update = "Jamais"
    if results_db: last_update = results_db[0]['last_updated']
    
    results = [dict(row) for row in results_db]
    achats = sum(1 for r in results if r['signal'] == "🟢 ACHAT")
    ventes = sum(1 for r in results if r['signal'] == "🔴 VENTE")
    neutres = sum(1 for r in results if r['signal'] == "🟡 NEUTRE") or sum(1 for r in results if "PAS DE NEWS" in r['signal'])
    
    return render_template('conseil_etf.html', results=results, achats=achats, ventes=ventes, neutres=neutres, date_maj=last_update)

@app.route('/api/update_etf_analysis')
def update_etf_analysis():
    """Mise à jour spécifique pour les ETF"""
    if 'user_id' not in session: return jsonify({'error': 'Non autorisé'}), 401

    def run_update_etf():
        conn = get_connection()
        API_KEY = EODHD_API_KEY
        BASE_URL = "https://eodhd.com/api/news"
        REALTIME_API_URL = "https://eodhd.com/api/real-time"
        
        results_to_save = []
        # Pour les ETF, on utilise une logique différente : Tendance de prix (Trend)
        # On ne cherche pas de news, mais l'historique EOD
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_ticker = {}
            for t in ETF_TICKERS:
                future_to_ticker[executor.submit(analyze_etf_trend, t, API_KEY, REALTIME_API_URL, ETF_NAMES_MAP)] = t
            
            for future in concurrent.futures.as_completed(future_to_ticker):
                try:
                    res = future.result()
                    if res: results_to_save.append(res)
                except Exception as e: print(f"Erreur ETF: {e}")
        
        now = datetime.now().strftime('%d/%m/%Y à %H:%M')
        conn = get_connection()
        conn.execute('DELETE FROM etf_analysis')
        for r in results_to_save:
            conn.execute('''INSERT INTO etf_analysis (ticker, name, score, nb_news, signal, signal_class, price, last_updated, expense_ratio, category, day_change_pct, trend_15d_pct)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                         (r['ticker'], r['name'], r['score'], r['nb_news'], r['signal'], r['signal_class'], r['price'], now, r.get('expense_ratio', 'N/A'), r.get('category', 'ETF'), r.get('day_change_pct', 0), r.get('trend_15d_pct', 0)))
        conn.commit()
        conn.close()

    thread = threading.Thread(target=run_update_etf)
    thread.daemon = True
    thread.start()
    return jsonify({'success': True, 'message': 'Analyse ETF lancée'})

@app.route('/api/check_etf_status')
def check_etf_status():
    if 'user_id' not in session: return jsonify({'error': 'Non autorisé'}), 401
    conn = get_connection()
    count = conn.execute('SELECT COUNT(*) as cnt FROM etf_analysis').fetchone()['cnt']
    conn.close()
    return jsonify({'count': count})

# Point d'entrée pour cPanel et Local
application = app

if __name__ == '__main__':
    app.run(debug=True, port=5010)
