# Utiliser une image Python légère
FROM python:3.11-slim

# Définir le répertoire de travail
WORKDIR /app

# Installer les dépendances système nécessaires (pour yfinance/pandas si besoin)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copier les fichiers de dépendances
COPY requirements.txt .

# Installer les dépendances Python
RUN pip install --no-cache-dir -r requirements.txt

# Copier tout le code de l'application
COPY . .

# Exposer le port que Fly.io utilise
EXPOSE 8080

# Commande pour lancer l'application avec gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]
