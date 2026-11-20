# ORIZZONTE — Contrat d'API (authentification + missions)

> Document destiné au développeur **front (Flutter)**.
> Toutes les réponses sont du **JSON** (sauf la page HTML du lien de vérification).
> Les status/body ci-dessous sont ceux **réellement observés** pendant les tests.

---

## 0. Comment joindre le backend

Le backend est en Django REST Framework. En démo (**téléphone et PC sur le même Wi-Fi**) :

```bash
# PC : récupérer son IP locale
ipconfig          # → ex. 192.168.1.20  (ligne "Adresse IPv4")
```

1. Sur le PC, dans `back/orizzonte-backend-main/`, créez un fichier `.env`
   (copie de `.env.example`) avec :

   ```env
   SITE_URL=http://192.168.1.20:8000
   ALLOWED_HOSTS=*
   ```

2. Lancer le serveur **accessible depuis le réseau** :

   ```bash
   venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
   ```

3. Côté Flutter, l'URL de base est : **`http://192.168.1.20:8000/api/`**
   (émulateur Android : `http://10.0.2.2:8000/api/`).

L'IP locale peut changer au redémarrage du routeur : relancez `ipconfig` et mettez `SITE_URL` à jour.
Le lien de vérification envoyé par email utilise **`SITE_URL`** (jamais `127.0.0.1`), il est donc ouvrable depuis le téléphone.

> **CORS** : inutile pour Flutter mobile (pas de navigateur). Réglé uniquement pour le dev web dans `settings.py`.

---

## 1. Rôles

| Rôle (`role`) | Créé par | Peut faire |
|---|---|---|
| `client` | inscription publique `/api/signup/` (défaut) | estime + confirme une mission |
| `driver` | inscription publique `/api/signup/` avec `"role": "driver"` | missions disponibles, accepter/démarrer/terminer, son historique |
| `boss` | **uniquement côté serveur** (`python manage.py create_boss`) | liste des chauffeurs, missions globales, dashboard |

- L'inscription publique n'accepte que **`client`** et **`driver`**. Toute autre valeur
  (`boss`, `admin`, `employee`, `""`, inconnue) est **refusée avec un `400`** et **aucun compte n'est créé**.
- Le rôle **`boss` ne peut JAMAIS être obtenu via l'API** : il se crée en ligne de commande
  (`python manage.py create_boss`). Le mot de passe est saisi masqué (ou via `BOSS_PASSWORD`), jamais en argument.
- Le champ `email_verified` envoyé dans un body est **ignoré** : un compte reste `false` jusqu'au clic du lien email.

---

## 2. Tokens JWT — points essentiels pour le front

| Élément | Valeur |
|---|---|
| Type d'en-tête | `Authorization: Bearer <access_token>` |
| Durée de vie **access** | **60 minutes** |
| Durée de vie **refresh** | **7 jours** |
| Rotation du refresh | **OUI** — chaque appel à `/api/token/refresh/` renvoie un **NOUVEAU** `access` **ET** un **NOUVEAU** `refresh` |
| Blacklist | **OUI** — l'ancien refresh devient invalide après rotation, et `logout` blackliste le refresh |

> ⚠️ **Règle Flutter** : à chaque `POST /api/token/refresh/`, **écrasez le refresh stocké par le nouveau**. Sinon, l'ancien étant blacklisté, l'utilisateur sera déconnecté au bout d'un moment.
> Stockez les tokens dans un stockage sûr (`flutter_secure_storage`, pas `SharedPreferences`).

---

## 3. Endpoints d'authentification

### 3.1 Inscription (client ou driver) — `POST /api/signup/`

- **Auth** : aucune.
- **Body** — `role` est **optionnel** ; valeurs autorisées : **`"client"`** (défaut) ou **`"driver"`** :

  ```json
  { "username": "demoflow", "email": "demoflow@example.com", "password": "TestPassword123!", "role": "client" }
  ```

  ```json
  { "username": "demodriver", "email": "demoflow@example.com", "password": "TestPassword123!", "role": "driver" }
  ```

- **`201 Created`** :

  ```json
  {
    "message": "Account created successfully. A verification email has been sent.",
    "user": {
      "id": 26, "username": "demoflow", "email": "demoflow@example.com",
      "role": "client", "email_verified": false,
      "date_joined": "2026-10-08T09:55:50.553478Z"
    }
  }
  ```

  Un email de vérification est envoyé automatiquement. **Le compte ne peut pas se connecter avant vérification.**

- **`400 Bad Request`** — **rôle interdit** (`boss`, `admin`, `employee`, `""`, valeur inconnue) : **aucun compte n'est créé** :

  ```json
  { "message": "Registration failed. Please check the errors below.",
    "errors": { "role": ["Invalid role. Allowed values: 'client', 'driver'."] } }
  ```

- **`400 Bad Request`** — email déjà utilisé, mot de passe trop faible… :

  ```json
  { "message": "Registration failed. Please check the errors below.",
    "errors": { "email": ["A user with this email already exists."] } }
  ```

`email_verified` envoyé dans le body est **ignoré** (reste `false`).

---

### 3.2 Vérification d'email — `GET /api/verify-email/?uid=<id>&token=<token>`

- **Auth** : aucune. **L'utilisateur ouvre ce lien depuis son email** (navigateur du téléphone).
- Le lien est de la forme : `http://192.168.1.20:8000/api/verify-email/?uid=26&token=xxxxx`
- **Ce que l'utilisateur voit dans le navigateur** (page HTML) :

  | Cas | Status | Page |
  |---|---|---|
  | Token valide | `200` | ✅ « Email verified successfully. You can now log in. » + *« Vous pouvez maintenant vous connecter… »* |
  | Déjà vérifié | `200` | ✅ « Email is already verified. » |
  | Token invalide / expiré / uid inconnu | `400` | ⚠️ « Invalid or expired verification token. » + *« Demandez un nouveau lien… »* |

- **Appel API (Flutter/Postman, sans `Accept: text/html`)** → JSON, mêmes status :

  ```json
  { "message": "Email verified successfully. You can now log in." }   // 200
  { "message": "Email is already verified." }                          // 200
  { "message": "Invalid or expired verification token." }              // 400
  ```

> **Côté Flutter** : l'utilisateur clique le lien dans son app email → le navigateur affiche la page. **Ensuite l'utilisateur revient à l'écran de login** de l'app pour se connecter. Vous n'avez pas besoin d'appeler cet endpoint depuis Flutter (sauf deep-link avancé) ; il suffit de dire à l'utilisateur de vérifier sa boîte mail puis de revenir se connecter.
> Détail technique : la page HTML n'est servie que si l'en-tête `Accept` contient `text/html` (navigateur). Flutter (`Accept: application/json` ou `*/*`) reçoit le JSON, aux **mêmes status**.

---

### 3.3 Renvoyer l'email de vérification — `POST /api/resend-verification/`

- **Auth** : aucune.
- **Body** : `{ "email": "demoflow@example.com" }`
- **Limite** : **5 envois par minute par IP** → au 6e : **`429 Too Many Requests`**.
- **`200 OK`** — réponse **toujours identique** (impossible de savoir si l'adresse existe) :

  ```json
  { "message": "If an account exists with this email, a verification email has been sent." }
  ```

- **`400 Bad Request`** (email mal formé uniquement) : `{ "message": "Invalid email address." }`
- **`429 Too Many Requests`** :

  ```json
  { "detail": "Request was throttled. Expected available in 42 seconds." }
  ```

Un compte **déjà vérifié** ne reçoit pas de mail (mais la réponse reste identique).

---

### 3.3b Vérification d'email — page HTML et retour au front

Quand l'utilisateur clique le lien dans son client mail, le **navigateur** ouvre l'URL et affiche une page HTML simple (fond clair, carte centrée, un emoji, un titre coloré vert si succès / rouge si erreur). Aucune action côté front n'est nécessaire : il suffit d'indiquer à l'utilisateur de **revenir sur l'écran de login** après avoir vu la page de succès.

Le front peut aussi appeler l'endpoint en JSON (si jamais il veut l'état), avec la même URL : `GET /api/verify-email/?uid=..&token=..`. Il recevra `{"message": "..."}` avec le code correspondant.

---

### 3.4 Connexion — `POST /api/token/`

- **Auth** : aucune.
- **Body** : **email + mot de passe** (pas le username) :

  ```json
  { "email": "demoflow@example.com", "password": "TestPassword123!" }
  ```

- **`200 OK`** (vérifié) :

  ```json
  {
    "id": 26, "username": "demoflow", "email": "demoflow@example.com",
    "role": "client",
    "access": "<jwt access 60 min>",
    "refresh": "<jwt refresh 7 jours>"
  }
  ```

- **`401 Unauthorized`** — **email non vérifié** (exactement ce message) :

  ```json
  { "detail": "Please verify your email before logging in." }
  ```

  > ⚠️ Le front doit traiter ce message spécifiquement : proposer « Renvoyer l'email de vérification » (→ `3.2`).

- **`401 Unauthorized`** — identifiants faux :

  ```json
  { "detail": "No active account found with the given credentials" }
  ```

---

### 3.5 Rafraîchir le token — `POST /api/token/refresh/`

- **Auth** : aucune.
- **Body** : `{ "refresh": "<refresh_token>" }`
- **`200 OK`** (rotation : **remplacez les deux**) :

  ```json
  { "access": "<nouveau access>", "refresh": "<NOUVEAU refresh>" }
  ```

- **`401 Unauthorized`** (expiré, déjà utilisé) :

  ```json
  { "detail": "Token is invalid or expired", "code": "token_not_valid" }
  ```

  Et si le refresh a été **blacklisté (après logout)** :

  ```json
  { "detail": "Token is blacklisted", "code": "token_blacklist" }
  ```

> Après un `logout`, le refresh est blacklisté : on obtient un `401`.

---

### 3.6 Déconnexion — `POST /api/logout/`

- **Auth** : `Authorization: Bearer <access_token>` **requis**.
- **Body** : `{ "refresh": "<refresh_token à blacklister>" }`
- **`200 OK`** :

  ```json
  { "message": "Logged out successfully. Token has been invalidated." }
  ```

  (Après logout, appeler `/api/token/refresh/` avec ce refresh renvoie `401`.)
- **`400`** si le refresh est manquant/mal formé.

---

### 3.7 Profil courant — `GET /api/users/me/`

- **Auth** : `Authorization: Bearer <access_token>` **requis**.
- **`200 OK`** :

  ```json
  {
    "id": 26, "username": "demoflow", "email": "demoflow@example.com",
    "role": "client", "email_verified": true,
    "date_joined": "2026-10-08T09:55:50.553478Z"
  }
  ```

- **`401 Unauthorized`** sans token / token expiré.

> Utilisez `role` pour router l'utilisateur vers le bon écran (client / driver / boss).

---

## 4. Chauffeurs (drivers) — gestion par le boss uniquement

> ⚠️ **Changement d'API** : la route `POST /api/users/employees/create/` a été **supprimée**.
> Elle renvoie désormais **`404` pour tout le monde** (boss compris, ainsi que client, driver et anonyme).
> Les chauffeurs s'inscrivent eux-mêmes via `POST /api/signup/` avec `"role": "driver"`.

Toutes ces routes exigent un access token **d'un utilisateur `role="boss"`**.

| Méthode | URL | Description |
|---|---|---|
| `GET` | `/api/users/employees/` | Liste des chauffeurs |
| `GET` | `/api/users/employees/<id>/` | Détail d'un chauffeur |
| `DELETE` | `/api/users/employees/<id>/` | Retirer un chauffeur |

- Les chemins et noms d'URL gardent le mot `employees` pour la compatibilité front, même si le rôle stocké est désormais `driver`.
- Seuls les comptes `role == "driver"` sont listés / retournés. Un id de client ou de boss → **`404`**.
- **`401`** sans token — `{"detail": "Authentication credentials were not provided."}`
- **`403`** avec un token non-boss — `{"detail": "Access denied. Only the Boss can perform this action."}`
- **`404`** si le chauffeur n'existe pas.
- **`404`** sur l'ancienne route `POST /api/users/employees/create/` (supprimée, pour **tous** les rôles).

> **Nouveau (Phase 3)** : la **liste** et le **détail** d'un chauffeur renvoient en plus le champ
> **`profile_status`** (`incomplete` | `pending_review` | `approved` | `rejected`, ou `null` si le
> profil n'existe pas encore). Le détail complet du profil (documents, identité) est exposé par
> la nouvelle API boss `/api/drivers/<id>/` — voir §8.

---

## 5. Routes missions (déjà existantes)

Préfixe : `/api/missions/`. Toutes exigent `Authorization: Bearer <access_token>`,
sauf refus `401` / `403` selon le rôle. Les règles métier (prix, statuts) ne changent pas — voir le code `missions/`.

| Rôle | Méthode | URL |
|---|---|---|
| client | `POST` | `/api/missions/estimate/` |
| client | `POST` | `/api/missions/confirm/` |
| driver | `GET` | `/api/missions/available/` |
| driver | `GET` | `/api/missions/my/` |
| driver | `POST` | `/api/missions/<id>/accept/` |
| driver | `POST` | `/api/missions/<id>/start/` |
| driver | `POST` | `/api/missions/<id>/complete/` |
| boss | `GET` | `/api/missions/all/` |
| boss | `GET` | `/api/missions/dashboard/` |
| boss | `GET` | `/api/missions/employee/<id>/` |
| boss | `POST` | `/api/missions/<id>/assign/` |
| boss | `POST` | `/api/missions/<id>/cancel/` |
| boss | `DELETE` | `/api/missions/<id>/delete/` |

Un appel hors rôle → `403` (même message que les chauffeurs ci-dessus).
Les chemins contenant `employee` (`/api/missions/employee/<id>/`, `/api/users/employees/`) sont **conservés** pour la compatibilité front.

### 5.1 Règle « chauffeur approuvé » (Phase 3)

Le profil chauffeur doit être **`approved`** avant de pouvoir travailler. Conséquences :

| Endpoint | Règle |
|---|---|
| `POST /api/missions/<id>/accept/` | **`IsApprovedDriver`** — refuse `403` si non approuvé |
| `POST /api/missions/<id>/start/` | **`IsApprovedDriver`** — refuse `403` si non approuvé |
| `POST /api/missions/<id>/complete/` | **`IsDriver` seulement** (règle historique conservée volontairement) |
| `GET /api/missions/available/`, `/my/` | ouverts à **tout** chauffeur (même non approuvé) |
| `POST /api/missions/<id>/assign/` (boss) | refuse `409` si le chauffeur ciblé n'est pas approuvé |

Réponse `403` des `accept`/`start` (forme exacte) :

```json
{
  "detail": "Your driver profile must be approved before you can accept or start a mission.",
  "code": "driver_not_approved",
  "profile_status": "incomplete" | "pending_review" | "rejected"
}
```

Refus de l'assignation boss (`409 Conflict`) :

```json
{ "detail": "The selected driver is not approved yet.",
  "code": "driver_not_approved",
  "profile_status": "pending_review" }
```

> **Pourquoi `complete` reste ouvert aux non-approuvés ?** Un chauffeur approuvé hier peut voir son
> profil repasser en `pending_review` (changement de document) **pendant** qu'il conduit. Le forcer à
> s'arrêter au milieu d'une mission serait dangereux : on le laisse **terminer** la mission en cours,
> mais il ne pourra **pas en accepter/démarrer** une nouvelle avant d'être ré-approuvé.

> **`complete` vérifie aussi que c'est bien le chauffeur assigné** (sinon `403`) et que la mission
> est **`in_progress`** (sinon `409`) : une mission non démarrée, déjà terminée ou annulée est refusée.

---

## 6. Parcours recommandé côté Flutter

```
Inscription (3.1)
      │  email envoyé
      ▼
L'utilisateur ouvre le lien dans son navigateur → page HTML de succès (3.3b)
      │
      ▼
Connexion /api/token/  (3.4)
   ├─ 401 "Please verify your email before logging in." → bouton « Renvoyer l'email » (3.3)
   └─ 200 → stocker access + refresh (secure storage), lire `role`
              │
              ├─ access expiré (401 sur une route) → POST /api/token/refresh/ → STOCKER le nouveau refresh
              └─ logout → POST /api/logout/ avec access + refresh, puis vider le stockage
```

**Intercepteur Dio recommandé** : sur un `401` d'une route protégée, tenter un `refresh` une fois, remplacer les deux tokens, rejouer la requête ; si le refresh échoue → rediriger vers le login.

---

## 6. Erreurs transverses

| Status | Signification |
|---|---|
| `400` | Données invalides (champs manquants, rôle de signup interdit, token de vérif invalide, refresh manquant) |
| `401` | Non authentifié / token expiré / identifiants faux / email non vérifié |
| `403` | Authentifié mais rôle insuffisant (ex. client qui appelle une route boss) |
| `404` | Ressource inexistante (chauffeur, mission…) ou route supprimée (`/api/users/employees/create/`) |
| `429` | Trop de requêtes (uniquement `resend-verification` — 5/min) |

---

## 7. Création du compte boss (côté serveur uniquement)

Le rôle `boss` **ne peut pas** être créé via l'API. Sur le serveur :

```bash
# Interactif : le mot de passe est demandé en saisie masquée (jamais dans l'historique)
python manage.py create_boss --email chekiroumohamedelamine26@gmail.com

# Non interactif (scripts) : mot de passe via variable d'environnement
# BOSS_PASSWORD=...  (jamais en argument de commande, jamais commité)
python manage.py create_boss --email chekiroumohamedelamine26@gmail.com
```

Le compte créé a : `role="boss"`, `email_verified=True`, `is_staff=True`, `is_superuser=True`.
Il se connecte normalement via `POST /api/token/` (avec son **email**) et reçoit `"role": "boss"`.

> Le front n'a donc **aucun** écran « créer un boss » : seul un client ou un driver s'inscrivent.

---

## 8. Profil chauffeur & documents privés (Phase 3)

Un chauffeur ne peut pas travailler tant que son profil n'est pas **`approved`**.
Cycle de vie :

```
incomplete ──submit──► pending_review ──approve──► approved
     ▲                       │                        │
     │                    reject                 (modif clé/doc)
     └─────────────── rejected ◄──── ... ──────► pending_review
```

- `status` ∈ `incomplete` | `pending_review` | `approved` | `rejected`.
- Le chauffeur **ne peut jamais** modifier `status`, `reviewed_*` ni `documents_version`
  (ces champs sont ignorés s'ils sont envoyés).
- Un **`documents_version`** (entier) est incrémenté à **chaque** modification de
  document ou d'information. Il sert à détecter les analyses périmées (voir §10).

### 8.1 `GET /api/drivers/me/profile/` — mon profil

- **Auth** : `driver` uniquement (`403` sinon).
- **`200 OK`** :

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
    "rejection_reason": "",
    "documents_version": 3,
    "missing": { "info": [], "documents": ["vehicle_registration"] },
    "documents": {
      "selfie":            { "uploaded": true,  "url": "/api/drivers/26/documents/selfie/" },
      "id_card_front":     { "uploaded": true,  "url": "/api/drivers/26/documents/id_card_front/" },
      "id_card_back":      { "uploaded": true,  "url": "/api/drivers/26/documents/id_card_back/" },
      "driving_license":   { "uploaded": true,  "url": "/api/drivers/26/documents/driving_license/" },
      "vehicle_registration": { "uploaded": false, "url": "/api/drivers/26/documents/vehicle_registration/" }
    },
    "created_at": "2026-10-08T09:55:50.553478Z",
    "updated_at": "2026-10-08T10:02:11.000000Z"
  }
  ```

> 🔒 **Aucun chemin de fichier n'est jamais renvoyé** : seulement `uploaded` (booléen) et une `url`
> vers un endpoint **authentifié**. Le fichier lui-même est stocké **hors du web** (pas d'URL publique).

### 8.2 `PATCH /api/drivers/me/profile/` — mettre à jour

- **Auth** : `driver`.
- **Body** (tous les champs optionnels, `partial`) :

  ```json
  {
    "full_name": "Amina Cherif",
    "phone": "0660000002",
    "date_of_birth": "1995-03-10",
    "national_id_number": "11112222",
    "driving_license_number": "DL-999",
    "driving_license_expiry": "2030-01-01",
    "vehicle_plate": "ab 123 cd"
  }
  ```

- Règles : le chauffeur doit avoir **≥ 18 ans**, le permis **non expiré**,
  `date_of_birth` non future. `vehicle_plate` est mis en **MAJUSCULES**.
  `national_id_number` et `driving_license_number` sont **uniques**.
- **`400`** : `{"national_id_number": ["This information is already in use."]}` par ex.
  (message **neutre** : n'indique jamais à qui appartient le numéro).
- Si le profil était **`approved`** et qu'un **champ clé** change
  (`national_id_number`, `driving_license_number`, `driving_license_expiry`,
  `vehicle_plate`) → retour automatique en **`pending_review`**.
  Un changement non-clé (`phone`, `full_name`…) **conserve** `approved`.

### 8.3 `POST /api/drivers/me/documents/` — téléverser une pièce (multipart)

- **Auth** : `driver`. **Throttle** : 20/heure.
- **Body** (`multipart/form-data`) :
  - `type` ∈ `selfie` | `id_card_front` | `id_card_back` | `driving_license` | `vehicle_registration`
  - `file` : une **image** JPEG / PNG / WebP, ≤ **`MAX_UPLOAD_MB`** (5 par défaut).
- **`200 OK`** : renvoie le profil complet (§8.1).
- **`400`** :

  ```json
  { "detail": "Unsupported or corrupted image file.", "code": "invalid_document" }
  ```

  Autres messages : fichier trop volumineux, format non supporté.
- Le fichier est **ré-encodé** (les métadonnées **EXIF/GPS sont supprimées**) et stocké
  sous un **nom aléatoire**. Un nouvel envoi **remplace** l'ancien fichier (supprimé).
  Un profil `approved` qui change un document repasse en **`pending_review`**.

### 8.4 `POST /api/drivers/me/submit/` — soumettre pour validation

- **Auth** : `driver`.
- Passe de `incomplete`/`rejected` à **`pending_review`** **si le profil est complet**.
- **`400`** si incomplet :

  ```json
  {
    "detail": "Your profile is incomplete.",
    "code": "profile_incomplete",
    "missing": {
      "info": ["driving_license_expiry", "vehicle_plate"],
      "documents": ["vehicle_registration"]
    }
  }
  ```

- **`409`** si déjà `pending_review` ou `approved` :
  `{"detail": "...", "code": "invalid_profile_transition"}`.

### 8.5 `GET /api/drivers/<id>/documents/<type>/` — télécharger une pièce

- **Auth** : le **chauffeur propriétaire**, le **boss**, ou l'**agent IA** (`X-Agent-Key`).
- `<id>` = **id utilisateur** du chauffeur.
- **`200`** : flux de l'image. En-têtes de sécurité :
  `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`,
  `Content-Disposition: inline; filename="document"`.
- **`401`** (anonyme), **`403`** (autre chauffeur), **`404`** (id/type/fichier inconnu).

---

## 9. Validation des chauffeurs par le boss (Phase 3)

Toutes ces routes exigent un access token **`role="boss"`**.

| Méthode | URL | Description |
|---|---|---|
| `GET` | `/api/drivers/pending/` | Chauffeurs en attente (`pending_review`), paginé |
| `GET` | `/api/drivers/<id>/` | Détail complet d'un profil (identité + documents) |
| `POST` | `/api/drivers/<id>/approve/` | Approuver / corriger vers `approved` |
| `POST` | `/api/drivers/<id>/reject/` | Rejeter (`reason` **obligatoire**) |

- `<id>` = **id utilisateur** du chauffeur.
- La **liste** et les endpoints `pending` **masquent** les numéros sensibles
  (`national_id_number`, `driving_license_number` → ex. `******7890`).
  Le **détail** `/api/drivers/<id>/` les affiche en clair (usage boss).
- `approve` sur un profil `incomplete` → **`409`** `invalid_profile_transition`.
- `reject` sans `reason` (ou blanc) → **`400`**.
- Chaque décision est **journalisée** (source, décision, raisons, confiance,
  `documents_version`, auteur, date). Le boss peut **corriger** une décision
  à tout moment (un `approve` peut corriger un `rejected` et inversement).
- Le chauffeur voit `status` et `rejection_reason` sur son propre profil (§8.1).

Exemple `reject` :

```http
POST /api/drivers/26/reject/
{ "reason": "La photo du permis est illisible, merci de la refaire." }
```

---

## 10. API Agent IA (service externe, Phase 3)

> Contrat détaillé et exemples pour l'implémentation de l'agent : voir **`AGENT_AI_CONTRACT.md`**.

- **Authentification** : en-tête **`X-Agent-Key: <clé>`**, comparée en temps constant à
  `AI_AGENT_API_KEY`. **Ce n'est PAS du JWT**.
- Si `AI_AGENT_API_KEY` est **vide**, tous ces endpoints sont **désactivés** (`401`).
- Un **JWT** (boss/chauffeur) **ne fonctionne pas** sur ces routes ;
  la clé agent **ne fonctionne pas** sur les routes boss.

| Méthode | URL | Description |
|---|---|---|
| `GET` | `/api/agent/drivers/pending/` | Profils `pending_review` (payload minimal) |
| `GET` | `/api/agent/drivers/<id>/` | Profil + liens documents |
| `GET` | `/api/agent/drivers/<id>/documents/<type>/` | Télécharger une pièce |
| `POST` | `/api/agent/drivers/<id>/review/` | Envoyer une décision |

**Body `review`** :

```json
{
  "decision": "approve",          // approve | reject | manual_review
  "reasons": ["clear documents"],
  "confidence": 0.97,
  "documents_version": 3
}
```

- `reasons` : max 20 textes courts ; **ne doivent contenir aucun numéro sensible**
  (sinon `400`).
- `documents_version` doit **correspondre** à la version analysée, sinon **`409`** :

  ```json
  { "detail": "...", "code": "documents_version_conflict", "current_documents_version": 4 }
  ```

- Agir sur un profil qui n'est plus `pending_review` → **`409`** `invalid_profile_transition`.
- **`200`** : `{"user_id": 26, "status": "pending_review", "applied": false, "documents_version": 3}`.
  `applied` indique si la décision a été **appliquée** (sinon simple recommandation).
- `AI_AUTO_APPROVE=False` (défaut) → un `approve` de l'agent n'est qu'une
  **recommandation** ; le boss tranche. `reject` s'applique toujours.
- `AI_AUTO_APPROVE=True` → un `approve` s'applique **uniquement** si
  `confidence >= AI_AUTO_APPROVE_MIN_CONFIDENCE` (0.90 par défaut).

---

### 10.1 Détails techniques & sécurité

`X-Agent-Key` est comparé en temps constant. Cette section rappelle les points clés.

- Authentification par en-tête **`X-Agent-Key`** comparé en **temps constant**
  (`hmac.compare_digest`) à la variable `.env` **`AI_AGENT_API_KEY`**.
- Si `AI_AGENT_API_KEY` est **vide**, **tous** les endpoints `/api/agent/...` sont
  **désactivés** (`401`).
- La clé **n'est jamais loggée** ni renvoyée dans une réponse.
- Un token **JWT** n'est **pas** accepté sur ces routes ; la clé agent n'est **pas**
  acceptée sur les routes boss.
- Le contrat détaillé (schémas, exemples, règles de confiance, `AI_AUTO_APPROVE`)
  est dans **`AGENT_AI_CONTRACT.md`**.

---

## 11. Nouvelles variables d'environnement (Phase 3)

À ajouter dans `.env` (voir `.env.example`) :

| Variable | Obligatoire | Défaut | Rôle |
|---|---|---|---|
| `PRIVATE_MEDIA_ROOT` | **OUI** | *(aucun)* | Dossier **absolu** des documents privés. Le serveur **refuse de démarrer** sans lui (check `drivers.E001`). Doit être hors `STATIC_ROOT`/`STATICFILES_DIRS`. |
| `MAX_UPLOAD_MB` | non | `5` | Taille maximale d'un document (Mo). |
| `AI_AGENT_API_KEY` | non | *(vide)* | Clé partagée de l'agent IA. Vide → endpoints agent **désactivés**. |
| `AI_AUTO_APPROVE` | non | `False` | `True` → l'agent peut approuver automatiquement. |
| `AI_AUTO_APPROVE_MIN_CONFIDENCE` | non | `0.90` | Seuil de confiance pour l'auto-approbation. |

> ⚠️ **Documents d'identité** : `PRIVATE_MEDIA_ROOT` doit pointer vers un dossier **dédié**,
> **jamais** servi par le serveur web. Les fichiers sont écrits avec des permissions
> restrictives et supprimés automatiquement quand le compte est effacé
> (`reset_users`, suppression d'un chauffeur).

---

## 12. Codes d'erreur stables (Phase 3)

| `code` | Où | Sens |
|---|---|---|
| `driver_not_approved` | accept/start (403), assign (409) | Le profil chauffeur n'est pas `approved` |
| `profile_incomplete` | `POST /api/drivers/me/submit/` (400) | Champs/documents manquants (`missing`) |
| `invalid_profile_transition` | submit/approve/reject/agent review (409) | Transition de statut interdite |
| `invalid_document` | upload document (400) | Fichier invalide / trop grand / format refusé |
| `documents_version_conflict` | agent review (409) | Documents modifiés depuis l'analyse |
| *(aucun champ `code`)* | endpoints agent (401) | Clé absente/incorrecte ou agent désactivé : le corps contient uniquement `detail` |

---

## 13. Estimation de prix et negociation (Phase 4)

Prefixe : `/api/`. Toutes ces routes exigent `Authorization: Bearer <access_token>`.

### 13.1 Estimation - `POST /api/missions/estimate/`

Le client envoie les parametres du trajet ; le serveur calcule le prix.
Aucune mission n'est creee a cette etape.

- **Role** : `client`.
- **Body (chemin historique)** - inchange depuis les phases precedentes :
  ```json
  {
    "truck_type": "large", "category": "closed", "capacity": 5,
    "size": "medium",
    "departure_latitude": "36.737232", "departure_longitude": "3.086472",
    "arrival_latitude": "36.365000", "arrival_longitude": "6.614000",
    "distance": "300.00", "date": "2024-08-01", "time": "09:00", "workers": 2
  }
  ```
- **Body (nouveau moteur)** - opt-in via les nouveaux champs :
  ```json
  {
    "truck_type": "large", "category": "closed", "capacity": 5,
    "departure_latitude": "36.7538", "departure_longitude": "3.0588",
    "arrival_latitude": "36.7538", "arrival_longitude": "3.1750",
    "date": "2026-11-16", "time": "10:00",
    "vehicle_class": "standard", "helpers": 0, "floors": 0, "duration_min": 20
  }
  ```
- **`200 OK`** (chemin historique) : `estimated_price` = 67500, `breakdown` identique
  a la formule historique, plus un champ additif `estimate` (montant moteur, `low`,
  `high`, `confidence`, `calibrated: false`, `quote_id: null`).
- **`200 OK`** (nouveau moteur) : `estimated_price` = montant moteur (ex. `850`),
  `estimate.quote_id` non null (quote persiste, TTL 30 min), `confidence` in
  {`normal`, `low`}.
- **`400`** : `invalid_distance`, `invalid_helpers`, `invalid_floors`,
  `invalid_handling_minutes`, `invalid_vehicle_class`, `invalid_coordinates`,
  `invalid_duration`.

> La reponse historique (`estimated_price` + `breakdown`) est byte-identique pour
> les anciens clients ; le champ `estimate` est purement additif.

### 13.2 Confirmation - `POST /api/missions/confirm/`

- **Role** : `client`.
- **Chemin historique** : body identique a l'estimation + `shipment_type`,
  `phone_number`. -> `201 Created` avec `MissionClientSerializer` ; `price` =
  formule historique, aucun snapshot de pricing.
- **Chemin nouveau moteur sans quote** : body avec `vehicle_class`/`helpers`/... ->
  le prix est recalcule cote serveur et le snapshot est stocke.
- **Chemin avec quote** : body minimal avec `quote_id` (UUID). Le quote fournit
  distance, date, heure, helpers, duree ; tout champ fourni doit correspondre
  exactement sinon `400` `quote_mismatch`. La route est re-validee (bornes de
  distance). Quote consomme (single-use).
- **`400`/`404`** : `quote_not_found` (404 si non proprietaire), `quote_expired`,
  `quote_already_used`, `quote_mismatch`, `invalid_distance`, etc.
- **`negotiable`** (bool, defaut `false`) : active la negociation sur la mission.

### 13.3 Regles publiques - `GET /api/pricing/rules/`

- **Role** : tout utilisateur authentifie.
- **`200 OK`** : tarif public (classes de vehicules, extras, multiplicateurs,
  bornes de distance, `negotiation` avec `tolerance`/`max_offers`/`min_step`/
  `offer_ttl_minutes`/`total_ttl_minutes`, `quote_ttl_minutes`).
- La **commission rate n'est jamais exposee** ici.

### 13.4 Negotiation

La negociation s'ouvre lorsqu'un **chauffeur approuve** fait la **premiere offre**
sur une mission `negotiable=true` et `pending`. La mission est alors **retenue**
(`held_by`, `hold_expires_at`) pendant 15 min (TTL de l'offre).

Roles : `client` (proprietaire de la mission) <-> `chauffeur` (celui qui a fait
la premiere offre). Non-participants -> `404`. Roles non autorises -> `403`.

#### 13.4.1 Lire la negociation - `GET /api/missions/<id>/negotiation/`

- **`200 OK`** : objet `Negotiation` avec `id`, `mission_id`, `status`,
  `currency`, `initial_price`, `current_amount`, `min_amount`, `max_amount`,
  `min_fare`, `max_offers`, `min_step`, `offer_count`, `is_final`, `your_turn`,
  `offers` (liste), `calibrated`, `config_version`, `expires_at`,
  `hold_expires_at`, `accepted_amount`, `commission_rate` (chauffeur uniquement),
  `commission_amount` (chauffeur uniquement), `driver_net` (chauffeur uniquement),
  `created_at`, `accepted_at`, `closed_at`.
- Le **client** ne recoit pas `commission_rate`, `commission_amount`, `driver_net`.
- **`404`** : `negotiation_not_found`.

#### 13.4.2 Faire une offre - `POST /api/missions/<id>/negotiation/offer/`

- **Body** :
  ```json
  { "amount": 900, "idempotency_key": "<cle opaque unique>" }
  ```
- **Premiere offre** (chauffeur approuve, mission disponible) -> ouvre la
  negociation, retient la mission, `201 Created`.
- **Contre-offre** -> `200 OK` si idempotent, sinon `201 Created`.
- **Regles** (dans l'ordre) : negociation fermee/expiree -> participant ->
  tour -> idempotence -> max offres (3) -> entier -> multiple de `min_step` ->
  `[0.85*E, 1.15*E]` -> `>= min_fare` -> monotone (client baisse, chauffeur
  monte) avec ecart `>= min_step`.
- **`400`** : `offer_not_aligned`, `offer_out_of_range`, `offer_below_min_fare`,
  `offer_not_monotonic`, `invalid_offer_amount`, `invalid_idempotency_key`.
- **`403`** : `not_your_turn`, `driver_not_approved`.
- **`409`** : `negotiation_closed`, `negotiation_expired`, `max_offers_reached`,
  `idempotency_conflict`, `mission_not_negotiable`, `mission_held`,
  `negotiation_already_open`.
- **Idempotence** : meme cle + meme montant -> `200` avec le resultat stocke ;
  meme cle + montant different -> `409` `idempotency_conflict`.

#### 13.4.3 Accepter - `POST /api/missions/<id>/negotiation/accept/`

- **Role** : participant (celui qui n'a **pas** fait la derniere offre).
- **`200 OK`** : `{ "negotiation": {...}, "mission": {...} }`. Le client recoit
  `MissionClientSerializer` (pas de `driver_net`/`commission_*`), le chauffeur
  recoit `MissionDriverListSerializer` (avec `driver_net` et `commission_rate`).
- Le prix est verrouille (`price_locked_at`), le chauffeur est assigne, la
  commission (10 %) et le net chauffeur sont copies de la negociation.
- **`403`** : `not_your_turn`.
- **`409`** : `negotiation_expired`, `driver_not_approved`, `mission_not_negotiable`.

#### 13.4.4 Refuser - `POST /api/missions/<id>/negotiation/decline/`

- **Role** : participant (celui qui n'a **pas** fait la derniere offre).
- Ferme la negociation (`status = "declined"`), libere la mission (hold leve),
  la mission reste `pending` pour un autre chauffeur.
- **`200 OK`** : negociation mise a jour.
- **`403`** : `not_your_turn`.
- **`409`** : `negotiation_closed`, `negotiation_expired`.

#### 13.4.5 Expiration

- TTL total de la negociation : `total_ttl_minutes` (30 par defaut).
- TTL par offre : `offer_ttl_minutes` (15 par defaut) ; chaque offre repousse
  le deadline, borne par le TTL total.
- Expiration **paresseuse** : verifiee a chaque appel d'endpoint.
- Commande manuelle : `python manage.py expire_pricing [--dry-run] [--now ISO]`.

### 13.5 Codes d'erreur stables (Phase 4)

| `code` | HTTP | Ou | Sens |
|---|---|---|---|
| `mission_not_negotiable` | 409 | offer/open/accept | Mission non negociable |
| `mission_held` | 409 | offer/open/accept | Mission retenue par un autre chauffeur |
| `negotiation_already_open` | 409 | offer (ouverture) | Negociation deja ouverte sur cette mission |
| `negotiation_not_found` | 404 | GET/offer/accept/decline | Pas de negociation ou non participant |
| `not_your_turn` | 403 | offer/accept/decline | Ce n'est pas le tour de l'appelant |
| `max_offers_reached` | 409 | offer | 3 offres deja faites (derniere finale) |
| `offer_not_aligned` | 400 | offer | Montant pas multiple de `min_step` |
| `offer_out_of_range` | 400 | offer | Montant hors `[0.85*E, 1.15*E]` |
| `offer_below_min_fare` | 400 | offer | Montant sous le tarif minimum |
| `offer_not_monotonic` | 400 | offer | Sens ou ecart insuffisant vs l'offre precedente |
| `invalid_offer_amount` | 400 | offer | Montant non entier / non positif |
| `invalid_idempotency_key` | 400 | offer | Cle manquante ou trop longue |
| `idempotency_conflict` | 409 | offer | Meme cle, montant different |
| `negotiation_closed` | 409 | offer/accept/decline | Negociation fermee (acceptee/declinee) |
| `negotiation_expired` | 409 | offer/accept/decline | TTL ecoule |
| `driver_not_approved` | 403/409 | offer/open/accept | Profil chauffeur non approuve |

### 13.6 Commandes disponibles

```bash
# Seed / refresh the placeholder pricing configuration
python manage.py seed_pricing [--config-version N] [--activate] [--force] [--json path]

# Expire stale negotiations and release orphan holds
python manage.py expire_pricing [--dry-run] [--now ISO-8601]

# Create a boss account (server-side only)
python manage.py create_boss --email <email>
```

> Les valeurs tarifaires sont des **placeholders** (`calibrated: false`). Voir
> `PRICING_CALIBRATION.md` pour la procedure de calibration avec de vrais devis.
'''
with open('API_DOCS.md', 'a', encoding='utf-8') as f:
    f.write(content)
print('appended', len(content), 'chars')
"---

## 14. Gestion de la configuration tarifaire (boss uniquement)

Préfixe : `/api/pricing/`. Toutes ces routes exigent un JWT avec `role="boss"`.

### 14.1 Lire la configuration active - `GET /api/pricing/config/`

- **Rôle** : `boss`.
- **`200 OK`** :
  ```json
  {
    "version": 1,
    "calibrated": false,
    "created_at": "2026-11-14T14:00:00Z",
    "updated_at": "2026-11-14T14:00:00Z",
    "parameters": { ... tous les paramètres tarifaires ... }
  }
  ```
- **`403`** si non-boss.

### 14.2 Mettre à jour la configuration - `POST /api/pricing/config/update/`

- **Rôle** : `boss`.
- **Throttle** : 5 requêtes par minute par utilisateur.
- **Body** :
  ```json
  {
    "password": "<mot de passe du boss, ressaisi>",
    "parameters": {
      "commission_rate": 0.15,
      "multiplier_cap": 1.6,
      "negotiation": { "max_offers": 4, "offer_ttl_minutes": 20 }
    }
  }
  ```
  `parameters` est une mise à jour **partielle** : seuls les champs fournis sont
  écrasés, les autres sont conservés. `version` et `calibrated` sont gérés
  côté serveur (`calibrated` reste `false`).
- **`200 OK`** : nouvelle configuration active (même forme que le GET).
  Une nouvelle ligne `PricingConfig` (version + 1) est créée et activée
  atomiquement ; l'ancienne est désactivée.
- **`403`** : `invalid_password` si le mot de passe est incorrect.
- **`400`** : erreurs de validation avec champs explicites :
  - `multiplier_cap` : doit être entre 1.0 et 3.0.
  - `commission_rate` : doit être entre 0.0 et 0.5.
  - `estimate_band` : doit être entre 0.0 et 0.5.
  - `negotiation.tolerance` : doit être entre 0.0 et 0.5.
  - `negotiation.max_offers` : doit être entre 1 et 5.
  - `negotiation.offer_ttl_minutes` : doit être entre 5 et 60.
  - `negotiation.total_ttl_minutes` : doit être entre 10 et 120.
  - `vehicle_classes.<classe>.{base,per_km,per_min,min_fare}` : entiers positifs.
- **`429`** : trop de requêtes (throttle).
- **Audit** : chaque mise à jour réussie crée une ligne `PricingAuditLog`
  (`action = "config_updated"`, `actor` = boss, `payload` = `{old_config_version,
  new_config_version, changes}` — jamais le mot de passe).

### 14.3 Codes d'erreur (Phase 5)

| `code` | HTTP | Ou | Sens |
|---|---|---|---|
| `invalid_password` | 403 | config/update | Mot de passe du boss incorrect |
| *(throttle)* | 429 | config/update | Plus de 5 tentatives par minute |

---
