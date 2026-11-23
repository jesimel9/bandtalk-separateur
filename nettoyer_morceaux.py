"""Appelle la fonction SQL de nettoyage automatique.

Supprime : les morceaux `pret` non écoutés depuis 7 jours, les morceaux `echec`
de plus de 24 h, et leurs fichiers associés dans Storage.

Appelé une fois par jour par un workflow GitHub Actions séparé.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

import supabase_client

load_dotenv()


def main() -> int:
    client = supabase_client.charger_depuis_env()
    n = client.nettoyer_morceaux_anciens()
    print(f"Nettoyage fait : {n} morceaux supprimés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
