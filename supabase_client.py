"""Lien avec Supabase : lire la file, télécharger un fichier, déposer les pistes.

Le serveur utilise la clé service_role (dans .env), qui contourne les RLS et
peut tout lire/écrire. C'est pour ça que ce code ne tourne JAMAIS sur le
téléphone : la clé service_role ne doit pas sortir du serveur.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
from supabase import Client, create_client

log = structlog.get_logger(__name__)


class ClientSupabase:
    """Enveloppe simple autour du client Supabase pour les opérations du serveur."""

    def __init__(self, url: str, cle_service: str):
        if not url or not cle_service:
            raise ValueError("SUPABASE_URL et SUPABASE_SERVICE_ROLE_KEY requis")
        # Formats acceptés :
        # - Ancien JWT (~200+ caracteres, contient "service_role" dans le payload base64)
        # - Nouveau format Supabase 2026 : sb_secret_... (plus court)
        est_legacy_jwt = "service_role" in cle_service or len(cle_service) >= 100
        est_nouveau = cle_service.startswith("sb_secret_")
        if not est_legacy_jwt and not est_nouveau:
            raise ValueError(
                "La clé Supabase ne ressemble pas à une clé serveur "
                "(ni JWT service_role, ni sb_secret_...)"
            )
        self.client: Client = create_client(url, cle_service)
        log.info("supabase_client_demarre", url=url)

    # ------------------------------------------------------------- File d'attente

    def prendre_prochain_morceau(self) -> dict[str, Any] | None:
        """Prend le plus ancien morceau 'en_attente' et le passe à 'en_cours'.

        Verrou optimiste (UPDATE conditionnel) : si deux serveurs tournent
        (improbable avec GitHub Actions + concurrency group), un seul l'attrape.
        """
        res = (
            self.client.table("morceaux_separation")
            .select("*")
            .eq("statut", "en_attente")
            .order("date_ajout")
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        morceau = res.data[0]
        upd = (
            self.client.table("morceaux_separation")
            .update({"statut": "en_cours"})
            .eq("id", morceau["id"])
            .eq("statut", "en_attente")
            .execute()
        )
        if not upd.data:
            return None  # un autre l'a pris entre-temps
        return morceau

    def reprendre_morceaux_bloques(self, minutes: int = 10) -> int:
        """Remet à 'en_attente' les morceaux 'en_cours' depuis plus de N minutes.

        Appelé au démarrage : si un run précédent a planté pendant un traitement,
        on reprend. UPDATE direct sur la table.
        """
        from datetime import datetime, timezone, timedelta

        seuil = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        try:
            res = (
                self.client.table("morceaux_separation")
                .update({"statut": "en_attente"})
                .eq("statut", "en_cours")
                .lt("date_ajout", seuil)
                .execute()
            )
            return len(res.data or [])
        except Exception as erreur:
            log.warning("reprendre_morceaux_bloques_echec", erreur=str(erreur))
            return 0

    def marquer_pret(self, morceau_id: str, pistes_urls: dict[str, str]) -> None:
        """Passe la ligne à 'pret' avec les URLs des 6 pistes."""
        self.client.table("morceaux_separation").update(
            {
                "statut": "pret",
                "pistes_urls": pistes_urls,
                "message_erreur": None,
            }
        ).eq("id", morceau_id).execute()
        log.info("morceau_pret", id=morceau_id, pistes=list(pistes_urls.keys()))

    def marquer_echec(self, morceau_id: str, message: str) -> None:
        """Passe la ligne à 'echec' avec une cause courte (≤ 200 caractères)."""
        message_court = message[:200]
        self.client.table("morceaux_separation").update(
            {
                "statut": "echec",
                "message_erreur": message_court,
            }
        ).eq("id", morceau_id).execute()
        log.warning("morceau_echec", id=morceau_id, message=message_court)

    def ecrire_duree(self, morceau_id: str, duree_secondes: int) -> None:
        self.client.table("morceaux_separation").update(
            {"duree_secondes": duree_secondes}
        ).eq("id", morceau_id).execute()

    def ecrire_bpm(self, morceau_id: str, bpm: int) -> None:
        self.client.table("morceaux_separation").update(
            {"bpm": bpm}
        ).eq("id", morceau_id).execute()

    # --------------------------------------------------------------------- Storage

    def telecharger_source(self, chemin: str, destination: Path) -> None:
        """Télécharge le fichier source depuis morceaux-sources vers destination locale."""
        donnees = self.client.storage.from_("morceaux-sources").download(chemin)
        destination.write_bytes(donnees)
        log.info("source_telechargee", chemin=chemin, octets=len(donnees))

    def deposer_piste(self, chemin_distant: str, fichier_local: Path) -> str:
        """Dépose une piste dans morceaux-pistes et renvoie son URL signée (24 h)."""
        contenu = fichier_local.read_bytes()
        self.client.storage.from_("morceaux-pistes").upload(
            path=chemin_distant,
            file=contenu,
            file_options={"content-type": "audio/mpeg", "upsert": "true"},
        )
        # URL signée : vivante 24 h, le téléphone la re-signera via re-fetch de la ligne
        signee = self.client.storage.from_("morceaux-pistes").create_signed_url(
            chemin_distant, 60 * 60 * 24
        )
        url = signee["signedURL"] if isinstance(signee, dict) else signee
        log.info("piste_deposee", chemin=chemin_distant, octets=len(contenu))
        return url

    def supprimer_source(self, chemin: str) -> None:
        """Supprime le fichier source après séparation réussie (économie d'espace)."""
        try:
            self.client.storage.from_("morceaux-sources").remove([chemin])
            log.info("source_supprimee", chemin=chemin)
        except Exception as erreur:
            # Pas grave si ça échoue : le nettoyage horaire s'en occupera
            log.warning("source_suppression_echec", chemin=chemin, erreur=str(erreur))

    # ---------------------------------------------------------- Nettoyage horaire

    def nettoyer_morceaux_anciens(self) -> int:
        """Appelle la fonction SQL de nettoyage (supprime > 7 jours sans écoute)."""
        try:
            res = self.client.rpc("nettoyer_morceaux_anciens").execute()
            n = len(res.data or [])
            log.info("nettoyage_fait", supprimes=n)
            return n
        except Exception as erreur:
            log.error("nettoyage_echec", erreur=str(erreur))
            return 0


def charger_depuis_env() -> ClientSupabase:
    """Fabrique le client depuis les variables d'environnement."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    cle = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    return ClientSupabase(url, cle)
