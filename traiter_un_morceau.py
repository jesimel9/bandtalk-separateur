"""Traite UN seul morceau en attente puis s'arrête.

Appelé par GitHub Actions toutes les 5 minutes (cron). Si aucun morceau n'est
en attente, le script sort tout de suite avec succès (coût ~1 min de minutes
GitHub).

Sinon : télécharge la source, lance Demucs, dépose les 6 pistes, met la ligne
à `pret`. Le prochain run GitHub Actions prendra le morceau suivant.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import structlog
from dotenv import load_dotenv

# Permet d'importer separation.py et supabase_client.py posés à côté
sys.path.insert(0, str(Path(__file__).parent))
import separation
import supabase_client

load_dotenv()

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger(__name__)


def main() -> int:
    duree_max = int(os.environ.get("DUREE_MAX_SECONDES", "600"))
    modele = os.environ.get("DEMUCS_MODELE", "htdemucs_6s")
    dossier_modeles = os.environ.get("DEMUCS_MODEL_DIR") or None
    debit_kbps = int(os.environ.get("DEBIT_MP3_KBPS", "192"))
    dossier_travail = Path(os.environ.get("DOSSIER_TRAVAIL", tempfile.mkdtemp()))
    dossier_travail.mkdir(parents=True, exist_ok=True)

    log.info("demarrage", modele=modele, duree_max_secondes=duree_max)
    debut = time.time()

    client = supabase_client.charger_depuis_env()

    # Au démarrage : récupère les morceaux bloqués (si un run précédent a planté)
    try:
        repris = client.reprendre_morceaux_bloques(minutes=10)
        if repris:
            log.info("morceaux_bloques_repris", nombre=repris)
    except Exception as erreur:
        log.warning("reprise_bloques_echec", erreur=str(erreur))

    # Prend UN morceau
    morceau = client.prendre_prochain_morceau()
    if morceau is None:
        log.info("aucun_morceau", duree_s=int(time.time() - debut))
        return 0

    morceau_id = morceau["id"]
    log.info("morceau_pris", id=morceau_id, nom=morceau.get("nom"))

    dossier_morceau = dossier_travail / morceau_id
    if dossier_morceau.exists():
        shutil.rmtree(dossier_morceau)
    dossier_morceau.mkdir(parents=True)

    code_sortie = 0
    try:
        # 1. Télécharger la source
        source_chemin = morceau["source_chemin"]
        extension = Path(source_chemin).suffix or ".mp3"
        fichier_source = dossier_morceau / f"source{extension}"
        client.telecharger_source(source_chemin, fichier_source)

        # 2. Vérifier la durée
        duree = separation.duree_secondes(fichier_source)
        if duree > 0:
            client.ecrire_duree(morceau_id, duree)
        if duree > duree_max:
            raise separation.SeparationError(
                f"Morceau trop long : {duree // 60} min {duree % 60} s "
                f"(maximum {duree_max // 60} min)"
            )

        # 3. Séparer
        dossier_pistes = dossier_morceau / "pistes"
        pistes = separation.separer(
            fichier_source=fichier_source,
            dossier_sortie=dossier_pistes,
            modele=modele,
            dossier_modeles=dossier_modeles,
            debit_kbps=debit_kbps,
        )

        # 4. Déposer les pistes
        urls: dict[str, str] = {}
        for nom, fichier in pistes.items():
            chemin_distant = f"{morceau_id}/{nom}.mp3"
            urls[nom] = client.deposer_piste(chemin_distant, fichier)

        # 5. Marquer prêt
        client.marquer_pret(morceau_id, urls)

        # 6. Supprimer la source
        client.supprimer_source(source_chemin)

        log.info("morceau_fini", id=morceau_id, duree_s=int(time.time() - debut))
    except separation.SeparationError as erreur:
        client.marquer_echec(morceau_id, str(erreur))
        log.warning("morceau_echec_controle", id=morceau_id, cause=str(erreur))
        # Code 0 : l'échec est noté dans la base, pas d'alerte GitHub
    except Exception as erreur:
        log.exception("morceau_crash", id=morceau_id)
        client.marquer_echec(morceau_id, f"Erreur interne : {type(erreur).__name__}")
        code_sortie = 1
    finally:
        shutil.rmtree(dossier_morceau, ignore_errors=True)

    return code_sortie


if __name__ == "__main__":
    sys.exit(main())
