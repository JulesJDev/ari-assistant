# Ari - Assistant Vocal IA Local

**Ari** est un assistant vocal intelligent fonctionnant entièrement en local, sans dépendance aux services cloud propriétaires. Il combine reconnaissance vocale (Whisper), génération de parole (Edge-TTS) et intelligence artificielle (Ollama) pour offrir une expérience conversationnelle naturelle et privée.

![Python](https://img.shields.io/badge/python-3.12+-blue?logo=python)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-active-success)

---

## 📋 Table des matières

- [Overview](#overview)
- [Stack Technique](#stack-technique)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Déploiement](#déploiement)
- [Dépannage](#dépannage)

---

## Overview

Ari est conçu pour être :

- **100% local** : vos conversations ne quittent jamais votre machine
- **Open-Source** : code libre, totalement transparent
- **Modulaire** : échangez les composants TTS/LLM selon vos besoins
- **Productif** : API REST complète, prête pour intégration

### Fonctionnalités principales

- 🎤 Reconnaissance vocale multi-langue (Whisper)
- 🔊 Synthèse vocale naturelle (Edge-TTS, Piper)
- 🤖 Conversation intelligente (Ollama/Llama)
- 🔍 Recherche web (Tavily, DuckDuckGo)
- 👤 Mémoire contextuelle par utilisateur
- 🔐 Authentification par code PIN
- 📱 API REST moderne (FastAPI)

---

## Stack Technique

| Composant | Technologie | Rôle |
|-----------|------------|------|
| API | FastAPI + Uvicorn | Serveur web asynchrone |
| LLM | Ollama (Llama 3.2) | Intelligence conversationnelle |
| STT | Whisper (OpenAI) | Reconnaissance vocale |
| TTS | Edge-TTS / Piper | Synthèse vocale |
| Recherche | Tavily / DuckDuckGo | Accès à l'information |
| Auth | Bcrypt + JWT | Sécurité authentification |
| Stockage | JSON + SQLite | Mémoire & logs |
| Config | python-dotenv | Gestion variables |

---

## Installation

### Prérequis

- **Python 3.12+** (recommandé 3.12)
- **Ollama** installé et en cours d'exécution
- **Git** (optionnel)

```bash
# Vérifier Python
python3 --version  # Doit afficher 3.12+

# Installer Ollama (Linux/Mac)
curl -fsSL https://ollama.com/install.sh | sh

# Démarrer Ollama
ollama serve

# Dans un autre terminal, télécharger un modèle
ollama pull llama3.2
```

### Installation Automatisée (recommandé)

```bash
# Cloner le dépôt (si applicable)
# git clone <repo-url> && cd ari

# Lancer le script d'installation
chmod +x scripts/install.sh
./scripts/install.sh
```

### Installation Manuelle

```bash
# Créer un environnement virtuel (recommandé)
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# ou .\venv\Scripts\activate  # Windows

# Installer les dépendances
pip install -r requirements.txt

# Créer les dossiers nécessaires
mkdir -p memory_profiles logs data

# Copier le fichier d'environnement
cp .env.example .env
# Éditer .env avec vos paramètres
nano .env  # ou vim, code, etc.
```

### Vérification

```bash
# Tester les imports
python -c "import fastapi, edge_tts, duckduckgo_search; print('OK')"

# Vérifier la structure
tree -L 2
# .
# ├── .env.example
# ├── .gitignore
# ├── README.md
# ├── requirements.txt
# ├── scripts/
# │   ├── install.sh
# │   └── run.sh
# ├── memory_profiles/
# ├── logs/
# └── src/  (ou app/)
```

---

## Usage

### Démarrage rapide

```bash
# Lancer le serveur
chmod +x scripts/run.sh
./scripts/run.sh
```

Le serveur démarre sur `http://localhost:8000` (ou le port défini dans `.env`).

### Documentation API

Une fois le serveur lancé :

- **Swagger UI** : http://localhost:8000/docs
- **ReDoc** : http://localhost:8000/redoc
- **Health Check** : GET http://localhost:8000/health

### Exemples d'appels

#### 1. Authentification (récupération token)

```bash
# Récupérer un token JWT (code PIN par défaut: 1234)
curl -X POST "http://localhost:8000/auth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&pin_code=1234"

# Réponse
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

#### 2. Conversation vocale

```bash
# Envoyer un audio pour transcription + réponse
curl -X POST "http://localhost:8000/v1/chat/audio" \
  -H "Authorization: Bearer VOTRE_TOKEN" \
  -F "audio_file=@message.wav"

# Réponse
{
  "transcription": "Bonjour, comment vas-tu ?",
  "response_text": "Je vais très bien, merci ! Comment puis-je vous aider ?",
  "response_audio_url": "/v1/audio/response_id.mp3",
  "conversation_id": "uuid..."
}
```

#### 3. Chat textuel simple

```bash
curl -X POST "http://localhost:8000/v1/chat/text" \
  -H "Authorization: Bearer VOTRE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Quelle est la capitale de la France ?",
    "user_id": "user123",
    "enable_search": false
  }'
```

#### 4. Recherche web

```bash
curl -X POST "http://localhost:8000/v1/search" \
  -H "Authorization: Bearer VOTRE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "dernières actualités IA 2026",
    "max_results": 5
  }'
```

---

## Configuration

### Variables d'environnement

Toutes les configurations se font via le fichier `.env`. Voir [.env.example](.env.example) pour la liste complète.

### Structure des dossiers

```
ari/
├── .env                  # Variables d'environnement (à créer)
├── .env.example          # Exemple
├── requirements.txt       # Dépendances Python
├── README.md
├── .gitignore
├── scripts/
│   ├── install.sh        # Installation auto
│   └── run.sh            # Lancement serveur
├── memory_profiles/       # Mémoires utilisateurs (JSON)
├── logs/                 # Fichiers logs rotés
├── data/                 # Base SQLite, autres données
└── src/ (ou app/)
    ├── main.py           # Point d'entrée FastAPI
    ├── config.py         # Configuration chargée depuis .env
    ├── auth.py           # Gestion auth/JWT
    ├── tts.py            # Synthèse vocale
    ├── stt.py            # Reconnaissance vocale
    ├── llm.py            # Interface Ollama
    ├── search.py         # Recherche web
    ├── memory.py         # Gestion mémoire
    ├── models/           # Schémas Pydantic
    └── api/              # Routes API
```

### Personalisation avancée

#### Modifier la voix TTS

Dans `.env` :
```bash
TTS_ENGINE=edge
EDGE_TTS_VOICE=fr-FR-HenriNeural  # Voix masculine française
# Liste complète: edge-tts --list-voices
```

#### Changer le modèle LLM

```bash
# Télécharger un modèle Ollama
ollama pull mistral

# Puis dans .env :
OLLAMA_MODEL=mistral
```

#### Ajuster Whisper

```bash
# Modèles disponibles (taille/qualité):
# tiny (39M), base (74M), small (244M), medium (769M), large (1550M)
WHISPER_MODEL=small
```

---

## API Reference

### Endpoints publics

| Route | Méthode | Description |
|-------|---------|-------------|
| `/health` | GET | État du service |
| `/docs` | GET | Documentation Swagger |
| `/redoc` | GET | Documentation ReDoc |

### Endpoints authentifiés

| Route | Méthode | Auth | Description |
|-------|---------|------|-------------|
| `/auth/token` | POST | Non | Obtenir token JWT |
| `/auth/verify` | GET | Oui | Vérifier token |
| `/v1/chat/text` | POST | Oui | Chat textuel |
| `/v1/chat/audio` | POST | Oui | Chat vocal (audio) |
| `/v1/audio/speak` | POST | Oui | Synthèse TTS pure |
| `/v1/search` | POST | Oui | Recherche web |
| `/v1/memory/users/{user_id}` | GET/PUT | Oui | Lecture/écriture mémoire |
| `/v1/memory/context/{user_id}` | GET/DELETE | Oui | Contexte conversation |

> **⚠️ Sécurité** : Tous les endpoints sauf `/health` et `/auth/token` nécessitent un en-tête `Authorization: Bearer <token>`.

### Schémas Pydantic

Voir `src/models/schemas.py` pour les structures de requête/réponse.

---

## Déploiement

### Avec systemd (Linux)

Créer un service systemd pour démarrage automatique :

```ini
# /etc/systemd/system/ari.service
[Unit]
Description=Ari Voice Assistant
After=network.target ollama.service

[Service]
Type=simple
User=jules
WorkingDirectory=/home/jules/ari
Environment="PATH=/home/jules/ari/venv/bin"
ExecStart=/home/jules/ari/venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Activer le service :

```bash
sudo systemctl daemon-reload
sudo systemctl enable ari.service
sudo systemctl start ari.service
sudo systemctl status ari.service
```

### Avec Docker

Créer un `Dockerfile` (non fourni ici, voir section docker-compose) :

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

#### Docker Compose (fourni)

Un `docker-compose.yml` est inclus pour faciliter le déploiement :

```bash
# Démarrer
docker-compose up -d

# Voir logs
docker-compose logs -f ari

# Arrêter
docker-compose down
```

> **Note** : Le Dockerfile n'est pas inclus dans ce sous-agent, mais doit être créé séparément.

### Sur un VPS / Cloud

1. Installer les prérequis (Python, Ollama)
2. Cloner le dépôt
3. Exécuter `scripts/install.sh`
4. Configurer `.env` (adapter PORT, HOST, clés)
5. Configurer un reverse proxy (Nginx) si besoin
6. Configurer un firewall (UFW) pour ouvrir le port
7. Mettre en place un service systemd (voir ci-dessus)
8. (Optionnel) Installer un certificat SSL avec Certbot

---

## Dépannage

### Ollama ne répond pas

```bash
# Vérifier qu'Ollama est en cours d'exécution
ps aux | grep ollama

# Démarrer manuellement
ollama serve

# Tester la connexion
curl http://localhost:11434/api/tags
```

### Erreur "ModuleNotFoundError"

```bash
# Réinstaller les dépendances
pip install -r requirements.txt --upgrade

# Vérifier l'environnement virtuel
which python
```

### Problème de mémoire avec Whisper

Les modèles Whisper sont gourmands en RAM :

```bash
# Utiliser un modèle plus petit
# Dans .env : WHISPER_MODEL=base

# Ou forter CPU-only (si pas de GPU)
export CUDA_VISIBLE_DEVICES=""
```

### Audio ne fonctionne pas

```bash
# Lister les périphériques audio
python -c "import sounddevice as sd; print(sd.query_devices())"

# Forcer un périphérique dans .env :
AUDIO_INPUT_DEVICE=1  # index
# ou
AUDIO_INPUT_DEVICE="USB Microphone"
```

### Logs rotés pas créés

```bash
# Créer manuellement le dossier logs
mkdir -p logs
chmod 755 logs
```

### Port déjà utilisé

Changer le port dans `.env` :
```bash
PORT=8080
```

Ou tuer le processus qui utilise le port 8000 :
```bash
lsof -ti:8000 | xargs kill -9
```

### Réinitialiser la mémoire utilisateur

Supprimer le fichier de profil :
```bash
rm memory_profiles/user123.json
```

Ou effacer tout le dossier :
```bash
rm -rf memory_profiles/*
```

---

## Support & Contribution

- **Issues** : https://github.com/votre-org/ari/issues
- **Discussions** : https://github.com/votre-org/ari/discussions
- **Documentation** : à venir

---

## Licence

MIT License - voir fichier `LICENSE` pour détails.

---

## Remerciements

- [OpenAI](https://openai.com) pour Whisper
- [Microsoft](https://microsoft.com) pour Edge-TTS
- [Ollama](https://ollama.ai) pour l'exécution locale des LLM
- [FastAPI](https://fastapi.tiangolo.com) pour le framework API
- Toute la communauté open-source

---

**Happy coding ! 🎙️🤖**

*Dernière mise à jour : Avril 2026*
