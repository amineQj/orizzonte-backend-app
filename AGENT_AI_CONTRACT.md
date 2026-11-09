# ORIZZONTE — Contrat d'intégration de l'agent IA

> Document destiné à l'équipe (ou au service) qui développe **l'agent IA externe**
> chargé de vérifier les documents des chauffeurs.
>
> L'agent est un **service séparé** : il n'a **pas** de compte utilisateur, **pas** de JWT.
> Il s'authentifie avec une **clé partagée** envoyée dans l'en-tête HTTP `X-Agent-Key`.

---

## 1. Rôle de l'agent

Pour chaque chauffeur en attente (`pending_review`), l'agent :

1. récupère la **liste** des dossiers en attente,
2. analyse les **documents** (images) et les informations du profil,
3. renvoie une **décision** : `approve`, `reject` ou `manual_review`,
   avec une **confiance** et des **raisons** courtes.

L'agent **n'approuve pas** de lui-même par défaut : ses décisions sont **journalisées**.
Un humain (le boss) reste décisionnaire, sauf si l'auto-approbation est activée
côté serveur (voir §7).

---

## 2. Authentification

| Élément | Valeur |
|---|---|
| En-tête | `X-Agent-Key: <clé>` |
| Clé attendue | variable d'environnement `AI_AGENT_API_KEY` (côté serveur) |
| Comparaison | en **temps constant** (`hmac.compare_digest`) |
| Si la clé configurée est **vide** | tous les endpoints `/api/agent/` sont **désactivés** → `401` |
| En-tête absent | `401` |
| En-tête erroné | `401` |

- Une clé **JWT** (`Authorization: Bearer …`) est **ignorée** sur ces routes.
- La clé `X-Agent-Key` **ne donne aucun accès** aux routes boss (`/api/drivers/...`).
- 🔐 **Ne jamais logger la clé**, ne jamais l'inclure dans une URL, un rapport ou un message d'erreur.

### Générer une clé

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Côté serveur, la mettre dans `.env` :

```env
AI_AGENT_API_KEY=<la-clé-générée>
```

---

## 2 bis. Quand l'agent a-t-il le droit d'agir ?

- Le dossier doit être **`pending_review`**. Sinon → `409 invalid_profile_transition`.
- Le **`documents_version`** envoyé dans la décision doit correspondre **exactement**
  à la version courante du profil. Sinon → `409 documents_version_conflict`.
  Cela empêche d'appliquer une analyse périmée (le chauffeur a modifié ses documents
  entre-temps).

---

## 3. Endpoints

Base : `https://<hôte>/api/`.

| Méthode | URL | Description |
|---|---|---|
| `GET` | `/api/agent/drivers/pending/` | Dossiers en attente (payload minimal) |
| `GET` | `/api/agent/drivers/<id>/` | Détail d'un dossier + liens documents |
| `GET` | `/api/agent/drivers/<id>/documents/<type>/` | Télécharger une pièce (image) |
| `POST` | `/api/agent/drivers/<id>/review/` | Envoyer une décision |

`<id>` = **id utilisateur** du chauffeur.
`<type>` ∈ `selfie` | `id_card_front` | `id_card_back` | `driving_license` | `vehicle_registration`.

Throttle : **60 requêtes/minute** par IP sur l'ensemble des endpoints agent.

---

## 4. `GET /api/agent/drivers/pending/`

- **`200 OK`** — liste (non paginée) des profils `pending_review`, **sans donnée personnelle** :

  ```json
  [
    { "user_id": 26, "documents_version": 3, "status": "pending_review", "updated_at": "2026-10-08T10:02:11Z" },
    { "user_id": 41, "documents_version": 1, "status": "pending_review", "updated_at": "2026-10-08T10:15:00Z" }
  ]
  ```

> La liste ne contient **ni nom, ni téléphone, ni numéro de pièce**. L'agent traite le dossier
> de façon anonyme tant qu'il n'a pas demandé le détail.

## 5. `GET /api/agent/drivers/<id>/`

- **`200 OK`** — informations nécessaires à l'analyse + liens des documents :

  ```json
  {
    "user_id": 26,
    "full_name": "Karim Benali",
    "phone": "0550000001",
    "date_of_birth": "1990-05-20",
    "national_id_number": "1099000001",
    "driving_license_number": "DL-0001",
    "driving_license_expiry": "2030-05-20",
    "vehicle_plate": "12345-116",
    "status": "pending_review",
    "documents_version": 3,
    "documents": {
      "selfie":              { "uploaded": true, "url": "/api/agent/drivers/26/documents/selfie/" },
      "id_card_front":       { "uploaded": true, "url": "/api/agent/drivers/26/documents/id_card_front/" },
      "id_card_back":        { "uploaded": true, "url": "/api/agent/drivers/26/documents/id_card_back/" },
      "driving_license":     { "uploaded": true, "url": "/api/agent/drivers/26/documents/driving_license/" },
      "vehicle_registration":{ "uploaded": false, "url": "/api/agent/drivers/26/documents/vehicle_registration/" }
    }
  }
  ```

- **`404`** si l'id n'existe pas.

## 6. `GET /api/agent/drivers/<id>/documents/<type>/`

- **Auth** : `X-Agent-Key`.
- **`200`** : le flux de l'image (JPEG/PNG/WebP).
  En-têtes : `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`.
- **`404`** si le type, le chauffeur ou le fichier est inconnu.
- **`401`** sans clé valide.

> Les documents sont des **pièces d'identité** : ne les stockez pas de façon durable côté agent
> au-delà du temps nécessaire à l'analyse, et ne les loggez jamais.

## 7. `POST /api/agent/drivers/<id>/review/`

- **Auth** : `X-Agent-Key`.
- **Body** (`application/json`) :

  ```json
  {
    "decision": "approve",
    "reasons": ["Photo nette", "Permis valide"],
    "confidence": 0.94,
    "documents_version": 3
  }
  ```

  | Champ | Type | Requis | Détail |
  |---|---|---|---|
  | `decision` | string | oui | `approve` \| `reject` \| `manual_review` |
  | `reasons` | liste de textes | non | max 20 éléments, **≤ 200 caractères** chacun ; **aucune suite de ≥ 6 chiffres** (données sensibles) sinon `400` |
  | `confidence` | float `0.0–1.0` | non (utile pour `approve`) | seuil d'auto-approbation |
  | `documents_version` | entier ≥ 0 | **oui** | doit correspondre à la version analysée |

- **`200 OK`** :

  ```json
  { "user_id": 26, "status": "pending_review", "applied": false, "documents_version": 3 }
  ```

  - `applied = true` : la décision a modifié le profil.
  - `applied = false` : décision **enregistrée comme recommandation** (le boss tranche).

### Sémantique des décisions

| `decision` | Effet par défaut | Effet si `AI_AUTO_APPROVE=True` |
|---|---|---|
| `approve` | enregistré, profil reste `pending_review` | `approved` **si** `confidence ≥ AI_AUTO_APPROVE_MIN_CONFIDENCE`, sinon recommandation |
| `reject` | profil → `rejected` (motif = raisons) | identique |
| `manual_review` | enregistré, profil inchangé | identique |

- Le motif de rejet affiché au chauffeur est construit à partir de `reasons`.

---

## 7 bis. Erreurs

| Status | `code` | Sens |
|---|---|---|
| `401` | *(pas de champ `code`)* | `AI_AGENT_API_KEY` vide côté serveur — corps : `{"detail": "AI agent endpoints are disabled."}` |
| `401` | *(pas de champ `code`)* | En-tête `X-Agent-Key` absent ou incorrect — corps : `{"detail": "Invalid X-Agent-Key."}` |
| `404` | — | Chauffeur / type de document / fichier inconnu |
| `400` | — | Corps invalide (décision inconnue, `reason` contenant un numéro, etc.) |
| `409` | `invalid_profile_transition` | Le profil n'est plus `pending_review` |
| `409` | `documents_version_conflict` | `documents_version` périmé (le champ `current_documents_version` indique la valeur à utiliser) |

---

## 8. Exemples (curl)

```bash
BASE=https://demo.orizzonte.local
KEY="$AI_AGENT_API_KEY"

# 1) Dossiers en attente
curl -s -H "X-Agent-Key: $KEY" "$BASE/api/agent/drivers/pending/"

# 2) Détail d'un dossier
curl -s -H "X-Agent-Key: $KEY" "$BASE/api/agent/drivers/26/"

# 3) Télécharger une pièce
curl -s -H "X-Agent-Key: $KEY" \
     "$BASE/api/agent/drivers/26/documents/selfie/" -o selfie.jpg

# 4) Envoyer une décision
curl -s -X POST -H "X-Agent-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"decision":"approve","reasons":["Documents lisibles"],"confidence":0.97,"documents_version":3}' \
     "$BASE/api/agent/drivers/26/review/"
```

---

## 9. Boucle de traitement recommandée

```
GET /api/agent/drivers/pending/
        │
        └─ pour chaque user_id (traiter un dossier à la fois) :
              GET /api/agent/drivers/<id>/          → lire documents_version + documents
              GET .../documents/<type>/             → télécharger chaque pièce « uploaded: true »
              analyse
              POST /api/agent/drivers/<id>/review/  → {decision, reasons, confidence, documents_version}
                    ├─ 200  → OK
                    └─ 409 documents_version_conflict → ignorer et retraiter plus tard
```

- Toujours renvoyer le **même** `documents_version` que celui lu à l'étape d'analyse.
- Sur `409 documents_version_conflict`, **ne pas** réessayer avec l'ancienne version :
  relire le dossier (la version courante est dans `current_documents_version`).
- Rester sous **60 requêtes/minute**.

---

## 10. Sécurité — checklist

- [ ] La clé `X-Agent-Key` n'apparaît dans **aucun log**, message d'erreur ou APM.
- [ ] La clé est injectée par variable d'environnement (pas dans le code, pas dans git).
- [ ] Les images téléchargées ne sont **pas** conservées au-delà du nécessaire.
- [ ] Les raisons ne contiennent **aucun numéro** de pièce/téléphone (rejeté en `400`).
- [ ] Les échanges se font en **HTTPS** en production.
