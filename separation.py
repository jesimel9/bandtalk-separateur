"""Séparation d'un morceau en 6 pistes avec Demucs v4 htdemucs_6s.

Appelé par app.py pour un fichier déjà téléchargé localement. Ne touche pas
Supabase (séparation des responsabilités : si Demucs plante, on sait où).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import ffmpeg
import structlog

log = structlog.get_logger(__name__)


# Noms des 6 pistes de htdemucs_6s (ordre Demucs : drums, bass, other, vocals,
# guitar, piano) traduit en français.
PISTES_NOMS = {
    "vocals": "voix",
    "drums": "batterie",
    "bass": "basse",
    "guitar": "guitare",
    "piano": "piano",
    "other": "autres",
}


class SeparationError(Exception):
    """Erreur contrôlée pendant la séparation : message affichable à l'utilisateur."""


def duree_secondes(fichier: Path) -> int:
    """Lit la durée d'un fichier audio avec ffprobe. 0 si inconnue."""
    try:
        info = ffmpeg.probe(str(fichier))
        duree = float(info["format"]["duration"])
        return int(duree)
    except Exception as erreur:
        log.warning("duree_inconnue", fichier=str(fichier), erreur=str(erreur))
        return 0


def separer(
    fichier_source: Path,
    dossier_sortie: Path,
    modele: str = "htdemucs_6s",
    dossier_modeles: str | None = None,
    debit_kbps: int = 192,
) -> dict[str, Path]:
    """Sépare un morceau en pistes MP3 et renvoie {nom_francais: chemin}.

    Pas de capture de stdout, les journaux Demucs vont sur la console (structlog
    les affiche plus propres à travers uvicorn).
    """
    if not fichier_source.exists():
        raise SeparationError(f"Fichier source introuvable : {fichier_source.name}")

    dossier_sortie.mkdir(parents=True, exist_ok=True)

    # Dossier intermédiaire pour Demucs (il écrit en WAV)
    dossier_wav = dossier_sortie / "wav_temp"
    dossier_wav.mkdir(exist_ok=True)

    # Appel de Demucs en subprocess pour isoler les crashes
    commande = [
        "python",
        "-I",
        "-m",
        "demucs.separate",
        "--shifts",
        "0",
        "--two-stems=vocals" if modele == "htdemucs" else "",
        "-n",
        modele,
        "-o",
        str(dossier_wav),
        "--mp3",
        "--mp3-bitrate",
        str(debit_kbps),
        str(fichier_source),
    ]
    commande = [arg for arg in commande if arg]  # retire les chaînes vides

    env = os.environ.copy()
    if dossier_modeles:
        env["TORCH_HOME"] = dossier_modeles

    log.info("demucs_demarre", modele=modele, fichier=fichier_source.name)
    try:
        resultat = subprocess.run(
            commande,
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=30 * 60,  # 30 min max par morceau (large marge)
        )
    except subprocess.TimeoutExpired as erreur:
        raise SeparationError(
            "Séparation trop longue (plus de 30 min), morceau refusé"
        ) from erreur
    except subprocess.CalledProcessError as erreur:
        cause = (erreur.stderr or "").strip().splitlines()
        message = cause[-1] if cause else "Demucs a échoué sans message"
        raise SeparationError(
            f"Demucs a échoué : {message[:150]}"
        ) from erreur

    # Demucs écrit dans wav_temp/<modele>/<nom-sans-extension>/<piste>.mp3
    nom_sans_ext = fichier_source.stem
    dossier_pistes = dossier_wav / modele / nom_sans_ext
    if not dossier_pistes.is_dir():
        raise SeparationError(
            f"Demucs n'a pas produit de dossier de pistes ({dossier_pistes})"
        )

    # Renomme en français et déplace dans dossier_sortie
    pistes: dict[str, Path] = {}
    for nom_anglais, nom_francais in PISTES_NOMS.items():
        source_mp3 = dossier_pistes / f"{nom_anglais}.mp3"
        if not source_mp3.exists():
            # htdemucs (4 pistes) ne produit pas guitar/piano : c'est OK si modele
            # n'est pas htdemucs_6s
            if modele != "htdemucs_6s":
                continue
            raise SeparationError(
                f"Piste manquante : {nom_anglais}.mp3 (modèle {modele})"
            )
        cible_mp3 = dossier_sortie / f"{nom_francais}.mp3"
        source_mp3.rename(cible_mp3)
        pistes[nom_francais] = cible_mp3

    # Nettoyage du dossier wav_temp
    try:
        import shutil

        shutil.rmtree(dossier_wav, ignore_errors=True)
    except Exception:
        pass

    log.info("demucs_fini", modele=modele, pistes=list(pistes.keys()))
    return pistes
