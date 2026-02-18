import sys
import os

# Ajouter le répertoire courant au path pour que Python trouve app.py
sys.path.insert(0, os.path.dirname(__file__))

from app import application
