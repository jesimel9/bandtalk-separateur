"""Traite les morceaux en attente puis reste 5 minutes a poller la base.

Deux modes :
- Cron automatique (MODE_REVEIL=false) : prend tous les morceaux disponibles,
  puis garde une fenetre de grace de 5 min pour rattraper un 2e envoi qui
  arriverait juste apres. Si AUCUN morceau au depart, sort tout de suite.
- Reveil par l'appli (MODE_REVEIL=true) : meme chose, mais poll 5 min MEME si
  aucun morceau au depart. Utilise quand l'utilisateur appuie sur « Reveiller
  le serveur » ou ouvre l'ecran « Nouveau morceau » : les deps Python
  (torch + demucs ~1 min 30) se chargent PENDANT que l'utilisateur selectionne
  et uploade son fichier. Quand son morceau arrive, le serveur est deja chaud
  et traite immediatement.

Variables d'environnement :
- MODE_REVEIL           : "true" | "false" (defaut "false")
- DUREE_POLL_SECONDES   : fenetre de poll apres chaque traitement (defaut 300)
- INTERVALLE_POLL_SECONDES : pause entre deux lectures de la base (defaut 5)
- DUREE_MAX_SECONDES    : longueur max d'un morceau (defaut 600 = 10 min)
- DEMUCS_MODELE         : modele (defaut htdemucs_6s)
- DEBIT_MP3_KBPS        : debit MP3 des pistes (defaut 192)
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


def _lire_bool(nom: str, defaut: bool = False) -> bool:
    valeur = (os.environ.get(nom) or "").strip().lower()
    if valeur in ("1", "true", "yes", "on", "oui"):
        return True
    if valeur in ("0", "false", "no", "off", "non", ""):
        return defaut
    return defaut


def traiter_un_morceau(
    client: supabase_client.ClientSupabase,
    dossier_travail: Path,
    *,
    modele: str,
    dossier_modeles: str | None,
    debit_kbps: int,
    duree_max: int,
) -> tuple[bool, int]:
    """Prend et traite UN morceau si disponible.

    Renvoie (traite, code_sortie) :
    - traite      : True si un morceau a ete pris (reussi ou echoue), False si file vide
    - code_sortie : 0 (OK ou echec controle) ou 1 (crash)
    """
    morceau = client.prendre_prochain_morceau()
    if morceau is None:
        return False, 0

    morceau_id = morceau["id"]
    log.info("morceau_pris", id=morceau_id, nom=morceau.get("nom"))

    dossier_morceau = dossier_travail / morceau_id
    if dossier_morceau.exists():
        shutil.rmtree(dossier_morceau)
    dossier_morceau.mkdir(parents=True)

    debut = time.time()
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

        # 2b. Détecter le BPM
        bpm = separation.detecter_bpm(fichier_source)
        if bpm is not None:
            client.ecrire_bpm(morceau_id, bpm)
            log.info("bpm_ecrit", id=morceau_id, bpm=bpm)

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

    return True, code_sortie


def main() -> int:
    duree_max = int(os.environ.get("DUREE_MAX_SECONDES", "600"))
    modele = os.environ.get("DEMUCS_MODELE", "htdemucs_6s")
    dossier_modeles = os.environ.get("DEMUCS_MODEL_DIR") or None
    debit_kbps = int(os.environ.get("DEBIT_MP3_KBPS", "192"))
    dossier_travail = Path(os.environ.get("DOSSIER_TRAVAIL", tempfile.mkdtemp()))
    dossier_travail.mkdir(parents=True, exist_ok=True)

    mode_reveil = _lire_bool("MODE_REVEIL", defaut=False)
    duree_poll = int(os.environ.get("DUREE_POLL_SECONDES", "300"))
    intervalle_poll = max(2, int(os.environ.get("INTERVALLE_POLL_SECONDES", "5")))

    log.info(
        "demarrage",
        modele=modele,
        duree_max_secondes=duree_max,
        mode_reveil=mode_reveil,
        duree_poll=duree_poll,
    )
    debut_global = time.time()

    client = supabase_client.charger_depuis_env()

    # Au démarrage : récupère les morceaux bloqués (si un run précédent a planté)
    try:
        repris = client.reprendre_morceaux_bloques(minutes=10)
        if repris:
            log.info("morceaux_bloques_repris", nombre=repris)
    except Exception as erreur:
        log.warning("reprise_bloques_echec", erreur=str(erreur))

    # 1) Traite tout ce qui est deja en file au demarrage
    nb_traites = 0
    code_total = 0
    while True:
        traite, code = traiter_un_morceau(
            client,
            dossier_travail,
            modele=modele,
            dossier_modeles=dossier_modeles,
            debit_kbps=debit_kbps,
            duree_max=duree_max,
        )
        if not traite:
            break
        nb_traites += 1
        if code != 0:
            code_total = code

    # 2) Decide du poll :
    #    - mode_reveil=True : toujours poll (meme si rien au depart)
    #    - mode_reveil=False : poll uniquement si on a deja traite au moins un
    #      morceau (fenetre de grace pour un 2e envoi)
    if nb_traites == 0 and not mode_reveil:
        log.info("aucun_morceau_sortie_directe", duree_s=int(time.time() - debut_global))
        return code_total

    # 3) Fenetre de poll
    fin_poll = time.time() + duree_poll
    log.info(
        "debut_poll",
        duree_s=duree_poll,
        intervalle_s=intervalle_poll,
        deja_traites=nb_traites,
    )
    while time.time() < fin_poll:
        time.sleep(intervalle_poll)
        traite, code = traiter_un_morceau(
            client,
            dossier_travail,
            modele=modele,
            dossier_modeles=dossier_modeles,
            debit_kbps=debit_kbps,
            duree_max=duree_max,
        )
        if traite:
            nb_traites += 1
            if code != 0:
                code_total = code
            # Chaque morceau traite prolonge la fenetre de 5 min
            fin_poll = time.time() + duree_poll

    log.info(
        "fin",
        total_traites=nb_traites,
        duree_totale_s=int(time.time() - debut_global),
        code=code_total,
    )
    return code_total


if __name__ == "__main__":
    sys.exit(main())
