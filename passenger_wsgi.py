import sys
import os

INTERP = "/home/hfqf5148/virtualenv/monpecule.fr/3.9/bin/python"
if sys.executable != INTERP:
    os.execl(INTERP, INTERP, *sys.argv)

sys.path.insert(0, os.path.dirname(__file__))

from app_flask import app as application
