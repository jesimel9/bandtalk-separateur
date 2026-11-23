# BandTalk — Séparateur de pistes (GitHub Actions)

Serveur « sans serveur » pour la séparation de musique en 6 pistes avec
Demucs v4 `htdemucs_6s` (Meta, licence MIT). Tourne gratuitement sur GitHub
Actions, scrute une table Supabase toutes les 5 minutes et traite un morceau
à la fois.

**Pas de carte bancaire. Pas de frais. Pas de serveur à maintenir.**

## Comment ça marche

```
Toutes les 5 minutes, GitHub Actions lance un petit job :

  1. Vérifie dans Supabase : y a-t-il un morceau en attente ?
  2. Si oui : télécharge la source, lance Demucs, dépose les 6 pistes.
  3. Si non : s'arrête tout de suite (~1 min de minutes consommées).
```

## Déploiement : 5 étapes

### 1. Créer un nouveau dépôt GitHub public

- Allez sur https://github.com/new
- Nom : `bandtalk-separateur` (ou autre)
- Visibilité : **Public** (Actions illimitées gratuitement)
- Ne pas cocher « Add a README »
- **Create repository**

### 2. Uploader ces fichiers dans le dépôt

Sur la page du nouveau dépôt, cliquer **« uploading an existing file »**
puis glisser-déposer TOUS les fichiers de ce dossier :

- `traiter_un_morceau.py`
- `nettoyer_morceaux.py`
- `separation.py`
- `supabase_client.py`
- `requirements.txt`
- `README.md`
- **le dossier `.github/`** (avec ses sous-dossiers `workflows/` et les deux
  fichiers YAML dedans) — IMPORTANT : vérifier qu'il part bien dans le dépôt

Puis **Commit changes**.

### 3. Ajouter 2 Secrets GitHub

- Sur le dépôt → onglet **Settings** → **Secrets and variables** → **Actions**
- Cliquer **New repository secret** et créer **deux** entrées :

| Name | Value |
|---|---|
| `SUPABASE_URL` | `https://uoarafoznwtoxjkftngf.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | la clé service_role (console Supabase → Settings → API → Reveal `service_role`) |

### 4. Activer les workflows

- Onglet **Actions** du dépôt
- Si un message dit « Workflows aren't being run on this forked repository » ou
  « Workflows are disabled », cliquer **I understand my workflows, go ahead and enable them**
- Le workflow « Séparateur BandTalk » devrait apparaître dans la liste

### 5. Lancer un premier run manuel pour tester

- Dans l'onglet Actions, cliquer sur **Séparateur BandTalk**
- Bouton **Run workflow** (à droite) → **Run workflow**
- Attendre ~2 min, le job doit finir en « ✔ Success » avec le message
  `aucun_morceau` dans les logs (aucun morceau en attente, c'est normal)

À partir de maintenant : **le workflow tourne tout seul toutes les 5 minutes.**
Quand un utilisateur ajoute un morceau depuis l'app BandTalk, il sera traité
au prochain run.

## Vérifier que ça marche

Depuis l'UI Supabase (pour simuler un morceau d'essai sans l'app) :

1. https://supabase.com/dashboard/project/uoarafoznwtoxjkftngf/storage/buckets/morceaux-sources
2. **Upload file** → un petit MP3 dans un dossier `essai-manuel/`
3. https://supabase.com/dashboard/project/uoarafoznwtoxjkftngf/editor
4. Table **morceaux_separation** → **Insert** :
   - `nom` : « Mon premier essai »
   - `source_chemin` : `essai-manuel/<nom-fichier>.mp3`
   - `appareil_id` : `essai-manuel`
5. Attendre le prochain run GitHub Actions (max 5 min)
6. La ligne passe à `statut=pret` avec les 6 URLs dans `pistes_urls`

## Limites

- **Délai maximum : 5 minutes** avant qu'un morceau soit pris en charge
  (cron minimal de GitHub Actions)
- **Un seul morceau par run** : si 10 morceaux arrivent en même temps, il faut
  50 minutes pour tous les traiter. Suffisant pour un petit groupe
  d'utilisateurs ; pour une montée en charge, voir « Montée en charge »
  ci-dessous.
- **Minutes GitHub** : illimitées pour un dépôt **public** ; 2 000 min/mois
  pour un dépôt **privé** (~400 morceaux de 3 min)

## Montée en charge

Si plus tard vous avez beaucoup d'utilisateurs :

- Déplacer vers un VPS payant (Hetzner CPX11 ~4 €/mois) et utiliser
  `serveur-separateur/app.py` au lieu de ce script (boucle sans pause entre les
  morceaux).
- Ou lancer plusieurs workflows en parallèle (plusieurs jobs par run).

## Fichiers

- `traiter_un_morceau.py` : prend 1 morceau, le traite, s'arrête
- `nettoyer_morceaux.py` : appelle la fonction SQL d'auto-nettoyage
- `separation.py` : appel de Demucs (partagé avec la version serveur)
- `supabase_client.py` : opérations Supabase (partagé avec la version serveur)
- `requirements.txt` : dépendances Python
- `.github/workflows/separateur.yml` : cron toutes les 5 min
- `.github/workflows/nettoyage.yml` : cron quotidien
