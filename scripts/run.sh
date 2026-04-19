#!/usr/bin/env bash

# ============================================
# Ari - Assistant Vocal IA Local
# Script de lancement du serveur
# ============================================
# Usage: ./scripts/run.sh
#        ./scripts/run.sh --reload   (mode dev avec rechargement auto)
# ============================================

set -e  # Arrêt à la première erreur

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Fonctions log
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERREUR]${NC} $1"
}

# Répertoire projet
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Variables par défaut
RELOAD_MODE=false
UVICORN_WORKERS=1
HOST="0.0.0.0"
PORT="${PORT:-8000}"
LOG_LEVEL="${LOG_LEVEL:-info}"

# Traitement arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --reload)
            RELOAD_MODE=true
            shift
            ;;
        --workers)
            UVICORN_WORKERS="$2"
            shift 2
            ;;
        *)
            log_error "Option inconnue: $1"
            echo "Usage: $0 [--reload] [--workers N]"
            exit 1
            ;;
    esac
done

# Banner
echo "============================================"
echo "    Ari - Assistant Vocal IA Local"
echo "    Démarrage du serveur"
echo "============================================"
echo ""

# Vérifier présence .env
if [ ! -f ".env" ]; then
    log_error "Fichier .env manquant !"
    log_info "Copiez .env.example vers .env et configurez-le :"
    echo "  cp .env.example .env"
    echo "  nano .env"
    exit 1
fi

# Charger variables .env (manuellement car pas encore de Python)
if [ -f ".env" ]; then
    log_info "Chargement des variables d'environnement..."
    set -a
    source .env 2>/dev/null || true
    set +a
fi

# Vérifier que les répertoires existent
mkdir -p memory_profiles logs data

# 1. Vérifier Ollama
log_info "Vérification d'Ollama..."
if ! command -v ollama &> /dev/null; then
    log_warning "Ollama non trouvé dans PATH."
    log_info "  Continuez sans Ollama ? L'Assistant ne pourra pas répondre."
    read -p "  Continuer quand même ? (o/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[oO]$ ]]; then
        log_error "Arrêt. Veuillez installer Ollama."
        exit 1
    fi
else
    if ! curl -s http://localhost:11434/api/tags &> /dev/null; then
        log_warning "Ollama n'est pas en cours d'exécution."
        log_info "  Démarrage d'Ollama en arrière-plan..."
        nohup ollama serve > logs/ollama.log 2>&1 &
        OLLAMA_PID=$!
        sleep 3
        if curl -s http://localhost:11434/api/tags &> /dev/null; then
            log_success "Ollama démarré (PID: $OLLAMA_PID)"
        else
            log_error "Impossible de démarrer Ollama. Démarrez-le manuellement."
            exit 1
        fi
    else
        log_success "Ollama est en cours d'exécution ✓"
    fi
fi

# 2. Vérifier Python/venv
log_info "Recherche de Python..."

if [ -d "venv" ]; then
    PYTHON_BIN="${PROJECT_DIR}/venv/bin/python"
    if [ -f "$PYTHON_BIN" ]; then
        log_success "Environnement virtuel trouvé ✓"
        PYTHON_CMD="$PYTHON_BIN"
    else
        log_error "Environnement virtuel corrompu. Réinstallez avec ./scripts/install.sh"
        exit 1
    fi
else
    log_warning "Aucun environnement virtuel trouvé. Utilisation de python3 système."
    PYTHON_CMD="python3"
fi

# Vérifier module FastAPI
if ! $PYTHON_CMD -c "import fastapi" 2>/dev/null; then
    log_error "FastAPI n'est pas installé."
    log_info "  Exécutez d'abord : ./scripts/install.sh"
    exit 1
fi

# 3. Préparer les logs avec rotation
log_info "Configuration de la rotation des logs..."
LOGS_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOGS_DIR}/ari.log"

# Créer fichier de log si absent
touch "$LOG_FILE"

# Configuration logrotate (si disponible)
if command -v logrotate &> /dev/null; then
    LOGROTATE_CONF="${LOGS_DIR}/logrotate.conf"
    cat > "$LOGROTATE_CONF" << EOF
$LOG_FILE {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 644 $(whoami) $(whoami)
}
EOF

    # Config logrotate activé si .conf existe
    if [ -f "$LOGROTATE_CONF" ]; then
        log_success "Configuration logrotate créée ✓"
    fi
fi

# 4. Lancement d'Uvicorn
log_info "Démarrage du serveur FastAPI..."
echo ""

UVICORN_CMD="uvicorn src.main:app"

if [ "$RELOAD_MODE" = true ]; then
    log_warning "Mode rechargement automatique activé (développement)."
    UVICORN_CMD+=" --reload"
fi

UVICORN_CMD+=" --host ${HOST} --port ${PORT}"
UVICORN_CMD+=" --workers ${UVICORN_WORKERS}"
UVICORN_CMD+=" --log-level ${LOG_LEVEL}"
UVICORN_CMD+=" --access-log"

# Afficher la commande
log_info "Commande : $UVICORN_CMD"
echo ""

# Exécuter
echo "============================================"
echo "    🚀 Serveur en cours d'exécution"
echo "============================================"
echo ""
log_info "URL API: http://${HOST}:${PORT}"
log_info "Docs : http://${HOST}:${PORT}/docs"
log_info "Health: http://${HOST}:${PORT}/health"
echo ""
log_info "Appuyez sur CTRL+C pour arrêter."
echo ""

# Exécuter avec le bon interpréteur
exec "$PYTHON_CMD" -m $UVICORN_CMD
